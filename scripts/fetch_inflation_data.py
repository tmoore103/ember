"""
fetch_inflation_data.py

Computes the Ember True Inflation Index from a mix of real-time data sources
and writes data/inflation.json for the True Inflation page on Ember.

Methodology
-----------
- Weights are BLS Consumer Expenditure Survey (CEX) derived — same source
  Truflation uses. Weights represent the share of typical household spending
  going to each category.
- Data sources are the freshest free options available:
    * Shelter:   Zillow Observed Rent Index (ZORI), national smoothed
                 seasonally adjusted. Falls back to S&P/Case-Shiller home
                 prices (FRED) if Zillow CSV is unavailable. Rents are what
                 consumers actually pay monthly; home prices are an asset.
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
    FRED:   https://fred.stlouisfed.org/docs/api/api_key.html
    EIA:    https://www.eia.gov/opendata/register.php
    Zillow: No key required — free public CSV downloads.
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

# Zillow ZORI download URLs (no API key required — these are free public CSVs).
# Try the smoothed+seasonally-adjusted version first; fall back to smoothed-only.
# These paths occasionally change — Zillow warns users of that on their data page.
ZILLOW_ZORI_URLS = [
    "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_sa_month.csv",
    "https://files.zillowstatic.com/research/public_csvs/zori/Metro_zori_uc_sfrcondomfr_sm_month.csv",
]

# Component definitions: series ID, BLS-CEX-derived weight, display name, source label.
# Weights sum to 1.00 and approximate BLS's 2024 consumer expenditure shares.
COMPONENTS = [
    {
        "name": "Shelter",
        "series": "ZORI_US",
        "source_type": "zillow_zori",
        "weight": 0.23,
        "source": "Zillow Observed Rent Index (ZORI), national, smoothed seasonally adjusted",
        "note": "Real market-rate rents tracked from millions of Zillow rental listings, "
                "replacing BLS's Owners' Equivalent Rent which lags by 6-12 months. "
                "Weight matches Truflation's published 23.2% housing weight (BLS-CEX uses 33% — "
                "we believe Truflation's weight better reflects current consumer expenditure).",
    },
    {
        "name": "Food",
        "series": "CPIUFDSL",
        "weight": 0.15,
        "source": "BLS CPI: Food (FRED: CPIUFDSL)",
        "note": "All food (at home and away from home), urban consumers. "
                "Weight matches Truflation's 15.3%.",
    },
    {
        "name": "Utilities",
        "series": "CUSR0000SEHF01",
        "weight": 0.05,
        "source": "BLS CPI: Electricity (FRED: CUSR0000SEHF01)",
        "note": "Electricity costs — used as proxy for household utility costs (NOT motor fuel). "
                "Electricity is the largest single household utility expense. Motor fuel is "
                "captured inside the Transportation component instead, matching Truflation's "
                "basket structure and avoiding the double-count that occurs when motor fuel "
                "is also a separate component.",
    },
    {
        "name": "Transportation",
        "series": "CPITRNSL",
        "weight": 0.19,
        "source": "BLS CPI: Transportation (FRED: CPITRNSL) — includes motor fuel",
        "note": "Vehicles, motor fuel, maintenance, insurance, public transit, airfare. "
                "BLS Transportation already includes motor fuel, so this component naturally "
                "carries our fuel exposure. Weight matches Truflation's published 19.8%.",
    },
    {
        "name": "Medical care",
        "series": "CPIMEDSL",
        "weight": 0.08,
        "source": "BLS CPI: Medical Care (FRED: CPIMEDSL)",
        "note": "Insurance premiums, hospital services, prescription drugs. "
                "Weight matches Truflation's 8.5%.",
    },
    {
        "name": "Apparel",
        "series": "CPIAPPSL",
        "weight": 0.03,
        "source": "BLS CPI: Apparel (FRED: CPIAPPSL)",
        "note": "Clothing and footwear. Carved out from the broad 'core' category — "
                "apparel has been deflating since 2023, which gets washed out in BLS Core CPI.",
    },
    {
        "name": "Recreation",
        "series": "CPIRECSL",
        "weight": 0.05,
        "source": "BLS CPI: Recreation (FRED: CPIRECSL)",
        "note": "Sports, hobbies, media, pets, recreational services. Carved out from "
                "broad 'core' — typically grows slower than headline core.",
    },
    {
        "name": "Education & Communication",
        "series": "CPIEDUSL",
        "weight": 0.05,
        "source": "BLS CPI: Education and Communication (FRED: CPIEDUSL)",
        "note": "Tuition, school supplies, phone plans, internet, postage. Carved out from "
                "broad 'core' — communications subindex has been deflating, dragging this lower.",
    },
    {
        "name": "Household furnishings",
        "series": "CUUR0000SAH3",
        "weight": 0.05,
        "source": "BLS CPI: Household Furnishings and Operations (FRED: CUUR0000SAH3)",
        "note": "Furniture, appliances, kitchen, cleaning supplies, household services. "
                "Carved out from broad 'core' — durables have been broadly deflating since 2023.",
    },
    {
        "name": "Other (residual core)",
        "series": "CPILFESL",
        "weight": 0.12,
        "source": "BLS Core CPI (FRED: CPILFESL)",
        "note": "Everything not captured above — alcohol, tobacco, personal care, "
                "miscellaneous services. Uses headline core CPI as a proxy.",
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


def fetch_zillow_zori():
    """Fetch Zillow Observed Rent Index (ZORI) for the United States.

    ZORI is a smoothed measure of typical observed market-rate rents, derived
    from millions of Zillow rental listings. It's published monthly as free
    public CSVs. We download the metro-level CSV (which also contains the US
    national row), find the "United States" row, and extract the monthly
    time series. Falls through to a backup URL if the primary 404s.

    Returns the same {date, value} shape as fetch_series for drop-in use.
    """
    import csv
    from io import StringIO

    headers = {
        # Some CDNs reject empty/missing User-Agents. Identify ourselves clearly.
        "User-Agent": "Mozilla/5.0 (compatible; ember-fire-inflation-fetcher/1.0)",
    }

    last_error = None
    for url in ZILLOW_ZORI_URLS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=60) as resp:
                content = resp.read().decode("utf-8")

            reader = csv.DictReader(StringIO(content))
            us_row = None
            for row in reader:
                # The national row is identified by RegionType=country in newer
                # CSVs and SizeRank=0 with RegionName="United States" in older
                # ones. Match either pattern.
                region_type = (row.get("RegionType") or "").strip().lower()
                region_name = (row.get("RegionName") or "").strip()
                if region_type == "country" or region_name == "United States":
                    us_row = row
                    break

            if not us_row:
                last_error = f"No United States row found in {url}"
                continue

            # Date columns are all the non-metadata fields that parse as ISO dates.
            metadata_cols = {
                "RegionID", "SizeRank", "RegionName", "RegionType", "StateName",
                "Metro", "City", "County", "State",
            }
            observations = []
            for col, val in us_row.items():
                if col in metadata_cols or not col:
                    continue
                if val is None or str(val).strip() == "":
                    continue
                try:
                    datetime.date.fromisoformat(col)  # confirms it's a date column
                except (ValueError, TypeError):
                    continue
                try:
                    observations.append({"date": col, "value": float(val)})
                except (ValueError, TypeError):
                    continue

            observations.sort(key=lambda o: o["date"])
            if len(observations) < 24:
                # Sanity check: we expect years of monthly data.
                last_error = f"Only {len(observations)} observations in {url}"
                continue

            return observations

        except Exception as e:
            last_error = f"{url}: {e}"
            continue

    raise RuntimeError(f"All Zillow ZORI URLs failed. Last error: {last_error}")


def fetch_component(comp, start_date="2010-01-01"):
    """Dispatch fetching based on component's source_type.

    For zillow_zori we don't pass start_date — Zillow's CSV is what it is, and
    we filter by date downstream if needed.
    """
    source_type = comp.get("source_type", "fred")
    if source_type == "eia_petroleum":
        return fetch_eia_gas(start_date)
    if source_type == "zillow_zori":
        return fetch_zillow_zori()
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
    try:
        target_prior_date = latest_date.replace(year=latest_date.year - 1)
    except ValueError:
        # Leap day (Feb 29) — the prior year has no Feb 29, so use Feb 28 instead.
        target_prior_date = latest_date.replace(year=latest_date.year - 1, day=28)

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
    print("  Shelter component:  using Zillow ZORI (rent index) with Case-Shiller fallback")
    print("  Utilities component: using BLS electricity CPI (motor fuel captured in Transportation)")

    # BLS headline CPI (for comparison)
    print(f"  {BLS_CPI_SERIES:30s}  BLS headline CPI")
    bls_observations = fetch_series(BLS_CPI_SERIES)
    bls_date, bls_yoy, _ = yoy_at(bls_observations, datetime.date.today())

    # Each component
    component_results = []
    all_series = []
    for comp in COMPONENTS:
        print(f"  {comp['series']:30s}  {comp['name']}")
        try:
            observations = fetch_component(comp)
        except Exception as e:
            # Zillow CSV path can change occasionally — if shelter via ZORI
            # fails, fall back to Case-Shiller (FRED CSUSHPISA) so the run
            # still completes with a sensible shelter component.
            if comp.get("source_type") == "zillow_zori":
                print(f"    ⚠ ZORI fetch failed ({e}). Falling back to Case-Shiller.")
                comp = {
                    **comp,
                    "series": "CSUSHPISA",
                    "source_type": "fred",
                    "source": "S&P/Case-Shiller U.S. National Home Price Index (FRED fallback)",
                    "note": "ZORI download unavailable this run — using home-price index instead.",
                }
                observations = fetch_component(comp)
            else:
                raise

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
                "changes across 10 consumer spending categories, modeled after "
                "Truflation's published category structure. Three methodological choices "
                "drive Ember's divergence from BLS CPI: "
                "(1) we use Zillow's Observed Rent Index (ZORI) for shelter instead of "
                "BLS's lagging Owners' Equivalent Rent; "
                "(2) we structure Energy/Transportation the way Truflation does — Utilities "
                "covers electricity (motor fuel is captured inside Transportation, avoiding "
                "the double-count that occurs when motor fuel is also a separate component); "
                "(3) we carve out apparel, recreation, education/communication, and "
                "household furnishings as separate components rather than folding them "
                "into a single 'core' category — important since tariff effects on goods "
                "are no longer uniformly deflationary."
            ),
            "weights_source": (
                "Truflation 2026 published weights where defensible (housing 23%, "
                "transportation 19%, food 15%, medical 8%), with sub-category carve-outs "
                "to expose goods inflation that gets averaged away in broad core indices"
            ),
            "data_sources": (
                "Zillow Research + Federal Reserve Economic Data (FRED), St. Louis Fed"
            ),
            "update_frequency": (
                "Daily. Component data updates monthly; we recompute daily to pick up "
                "any revisions."
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
