#!/usr/bin/env python3
"""Fetch ticker stats for the Ember portfolio simulator.

Pulls 10-year CAGR, TTM dividend yield, and 2022 calendar-year return
for each ticker in the curated list, writes results to data/tickers.json.

If a ticker fails to fetch and a previous value exists, the previous value
is kept so transient yfinance errors don't blow away good data.

Run manually:
    pip install -r requirements.txt
    python scripts/fetch_ticker_data.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

import yfinance as yf

# Curated list of ETFs to track. Keep in sync with the TICKERS object in index.html.
TICKERS: dict[str, dict] = {
    "VOO":  {"name": "Vanguard S&P 500",             "lev": False},
    "VTI":  {"name": "Vanguard Total US Market",     "lev": False},
    "SPY":  {"name": "SPDR S&P 500",                 "lev": False},
    "QQQ":  {"name": "Invesco Nasdaq 100",           "lev": False},
    "TQQQ": {"name": "ProShares UltraPro QQQ",       "lev": True},
    "UPRO": {"name": "ProShares UltraPro S&P 500",   "lev": True},
    "SCHD": {"name": "Schwab US Dividend Equity",    "lev": False},
    "VXUS": {"name": "Vanguard Total International", "lev": False},
    "BND":  {"name": "Vanguard Total Bond Market",   "lev": False},
}

# Cash is a special "ticker" — not on the market, set manually.
CASH = {"name": "Cash / HYSA", "lev": False, "cagr": 2.0, "yield": 4.5, "ret22": 1.5}

OUTPUT_PATH = "data/tickers.json"


def fetch_stats(symbol: str) -> dict:
    """Return cagr, yield (TTM), and 2022 calendar-year return for a ticker."""
    t = yf.Ticker(symbol)

    # 10-year history with dividend reinvestment (auto_adjust=True) for CAGR.
    hist = t.history(period="10y", auto_adjust=True)
    if len(hist) < 2:
        raise RuntimeError("no price history returned")

    start_price = float(hist["Close"].iloc[0])
    end_price = float(hist["Close"].iloc[-1])
    years = (hist.index[-1] - hist.index[0]).days / 365.25
    cagr = ((end_price / start_price) ** (1 / years) - 1) * 100

    # TTM dividend yield from actual distributions in the past year.
    yield_pct = 0.0
    try:
        divs = t.dividends
        if len(divs) > 0:
            cutoff = hist.index[-1] - timedelta(days=365)
            # Normalize timezones so the comparison works
            if divs.index.tz is not None and cutoff.tzinfo is None:
                cutoff = cutoff.tz_localize(divs.index.tz)
            elif divs.index.tz is None and cutoff.tzinfo is not None:
                cutoff = cutoff.tz_localize(None)
            ttm = float(divs[divs.index >= cutoff].sum())
            yield_pct = (ttm / end_price) * 100
    except Exception:
        pass

    # 2022 calendar-year total return.
    ret22 = None
    try:
        hist_22 = t.history(start="2022-01-01", end="2023-01-01", auto_adjust=True)
        if len(hist_22) >= 2:
            ret22 = (float(hist_22["Close"].iloc[-1]) / float(hist_22["Close"].iloc[0]) - 1) * 100
    except Exception:
        pass

    return {
        "cagr":  round(cagr, 2),
        "yield": round(yield_pct, 2),
        "ret22": round(ret22, 2) if ret22 is not None else None,
    }


def main() -> int:
    os.makedirs("data", exist_ok=True)

    # Load existing data as fallback for failed fetches.
    existing: dict = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH) as f:
                existing = json.load(f).get("tickers", {})
        except Exception:
            pass

    result: dict = {}
    failures: list[str] = []

    for symbol, meta in TICKERS.items():
        try:
            stats = fetch_stats(symbol)
            result[symbol] = {**meta, **stats}
            print(f"  {symbol:5s}  CAGR={stats['cagr']:6.2f}%  yield={stats['yield']:5.2f}%  2022={stats['ret22']}")
        except Exception as e:
            failures.append(symbol)
            if symbol in existing:
                result[symbol] = existing[symbol]
                print(f"  {symbol:5s}  fetch failed — kept previous data ({e})")
            else:
                result[symbol] = {**meta, "cagr": None, "yield": None, "ret22": None}
                print(f"  {symbol:5s}  fetch failed and no fallback ({e})")

    # Cash entry is set manually.
    result["CASH"] = dict(CASH)

    payload = {
        "updated": datetime.utcnow().isoformat() + "Z",
        "tickers": result,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    print(f"\nWrote {len(result)} tickers to {OUTPUT_PATH}")
    if failures:
        print(f"Failures: {', '.join(failures)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
