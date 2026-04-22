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
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# Fatturapa TipoDocumento classification for the recap:
#   - CREDIT_NOTE  -> SUBTRACTED (they reverse a previous invoice; net effect
#                      on fatturato/IVA should be negative).
#   - NON_COMMERCIAL -> SKIPPED entirely (integrazioni, autofatture, estrazioni
#                      Deposito IVA, registrazione SM: tax-compliance docs,
#                      not real commercial transactions).
# Every other TD code (TD01/02/03/05/06/07/09/24/25/26/27) is added.
CREDIT_NOTE_TD_CODES = frozenset({"TD04", "TD08"})
NON_COMMERCIAL_TD_CODES = frozenset({
    "TD16", "TD17", "TD18", "TD19", "TD20", "TD21", "TD22", "TD23", "TD28",
})

# Delay inserted after each outgoing AdE HTTP call. Set from the --delay CLI
# arg at startup; 0 means no throttling. Keep this conservative by default:
# the portal has never published rate limits, but a gentle cadence reduces
# the chance of triggering server-side anomaly detection.
API_DELAY = 0.5


def _throttle() -> None:
    """Sleep API_DELAY seconds if throttling is enabled."""
    if API_DELAY > 0:
        time.sleep(API_DELAY)

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


def _parse_amount(s: str | None) -> float:
    """Parse an AdE amount string (e.g. '+000000001234.56' or '-1234,56')."""
    if not s:
        return 0.0
    s = s.strip()
    if s.startswith("+"):
        s = s[1:]
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_invoice_year(s: str | None) -> int | None:
    """Extract the year from an AdE invoice date, trying common formats."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).year
        except ValueError:
            continue
    m = re.search(r"(19|20)\d{2}", s)
    return int(m.group(0)) if m else None


def _invoice_year_folder(inv: dict) -> str:
    """Year bucket for directory layout; 'unknown' if date can't be parsed."""
    y = _parse_invoice_year(inv.get("dataFattura"))
    return str(y) if y else "unknown"


def _invoice_country_bucket(inv: dict, label: str) -> str:
    """'italiane' for IT→IT counterparts, otherwise 'transfrontaliere'."""
    country_key = "idPaeseCedente" if label == "received" else "idPaeseCessionario"
    return "italiane" if inv.get(country_key) == "IT" else "transfrontaliere"


def _invoice_output_path(inv: dict, label_dir: Path, label: str) -> Path:
    """Compute the target XML path under <label>/<year>/<italiane|transfrontaliere>."""
    file_id = f"{inv.get('tipoInvio', '')}{inv.get('idFattura', '')}"
    return (
        label_dir
        / _invoice_year_folder(inv)
        / _invoice_country_bucket(inv, label)
        / f"{file_id}.xml"
    )


def _local_tag(el: ET.Element) -> str:
    """Return an XML element's tag name without its namespace prefix."""
    return el.tag.split("}", 1)[-1] if "}" in el.tag else el.tag


def _parse_fatturapa_xml(xml_path: Path) -> list[dict]:
    """
    Parse a fatturapa XML and return one record per FatturaElettronicaBody.

    Each record: {"year": int | None, "imponibile": float, "imposta": float}.

    The authoritative IVA totals live inside DatiBeniServizi/DatiRiepilogo
    (multiple blocks per invoice, one per rate). This is what fiscal accounting
    actually uses — the metadata 'imposta' field from the listing endpoint is
    unreliable and should not be trusted for VAT calculations.
    """
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, FileNotFoundError, OSError):
        return []

    root = tree.getroot()
    bodies = [e for e in root.iter() if _local_tag(e) == "FatturaElettronicaBody"]
    if not bodies:
        bodies = [root]

    records: list[dict] = []
    for body in bodies:
        year: int | None = None
        tipo_doc: str | None = None
        totale = 0.0
        ritenute = 0.0
        for gen in body.iter():
            if _local_tag(gen) != "DatiGeneraliDocumento":
                continue
            for child in gen.iter():
                name = _local_tag(child)
                if name == "Data" and year is None:
                    y = _parse_invoice_year((child.text or "").strip())
                    if y:
                        year = y
                elif name == "TipoDocumento" and tipo_doc is None:
                    tipo_doc = (child.text or "").strip() or None
                elif name == "ImportoTotaleDocumento" and not totale:
                    totale = _parse_amount(child.text)
                elif name == "ImportoRitenuta":
                    # An invoice can carry multiple <DatiRitenuta> blocks
                    # (RT01, RT02, ...). Sum all ImportoRitenuta entries.
                    ritenute += _parse_amount(child.text)
            break  # only one DatiGeneraliDocumento per body

        imposta = 0.0
        imponibile = 0.0
        for dr in body.iter():
            if _local_tag(dr) != "DatiRiepilogo":
                continue
            for child in dr:
                name = _local_tag(child)
                if name == "Imposta":
                    imposta += _parse_amount(child.text)
                elif name == "ImponibileImporto":
                    imponibile += _parse_amount(child.text)

        records.append({
            "year": year,
            "tipo_documento": tipo_doc,
            "imponibile": imponibile,
            "imposta": imposta,
            "totale": totale,
            "ritenute": ritenute,
        })

    return records


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


def _dump_localstorage(context) -> dict[str, str]:
    """
    Return a flat dump of localStorage across every open page whose origin
    is the AdE invoices portal. localStorage is origin-scoped, so we scan
    each page to find the one that holds the SPA's state.
    """
    for p in context.pages:
        try:
            url = p.url
        except Exception:
            continue
        if "ivaservizi.agenziaentrate.gov.it" not in url:
            continue
        try:
            items = p.evaluate("""() => {
                const out = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out[k] = localStorage.getItem(k);
                }
                return out;
            }""")
        except Exception:
            continue
        if items:
            return items
    return {}


def _read_tokens_from_context(context) -> dict[str, str]:
    """Read x-b2bcookie and x-token from the SPA's localStorage."""
    items = _dump_localstorage(context)
    tokens: dict[str, str] = {}
    b2b = items.get("FattCorrActiveB2B") or ""
    tok = items.get("FattCorrActiveToken") or ""
    if b2b:
        tokens["x-b2bcookie"] = b2b
    if tok:
        tokens["x-token"] = tok
    return tokens


def _browser_login() -> tuple[dict[str, str], dict[str, str]]:
    """Open Chromium, wait for CIE login, extract session. Returns (cookies, headers)."""
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
    session: requests.Session, kind: str, start: date, end: date,
) -> list[dict]:
    """
    Fetch domestic invoice list. Both endpoints are session-scoped — no VAT
    is required in the path. The portal's own SPA calls them exactly like this.

    - emesse:   /fe/emesse/dal/{D}/al/{D}                       (filters by issue date)
    - ricevute: /fe/ricevute/dal/{D}/al/{D}/ricerca/ricezione   (filters by reception date)
    """
    if kind == "emesse":
        url = (
            f"{BASE_URL}/fe/emesse/dal/{_fmt_date(start)}/al/{_fmt_date(end)}"
            f"?v={_ts()}"
        )
    else:
        url = (
            f"{BASE_URL}/fe/ricevute/dal/{_fmt_date(start)}/al/{_fmt_date(end)}"
            f"/ricerca/ricezione?v={_ts()}"
        )
    resp = session.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    _throttle()
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
    _throttle()
    return resp.json().get("fatture", [])


def fetch_invoice_list(
    session: requests.Session, kind: str, start: date, end: date,
) -> list[dict]:
    """Fetch both domestic and cross-border invoices, merged and deduplicated."""
    domestic = _fetch_domestic(session, kind, start, end)
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
    _throttle()
    return resp.content


# ---------------------------------------------------------------------------
# Recap
# ---------------------------------------------------------------------------

# A recap record represents one invoice body contribution to the yearly totals.
# Shape: {"year": int | None, "kind": "issued"|"received",
#         "totale": float, "imposta": float, "domestic": bool}
# Values may be negative for credit-note bodies (TD04/TD08) so they subtract
# from the running totals.

def _eur(x: float) -> str:
    """Italian-style euro formatting: '€ 1.234,56'."""
    return f"€ {x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _print_stats_table(
    records: list[dict],
    title: str,
    footnote: str | None = None,
) -> None:
    stats: dict[int, dict[str, float]] = {}
    for rec in records:
        year = rec.get("year")
        if year is None:
            continue
        bucket = stats.setdefault(year, {
            "fatturato": 0.0, "costi": 0.0,
            "iva_emessa": 0.0, "iva_ricevuta": 0.0,
        })
        if rec["kind"] == "issued":
            bucket["fatturato"] += rec["totale"]
            if rec["domestic"]:
                bucket["iva_emessa"] += rec["imposta"]
        else:
            bucket["costi"] += rec["totale"]
            if rec["domestic"]:
                bucket["iva_ricevuta"] += rec["imposta"]

    if not stats:
        return

    table = Table(title=title)
    table.add_column("Anno", style="bold cyan")
    table.add_column("Fatturato", justify="right")
    table.add_column("Costi", justify="right")
    table.add_column("IVA emessa (IT)", justify="right")
    table.add_column("IVA ricevuta (IT)", justify="right")
    table.add_column("Saldo IVA", justify="right", style="bold")

    for year in sorted(stats.keys()):
        s = stats[year]
        table.add_row(
            str(year),
            _eur(s["fatturato"]),
            _eur(s["costi"]),
            _eur(s["iva_emessa"]),
            _eur(s["iva_ricevuta"]),
            _eur(s["iva_emessa"] - s["iva_ricevuta"]),
        )

    console.print()
    console.print(table)
    if footnote:
        console.print(f"[dim]{footnote}[/]")
    console.print()


def _records_from_metadata(
    issued: list[dict] | None,
    received: list[dict] | None,
) -> tuple[list[dict], int, int]:
    """Build recap records from the listing-endpoint metadata.

    Fast but IVA is NOT trustworthy — the metadata 'imposta' field is known
    to be unreliable. Suitable for the `list` command as a rough preview.
    Returns (records, non_commercial_excluded, credit_notes_subtracted).
    """
    records: list[dict] = []
    skipped = 0
    subtracted = 0

    def _emit(inv: dict, kind: str, country_key: str) -> None:
        nonlocal skipped, subtracted
        td = inv.get("tipoDocumento") or ""
        if td in NON_COMMERCIAL_TD_CODES:
            skipped += 1
            return
        sign = -1.0 if td in CREDIT_NOTE_TD_CODES else 1.0
        if sign < 0:
            subtracted += 1
        imponibile = _parse_amount(inv.get("imponibile"))
        imposta = _parse_amount(inv.get("imposta"))
        records.append({
            "year": _parse_invoice_year(inv.get("dataFattura")),
            "kind": kind,
            "totale": sign * (imponibile + imposta),
            "imposta": sign * imposta,
            "domestic": inv.get(country_key) == "IT",
        })

    for inv in issued or []:
        _emit(inv, "issued", "idPaeseCessionario")
    for inv in received or []:
        _emit(inv, "received", "idPaeseCedente")
    return records, skipped, subtracted


def _records_from_xml(
    jobs: list[tuple[dict, str, Path]],
    max_workers: int = 8,
) -> tuple[list[dict], int, int, int, int]:
    """Parse XMLs in parallel and build accurate recap records.

    `jobs` is a list of (invoice_metadata, label, xml_path) tuples — each
    pointing to a downloaded file on disk. Returns
    (records, parsed, missing, skipped, subtracted):
      - parsed     : invoices whose XML was successfully read
      - missing    : invoices whose XML was missing / unparseable
      - skipped    : bodies skipped (NON_COMMERCIAL_TD_CODES)
      - subtracted : bodies subtracted (CREDIT_NOTE_TD_CODES)
    """
    if not jobs:
        return [], 0, 0, 0, 0

    records: list[dict] = []
    parsed = 0
    missing = 0
    skipped = 0
    subtracted = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Parsing XML for accurate IVA", total=len(jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_parse_fatturapa_xml, xml_path): (inv, label)
                for inv, label, xml_path in jobs
            }
            for fut in as_completed(futures):
                inv, label = futures[fut]
                bodies = fut.result()
                if not bodies:
                    missing += 1
                    progress.update(task, advance=1)
                    continue
                parsed += 1
                domestic_key = (
                    "idPaeseCedente" if label == "received" else "idPaeseCessionario"
                )
                domestic = inv.get(domestic_key) == "IT"
                for body in bodies:
                    td = body.get("tipo_documento") or ""
                    if td in NON_COMMERCIAL_TD_CODES:
                        skipped += 1
                        continue
                    sign = -1.0 if td in CREDIT_NOTE_TD_CODES else 1.0
                    if sign < 0:
                        subtracted += 1
                    # Fatturato / costi = ImportoTotaleDocumento minus any
                    # <ImportoRitenuta> (ritenuta d'acconto) — this is the
                    # "netto a pagare" that accountants use as fatturato.
                    # Fall back to imponibile+imposta if header is missing.
                    raw_totale = body["totale"] or (body["imponibile"] + body["imposta"])
                    netto = raw_totale - body["ritenute"]
                    records.append({
                        "year": body["year"] or _parse_invoice_year(inv.get("dataFattura")),
                        "kind": label,
                        "totale": sign * netto,
                        "imposta": sign * body["imposta"],
                        "domestic": domestic,
                    })
                progress.update(task, advance=1)

    return records, parsed, missing, skipped, subtracted


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _validate_common_args(args: argparse.Namespace) -> tuple[date, date]:
    """Validate args, return (start_date, end_date). Exits on error."""
    today = date.today()
    if args.from_date:
        try:
            start_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        except ValueError:
            console.print("[bold red]Error:[/] --from must be YYYY-MM-DD.")
            sys.exit(1)
    else:
        start_date = today - timedelta(days=365)
    if args.to_date:
        try:
            end_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        except ValueError:
            console.print("[bold red]Error:[/] --to must be YYYY-MM-DD.")
            sys.exit(1)
    else:
        end_date = today
    if start_date > end_date:
        console.print("[bold red]Error:[/] --from is after --to.")
        sys.exit(1)
    if start_date > today:
        console.print("[bold red]Error:[/] --from date is in the future.")
        sys.exit(1)
    if end_date > today:
        console.print("[bold red]Error:[/] --to date is in the future.")
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

    # Jobs for the post-download XML parser: (invoice_metadata, label, xml_path).
    parse_jobs: list[tuple[dict, str, Path]] = []

    for api_kind, label in kinds:
        label_dir = out_root / label
        label_dir.mkdir(parents=True, exist_ok=True)

        # Fetch invoice lists across all date windows
        all_invoices: list[dict] = []
        for r_start, r_end in ranges:
            with console.status(
                f"[cyan]Fetching {label} invoices {r_start} to {r_end}...",
                spinner="dots",
            ):
                invoices = fetch_invoice_list(
                    session, api_kind, r_start, r_end,
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

        # Download XMLs — each goes under <label>/<year>/<italiane|transfrontaliere>/.
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
                out_file = _invoice_output_path(inv, label_dir, label)
                out_file.parent.mkdir(parents=True, exist_ok=True)

                if out_file.exists():
                    progress.update(task, advance=1, description=f"[dim]{number} (cached)[/]")
                    parse_jobs.append((inv, label, out_file))
                    continue

                progress.update(task, description=f"{number}")
                try:
                    xml_data = download_xml(session, send_type, invoice_id)
                    out_file.write_bytes(xml_data)
                    parse_jobs.append((inv, label, out_file))
                except requests.HTTPError as e:
                    console.print(f"  [red]Error downloading {number}:[/] {e}")

                progress.update(task, advance=1)

        # Write one summary.json per year, bundling invoices under that year
        # regardless of their italiane/transfrontaliere split.
        by_year: dict[str, list[dict]] = {}
        for inv in all_invoices:
            by_year.setdefault(_invoice_year_folder(inv), []).append(inv)
        for year_key, year_invoices in by_year.items():
            year_dir = label_dir / year_key
            year_dir.mkdir(parents=True, exist_ok=True)
            (year_dir / "summary.json").write_text(
                json.dumps(year_invoices, indent=2, ensure_ascii=False)
            )
        console.print(
            f"[dim]Summaries saved under {label_dir}/<year>/summary.json "
            f"({len(by_year)} year{'s' if len(by_year) != 1 else ''})[/]\n"
        )

    records, parsed, missing, skipped, subtracted = _records_from_xml(parse_jobs)
    note_parts = [
        "Fatturato/Costi = sum of <ImportoTotaleDocumento> (grand total, incl. IVA). "
        "IVA totals are restricted to domestic IT→IT transactions.",
    ]
    if subtracted:
        note_parts.append(
            f"{subtracted} credit note(s) (TD04/TD08) subtracted from totals."
        )
    if skipped:
        note_parts.append(
            f"{skipped} tax-integration doc(s) (TD16–TD23/TD28) excluded."
        )
    if missing:
        note_parts.append(
            f"{missing} invoice(s) excluded (XML missing or unparseable)."
        )
    if parsed:
        _print_stats_table(
            records,
            title="Recap per anno d'imposta (XML-accurate)",
            footnote=" ".join(note_parts),
        )

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

    collected: dict[str, list[dict]] = {}

    for api_kind, label in kinds:
        all_invoices: list[dict] = []
        for r_start, r_end in ranges:
            with console.status(
                f"[cyan]Fetching {label} invoices {r_start} to {r_end}...",
                spinner="dots",
            ):
                all_invoices.extend(
                    fetch_invoice_list(session, api_kind, r_start, r_end)
                )

        collected[label] = all_invoices

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

    records, meta_skipped, meta_subtracted = _records_from_metadata(
        collected.get("issued"), collected.get("received"),
    )
    note = (
        "Preview only — computed from listing-endpoint metadata (IVA values "
        "from AdE may be inaccurate). Run `download` for authoritative "
        "totals recomputed from each XML."
    )
    if meta_subtracted:
        note += f" {meta_subtracted} credit note(s) subtracted."
    if meta_skipped:
        note += f" {meta_skipped} tax-integration doc(s) excluded."
    _print_stats_table(
        records,
        title="Recap per anno d'imposta (metadata preview)",
        footnote=note,
    )


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
    dl.add_argument(
        "--from", dest="from_date", default=None,
        help="Start date (YYYY-MM-DD). Default: 365 days before today.",
    )
    dl.add_argument(
        "--to", dest="to_date", default=None,
        help="End date (YYYY-MM-DD). Default: today.",
    )
    dl.add_argument(
        "--type",
        choices=["issued", "received", "all"],
        default="issued",
        help="Invoice type to download (default: issued)",
    )
    dl.add_argument("--output", default="output", help="Output directory (default: output)")
    dl.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds to wait after each AdE API call (default: 0.5). Use 0 "
             "to disable throttling, or raise it (e.g. 2) for gentler pacing.",
    )

    # -- list --
    ls = subparsers.add_parser("list", help="List invoices without downloading")
    ls.add_argument(
        "--from", dest="from_date", default=None,
        help="Start date (YYYY-MM-DD). Default: 365 days before today.",
    )
    ls.add_argument(
        "--to", dest="to_date", default=None,
        help="End date (YYYY-MM-DD). Default: today.",
    )
    ls.add_argument(
        "--type",
        choices=["issued", "received", "all"],
        default="issued",
        help="Invoice type to list (default: issued)",
    )
    ls.add_argument(
        "--delay", type=float, default=0.5,
        help="Seconds to wait after each AdE API call (default: 0.5). Use 0 "
             "to disable throttling, or raise it (e.g. 2) for gentler pacing.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.delay < 0:
        console.print("[bold red]Error:[/] --delay must be >= 0.")
        sys.exit(1)

    global API_DELAY
    API_DELAY = args.delay

    if args.command == "download":
        cmd_download(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
