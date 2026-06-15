#!/usr/bin/env python3
"""
fetch_truflation.py

Scrapes Truflation's current US CPI YoY headline from their public marketplace
page and writes data/truflation.json for the True Inflation page on Ember.

Why this is OK to scrape: the headline number is publicly displayed on the
Truflation marketplace page with no login required (it's just JS-rendered).
Their paid product is the granular category data and historical detail. We
scrape only the headline, attribute it clearly with a prominent link back to
truflation.com, and hit them only once a day. That's closer to "quoting a
press release" than "circumventing a paywall."

Extraction strategy
-------------------
Both strategies anchor on the heading text "Truflation US CPI Inflation Index"
and look ONLY at content within ~200 chars after it. This isolates the
headline number from nearby category sub-readings (Goods, Core, Services,
PCE) or sub-window averages (7-day, 30-day) that appear elsewhere on the
page in the same DOM container.

  1. Find the heading via Playwright locator, walk ancestor containers, look
     within a narrow window after the heading position.
  2. Body-text scan with the same narrow-window-after-heading approach.

Sanity check
------------
After extraction, we compare the new value to the existing value in
data/truflation.json. If they differ by more than 0.5pp without an
intermediate reading, we refuse to overwrite — Truflation's smoothed daily
index is an aggregate of 13M+ data points and doesn't move that fast. A
large delta almost always indicates an extraction error rather than a real
move. To force an update past the sanity check, edit data/truflation.json
directly.

If the script fails to find a plausible value (or fails the sanity check),
it keeps the previous JSON intact and exits 0, so the workflow doesn't fail
loudly every time Truflation tweaks their page.

Run locally
-----------
    pip install playwright
    playwright install chromium
    python scripts/fetch_truflation.py

In GitHub Actions, Playwright + Chromium are installed by the workflow.
"""

from __future__ import annotations

import json
import re
import sys
import datetime
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "data" / "truflation.json"

PRIMARY_URL  = "https://truflation.com/marketplace/us-inflation-rate"
FALLBACK_URL = "https://truflation.com/"

# Sanity bounds for a plausible US CPI YoY reading. Anything outside this is
# almost certainly NOT the headline (could be a page version number, a stat
# from a different section, etc.) and is rejected.
MIN_VALID = -5.0
MAX_VALID = 25.0

# A normal-looking user agent. Playwright's default UA contains "HeadlessChrome"
# which some sites use to block bots.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


def looks_plausible(value: float) -> bool:
    """Whether a numeric value looks like a real US CPI YoY reading."""
    return MIN_VALID <= value <= MAX_VALID


def try_extract(page) -> Optional[float]:
    """Run extraction strategies in priority order; return the first hit.

    Both strategies anchor on the heading text and look ONLY at content
    immediately after it (within ~200 chars). This avoids picking up nearby
    category sub-readings (Goods, Core, Services), sub-window averages
    (7-day, 30-day), or other decimals that appear elsewhere on the page.
    """
    HEADING_TEXT = "Truflation US CPI Inflation Index"
    WINDOW_CHARS = 200  # chars after the heading to search for the headline number

    # Strategy 1: locate the heading via DOM, then look at text IMMEDIATELY
    # after it in each ancestor container. The page renders:
    #     "Truflation US CPI Inflation Index TruCPI-US"
    #     "1.84"           <-- our target, appears right after the heading
    #     "Index"
    # Sub-readings and category breakdowns appear further down in the same
    # container, so a narrow window after the heading isolates the headline.
    try:
        heading = page.locator(f'text=/{HEADING_TEXT}/').first
        if heading.count() > 0:
            for depth in (1, 2, 3, 4, 5):
                try:
                    container = heading.locator(f'xpath=ancestor::*[{depth}]')
                    text = container.inner_text(timeout=2000)
                except Exception:
                    continue

                heading_pos = text.find(HEADING_TEXT)
                if heading_pos == -1:
                    continue

                after_heading = text[heading_pos + len(HEADING_TEXT):]
                # Drop the secondary "TruCPI-US" label that immediately follows
                # the heading so it doesn't push the headline number out of
                # our search window.
                after_heading = after_heading.replace('TruCPI-US', '').lstrip()
                window = after_heading[:WINDOW_CHARS]

                m = re.search(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])', window)
                if m:
                    val = float(m.group(1))
                    if looks_plausible(val):
                        print(f"  ✓ Strategy 1 (heading +{WINDOW_CHARS}ch, depth={depth}): {val}")
                        return val
    except Exception as e:
        print(f"  Strategy 1 errored: {e}")

    # Strategy 2: same idea but on the full visible body text — useful if
    # the DOM-anchored strategy can't find the heading element (e.g. the page
    # renders the heading inside a shadow root or unusual structure).
    try:
        full_text = page.locator('body').inner_text(timeout=2000)
        heading_pos = full_text.find(HEADING_TEXT)
        if heading_pos != -1:
            start = heading_pos + len(HEADING_TEXT)
            after = full_text[start:start + WINDOW_CHARS + 100]
            after = after.replace('TruCPI-US', '').lstrip()
            window = after[:WINDOW_CHARS]
            matches = re.findall(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])', window)
            for raw in matches:
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if looks_plausible(val):
                    print(f"  ✓ Strategy 2 (body text +{WINDOW_CHARS}ch after heading): {val}")
                    return val
        else:
            # Last-resort fallback: heading text not found at all. Scan the
            # top of the page. Less reliable; flagged in the log.
            head = full_text[:1500]
            matches = re.findall(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])', head)
            for raw in matches:
                try:
                    val = float(raw)
                except ValueError:
                    continue
                if looks_plausible(val):
                    print(f"  ⚠ Strategy 2 fallback (heading not found, top-of-page scan): {val}")
                    return val
    except Exception as e:
        print(f"  Strategy 2 errored: {e}")

    return None


def scrape(url: str) -> Optional[float]:
    """Launch a browser, load the URL, wait for render, run extraction."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=USER_AGENT,
        )
        page = context.new_page()
        try:
            print(f"  Loading {url} ...")
            page.goto(url, timeout=45000)
            # Wait for network to settle, then give JS widgets a moment to
            # finish populating values.
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            page.wait_for_timeout(3000)
            return try_extract(page)
        except PlaywrightTimeout:
            print(f"  ⚠ Timeout loading {url}")
            return None
        except Exception as e:
            print(f"  ⚠ Error: {e}")
            return None
        finally:
            browser.close()


def main() -> int:
    print("Fetching Truflation US CPI YoY headline...\n")

    # Read the existing value first so we can sanity-check the scrape against it.
    existing = None
    existing_value: Optional[float] = None
    if OUTPUT_PATH.exists():
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
                existing_value = existing.get("truflation_cpi_yoy")
        except Exception:
            pass

    value: Optional[float] = None
    for url in (PRIMARY_URL, FALLBACK_URL):
        value = scrape(url)
        if value is not None:
            break
        print(f"  No plausible value at {url}; trying the next URL.\n")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if value is None:
        print("\n⚠ Could not extract Truflation value from any source.")
        if existing:
            print(f"  Keeping existing value ({existing_value}%) "
                  f"from {existing.get('as_of', 'unknown date')}.")
            return 0  # don't fail the workflow — existing data persists
        print("  No existing data to keep. Exiting non-zero.")
        return 1

    # Sanity check: refuse to overwrite if the new value differs from the
    # existing one by more than SANITY_THRESHOLD pp. Truflation's index is a
    # smoothed daily aggregate of 13M+ data points — it doesn't move that
    # fast. A large delta is almost always an extraction error (sub-category
    # reading, sub-window average, or page-structure change).
    SANITY_THRESHOLD = 0.5
    if existing_value is not None:
        delta = abs(value - existing_value)
        if delta > SANITY_THRESHOLD:
            print(f"\n⚠ Sanity check FAILED: new value {value}% differs from "
                  f"existing {existing_value}% by {delta:.2f}pp "
                  f"(threshold: {SANITY_THRESHOLD}pp).")
            print(f"  This is almost certainly an extraction error — Truflation's")
            print(f"  smoothed daily index does not move that fast.")
            print(f"  Keeping existing value ({existing_value}%). If Truflation has")
            print(f"  genuinely moved this much, manually edit data/truflation.json")
            print(f"  or temporarily raise SANITY_THRESHOLD in this script.")
            return 0  # exit cleanly — existing data persists

    payload = {
        "as_of": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        "truflation_cpi_yoy": round(value, 2),
        "source": "Truflation",
        "source_url": "https://truflation.com/marketplace/us-inflation-rate",
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"\n✓ Wrote {OUTPUT_PATH}")
    print(f"  Truflation US CPI YoY = {value}%")
    if existing_value is not None:
        delta = value - existing_value
        sign = "+" if delta >= 0 else ""
        print(f"  Change from previous: {sign}{delta:.2f}pp (was {existing_value}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
