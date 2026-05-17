"""Smoke test: prove we can pull the fields the user wants from live Google Maps."""
import asyncio
import json
import re
import sys
from urllib.parse import quote_plus

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.async_api import async_playwright


async def main():
    keyword = "Gold Jewellery"
    location = "Raipur, Chhattisgarh, India"
    query = f"{keyword} {location}"
    url = f"https://www.google.com/maps/search/{quote_plus(query)}/?hl=en"
    print(f"[smoke] URL: {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--lang=en-US"])
        ctx = await browser.new_context(
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)

        # Handle consent if Google shows it
        try:
            consent = page.locator("button:has-text('Accept all'), button:has-text('Reject all')").first
            if await consent.count() > 0:
                await consent.click(timeout=3000)
                print("[smoke] consent dismissed")
        except Exception:
            pass

        # Wait for the result feed
        feed_sel = 'div[role="feed"]'
        try:
            await page.wait_for_selector(feed_sel, timeout=20_000)
        except Exception:
            print("[smoke] no feed found — maybe a single-place page; dumping URL/title and exiting")
            print("URL:", page.url)
            print("TITLE:", await page.title())
            html = await page.content()
            print("HTML LEN:", len(html))
            await browser.close()
            return

        # Scroll the feed a few times to load more results
        for i in range(3):
            await page.evaluate(
                """sel => {
                  const el = document.querySelector(sel);
                  if (el) el.scrollTo(0, el.scrollHeight);
                }""",
                feed_sel,
            )
            await page.wait_for_timeout(1500)

        # Get all place cards (anchors that link to /maps/place/)
        cards = await page.locator(f'{feed_sel} a[href*="/maps/place/"]').all()
        print(f"[smoke] found {len(cards)} place cards")

        results = []
        for idx, card in enumerate(cards[:5]):  # only inspect first 5
            try:
                href = await card.get_attribute("href")
                title = await card.get_attribute("aria-label")
                # the parent container has rating/reviews + category snippets
                parent = card.locator("xpath=..")
                text = await parent.inner_text()
                results.append({"i": idx, "title": title, "href": href, "snippet": text[:300]})
            except Exception as e:
                results.append({"i": idx, "error": str(e)})

        print("\n=== CARD-LEVEL DATA ===")
        for r in results:
            print(json.dumps(r, indent=2, ensure_ascii=False))

        # Click the first card to inspect the detail panel
        if cards:
            print("\n=== CLICKING FIRST CARD FOR DETAIL ===")
            await cards[0].click()
            await page.wait_for_timeout(3500)

            # detail panel header
            try:
                h1 = await page.locator("h1").first.inner_text(timeout=5000)
            except Exception:
                h1 = None
            # buttons with data-item-id give us phone, website, address
            data_btns = await page.locator("button[data-item-id], a[data-item-id]").all()
            kv = {}
            for b in data_btns:
                try:
                    item_id = await b.get_attribute("data-item-id") or ""
                    aria = await b.get_attribute("aria-label") or ""
                    kv[item_id] = aria
                except Exception:
                    pass

            # category usually in a button under the title
            try:
                category = await page.locator('button[jsaction*="category"]').first.inner_text(timeout=2000)
            except Exception:
                category = None

            # rating + reviewsCount
            try:
                rating_txt = await page.locator('div.F7nice').first.inner_text(timeout=2000)
            except Exception:
                rating_txt = None

            detail = {
                "url": page.url,
                "title_h1": h1,
                "category": category,
                "rating_block": rating_txt,
                "data_items": kv,
            }
            print(json.dumps(detail, indent=2, ensure_ascii=False))

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
