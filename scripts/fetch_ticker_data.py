#!/usr/bin/env python3
"""Discover, filter, rank, and persist ticker data for the Ember portfolio simulator.

Pipeline:
  1. Load candidate tickers from data/universe.json (categorized).
  2. For each candidate, fetch 10-year price history, stats, and fund metadata.
  3. Apply filters:
       - History: >= 10 years of trading data
       - Average daily volume: >= 100,000 shares
       - Net assets / AUM:    >= $50,000,000
  4. Rank surviving candidates within each category by 10-year CAGR.
  5. Take the top N per category (where N is the category's target_count).
  6. Write the winners to data/tickers.json, tagged with category info.

Tickers that fail filters or fetches are skipped. Categories with fewer
qualifying candidates than the target simply get fewer entries.

Run manually:
    pip install -r requirements.txt
    python scripts/fetch_ticker_data.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf

UNIVERSE_PATH = "data/universe.json"
OUTPUT_PATH   = "data/tickers.json"

# Filter thresholds — funds must clear all three to be considered.
MIN_HISTORY_DAYS = int(252 * 9.5)   # ~9.5 trading years (so a fund just shy of 10y still qualifies)
MIN_AVG_VOLUME   = 100_000          # 100K shares/day
MIN_NET_ASSETS   = 50_000_000       # $50M

# Cash is always included as a special "ticker" — not from the market.
CASH = {
    "name": "Cash / HYSA",
    "lev": False,
    "cagr": 2.0, "stddev": 0.5, "yield": 4.5, "ret22": 1.5,
    "description": "High-yield savings or money-market equivalent. Assumed steady real-dollar value with a modest yield matching prevailing short-rate benchmarks.",
    "inception": None,
    "expense_ratio": 0.0,
    "avg_volume": None,
    "prices": [],
    "category": "cash-equivalent",
    "category_label": "Cash equivalents",
}

# Heuristic patterns used to detect leveraged funds when the category alone doesn't tell us.
LEV_NAME_PATTERN = re.compile(
    r"\b(2x|3x|ultrapro|ultra|daily\s+\w+\s+bull|daily\s+\w+\s+bear|3X|leveraged)\b",
    re.IGNORECASE,
)


def load_universe() -> dict:
    """Read the universe.json file with categorized candidate tickers."""
    if not os.path.exists(UNIVERSE_PATH):
        raise FileNotFoundError(f"Missing {UNIVERSE_PATH} — Phase 3 requires the curated universe file.")
    with open(UNIVERSE_PATH) as f:
        return json.load(f)


def fetch_stats_and_prices(t: yf.Ticker) -> dict | None:
    """Compute CAGR, volatility, TTM yield, 2022 return, and monthly prices.

    Returns None if the ticker fails the history-length filter.
    """
    hist = t.history(period="10y", auto_adjust=True)
    if len(hist) < MIN_HISTORY_DAYS:
        return None

    start_price = float(hist["Close"].iloc[0])
    end_price   = float(hist["Close"].iloc[-1])
    years       = (hist.index[-1] - hist.index[0]).days / 365.25
    cagr        = ((end_price / start_price) ** (1 / years) - 1) * 100

    daily_returns = hist["Close"].pct_change().dropna()
    stddev = float(daily_returns.std()) * math.sqrt(252) * 100 if len(daily_returns) > 1 else 0.0

    # TTM dividend yield
    yield_pct = 0.0
    try:
        divs = t.dividends
        if len(divs) > 0:
            cutoff = hist.index[-1] - timedelta(days=365)
            if divs.index.tz is not None and cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize(divs.index.tz)
            elif divs.index.tz is None and cutoff.tzinfo is not None:
                cutoff = cutoff.tz_localize(None)
            ttm = float(divs[divs.index >= cutoff].sum())
            yield_pct = (ttm / end_price) * 100
    except Exception:
        pass

    # 2022 calendar-year return
    ret22 = None
    try:
        hist_22 = t.history(start="2022-01-01", end="2023-01-01", auto_adjust=True)
        if len(hist_22) >= 2:
            ret22 = (float(hist_22["Close"].iloc[-1]) / float(hist_22["Close"].iloc[0]) - 1) * 100
    except Exception:
        pass

    # Monthly close prices for the analyzer chart
    monthly = hist["Close"].resample("ME").last().dropna()
    monthly_prices = [
        [d.strftime("%Y-%m-%d"), round(float(p), 2)]
        for d, p in monthly.items()
    ]

    return {
        "cagr":   round(cagr, 2),
        "stddev": round(stddev, 2),
        "yield":  round(yield_pct, 2),
        "ret22":  round(ret22, 2) if ret22 is not None else None,
        "prices": monthly_prices,
    }


def fetch_metadata(t: yf.Ticker) -> dict:
    """Pull description, inception, expense ratio, average volume, and net assets."""
    try:
        info = t.info or {}
    except Exception:
        info = {}

    description = (info.get("longBusinessSummary")
                   or info.get("description")
                   or info.get("longName")
                   or "").strip()
    if len(description) > 400:
        description = description[:397].rsplit(" ", 1)[0] + "..."

    inception_date = None
    inception_ts = info.get("fundInceptionDate")
    if inception_ts:
        try:
            inception_date = datetime.fromtimestamp(int(inception_ts), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass

    expense_ratio = info.get("annualReportExpenseRatio")
    if expense_ratio is not None:
        try:
            expense_ratio = round(float(expense_ratio) * 100, 3)
        except Exception:
            expense_ratio = None

    avg_volume = (info.get("averageDailyVolume3Month")
                  or info.get("averageVolume")
                  or info.get("averageDailyVolume10Day"))
    if avg_volume is not None:
        try:
            avg_volume = int(avg_volume)
        except Exception:
            avg_volume = None

    # Net assets / total AUM — yfinance returns dollars
    net_assets = info.get("totalAssets") or info.get("netAssets")
    if net_assets is not None:
        try:
            net_assets = int(net_assets)
        except Exception:
            net_assets = None

    name = info.get("longName") or info.get("shortName") or ""

    return {
        "name":          name,
        "description":   description,
        "inception":     inception_date,
        "expense_ratio": expense_ratio,
        "avg_volume":    avg_volume,
        "net_assets":    net_assets,
    }


def detect_leverage(category_id: str, name: str) -> bool:
    """A fund is leveraged if its category says so, or its name signals it."""
    if category_id in ("3x-leveraged", "2x-leveraged"):
        return True
    return bool(LEV_NAME_PATTERN.search(name or ""))


def fetch_candidate(symbol: str, category_id: str, category_label: str) -> dict | None:
    """Fetch stats + metadata for a single candidate. Returns None on filter failure."""
    try:
        t = yf.Ticker(symbol)
        stats = fetch_stats_and_prices(t)
        if stats is None:
            return {"symbol": symbol, "reason": "insufficient history (<10y)"}

        metadata = fetch_metadata(t)

        # AUM filter
        if not metadata["net_assets"] or metadata["net_assets"] < MIN_NET_ASSETS:
            assets_str = f"${metadata['net_assets']:,}" if metadata["net_assets"] else "unknown"
            return {"symbol": symbol, "reason": f"AUM {assets_str} < ${MIN_NET_ASSETS:,}"}

        # Volume filter
        if not metadata["avg_volume"] or metadata["avg_volume"] < MIN_AVG_VOLUME:
            vol_str = f"{metadata['avg_volume']:,}" if metadata["avg_volume"] else "unknown"
            return {"symbol": symbol, "reason": f"volume {vol_str} < {MIN_AVG_VOLUME:,}"}

        # Passed all filters — assemble the full record.
        name = metadata.pop("name") or symbol
        metadata.pop("net_assets")  # already used for filtering; not needed in final tickers.json

        return {
            "symbol": symbol,
            "passed": True,
            "data": {
                "name":           name,
                "lev":            detect_leverage(category_id, name),
                "category":       category_id,
                "category_label": category_label,
                **stats,
                **metadata,
            },
        }
    except Exception as e:
        return {"symbol": symbol, "reason": f"fetch error: {e}"}


def main() -> int:
    os.makedirs("data", exist_ok=True)
    universe = load_universe()

    final_tickers: dict = {}
    summary: list[str] = []

    for cat_id, cat_info in universe["categories"].items():
        label  = cat_info["label"]
        target = cat_info["target_count"]
        candidates = cat_info["candidates"]

        print(f"\n{label} (target {target} of {len(candidates)} candidates)")
        print("-" * 64)

        qualified: list = []
        for symbol in candidates:
            result = fetch_candidate(symbol, cat_id, label)
            if result and result.get("passed"):
                d = result["data"]
                qualified.append((symbol, d))
                print(f"  ✓ {symbol:6s}  CAGR={d['cagr']:6.2f}%  vol={d['avg_volume']:>12,}")
            else:
                reason = (result or {}).get("reason", "unknown")
                print(f"  ✗ {symbol:6s}  {reason}")

        # Rank by 10-year CAGR (descending). Treat None CAGR as worst.
        qualified.sort(key=lambda kv: kv[1]["cagr"] if kv[1]["cagr"] is not None else -999, reverse=True)
        winners = qualified[:target]

        for sym, data in winners:
            final_tickers[sym] = data

        summary.append(f"  {label:30s}  {len(winners):2d} of {target} target  ({len(qualified)} qualified)")

    # Always include CASH as a known special case.
    final_tickers["CASH"] = dict(CASH)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tickers": final_tickers,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"\n{'='*64}")
    print(f"Wrote {len(final_tickers)} tickers to {OUTPUT_PATH}")
    print(f"{'='*64}")
    for line in summary:
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
