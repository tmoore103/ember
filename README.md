# Ember

A FIRE & retirement calculator with a portfolio simulator. Built for early retirement, works for any retirement.

## Project structure

```
.
├── index.html                       # The app (single-file, deployable as-is)
├── data/
│   └── tickers.json                 # ETF stats — refreshed weekly by GitHub Action
├── scripts/
│   └── fetch_ticker_data.py         # Pulls fresh stats from yfinance
├── .github/workflows/
│   └── refresh-tickers.yml          # Weekly schedule to refresh ticker data
├── requirements.txt
└── README.md
```

## Local development

Open `index.html` in any browser. No build step. The site fetches `data/tickers.json` on load and falls back to its built-in hardcoded values if the fetch fails (e.g., when opened from the local filesystem).

## How the data pipeline works

1. Every Monday at 06:00 UTC, the GitHub Action runs `scripts/fetch_ticker_data.py`.
2. The script pulls 10-year CAGR, TTM dividend yield, and 2022 calendar-year return for each ticker via yfinance.
3. If anything changed, the Action commits the updated `data/tickers.json`.
4. Netlify sees the new commit and auto-deploys the site, so the live UI gets the refreshed numbers.

## Refreshing ticker data manually

```bash
pip install -r requirements.txt
python scripts/fetch_ticker_data.py
```

Or trigger the workflow manually from the **Actions** tab in GitHub (the "Refresh ticker data" workflow has a "Run workflow" button thanks to `workflow_dispatch`).

## Adding a new ticker

1. Add the symbol to the `TICKERS` dict in `scripts/fetch_ticker_data.py`.
2. Add the symbol to the `TICKERS` object in `index.html` (with reasonable defaults — the pipeline will overwrite them on the next run).
3. Push. The next pipeline run will populate the JSON; the next page load will pick it up.

## Deployment

Netlify is connected to this repo. Pushing to `main` triggers an auto-deploy. There's no build step — Netlify just serves the static files.
