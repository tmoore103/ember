"""
fetch_inflation_data.py

Computes the Ember True Inflation Index from FRED data and writes
data/inflation.json for the True Inflation page on Ember.

Methodology
-----------
- Weights are BLS Consumer Expenditure Survey (CEX) derived — same source
  Truflation uses. Weights represent the share of typical household spending
  going to each category.
- Data sources are the freshest free options available via FRED. The key
  methodological choice: we use S&P/Case-Shiller home prices for the shelter
  component instead of BLS's Owners' Equivalent Rent (OER), which historically
  lags real housing market movements by 12-18 months. This is what drives
  Ember's number to diverge from BLS CPI in the direction of "what people
  actually feel."
- All other components are pulled from BLS CPI subindexes via FRED.

Run locally
-----------
    $env:FRED_API_KEY="your_key_here"        # PowerShell
    python scripts/fetch_inflation_data.py

In GitHub Actions, FRED_API_KEY is injected from repository secrets.
"""

import os
import json
import datetime
from pathlib import Path
import urllib.request
import urllib.parse

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "data" / "inflation.json"

API_KEY = os.environ.get("FRED_API_KEY")
if not API_KEY:
    raise SystemExit(
        "FRED_API_KEY env var not set. Get a free key from "
        "https://fred.stlouisfed.org/docs/api/api_key.html"
    )

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"

# Component definitions: series ID, BLS-CEX-derived weight, display name, source label.
# Weights sum to 1.00 and approximate BLS's 2024 consumer expenditure shares.
COMPONENTS = [
    {
        "name": "Shelter",
        "series": "CSUSHPISA",
        "weight": 0.33,
        "source": "S&P/Case-Shiller U.S. National Home Price Index",
        "note": "Uses real home prices instead of BLS's lagging Owners' Equivalent Rent.",
    },
    {
        "name": "Food",
        "series": "CPIUFDSL",
        "weight": 0.14,
        "source": "BLS CPI: Food (FRED: CPIUFDSL)",
        "note": "All food (at home and away from home), urban consumers.",
    },
    {
        "name": "Energy",
        "series": "CPIENGSL",
        "weight": 0.07,
        "source": "BLS CPI: Energy (FRED: CPIENGSL)",
        "note": "Gasoline, electricity, natural gas, heating oil.",
    },
    {
        "name": "Transportation",
        "series": "CPITRNSL",
        "weight": 0.16,
        "source": "BLS CPI: Transportation (FRED: CPITRNSL)",
        "note": "Vehicles, fuel, maintenance, public transit, airfare.",
    },
    {
        "name": "Medical care",
        "series": "CPIMEDSL",
        "weight": 0.08,
        "source": "BLS CPI: Medical Care (FRED: CPIMEDSL)",
        "note": "Insurance premiums, hospital services, prescription drugs.",
    },
    {
        "name": "All other (core ex-food, energy)",
        "series": "CPILFESL",
        "weight": 0.22,
        "source": "BLS Core CPI (FRED: CPILFESL)",
        "note": "Apparel, recreation, education, communication, services.",
    },
]

# BLS headline CPI series for side-by-side comparison
BLS_CPI_SERIES = "CPIAUCSL"

# Sanity check on weights
_total_weight = sum(c["weight"] for c in COMPONENTS)
assert abs(_total_weight - 1.0) < 0.01, f"Weights sum to {_total_weight}, expected 1.00"


# ─────────────────────────────────────────────────────────────────────────────
# FRED API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_series(series_id, start_date="2010-01-01"):
    """Fetch a FRED series. Returns chronologically sorted list of {date, value}."""
    params = {
        "series_id": series_id,
        "api_key": API_KEY,
        "file_type": "json",
        "sort_order": "asc",
        "observation_start": start_date,
    }
    url = FRED_BASE + "?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    # FRED uses "." for missing values; skip those.
    return [
        {"date": obs["date"], "value": float(obs["value"])}
        for obs in data.get("observations", [])
        if obs["value"] != "."
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Computation
# ─────────────────────────────────────────────────────────────────────────────

def _parse(date_str):
    return datetime.date.fromisoformat(date_str)


def yoy_at(observations, as_of_date):
    """Compute YoY % change in a series as of a given date.
    Returns (latest_obs_date, yoy_pct, prior_obs_date) or (None, None, None)
    if insufficient data."""
    candidates = [o for o in observations if _parse(o["date"]) <= as_of_date]
    if len(candidates) < 2:
        return None, None, None

    latest = candidates[-1]
    latest_date = _parse(latest["date"])
    target_prior_date = latest_date.replace(year=latest_date.year - 1)

    # Find the observation closest to 12 months before the latest one.
    prior = min(
        observations,
        key=lambda o: abs((_parse(o["date"]) - target_prior_date).days),
    )
    # Need at least ~11 months of separation to be a real YoY comparison.
    if abs((_parse(prior["date"]) - target_prior_date).days) > 45:
        return None, None, None

    yoy_pct = (latest["value"] - prior["value"]) / prior["value"] * 100
    return latest["date"], round(yoy_pct, 2), prior["date"]


def build_history(all_series, bls_series, months=60):
    """Build a historical YoY series of (date, ember, bls) for charting.

    For each month going back `months`, computes what the Ember Index and
    BLS CPI YoY readings would have been *as of that month*, using only
    data available up to that point.
    """
    history = []
    bls_dates = [_parse(o["date"]) for o in bls_series]

    # Walk backwards from the most recent month.
    for i in range(min(months, len(bls_dates) - 12)):
        idx = len(bls_dates) - 1 - i
        as_of = bls_dates[idx]

        # BLS YoY at this point
        _, bls_yoy, _ = yoy_at(bls_series, as_of)
        if bls_yoy is None:
            continue

        # Ember weighted sum at this point
        ember_yoy = 0.0
        valid = True
        for comp_def, observations in all_series:
            _, comp_yoy, _ = yoy_at(observations, as_of)
            if comp_yoy is None:
                valid = False
                break
            ember_yoy += comp_yoy * comp_def["weight"]

        if valid:
            history.append({
                "date": as_of.isoformat()[:7],  # YYYY-MM
                "ember": round(ember_yoy, 2),
                "bls": round(bls_yoy, 2),
            })

    history.reverse()  # chronological order
    return history


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print(f"Fetching {len(COMPONENTS) + 1} FRED series...")

    # BLS headline CPI (for comparison)
    print(f"  {BLS_CPI_SERIES:12s}  BLS headline CPI")
    bls_observations = fetch_series(BLS_CPI_SERIES)
    bls_date, bls_yoy, _ = yoy_at(bls_observations, datetime.date.today())

    # Each component
    component_results = []
    all_series = []
    for comp in COMPONENTS:
        print(f"  {comp['series']:12s}  {comp['name']}")
        observations = fetch_series(comp["series"])
        date, yoy, _ = yoy_at(observations, datetime.date.today())
        contribution = round(yoy * comp["weight"], 2) if yoy is not None else None
        component_results.append({
            "name": comp["name"],
            "weight": comp["weight"],
            "yoy": yoy,
            "contribution": contribution,
            "source": comp["source"],
            "note": comp["note"],
            "as_of": date,
        })
        all_series.append((comp, observations))

    # Ember True Inflation = weighted sum of component YoYs
    true_inflation = round(
        sum(c["contribution"] for c in component_results if c["contribution"] is not None),
        2,
    )

    print("\nBuilding 5-year history for chart...")
    history = build_history(all_series, bls_observations, months=60)

    output = {
        "as_of": datetime.date.today().isoformat(),
        "data_through": max(c["as_of"] for c in component_results if c["as_of"]),
        "true_inflation_yoy": true_inflation,
        "bls_cpi_yoy": bls_yoy,
        "bls_cpi_as_of": bls_date,
        "gap": round(true_inflation - bls_yoy, 2),
        "components": component_results,
        "history": history,
        "methodology": {
            "summary": (
                "Ember True Inflation is a weighted average of year-over-year price "
                "changes across six consumer spending categories. Weights are BLS "
                "Consumer Expenditure Survey derived (the same source Truflation uses). "
                "The key methodological choice: we use S&P/Case-Shiller home prices "
                "for the shelter component instead of BLS's Owners' Equivalent Rent (OER), "
                "which historically lags real housing market movements by 12-18 months. "
                "This is what makes Ember's number diverge from BLS CPI in the direction "
                "of what people actually experience."
            ),
            "weights_source": "BLS Consumer Expenditure Survey (CEX), 2024 weights",
            "data_sources": "Federal Reserve Economic Data (FRED), St. Louis Fed",
            "update_frequency": (
                "Daily. Component data updates monthly; we recompute daily to pick up "
                "any FRED revisions."
            ),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    # Pretty print results
    print(f"\nWritten to {OUTPUT_PATH}\n")
    print(f"  Ember True Inflation: {true_inflation:+.2f}% YoY")
    print(f"  BLS Headline CPI:     {bls_yoy:+.2f}% YoY")
    print(f"  Gap:                  {true_inflation - bls_yoy:+.2f} pp\n")
    print("  Component breakdown:")
    print(f"  {'Component':35s} {'YoY':>8s}   {'Weight':>6s}   {'Contribution':>13s}")
    print(f"  {'-' * 35} {'-' * 8}   {'-' * 6}   {'-' * 13}")
    for c in component_results:
        yoy_str = f"{c['yoy']:+.2f}%" if c['yoy'] is not None else "n/a"
        contrib_str = f"{c['contribution']:+.2f} pp" if c['contribution'] is not None else "n/a"
        print(f"  {c['name']:35s} {yoy_str:>8s}   {c['weight']:>6.2f}   {contrib_str:>13s}")


if __name__ == "__main__":
    main()
