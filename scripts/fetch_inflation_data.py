"""
fetch_inflation_data.py

Computes the Ember True Inflation Index from FRED data and writes
data/inflation.json for the True Inflation page on Ember.

Methodology
-----------
- Weights are BLS Consumer Expenditure Survey (CEX) derived — same source
  Truflation uses. Weights represent the share of typical household spending
  going to each category.
- Data sources are the freshest free options available:
    * Shelter:   S&P/Case-Shiller home prices (FRED) instead of BLS's lagging
                 Owners' Equivalent Rent (OER) — captures real housing prices.
    * Energy:    EIA weekly retail gasoline (when EIA_API_KEY is set) instead
                 of BLS's monthly Energy CPI — captures pump-price moves in
                 near-real-time. Falls back to BLS CPIENGSL if EIA key missing.
    * All other: BLS CPI subindexes via FRED (slow-moving categories where
                 BLS's monthly data is acceptable).

Run locally
-----------
    $env:FRED_API_KEY="your_fred_key_here"        # PowerShell
    $env:EIA_API_KEY="your_eia_key_here"          # PowerShell (optional)
    python scripts/fetch_inflation_data.py

In GitHub Actions, both keys are injected from repository secrets.

API key signup:
    FRED: https://fred.stlouisfed.org/docs/api/api_key.html
    EIA:  https://www.eia.gov/opendata/register.php
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

# EIA key is optional — if missing, the Energy component falls back to BLS CPIENGSL.
EIA_API_KEY = os.environ.get("EIA_API_KEY")

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
EIA_BASE  = "https://api.eia.gov/v2/petroleum/pri/gnd/data/"

# EIA series ID for "Weekly U.S. Regular All Formulations Retail Gasoline Prices" ($/gallon)
EIA_GAS_SERIES = "EMM_EPMR_PTE_NUS_DPG"

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
        "series": EIA_GAS_SERIES if EIA_API_KEY else "CPIENGSL",
        "source_type": "eia_petroleum" if EIA_API_KEY else "fred",
        "weight": 0.07,
        "source": (
            "EIA Weekly U.S. Retail Gasoline Prices (regular, all formulations)"
            if EIA_API_KEY else
            "BLS CPI: Energy (FRED: CPIENGSL)"
        ),
        "note": (
            "Real-time weekly retail gasoline prices replace BLS's monthly Energy CPI. "
            "Gasoline is the dominant driver of energy price volatility, so it serves "
            "as a leading proxy for the broader energy basket while electricity and "
            "natural gas move slowly."
            if EIA_API_KEY else
            "Gasoline, electricity, natural gas, heating oil. Set EIA_API_KEY env var "
            "to swap in real-time weekly gas prices for a more responsive read."
        ),
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


def fetch_eia_gas(start_date="2010-01-01"):
    """Fetch EIA weekly U.S. retail gasoline prices ($/gallon, regular all formulations).

    Uses the EIA v2 API petroleum/pri/gnd endpoint. The series ID
    EMM_EPMR_PTE_NUS_DPG is the national weekly average regular gasoline retail
    price published every Monday afternoon (Tuesday after holidays).
    Returns the same {date, value} shape as fetch_series for drop-in use.
    """
    if not EIA_API_KEY:
        raise RuntimeError("EIA_API_KEY not set — cannot fetch EIA series")

    params = [
        ("api_key", EIA_API_KEY),
        ("frequency", "weekly"),
        ("data[0]", "value"),
        ("facets[series][]", EIA_GAS_SERIES),
        ("start", start_date),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", "5000"),
    ]
    url = EIA_BASE + "?" + urllib.parse.urlencode(params, safe="[]")

    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())

    rows = data.get("response", {}).get("data", [])
    return [
        {"date": r["period"], "value": float(r["value"])}
        for r in rows
        if r.get("value") not in (None, "", ".")
    ]


def fetch_component(comp, start_date="2010-01-01"):
    """Dispatch fetching based on component's source_type."""
    source_type = comp.get("source_type", "fred")
    if source_type == "eia_petroleum":
        return fetch_eia_gas(start_date)
    return fetch_series(comp["series"], start_date)


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
    print(f"Fetching {len(COMPONENTS) + 1} data series...")
    if EIA_API_KEY:
        print("  Energy component: using EIA real-time gas prices ✓")
    else:
        print("  Energy component: using BLS CPIENGSL (set EIA_API_KEY for real-time gas)")

    # BLS headline CPI (for comparison)
    print(f"  {BLS_CPI_SERIES:30s}  BLS headline CPI")
    bls_observations = fetch_series(BLS_CPI_SERIES)
    bls_date, bls_yoy, _ = yoy_at(bls_observations, datetime.date.today())

    # Each component
    component_results = []
    all_series = []
    for comp in COMPONENTS:
        print(f"  {comp['series']:30s}  {comp['name']}")
        observations = fetch_component(comp)
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
                "Two methodological choices drive Ember's divergence from BLS CPI: "
                "(1) we use S&P/Case-Shiller home prices for the shelter component "
                "instead of BLS's Owners' Equivalent Rent (OER), which lags real "
                "housing prices by 12-18 months; "
                + (
                    "(2) we use EIA's weekly retail gasoline prices for the energy "
                    "component instead of BLS's monthly Energy CPI, which captures "
                    "pump-price moves about 30 days late."
                    if EIA_API_KEY else
                    "(2) energy uses BLS Energy CPI for now — set EIA_API_KEY in the "
                    "pipeline to swap in real-time weekly gas prices."
                )
            ),
            "weights_source": "BLS Consumer Expenditure Survey (CEX), 2024 weights",
            "data_sources": (
                "Federal Reserve Economic Data (FRED) + EIA Petroleum Data API"
                if EIA_API_KEY else
                "Federal Reserve Economic Data (FRED), St. Louis Fed"
            ),
            "update_frequency": (
                "Daily. Component data updates monthly (FRED) and weekly (EIA gas); "
                "we recompute daily to pick up any revisions."
                if EIA_API_KEY else
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
