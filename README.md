# sdi-pull

CLI tool to bulk-download electronic invoices (Fatture Elettroniche) as XML from the Italian Revenue Agency (Agenzia delle Entrate) portal, the invoices managed by the SdI (Sistema di Interscambio).

## Intended use

This tool is for **personal use by the holder of the credentials**. It logs in with **your** CIE (or SPID/CNS) via a real browser you control, captures **your** session tokens from **your** browser's localStorage, and downloads **your own** invoices through the same public API endpoints the official web console (`ivaservizi.agenziaentrate.gov.it/cons/cons-web/`) already calls.

It does **not**:

- bypass or weaken any authentication mechanism,
- access data belonging to third parties,
- scrape, enumerate, or brute-force anything,
- store or transmit your credentials (the CIE flow happens entirely in the browser window you see).

Think of it as a convenience wrapper that clicks "Download XML" on every invoice for you, instead of doing it by hand.

## Disclaimer

This project is **not affiliated with, endorsed by, or sponsored by Agenzia delle Entrate** or any Italian government body. The portal's API endpoints are undocumented and can change without notice — if AdE changes the SPA, this tool may stop working until updated. No warranty, no guarantees of fitness for any purpose (see the [LICENSE](LICENSE)).

Use of the Agenzia delle Entrate portal is subject to its own Terms of Service. By running this tool, you are responsible for ensuring your use complies with those terms and with applicable law. The author provides the code; you run it under your own identity and responsibility.

## How it works

1. Opens a Chromium window to the Agenzia delle Entrate portal
2. You complete CIE authentication (QR code or CIE + PIN)
3. The tool automatically captures session cookies and security tokens
4. Browser closes, and all invoices are downloaded via API in the background

The portal limits queries to 3-month windows — the tool handles this automatically by splitting the date range.

## Installation

The only prerequisite is [uv](https://docs.astral.sh/uv/).

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and enter the repo
git clone https://github.com/r0metheus/sdi-pull && cd sdi-pull

# Install the Chromium build used for CIE login
uv run playwright install chromium
```

> **First run is slow-ish** (~30 seconds): uv downloads Python and the dependencies once, caches them, and subsequent runs are instant.

## Usage

The script is self-contained and you can run it directly:

```bash
./sdi-pull.py download --from 2026-01-01
```

Or explicitly through uv:

```bash
uv run sdi-pull.py download --from 2026-01-01
```

### Download invoices

```bash
# All issued invoices from Jan 1, 2026 to today
./sdi-pull.py download --from 2026-01-01

# Received invoices only
./sdi-pull.py download --from 2026-01-01 --type received

# Both issued and received
./sdi-pull.py download --from 2026-01-01 --type all

# Custom output directory
./sdi-pull.py download --from 2026-01-01 --output ./my-invoices

# Operating under delegation for a third-party VAT
./sdi-pull.py download --from 2026-01-01 --vat-id 12345678901
```

### List invoices (no download)

```bash
./sdi-pull.py list --from 2026-01-01
./sdi-pull.py list --from 2026-01-01 --type all
```

### Commands reference

| Command    | Description                                |
|------------|--------------------------------------------|
| `download` | Download invoice XML files                 |
| `list`     | List invoices in a table without downloading |

### Common options

| Option      | Description                                              | Default   |
|-------------|----------------------------------------------------------|-----------|
| `--from`    | Start date (YYYY-MM-DD)                                  | required  |
| `--type`    | `issued`, `received`, or `all`                           | `issued`  |
| `--output`  | Output directory (download only)                         | `output`  |
| `--vat-id`  | Target Partita IVA (only under delegation; see below)    | none      |

For self-service (user logged in with their own CIE), `--vat-id` is not needed — the session is scoped to the authenticated identity.

For delegated access (e.g. a commercialista logged in with their own CIE, operating on behalf of an assistito), pass `--vat-id` with the target VAT so the query is routed to the correct entity.

## Output structure

```
output/
  issued/
    issued_invoice_1.xml
    issued_invoice_2.xml
    summary.json
  received/
    received_invoice_1.xml
    summary.json
```

Each XML file is the original electronic invoice as stored by AdE. The `summary.json` contains metadata for all invoices in that category.

## Features

- [x] CIE authentication via browser (automatic session capture)
- [x] Download issued invoices as XML (domestic + cross-border)
- [x] Download received invoices as XML (domestic + cross-border)
- [x] Automatic 3-month window splitting
- [x] Skip already downloaded files (incremental)
- [x] List invoices without downloading
- [x] JSON summary export
- [x] Rich terminal UI (tables, progress bars, spinners)
- [x] Session caching (`~/.sdi-pull/session.json`) — skips login if session is still valid

## Roadmap

- [ ] Filter by counterpart (client/supplier name or VAT)
- [ ] Filter by date range (custom end date, not just today)
- [ ] Filter by invoice amount

## Requirements

- [uv](https://docs.astral.sh/uv/) (installs Python 3.10+ and all dependencies automatically)
- A working [CIE](https://www.cartaidentita.interno.gov.it/) (Italian electronic identity card) with either an NFC reader or the [CieID](https://www.cartaidentita.interno.gov.it/cie-id/) mobile app for QR authentication

## For contributors

If you want a persistent dev environment instead of one-shot script runs:

```bash
uv sync                               # install deps into .venv
uv run playwright install chromium    # one-time browser setup
uv run sdi-pull.py --help             # run within the env
```

Dependency versions are pinned with hashes in `uv.lock` — commit it together with any change to `pyproject.toml`.

## License

Licensed under the [GNU Affero General Public License v3.0 or later](LICENSE) (AGPL-3.0-or-later).

If you run a modified version of this software as part of a network-accessible service, you are required to make the corresponding source code available to the users of that service. Local use (CLI, desktop, private scripts) carries no additional obligations beyond standard GPL terms.