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

Multi-strategy extraction makes the script resilient to small DOM changes:
  1. Find the index-name heading, look in its container for a nearby decimal.
  2. Fall back to text-pattern scanning across the visible page text.

If the script fails to find a plausible value, it keeps the previous JSON
intact and exits 0 (so the workflow doesn't fail loudly every time
Truflation tweaks their page).

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
    """Run extraction strategies in priority order; return the first hit."""

    # Strategy 1: locate the index-name heading, then look at its parent
    # container text for a decimal value. The page renders:
    #     "Truflation US CPI Inflation Index TruCPI-US"
    #     "1.84"           <-- our target, was "0" pre-render
    #     "Index"
    try:
        heading = page.locator('text=/Truflation US CPI Inflation Index/').first
        if heading.count() > 0:
            # Walk up a few ancestors to find a container that has the number too.
            for depth in (1, 2, 3, 4):
                try:
                    container = heading.locator(f'xpath=ancestor::*[{depth}]')
                    text = container.inner_text(timeout=2000)
                except Exception:
                    continue
                # Strip the heading labels so they don't trick the regex.
                cleaned = text.replace('Truflation US CPI Inflation Index', '')
                cleaned = cleaned.replace('TruCPI-US', '').strip()
                m = re.search(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])', cleaned)
                if m:
                    val = float(m.group(1))
                    if looks_plausible(val):
                        print(f"  ✓ Strategy 1 (heading container, depth={depth}): {val}")
                        return val
    except Exception as e:
        print(f"  Strategy 1 errored: {e}")

    # Strategy 2: scan the first chunk of visible page text for the first
    # decimal value in the plausible range. The headline always appears near
    # the top of the page.
    try:
        full_text = page.locator('body').inner_text(timeout=2000)
        head = full_text[:1500]  # focus on the top of the page
        matches = re.findall(r'(?<![\d.])(-?\d+\.\d+)(?![\d.])', head)
        for raw in matches:
            try:
                val = float(raw)
            except ValueError:
                continue
            if looks_plausible(val):
                print(f"  ✓ Strategy 2 (text scan, top of page): {val}")
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

    value: Optional[float] = None
    for url in (PRIMARY_URL, FALLBACK_URL):
        value = scrape(url)
        if value is not None:
            break
        print(f"  No plausible value at {url}; trying the next URL.\n")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = None
    if OUTPUT_PATH.exists():
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f)
        except Exception:
            pass

    if value is None:
        print("\n⚠ Could not extract Truflation value from any source.")
        if existing:
            print(f"  Keeping existing value ({existing.get('truflation_cpi_yoy')}%) "
                  f"from {existing.get('as_of', 'unknown date')}.")
            return 0  # don't fail the workflow — existing data persists
        print("  No existing data to keep. Exiting non-zero.")
        return 1

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
