# Media Deletion Detector

Automatically discover and archive deleted or altered articles from media sites (e.g., `fontanka.ru`) using the Wayback Machine. Helps investigate censorship and content manipulation.

## Features

- **Fetch all historical URLs** directly from Wayback Machine CDX API (no external tools)
- **Split requests by month** to avoid timeouts, save per‑year/month files
- **Content‑only comparison** (ignores ads, menus, layout) using `trafilatura`
- **Detect** `deleted` (404 live, archived copy exists) and `changed` (text differs)
- **Save full HTML** of deleted/changed articles as evidence
- **Stop after target number** of examples

## Prerequisites

- Python 3.8+
- `uv` (fast Python package manager)

## Installation

### 1. Install `uv`

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install project dependencies

```bash
uv sync
```

This installs `requests` and `trafilatura` (used for article-text extraction).
Content-only comparison requires `trafilatura`; without it the detector falls
back to full-page HTML comparison, which is noisy.

## Step 1: Fetch URLs from Wayback Machine

The command you'll use:

```bash
uv run src/fetch_cdx_urls.py --domain www.fontanka.ru --from 2015 --to 2025 --output-dir fontanka_urls
```

**What it does**:
- Queries the CDX API for each month from 2015‑01 to 2025‑12.
- Saves each month's URLs into a separate text file under `fontanka_urls/`.
- Files are named like `www_fontanka_ru_2015_01.txt`, `www_fontanka_ru_2015_02.txt`, etc.

**Why per‑month?**
Full‑year requests often time out (Fontanka 2015 alone had 165k+ URLs). Month splitting is reliable and lets you resume partial runs.

**Robustness**:
- **Resume** — months that already have a non‑empty output file are skipped, so you can interrupt and restart the run without losing progress.
- **Retries** — 5 attempts per CDX request with 2 / 5 / 15 / 30 / 60 s backoff, 120 s timeout. The Wayback CDX endpoint frequently returns 504/timeouts even when `web.archive.org` itself is up, so long backoff is needed.
- **Weekly fallback** — if all month‑level retries fail, the script automatically splits the month into ~7‑day windows and unions the results. Large months (e.g. Jan 2015) often only succeed at smaller window sizes.

**Arguments**:

| Argument | Description |
|----------|-------------|
| `--domain` | Domain to fetch (default: `www.fontanka.ru`) |
| `--from` | Start year (YYYY) |
| `--to` | End year (YYYY) |
| `--output-dir` | Directory to save per‑month files |
| `--no-split-months` | Fetch whole years at once (not recommended) |

## Step 2: Combine month files (optional)

If you want a single URL list for analysis:

```bash
cat fontanka_urls/*.txt > all_fontanka_urls.txt
```

## Step 3: Detect deletions and changes

Basic command (checks a random sample of 2000 URLs, stops after 50 findings):

```bash
uv run src/deletion_detector.py --urls-file all_fontanka_urls.txt --target 50 --limit 2000
```

**Arguments**:

| Argument | Description |
|----------|-------------|
| `--urls-file` | Text file with one URL per line – **required** |
| `--target` | Stop after this many `deleted` + `changed` (default 50) |
| `--limit` | Max URLs to check (random sample) – omit to check all |
| `--output-dir` | Where to save HTML files (default `saved_articles`) |
| `--workers` | Concurrent worker threads (default 8). Lower if Wayback rate-limits you. |
| `--delay` | Per-task delay after each URL in seconds (default 0). Bump up if rate-limited. |
| `--full-page` | Compare full HTML instead of extracted text (more false positives) |

URLs are processed concurrently across `--workers` threads, sharing a pooled
`requests.Session` so TLS handshakes are amortized. With 8 workers and a fast
network this is roughly an order of magnitude faster than the sequential
version. If you see `429`/`503` responses or `Connection refused`, drop
`--workers` (e.g. `--workers 3 --delay 0.5`).

### Examples

**Test with 5 examples from 100 random URLs:**
```bash
uv run src/deletion_detector.py --urls-file all_fontanka_urls.txt --target 5 --limit 100
```

**Full scan (no limit) – may take days for millions of URLs:**
```bash
uv run src/deletion_detector.py --urls-file all_fontanka_urls.txt --target 1000
```

## Output

- **HTML files** – saved in `saved_articles/` (or your `--output-dir`).
  Each file is the archived snapshot HTML (with a few `<!-- ... -->` metadata
  comments at the top: original URL, verdict, comparison mode, archive URL).
  Filename: `{verdict}_{mode}_{timestamp}_{sanitized_url}.html`
- **JSON report** – `report_<YYYYMMDD_HHMMSS>.json` in the working directory.
  Contains the run parameters and a `deleted_changed_articles` array with the
  URL, verdict, live status, archive timestamp/URL, saved file path, and the
  live/archive content digests for every finding.

## Understanding Results

| Verdict    | Meaning |
|------------|---------|
| `deleted`  | Live page returns 404, but the Wayback Machine has a copy. |
| `changed`  | Live page returns 200, but the extracted article text differs from the latest 200 archive snapshot. |
| (unchanged) | No action – text matches archive (or page never archived, or extraction failed on one side). |

Archive snapshots are fetched with the Wayback `id_` modifier
(`/web/<timestamp>id_/<url>`) so the toolbar/banner Wayback normally injects
is not part of the comparison. Extracted text is NFKC-normalized and
whitespace-collapsed before hashing to avoid spurious diffs.

## Full Workflow Example

```bash
# 1. Fetch URLs (one time, per month)
uv run src/fetch_cdx_urls.py --domain www.fontanka.ru --from 2015 --to 2025 --output-dir fontanka_urls

# 2. Combine all month files
cat fontanka_urls/*.txt > all_fontanka_urls.txt

# 3. Run detection (sample 5000 URLs, get 50 examples)
uv run src/deletion_detector.py --urls-file all_fontanka_urls.txt --target 50 --limit 5000

# 4. Examine outputs
ls saved_articles/
cat report_*.json | jq '.deleted_changed_articles[].url'
```

## Troubleshooting

- **`Connection refused` / `Max retries exceeded` to `web.archive.org`** —
  the Wayback cluster is unreachable from your network. Check
  https://status.archive.org/ and confirm with
  `curl -I --max-time 5 https://web.archive.org/`. The main `archive.org` host
  can be reachable while the Wayback cluster is down; the CDX fetcher and the
  detector both rely on the Wayback cluster.
- **All findings are `changed`, no `deleted`** — make sure live HTTP errors are
  reaching the detector (some sites return a 200 "Not found" page instead of a
  real 404; those will never be flagged as deleted).