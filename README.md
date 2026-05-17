# Google Maps Lead Scraper

Free, local-first Google Maps business scraper with a web UI. No API key. No external services. Saves CSV + JSON to `data/` automatically.

## What it extracts (per place)

`title, totalScore, reviewsCount, street, city, state, postalCode, countryCode, website, phone, categories, categoryName, plusCode, url, fullAddress, placeId, searchString`

## Setup (one time)

```bat
pip install -r requirements.txt
python -m playwright install chromium
```

## Run

Double-click `run.bat`, or:

```bat
python -m uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000 in your browser.

1. Type a **Keyword** (e.g. `Gold Jewellery`).
2. Optional **Location** (e.g. `Raipur, Chhattisgarh, India`).
3. Set the number of **Leads** you want (1–200).
4. Click **Search**. Results stream in live; download CSV or JSON when done.

Every job is also auto-saved to `data/<timestamp>_<keyword>_<id>.csv` and `.json`.

## CLI (no UI)

```bat
python -m app.scraper "Gold Jewellery" "Raipur, Chhattisgarh, India" 25
```

Prints a JSON array of leads to stdout.

## How it works

- Playwright (headless Chromium) opens `https://www.google.com/maps/search/<query>`.
- Scrolls the result feed until enough cards are loaded or end-of-list is reached.
- Clicks each card, reads the detail panel (`data-item-id` attributes for phone / website / address / plus code, `div.F7nice` for rating + review count, `button[jsaction*="category"]` for category).
- Streams each finished lead to the browser over Server-Sent Events.

## Limits & notes

- Speed: ~1–3 seconds per lead. 50 leads ≈ 1–2 minutes.
- Google may rate-limit a single IP after a few hundred queries in quick succession. For personal use this isn't a problem. If you hit a soft block, wait a few minutes or restart your router.
- Selectors can drift if Google ships UI changes. If a field stops populating, update the relevant block in `app/scraper.py` (look for `_extract_detail_panel`).
- This scrapes only what Google Maps publicly shows to logged-out users.
