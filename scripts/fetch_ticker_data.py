#!/usr/bin/env python3
"""Fetch ticker stats for the Ember portfolio simulator.

Pulls 10-year CAGR, annualized volatility, TTM dividend yield, 2022
calendar-year return, fund metadata (description, inception date,
expense ratio, average daily volume), and monthly close-price history
for each ticker in the curated list. Writes results to data/tickers.json.

If a ticker fails to fetch and a previous value exists, the previous value
is kept so transient yfinance errors don't blow away good data.

Run manually:
    pip install -r requirements.txt
    python scripts/fetch_ticker_data.py
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import yfinance as yf

# Curated list of ETFs to track. Keep in sync with the TICKERS object in index.html.
TICKERS: dict[str, dict] = {
    "VOO":   {"name": "Vanguard S&P 500",                       "lev": False},
    "VTI":   {"name": "Vanguard Total US Market",               "lev": False},
    "SPY":   {"name": "SPDR S&P 500",                           "lev": False},
    "QQQ":   {"name": "Invesco Nasdaq 100",                     "lev": False},
    "QQQM":  {"name": "Invesco Nasdaq 100 (low-cost)",          "lev": False},
    "VFIAX": {"name": "Vanguard 500 Index Admiral",             "lev": False},
    "SPYG":  {"name": "SPDR S&P 500 Growth",                    "lev": False},
    "VGT":   {"name": "Vanguard Information Technology",        "lev": False},
    "SMH":   {"name": "VanEck Semiconductor",                   "lev": False},
    "SOXX":  {"name": "iShares Semiconductor",                  "lev": False},
    "XLP":   {"name": "Consumer Staples Select Sector SPDR",    "lev": False},
    "TQQQ":  {"name": "ProShares UltraPro QQQ (3x)",            "lev": True},
    "UPRO":  {"name": "ProShares UltraPro S&P 500 (3x)",        "lev": True},
    "USD":   {"name": "ProShares Ultra Semiconductors (2x)",    "lev": True},
    "SOXL":  {"name": "Direxion Daily Semiconductor Bull (3x)", "lev": True},
    "RETL":  {"name": "Direxion Daily Retail Bull (3x)",        "lev": True},
    "NAIL":  {"name": "Direxion Daily Homebuilders Bull (3x)",  "lev": True},
    "DFEN":  {"name": "Direxion Daily Aerospace & Defense (3x)","lev": True},
    "FNGU":  {"name": "MicroSectors FANG+ (3x ETN)",            "lev": True},
    "SCHD":  {"name": "Schwab US Dividend Equity",              "lev": False},
    "VYM":   {"name": "Vanguard High Dividend Yield",           "lev": False},
    "SPYD":  {"name": "SPDR S&P 500 High Dividend",             "lev": False},
    "JEPI":  {"name": "JPMorgan Equity Premium Income",         "lev": False},
    "JEPQ":  {"name": "JPMorgan Nasdaq Equity Premium Income",  "lev": False},
    "QYLD":  {"name": "Global X Nasdaq-100 Covered Call",       "lev": False},
    "QQQI":  {"name": "NEOS Nasdaq-100 High Income",            "lev": False},
    "NVDY":  {"name": "YieldMax NVDA Option Income",            "lev": False},
    "SVOL":  {"name": "Simplify Volatility Premium",            "lev": False},
    "VNQ":   {"name": "Vanguard Real Estate (REITs)",           "lev": False},
    "VWO":   {"name": "Vanguard Emerging Markets",              "lev": False},
    "VXUS":  {"name": "Vanguard Total International",           "lev": False},
    "GLD":   {"name": "SPDR Gold Shares",                       "lev": False},
    "BND":   {"name": "Vanguard Total Bond Market",             "lev": False},
    "AGG":   {"name": "iShares Core US Aggregate Bond",         "lev": False},
    "TLT":   {"name": "iShares 20+ Year Treasury Bond",         "lev": False},
    "SCHP":  {"name": "Schwab US TIPS",                         "lev": False},
    "FLRT":  {"name": "Pacific Asset Floating Rate Income",     "lev": False},
    "MAIN":  {"name": "Main Street Capital (BDC)",              "lev": False},
    "HTGC":  {"name": "Hercules Capital (BDC)",                 "lev": False},
    "PFLT":  {"name": "PennantPark Floating Rate (BDC)",        "lev": False},
    "QPUX":  {"name": "QPUX",                                   "lev": False},
    "DRAM":  {"name": "DRAM",                                   "lev": False},
}

# Cash is a special "ticker" — not on the market, set manually.
CASH = {
    "name": "Cash / HYSA",
    "lev": False,
    "cagr": 2.0, "stddev": 0.5, "yield": 4.5, "ret22": 1.5,
    "description": "High-yield savings or money-market equivalent. Assumed steady real-dollar value with a modest yield matching prevailing short-rate benchmarks.",
    "inception": None,
    "expense_ratio": 0.0,
    "avg_volume": None,
    "prices": [],
}

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

    # Annualized volatility: stddev of daily returns × sqrt(252 trading days).
    daily_returns = hist["Close"].pct_change().dropna()
    if len(daily_returns) > 1:
        stddev = float(daily_returns.std()) * math.sqrt(252) * 100
    else:
        stddev = 0.0

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

    # Monthly close prices over the same 10-year window (for the analyzer chart).
    # Resample to month-end; drop any NaN months that resulted from gaps.
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


def fetch_metadata(symbol: str) -> dict:
    """Return fund description, inception date, expense ratio, and avg volume."""
    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
    except Exception:
        info = {}

    # Description — prefer the longer business summary, fall back to shorter fields.
    description = (info.get("longBusinessSummary")
                   or info.get("description")
                   or info.get("longName")
                   or "").strip()
    if len(description) > 400:
        # Truncate at last whole word before the limit and append ellipsis.
        description = description[:397].rsplit(" ", 1)[0] + "..."

    # Inception date — yfinance returns a Unix timestamp; convert to YYYY-MM-DD.
    inception_date = None
    inception_ts = info.get("fundInceptionDate")
    if inception_ts:
        try:
            inception_date = datetime.fromtimestamp(int(inception_ts), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass

    # Expense ratio — yfinance returns as decimal (0.0003 = 0.03%); convert to percent.
    expense_ratio = info.get("annualReportExpenseRatio")
    if expense_ratio is not None:
        try:
            expense_ratio = round(float(expense_ratio) * 100, 3)
        except Exception:
            expense_ratio = None

    # Average daily volume — try the 3-month average first for stability.
    avg_volume = (info.get("averageDailyVolume3Month")
                  or info.get("averageVolume")
                  or info.get("averageDailyVolume10Day"))
    if avg_volume is not None:
        try:
            avg_volume = int(avg_volume)
        except Exception:
            avg_volume = None

    return {
        "description":   description,
        "inception":     inception_date,
        "expense_ratio": expense_ratio,
        "avg_volume":    avg_volume,
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
            try:
                metadata = fetch_metadata(symbol)
            except Exception as me:
                # Metadata failure shouldn't kill the stats fetch.
                metadata = {"description": "", "inception": None, "expense_ratio": None, "avg_volume": None}
                print(f"  {symbol:5s}  metadata fetch failed ({me})")
            result[symbol] = {**meta, **stats, **metadata}
            print(f"  {symbol:5s}  CAGR={stats['cagr']:6.2f}%  σ={stats['stddev']:5.2f}%  yield={stats['yield']:5.2f}%  2022={stats['ret22']}")
        except Exception as e:
            failures.append(symbol)
            if symbol in existing:
                result[symbol] = existing[symbol]
                print(f"  {symbol:5s}  fetch failed — kept previous data ({e})")
            else:
                result[symbol] = {
                    **meta,
                    "cagr": None, "stddev": None, "yield": None, "ret22": None,
                    "description": "", "inception": None, "expense_ratio": None, "avg_volume": None,
                    "prices": [],
                }
                print(f"  {symbol:5s}  fetch failed and no fallback ({e})")

    # Cash entry is set manually.
    result["CASH"] = dict(CASH)

    payload = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
