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
# Default window: last 365 days, until today
./sdi-pull.py download

# Specific window
./sdi-pull.py download --from 2026-01-01 --to 2026-04-22

# Open-ended window (--to defaults to today)
./sdi-pull.py download --from 2026-01-01

# Received invoices only
./sdi-pull.py download --from 2026-01-01 --type received

# Both issued and received
./sdi-pull.py download --from 2026-01-01 --type all

# Custom output directory
./sdi-pull.py download --from 2026-01-01 --output ./my-invoices
```

### List invoices (no download)

```bash
./sdi-pull.py list
./sdi-pull.py list --from 2026-01-01 --to 2026-04-22
./sdi-pull.py list --type all
```

### Yearly recap

Both commands end with a recap grouped by tax year:

- **Fatturato** — total of issued invoices (`imponibile`)
- **Costi** — total of received invoices (`imponibile`)
- **IVA emessa / ricevuta** — VAT totals restricted to domestic IT→IT transactions (cross-border amounts are excluded from VAT since they don't carry Italian VAT)
- **Saldo IVA** — `IVA emessa − IVA ricevuta`, i.e. net VAT position per year

Two levels of accuracy, depending on the command:

- `list` shows a **fast preview** built from the listing-endpoint metadata. Fatturato and costi are accurate; IVA values may be off because AdE's list payload is unreliable for this field.
- `download` recomputes the recap **from every downloaded XML**, summing `<DatiRiepilogo>/<Imposta>` and `<ImponibileImporto>` across all rate blocks. XMLs are parsed in parallel after downloads finish. These are the totals to trust for fiscal purposes.

### Commands reference

| Command    | Description                                |
|------------|--------------------------------------------|
| `download` | Download invoice XML files                 |
| `list`     | List invoices in a table without downloading |

### Common options

| Option      | Description                                                 | Default                 |
|-------------|-------------------------------------------------------------|-------------------------|
| `--from`    | Start date (YYYY-MM-DD)                                     | 365 days before today   |
| `--to`      | End date (YYYY-MM-DD)                                       | today                   |
| `--type`    | `issued`, `received`, or `all`                              | `issued`                |
| `--output`  | Output directory (download only)                            | `output`                |
| `--delay`   | Seconds to wait after each AdE API call (throttling)        | `0.5`                   |

All queries are session-scoped — the authenticated identity (or the currently selected "utenza di lavoro") determines what's returned. No VAT argument is required. Delegation support (multi-entity selection for commercialisti) is planned for a later release.

### Throttling

Every outgoing call to AdE is followed by a configurable sleep via `--delay` (default 0.5s). The portal publishes no rate limits, but a gentle cadence avoids triggering server-side anomaly detection — especially when pulling large historical windows.

```bash
./sdi-pull.py download --delay 0      # no throttling (fastest, more aggressive)
./sdi-pull.py download --delay 2      # gentle — good for multi-year backfills
```

The delay applies to every API call (list endpoints, XML downloads). It does **not** slow down the local XML parsing phase, which happens in parallel after downloads complete.

## Output structure

Files are split by **year of the invoice date** and then by **counterpart country**:

```
output/
  issued/
    2024/
      italiane/           # counterpart is IT
        FPR12345...xml
      transfrontaliere/   # counterpart is non-IT
        FPR67890...xml
      summary.json        # metadata for every issued invoice dated 2024
    2025/
      italiane/
      transfrontaliere/
      summary.json
  received/
    2024/
      italiane/
      transfrontaliere/
      summary.json
    2025/
      ...
```

Each XML is the original fatturapa document as stored by AdE. `summary.json` in each year folder contains the listing-endpoint metadata for every invoice dated in that year, regardless of the italiane/transfrontaliere split.

### How the recap totals are computed

- **Fatturato / Costi** = `<ImportoTotaleDocumento>` minus any `<ImportoRitenuta>` (ritenuta d'acconto). This matches the "netto a pagare" that Italian accountants use as fatturato — i.e. the amount the client actually pays.
- **Credit notes (TD04, TD08)** are **subtracted** — a credit note that cancels a previous invoice brings the net contribution back to zero.
- **Tax-integration documents (TD16–TD23, TD28)** — integrazioni, autofatture, estrazioni Deposito IVA, registrazione SM — are **skipped entirely**, because they aren't real commercial transactions.
- **IVA emessa / ricevuta** are restricted to domestic IT→IT invoices (cross-border invoices under reverse charge carry no Italian VAT). IVA is computed from `<DatiRiepilogo>/<Imposta>` summed across every rate block.
- Skipped / subtracted / missing documents are still **downloaded and kept** in the correct year/country folder — the filter only affects the recap totals.

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
- [x] Yearly recap (fatturato, costi, IVA) grouped by tax year

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