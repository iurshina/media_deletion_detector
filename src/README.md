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
| `--output-dir` | Where to save HTML/diff files (default `saved_articles`) |
| `--delay` | Seconds between URL checks (default 1.0) |
| `--full-page` | Compare full HTML instead of extracted text (more false positives) |

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
  Filename: `{verdict}_{mode}_{timestamp}_{sanitized_url}.html`

## Understanding Results

| Verdict    | Meaning |
|------------|---------|
| `deleted`  | Live page returns 404, but the Wayback Machine has a copy. |
| `changed`  | Live page returns 200, but the extracted article text differs from the archive. A `.diff` file shows what changed. |
| (unchanged) | No action – text matches archive (or page never archived). |

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