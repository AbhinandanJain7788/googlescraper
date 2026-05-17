"""Perf test: measure per-lead timing + email yield on the CURRENT (unchanged) code.

Run a small scrape (5 leads), record exactly when each lead arrives, whether it has
an email, and how long the website + email phase took on each. No code changes.
"""
import asyncio
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from app.scraper import GoogleMapsScraper

KEYWORD = "Gold Jewellery"
LOCATION = "Raipur, Chhattisgarh, India"
N = 5


async def main():
    started = time.time()
    last_t = started
    rows = []

    def on_status(msg):
        t = time.time()
        print(f"[{t-started:6.2f}s] STATUS: {msg}")

    def on_result(item):
        nonlocal last_t
        t = time.time()
        gap = t - last_t
        last_t = t
        rows.append({
            "t_abs": t - started,
            "gap_s": gap,
            "title": item.get("title"),
            "website": item.get("website"),
            "email": item.get("email"),
            "emails_count": len(item.get("emails") or []),
            "phone": item.get("phone"),
        })
        em = item.get("email") or "(none)"
        site = item.get("website") or "(no website)"
        print(f"[{t-started:6.2f}s] +{gap:5.2f}s LEAD: {item.get('title')!r}")
        print(f"           website={site}  email={em}  (#emails={len(item.get('emails') or [])})")

    print(f"=== PERF TEST: {KEYWORD} in {LOCATION}, N={N}, fetch_emails=True (current code, no changes) ===\n")
    async with GoogleMapsScraper(headless=True, fetch_emails=True) as s:
        await s.scrape(
            keyword=KEYWORD,
            location=LOCATION,
            max_results=N,
            on_result=on_result,
            on_status=on_status,
            auto_grid=False,  # keep test simple — one tile
            restrict_to_location=False,  # don't add geocode latency to the measurement
        )

    total = time.time() - started
    with_email = sum(1 for r in rows if r["email"])
    with_site = sum(1 for r in rows if r["website"])

    print("\n=== SUMMARY ===")
    print(f"Total time:     {total:.1f}s for {len(rows)} leads")
    print(f"Avg per lead:   {total/max(len(rows),1):.1f}s")
    print(f"With website:   {with_site}/{len(rows)}")
    print(f"With email:     {with_email}/{len(rows)}")
    print(f"Email yield:    {with_email/max(len(rows),1)*100:.0f}%")
    print(f"Email-of-sites: {with_email}/{with_site} ({with_email/max(with_site,1)*100:.0f}% of sites had extractable email)")
    print("\nPer-lead gaps (seconds between consecutive lead emissions):")
    for i, r in enumerate(rows, 1):
        em_flag = "Y" if r["email"] else "-"
        site_flag = "Y" if r["website"] else "-"
        print(f"  #{i:2d}  gap={r['gap_s']:6.2f}s  site={site_flag}  email={em_flag}  {r['title']!r}")


if __name__ == "__main__":
    asyncio.run(main())
