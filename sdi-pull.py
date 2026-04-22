#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "playwright>=1.50.0",
#     "requests>=2.31.0",
#     "rich>=13.0.0",
# ]
# ///
"""
sdi-pull - Download electronic invoices (XML) from the Italian SdI
(Sistema di Interscambio) via the Agenzia delle Entrate portal.

"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

console = Console()

BASE_URL = "https://ivaservizi.agenziaentrate.gov.it/cons/cons-services/rs"
CONSOLE_URL = "https://ivaservizi.agenziaentrate.gov.it/cons/cons-web/"
MAX_WINDOW_DAYS = 90
HTTP_TIMEOUT = 30

# The /fe/emesse/.../piva/{piva} endpoint requires an 11-digit value in the
# path. In the self-service case (user logged in with their own CIE) the
# backend scopes the query by session tokens and ignores the path param —
# any 11-digit placeholder works. In the delegated case (commercialista
# logged in with CIE operating on behalf of an assistito) the path param
# may be used to select the target entity, so callers should pass the real
# VAT when operating under delegation.
PIVA_PATH_PLACEHOLDER = "00000000000"
VAT_ID_RE = re.compile(r"^\d{11}$")

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,it;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "DNT": "1",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> int:
    """Current timestamp in milliseconds (used as cache-buster param)."""
    return int(time.time() * 1000)


def _referer() -> str:
    """Current Referer URL (the console URL with a fresh cache-buster)."""
    return f"{CONSOLE_URL}?v={_ts()}"


def _date_ranges(start: date, end: date) -> list[tuple[date, date]]:
    """Split a date range into chunks of max 90 days (API constraint)."""
    ranges = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        ranges.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return ranges


def _fmt_date(d: date) -> str:
    """Format date as DDMMYYYY for the API."""
    return d.strftime("%d%m%Y")


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------

SESSION_DIR = Path.home() / ".sdi-pull"
SESSION_FILE = SESSION_DIR / "session.json"


def _save_session(cookies: dict[str, str], headers: dict[str, str]) -> None:
    """Persist session to ~/.sdi-pull/session.json with 0600 perms (contains live tokens)."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        SESSION_DIR.chmod(0o700)
    except OSError:
        pass
    payload = json.dumps({
        "cookies": cookies,
        "headers": headers,
        "saved_at": datetime.now().isoformat(),
    }, indent=2)
    fd = os.open(SESSION_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass


def _load_session() -> tuple[dict[str, str], dict[str, str]] | None:
    """Load cached session if it exists. Returns (cookies, headers) or None."""
    if not SESSION_FILE.exists():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text())
        cookies = data["cookies"]
        headers = data["headers"]
        if "x-b2bcookie" not in headers or "x-token" not in headers:
            return None
        if "User-Agent" not in headers:
            return None
        return cookies, headers
    except (json.JSONDecodeError, KeyError):
        return None


def _test_session(cookies: dict[str, str], headers: dict[str, str]) -> bool:
    """Test if a cached session is still valid with a lightweight API call.

    Uses the cross-border endpoint which doesn't require a VAT id, so the
    probe can't be poisoned by a fabricated piva.
    """
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(DEFAULT_HEADERS)
    session.headers["Referer"] = _referer()
    session.headers.update(headers)

    today = _fmt_date(date.today())
    url = f"{BASE_URL}/ft/emesse/dal/{today}/al/{today}?v={_ts()}"
    try:
        resp = session.get(url, timeout=10)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def _clear_session() -> None:
    """Remove cached session file."""
    if SESSION_FILE.exists():
        SESSION_FILE.unlink()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _wait_for_login(context, timeout_s: int = 300) -> bool:
    """Poll browser cookies until FATSC appears (only set after login)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(1)
        cookie_names = {c["name"] for c in context.cookies()}
        if "FATSC" in cookie_names:
            return True
    return False


def _read_tokens_from_context(context) -> dict[str, str]:
    """
    Read x-b2bcookie and x-token from the SPA's localStorage across all
    open pages. localStorage is origin-scoped, so the tokens only exist
    on pages served from ivaservizi.agenziaentrate.gov.it — we scan every
    page rather than binding to one that might have navigated away.
    """
    for p in context.pages:
        try:
            url = p.url
        except Exception:
            continue
        if "ivaservizi.agenziaentrate.gov.it" not in url:
            continue
        try:
            result = p.evaluate("""() => ({
                'x-b2bcookie': localStorage.getItem('FattCorrActiveB2B') || '',
                'x-token': localStorage.getItem('FattCorrActiveToken') || '',
            })""")
        except Exception:
            continue
        tokens = {k: v for k, v in result.items() if v}
        if "x-b2bcookie" in tokens and "x-token" in tokens:
            return tokens
    return {}


def _browser_login() -> tuple[dict[str, str], dict[str, str]]:
    """Open bundled Chromium, wait for CIE login, extract session. Returns (cookies, headers)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        real_ua = page.evaluate("() => navigator.userAgent")

        console.print(Panel(
            "[bold]Complete CIE authentication in the browser window.[/bold]\n"
            "The browser will close automatically once the session is ready.",
            title="CIE Login",
            border_style="cyan",
        ))
        page.goto(CONSOLE_URL)

        with console.status("[cyan]Waiting for login...", spinner="dots"):
            if not _wait_for_login(context):
                console.print("[bold red]Error:[/] login timeout (5 min).")
                browser.close()
                sys.exit(1)

        console.print("[green]Login detected.[/]")

        # Let the post-login landing page (/instr/InstradamentofcWeb/...)
        # finish its own redirects before we navigate away — interrupting it
        # mid-flight is what was bouncing the user back to the login page.
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        # Navigate straight to the invoices console home and wait for the
        # SPA to finish populating localStorage. No further user interaction.
        with console.status("[cyan]Loading invoices console...", spinner="dots"):
            try:
                page.goto(CONSOLE_URL)
                page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass

            deadline = time.monotonic() + 60
            custom_headers: dict[str, str] = {}
            while time.monotonic() < deadline:
                custom_headers = _read_tokens_from_context(context)
                if (
                    "x-b2bcookie" in custom_headers
                    and "x-token" in custom_headers
                ):
                    break
                time.sleep(1)

        if "x-b2bcookie" not in custom_headers or "x-token" not in custom_headers:
            console.print(
                "[bold red]Error:[/] session tokens not found in localStorage "
                "after loading the invoices console."
            )
            browser.close()
            sys.exit(1)

        if real_ua:
            custom_headers["User-Agent"] = real_ua

        cookies = {c["name"]: c["value"] for c in context.cookies()}

        console.print(
            f"[green]Session captured:[/] {len(cookies)} cookies, "
            f"{len(custom_headers)} custom headers."
        )
        browser.close()
        console.print("[dim]Browser closed.[/]\n")

    return cookies, custom_headers


def authenticate() -> tuple[dict[str, str], dict[str, str]]:
    """
    Return a valid session. Tries cached session first, falls back to browser login.
    """
    cached = _load_session()
    if cached:
        cookies, headers = cached
        with console.status("[cyan]Testing cached session...", spinner="dots"):
            if _test_session(cookies, headers):
                console.print("[green]Cached session is valid.[/]\n")
                return cookies, headers
        console.print("[yellow]Cached session expired, re-authenticating...[/]\n")
        _clear_session()

    cookies, headers = _browser_login()
    _save_session(cookies, headers)
    return cookies, headers


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _fetch_domestic(
    session: requests.Session,
    kind: str,
    start: date,
    end: date,
    vat_id: str | None = None,
) -> list[dict]:
    """
    Fetch domestic invoice list.

    - emesse:   /fe/emesse/dal/.../al/.../piva/{piva}            (filters by issue date;
                piva in path is typically session-scoped, but is honored under
                delegation — pass the target VAT when operating as a delegate)
    - ricevute: /fe/ricevute/dal/.../al/.../ricerca/ricezione    (filters by reception date;
                authenticated user is implicit recipient)
    """
    if kind == "emesse":
        piva = vat_id or PIVA_PATH_PLACEHOLDER
        url = (
            f"{BASE_URL}/fe/emesse/dal/{_fmt_date(start)}/al/{_fmt_date(end)}"
            f"/piva/{piva}?v={_ts()}"
        )
    else:
        url = (
            f"{BASE_URL}/fe/ricevute/dal/{_fmt_date(start)}/al/{_fmt_date(end)}"
            f"/ricerca/ricezione?v={_ts()}"
        )
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("fatture", [])


def _fetch_cross_border(
    session: requests.Session, kind: str, start: date, end: date,
) -> list[dict]:
    """Fetch cross-border invoice list. Endpoint: /ft/{kind}/dal/.../al/..."""
    url = (
        f"{BASE_URL}/ft/{kind}/dal/{_fmt_date(start)}/al/{_fmt_date(end)}"
        f"?v={_ts()}"
    )
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("fatture", [])


def fetch_invoice_list(
    session: requests.Session,
    kind: str,
    start: date,
    end: date,
    vat_id: str | None = None,
) -> list[dict]:
    """Fetch both domestic and cross-border invoices, merged and deduplicated."""
    domestic = _fetch_domestic(session, kind, start, end, vat_id=vat_id)
    cross_border = _fetch_cross_border(session, kind, start, end)

    # Deduplicate by idFattura (in case an invoice appears in both)
    seen = {inv.get("idFattura") for inv in domestic if inv.get("idFattura")}
    for inv in cross_border:
        inv_id = inv.get("idFattura")
        if inv_id and inv_id not in seen:
            domestic.append(inv)
            seen.add(inv_id)

    return domestic


def download_xml(session: requests.Session, send_type: str, invoice_id: str) -> bytes:
    """Download the XML file for a single invoice."""
    file_id = f"{send_type}{invoice_id}"
    url = (
        f"{BASE_URL}/fatture/file/{file_id}"
        f"?tipoFile=FILE_FATTURA&download=1&v={_ts()}"
    )
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.content


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _validate_common_args(args: argparse.Namespace) -> tuple[date, date]:
    """Validate --vat-id (if provided) and --from, return (start_date, end_date)."""
    if getattr(args, "vat_id", None) and not VAT_ID_RE.match(args.vat_id):
        console.print("[bold red]Error:[/] --vat-id must be exactly 11 digits.")
        sys.exit(1)
    try:
        start_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
    except ValueError:
        console.print("[bold red]Error:[/] --from must be YYYY-MM-DD.")
        sys.exit(1)
    end_date = date.today()
    if start_date > end_date:
        console.print("[bold red]Error:[/] --from date is in the future.")
        sys.exit(1)
    span_days = (end_date - start_date).days
    if span_days > 366 * 2:
        console.print(
            f"[bold yellow]Warning:[/] requested range is {span_days} days "
            f"(~{span_days // 365} years). This will issue many API calls."
        )
        try:
            reply = input("Continue? [y/N]: ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("y", "yes"):
            console.print("Aborted.")
            sys.exit(1)
    return start_date, end_date


def cmd_download(args: argparse.Namespace) -> None:
    """Download all invoices (issued and/or received) as XML."""
    start_date, end_date = _validate_common_args(args)

    out_root = Path(args.output).expanduser().resolve()
    if str(out_root) == "/" or out_root == Path(out_root.anchor):
        console.print("[bold red]Error:[/] refusing to write to filesystem root.")
        sys.exit(1)

    kinds = []
    if args.type in ("issued", "all"):
        kinds.append(("emesse", "issued"))
    if args.type in ("received", "all"):
        kinds.append(("ricevute", "received"))

    # Authenticate
    cookies, custom_headers = authenticate()

    # Setup HTTP session
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(DEFAULT_HEADERS)
    session.headers["Referer"] = _referer()
    session.headers.update(custom_headers)

    ranges = _date_ranges(start_date, end_date)
    console.print(
        f"Period: [bold]{start_date}[/] to [bold]{end_date}[/] "
        f"({len(ranges)} window{'s' if len(ranges) > 1 else ''} of max 3 months)\n"
    )

    for api_kind, label in kinds:
        out_dir = out_root / label
        out_dir.mkdir(parents=True, exist_ok=True)

        # Fetch invoice lists across all date windows
        all_invoices: list[dict] = []
        for r_start, r_end in ranges:
            with console.status(
                f"[cyan]Fetching {label} invoices {r_start} to {r_end}...",
                spinner="dots",
            ):
                invoices = fetch_invoice_list(
                    session, api_kind, r_start, r_end, vat_id=args.vat_id,
                )
                all_invoices.extend(invoices)
            console.print(
                f"  {r_start} to {r_end}: "
                f"[bold]{len(invoices)}[/] invoice{'s' if len(invoices) != 1 else ''}"
            )

        if not all_invoices:
            console.print(f"\nNo {label} invoices found.\n")
            continue

        # Summary table
        table = Table(title=f"{label.capitalize()} Invoices ({len(all_invoices)} total)")
        table.add_column("#", style="dim", width=4)
        table.add_column("Number", style="cyan")
        table.add_column("Date")
        table.add_column("Counterpart")
        table.add_column("Country", justify="center")
        table.add_column("Amount", justify="right")
        table.add_column("Status")

        for i, inv in enumerate(all_invoices, 1):
            if label == "received":
                counterpart = inv.get("denominazioneEmittente", "N/A")
                country = inv.get("idPaeseCedente", "")
            else:
                counterpart = inv.get("denominazioneCliente", "N/A")
                country = inv.get("idPaeseCessionario", "")
            table.add_row(
                str(i),
                inv.get("numeroFattura", "N/A"),
                inv.get("dataFattura", "N/A"),
                counterpart,
                country,
                inv.get("imponibile", "N/A").replace("+", "").replace("000000", "").strip(),
                inv.get("stato", "N/A"),
            )

        console.print()
        console.print(table)
        console.print()

        # Download XMLs
        downloadable = [
            inv for inv in all_invoices
            if inv.get("fileDownload", {}).get("fileDownload", 0) == 1
        ]
        skipped = len(all_invoices) - len(downloadable)
        if skipped:
            console.print(f"[yellow]{skipped} invoice(s) not available for download.[/]")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Downloading {label} XML", total=len(downloadable))

            for inv in downloadable:
                send_type = inv["tipoInvio"]
                invoice_id = inv["idFattura"]
                number = inv.get("numeroFattura", "N/A")
                file_id = f"{send_type}{invoice_id}"
                out_file = out_dir / f"{file_id}.xml"

                if out_file.exists():
                    progress.update(task, advance=1, description=f"[dim]{number} (cached)[/]")
                    continue

                progress.update(task, description=f"{number}")
                try:
                    xml_data = download_xml(session, send_type, invoice_id)
                    out_file.write_bytes(xml_data)
                except requests.HTTPError as e:
                    console.print(f"  [red]Error downloading {number}:[/] {e}")

                progress.update(task, advance=1)
                time.sleep(0.5)

        # Save JSON summary
        summary_file = out_dir / "summary.json"
        summary_file.write_text(json.dumps(all_invoices, indent=2, ensure_ascii=False))
        console.print(f"[dim]Summary saved to {summary_file}[/]\n")

    console.print("[bold green]Done![/]")


def cmd_list(args: argparse.Namespace) -> None:
    """List invoices without downloading."""
    start_date, end_date = _validate_common_args(args)

    kinds = []
    if args.type in ("issued", "all"):
        kinds.append(("emesse", "issued"))
    if args.type in ("received", "all"):
        kinds.append(("ricevute", "received"))

    cookies, custom_headers = authenticate()

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(DEFAULT_HEADERS)
    session.headers["Referer"] = _referer()
    session.headers.update(custom_headers)

    ranges = _date_ranges(start_date, end_date)

    for api_kind, label in kinds:
        all_invoices: list[dict] = []
        for r_start, r_end in ranges:
            with console.status(
                f"[cyan]Fetching {label} invoices {r_start} to {r_end}...",
                spinner="dots",
            ):
                all_invoices.extend(
                    fetch_invoice_list(
                        session, api_kind, r_start, r_end, vat_id=args.vat_id,
                    )
                )

        if not all_invoices:
            console.print(f"No {label} invoices found.\n")
            continue

        table = Table(title=f"{label.capitalize()} Invoices ({len(all_invoices)} total)")
        table.add_column("#", style="dim", width=4)
        table.add_column("Number", style="cyan")
        table.add_column("Date")
        table.add_column("Counterpart")
        table.add_column("Country", justify="center")
        table.add_column("Amount", justify="right")
        table.add_column("Tax", justify="right")
        table.add_column("Type")
        table.add_column("Status")

        for i, inv in enumerate(all_invoices, 1):
            if label == "received":
                counterpart = inv.get("denominazioneEmittente", "N/A")
                country = inv.get("idPaeseCedente", "")
            else:
                counterpart = inv.get("denominazioneCliente", "N/A")
                country = inv.get("idPaeseCessionario", "")
            table.add_row(
                str(i),
                inv.get("numeroFattura", "N/A"),
                inv.get("dataFattura", "N/A"),
                counterpart,
                country,
                inv.get("imponibile", "N/A").replace("+", "").replace("000000", "").strip(),
                inv.get("imposta", "N/A").replace("+", "").replace("000000", "").strip(),
                inv.get("tipoDocumento", "N/A"),
                inv.get("stato", "N/A"),
            )

        console.print()
        console.print(table)
        console.print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sdi-pull",
        description="Download electronic invoices (XML) from the Italian SdI via CIE login.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- download --
    dl = subparsers.add_parser("download", help="Download invoice XML files")
    dl.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    dl.add_argument(
        "--vat-id",
        default=None,
        help="Target VAT (Partita IVA). Only needed when operating under "
             "delegation for a third party; omit for self-service.",
    )
    dl.add_argument(
        "--type",
        choices=["issued", "received", "all"],
        default="issued",
        help="Invoice type to download (default: issued)",
    )
    dl.add_argument("--output", default="output", help="Output directory (default: output)")

    # -- list --
    ls = subparsers.add_parser("list", help="List invoices without downloading")
    ls.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    ls.add_argument(
        "--vat-id",
        default=None,
        help="Target VAT (Partita IVA). Only needed when operating under "
             "delegation for a third party; omit for self-service.",
    )
    ls.add_argument(
        "--type",
        choices=["issued", "received", "all"],
        default="issued",
        help="Invoice type to list (default: issued)",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "download":
        cmd_download(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
