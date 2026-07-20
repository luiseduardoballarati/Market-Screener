# Market Screener

Scans the entire US stock universe for symbols that moved ±50% in the last week or month, and emails you the results.

Runs in two passes to stay fast and polite with Yahoo Finance's free endpoints:

1. **Prescreen** — batches of 100 tickers, two months of daily closes each, filtered on 1W/1M move and a minimum price. Thousands of symbols down to a handful.
2. **Full fetch** — five years of history for the survivors only, one ticker at a time with retries, exponential backoff and a 7-day Parquet cache.

Output is a CSV plus an HTML email with the top 100 matches and their 1W / 1M / 3M / 6M / 1Y / 5Y growth.

## Setup

```bash
git clone https://github.com/luiseduardoballarati/Market-Screener.git
cd Market-Screener
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then fill it in
```

### Ticker universe

You need a CSV with a column of US ticker symbols. Point `CSV_PATH` at it. Any file with a `ticker` column works — if that column name is missing, the first column is used.

### Email

Set `SENDGRID_API_KEY` to send via SendGrid. Leave it empty and the script falls back to SMTP using `EMAIL_USER` / `EMAIL_PASS`. For Gmail with 2FA, `EMAIL_PASS` must be an app password, not your account password.

## Usage

```bash
python market_scrapper.py
```

Expect a long run. The prescreen sleeps between batches and the full fetch sleeps ~2.5s per ticker by design — the rate limiting is deliberate, not a bug. Results are cached in `cache_hist/` for a week, so repeat runs are much faster.

### Tuning

All thresholds live at the top of the script:

| Setting | Default | What it does |
|---|---|---|
| `THRESHOLD_1M` | 50.0 | Move size (%) required to qualify, up or down |
| `MIN_PRICE_USD` | 10.0 | Ignore anything below this price |
| `LOOKBACK_YEARS` | 5 | History depth for the full fetch |
| `CACHE_MAX_AGE_DAYS` | 7 | Refetch cached tickers older than this |
| `BATCH_SIZE_LIGHT` | 100 | Tickers per prescreen batch |

### Scheduling

Call the venv's Python directly — `source` doesn't reliably activate a venv under cron — and `cd` in first, since cron runs with `PWD=/`:

```cron
0 8 * * 1 cd /path/to/Market-Screener && /path/to/Market-Screener/.venv/bin/python market_scrapper.py >> cron.log 2>&1
```

## Disclaimer

A personal hobby project, not investment advice. The data is Yahoo Finance's free feed — expect gaps, bad ticks and delisted symbols. A ±50% move is a starting point for research, nothing more.

## License

MIT
