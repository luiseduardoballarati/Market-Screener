#!/usr/bin/env python3
import os, smtplib, base64, time, random
from pathlib import Path
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from email.mime.text import MIMEText

import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

# =========================
# Config
# =========================

CSV_PATH = "[PATH-TO-CSV-WITH-TICKERS]"   # CSV file with at least a ticker symbol column
COLUMN   = "ticker"
LOOKBACK_YEARS  = 5                  # history horizon for snapshots (years)
THRESHOLD_1M    = 50.0               # threshold for short-term change (percent)
MIN_PRICE_USD   = 10.0               # require current price >= $10
BASE_SLEEP_SEC  = 2.5                # base sleep between ticker fetches
MAX_RETRIES     = 4                  # max retries for data fetch
BACKOFF_FACTOR  = 1.8                # exponential backoff factor for retries
JITTER_SEC      = 0.5                # random jitter added to sleeps
CACHE_DIR          = Path("cache_hist")   # cache directory for per-ticker data
CACHE_MAX_AGE_DAYS = 7                  # refetch if cache older than this (days)
BATCH_SIZE_LIGHT   = 100                # batch size for 2-month prescreen fetch
SLEEP_BETWEEN_BATCHES = 1.2             # base sleep between quick batches
OUT_DIR         = Path("screener")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# =========================
# Small helpers
# =========================

def _parse_emails(s: str | None) -> list[str]:
    if not s:
        return []
    return [e.strip() for e in s.replace(";", ",").split(",") if e.strip()]

def trading_price_on_or_before(series: pd.Series, target_date: pd.Timestamp):
    s = series.dropna()
    s = s[s.index <= target_date]
    if s.empty:
        return None
    return float(s.iloc[-1])

def growth_table_html(growth_df: pd.DataFrame, title: str) -> str:
    fmt = growth_df.copy()
    price_cols = ["5Y_Ago", "1Y_Ago", "6M_Ago", "3M_Ago", "1M_Ago", "1W_Ago", "Current"]
    for c in price_cols:
        if c in fmt.columns:
            fmt[c] = fmt[c].map(lambda x: "" if pd.isna(x) else f"${x:,.2f}")
    for c in ["Growth_1W_%", "Growth_1M_%", "Growth_3M_%", "Growth_6M_%", "Growth_1Y_%", "Growth_5Y_%"]:
        if c in fmt.columns:
            fmt[c] = fmt[c].map(lambda x: "" if pd.isna(x) else f"{x:.2f}%")
    html_table = fmt.reset_index().to_html(index=False, border=0, escape=False)
    return f"""
    <h3 style="margin:16px 0 8px 0;font-family:Arial,sans-serif;">{title}</h3>
    <div style="font-family:Arial,sans-serif;font-size:13px">{html_table}</div>
    """

# =========================
# Email (SendGrid first, SMTP fallback)
# =========================

def send_email_with_attachments(subject: str, body_html: str, attachments: list[Path]):
    env_path = Path(__file__).with_name(".env")
    load_dotenv(dotenv_path=env_path)

    # Prefer SendGrid if API key is present
    sg_key = os.getenv("SENDGRID_API_KEY")
    to_list  = _parse_emails(os.getenv("EMAIL_TO"))
    cc_list  = _parse_emails(os.getenv("EMAIL_CC"))
    bcc_list = _parse_emails(os.getenv("EMAIL_BCC"))
    from_addr = os.getenv("EMAIL_FROM") or os.getenv("EMAIL_USER")

    if sg_key:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, To, Cc, Bcc, Attachment, FileContent, FileName, FileType, Disposition
        if not (from_addr and to_list):
            raise RuntimeError("EMAIL_FROM (or EMAIL_USER) and EMAIL_TO are required for SendGrid.")
        message = Mail(from_email=from_addr, subject=subject, html_content=body_html)
        message.to = [To(e) for e in to_list]
        if cc_list:
            message.cc  = [Cc(e) for e in cc_list]
        if bcc_list:
            message.bcc = [Bcc(e) for e in bcc_list]
        # Attach files
        for p in attachments:
            with open(p, "rb") as f:
                data = f.read()
            enc = base64.b64encode(data).decode()
            att = Attachment(FileContent(enc), FileName(p.name), FileType("application/octet-stream"), Disposition("attachment"))
            message.attachment = (message.attachment or []) + [att]
        resp = SendGridAPIClient(sg_key).send(message)
        if resp.status_code >= 300:
            raise RuntimeError(f"SendGrid send failed: {resp.status_code} {resp.body}")
        return

    # SMTP fallback if no SendGrid
    user = os.getenv("EMAIL_USER")
    pwd  = os.getenv("EMAIL_PASS")
    smtp_server = os.getenv("SMTP_SERVER", "smtp.office365.com")
    smtp_port   = int(os.getenv("SMTP_PORT", "587"))
    if not all([user, pwd]) or not to_list:
        raise RuntimeError("No SENDGRID_API_KEY and missing SMTP credentials (EMAIL_USER/PASS/TO).")

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html"))
    for path in attachments:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=path.name)
        part["Content-Disposition"] = f'attachment; filename="{path.name}"'
        msg.attach(part)

    all_rcpts = to_list + cc_list + bcc_list
    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(user, pwd)
            server.sendmail(user, all_rcpts, msg.as_string())
    except smtplib.SMTPAuthenticationError:
        with smtplib.SMTP_SSL(smtp_server, 465, timeout=30) as server:
            server.ehlo()
            server.login(user, pwd)
            server.sendmail(user, all_rcpts, msg.as_string())

# =========================
# Core data functions
# =========================

def read_all_tickers_from_csv(csv_path=CSV_PATH, column=COLUMN) -> list[str]:
    df = pd.read_csv(csv_path)
    col = column if column in df.columns else df.columns[0]
    vals = df[col].dropna().astype(str).str.strip().tolist()
    seen, out = set(), []
    for t in vals:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

def _cache_path_for(ticker: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{ticker.upper()}.parquet"

def _is_cache_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    age_days = (pd.Timestamp.utcnow() - pd.Timestamp(p.stat().st_mtime, unit="s")).days
    return age_days <= CACHE_MAX_AGE_DAYS

def fetch_one_ticker_slow(ticker: str, start_date: pd.Timestamp) -> pd.DataFrame:
    """
    Fetches historical data for one ticker with retries and caching.
    Returns a DataFrame with columns Date, Close, Ticker (or empty if failed).
    """
    cp = _cache_path_for(ticker)
    if _is_cache_fresh(cp):
        try:
            return pd.read_parquet(cp)
        except Exception:
            pass  # If cache is corrupt, refetch

    time.sleep(BASE_SLEEP_SEC + random.uniform(0, JITTER_SEC))
    tries = 0
    delay = BASE_SLEEP_SEC
    while tries <= MAX_RETRIES:
        try:
            df = yf.download(ticker, start=start_date.strftime("%Y-%m-%d"),
                             auto_adjust=True, progress=False, group_by="ticker", threads=False)
            if isinstance(df.columns, pd.MultiIndex):
                s = df[ticker]["Close"].dropna()
            else:
                s = df["Close"].dropna()
            if s.empty:
                return pd.DataFrame(columns=["Date", "Close", "Ticker"])
            out_df = s.rename("Close").to_frame().reset_index()
            out_df["Ticker"] = ticker
            out_df["Date"] = pd.to_datetime(out_df["Date"]).dt.tz_localize(None)
            try:
                out_df.to_parquet(cp, index=False)
            except Exception:
                pass  # If caching fails, continue without cache
            return out_df
        except Exception as e:
            tries += 1
            if tries > MAX_RETRIES:
                print(f"[FAIL] {ticker}: {e}")
                return pd.DataFrame(columns=["Date", "Close", "Ticker"])
            sleep_for = delay + random.uniform(0, JITTER_SEC)
            print(f"[RETRY {tries}/{MAX_RETRIES}] {ticker}: {e} -> sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
            delay *= BACKOFF_FACTOR

def fetch_history_streaming(tickers: list[str], start_date: pd.Timestamp) -> pd.DataFrame:
    """
    Sequentially downloads full historical data for each ticker (polite rate limiting).
    """
    frames = []
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t.strip()))
    print(f"Streaming download: {len(tickers)} tickers")
    for i, t in enumerate(tickers, 1):
        df = fetch_one_ticker_slow(t, start_date=start_date)
        if not df.empty:
            frames.append(df)
        if i % 100 == 0:
            print(f"  …processed {i} / {len(tickers)}")
    if not frames:
        return pd.DataFrame(columns=["Date", "Close", "Ticker"])
    return pd.concat(frames, ignore_index=True)

def build_snapshot_table(history_df: pd.DataFrame) -> pd.DataFrame:
    today = pd.Timestamp.today(tz="UTC").tz_localize(None)
    targets = {
        "Current": today,
        "1D_Ago": today - pd.Timedelta(days=1),
        "5D_Ago": today - pd.Timedelta(days=5),
        "1W_Ago": today - pd.Timedelta(days=7),
        "1M_Ago": today - pd.Timedelta(days=30),
        "3M_Ago": today - pd.Timedelta(days=91),
        "6M_Ago": today - pd.Timedelta(days=182),
        "1Y_Ago": today - pd.Timedelta(days=365),
        "5Y_Ago": today - pd.Timedelta(days=5 * 365),
    }
    rows = []
    for tkr, df_t in history_df.groupby("Ticker"):
        s = df_t.set_index("Date")["Close"].sort_index()
        row = {"Ticker": tkr}
        for label, when in targets.items():
            row[label] = trading_price_on_or_before(s, when)
        rows.append(row)
    return pd.DataFrame(rows).set_index("Ticker")

def compute_growth_rates(snapshot: pd.DataFrame) -> pd.DataFrame:
    required_cols = ["Current", "1W_Ago", "1M_Ago", "3M_Ago", "6M_Ago", "1Y_Ago", "5Y_Ago"]
    for c in required_cols:
        if c not in snapshot.columns:
            raise ValueError(f"Snapshot missing column: {c}")
    def pct_change(cur, past):
        return ((cur / past - 1.0) * 100.0).where(past.notna())
    out = pd.DataFrame(index=snapshot.index)
    out["Growth_1D_%"] = pct_change(snapshot["Current"], snapshot["1D_Ago"]).round(2)
    out["Growth_5D_%"] = pct_change(snapshot["Current"], snapshot["5D_Ago"]).round(2)
    out["Growth_1W_%"] = pct_change(snapshot["Current"], snapshot["1W_Ago"]).round(2)
    out["Growth_1M_%"] = pct_change(snapshot["Current"], snapshot["1M_Ago"]).round(2)
    out["Growth_3M_%"] = pct_change(snapshot["Current"], snapshot["3M_Ago"]).round(2)
    out["Growth_6M_%"] = pct_change(snapshot["Current"], snapshot["6M_Ago"]).round(2)
    out["Growth_1Y_%"] = pct_change(snapshot["Current"], snapshot["1Y_Ago"]).round(2)
    out["Growth_5Y_%"] = pct_change(snapshot["Current"], snapshot["5Y_Ago"]).round(2)
    return out

def _sleep_with_jitter(base=SLEEP_BETWEEN_BATCHES):
    time.sleep(base + random.uniform(0, JITTER_SEC))

def _download_batch_light(batch: list[str]):
    """
    Downloads last 2 months of daily data for a batch of tickers.
    Returns a DataFrame with a MultiIndex (ticker -> Close) if successful, or None on failure.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.download(
                tickers=batch,
                period="2mo",
                interval="1d",
                auto_adjust=True,
                group_by="ticker",
                progress=False,
                threads=True,
            )
            return df
        except Exception as e:
            if attempt == MAX_RETRIES:
                print(f"[LIGHT BATCH FAIL] {len(batch)} tickers: {e}")
                return None
            sleep_for = (SLEEP_BETWEEN_BATCHES * (BACKOFF_FACTOR ** (attempt - 1))) + random.uniform(0, JITTER_SEC)
            print(f"[LIGHT RETRY {attempt}/{MAX_RETRIES}] sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)

def prescreen_monthly_movers(all_tickers: list[str], min_growth_pct=50.0, min_price=10.0) -> pd.DataFrame:
    """
    Quickly scans all tickers (last 2 months of data) to find those with ±{min_growth_pct}% move in 1W or 1M.
    Returns DataFrame indexed by Ticker with columns ['Current','1W_Ago','1M_Ago','Growth_1W_%','Growth_1M_%'] for matches.
    """
    tickers = list(dict.fromkeys(t.strip().upper() for t in all_tickers if t.strip()))
    winners = []
    for i in range(0, len(tickers), BATCH_SIZE_LIGHT):
        batch = tickers[i:i + BATCH_SIZE_LIGHT]
        df = _download_batch_light(batch)
        _sleep_with_jitter()
        if df is None:
            continue

        # Normalize to series per ticker
        if isinstance(df.columns, pd.MultiIndex):
            for t in batch:
                try:
                    s = df[t]["Close"].dropna()
                except Exception:
                    continue
                if s.empty or len(s) < 2:
                    continue
                cur = float(s.iloc[-1])
                last_date = s.index.max()
                target_1w = last_date - pd.Timedelta(days=7)
                target_1m = last_date - pd.Timedelta(days=30)
                s_trunc_1w = s[s.index <= target_1w]
                s_trunc_1m = s[s.index <= target_1m]
                price_1w = float(s_trunc_1w.iloc[-1]) if not s_trunc_1w.empty else None
                price_1m = float(s_trunc_1m.iloc[-1]) if not s_trunc_1m.empty else None
                growth_1w = ((cur / price_1w - 1.0) * 100.0) if price_1w is not None else None
                growth_1m = ((cur / price_1m - 1.0) * 100.0) if price_1m is not None else None
                qualifies = False
                if growth_1w is not None and (growth_1w >= min_growth_pct or growth_1w <= -min_growth_pct):
                    qualifies = True
                if growth_1m is not None and (growth_1m >= min_growth_pct or growth_1m <= -min_growth_pct):
                    qualifies = True
                if not qualifies or cur < min_price:
                    continue
                g1w = round(growth_1w, 2) if growth_1w is not None else None
                g1m = round(growth_1m, 2) if growth_1m is not None else None
                winners.append((t, cur, price_1w, price_1m, g1w, g1m))
        else:
            # Single-ticker edge case
            s = df.get("Close", pd.Series(dtype="float64")).dropna()
            if s.empty or len(s) < 2:
                continue
            cur = float(s.iloc[-1])
            last_date = s.index.max()
            target_1w = last_date - pd.Timedelta(days=7)
            target_1m = last_date - pd.Timedelta(days=30)
            s_trunc_1w = s[s.index <= target_1w]
            s_trunc_1m = s[s.index <= target_1m]
            price_1w = float(s_trunc_1w.iloc[-1]) if not s_trunc_1w.empty else None
            price_1m = float(s_trunc_1m.iloc[-1]) if not s_trunc_1m.empty else None
            growth_1w = ((cur / price_1w - 1.0) * 100.0) if price_1w is not None else None
            growth_1m = ((cur / price_1m - 1.0) * 100.0) if price_1m is not None else None
            qualifies = False
            if growth_1w is not None and (growth_1w >= min_growth_pct or growth_1w <= -min_growth_pct):
                qualifies = True
            if growth_1m is not None and (growth_1m >= min_growth_pct or growth_1m <= -min_growth_pct):
                qualifies = True
            if not qualifies or cur < min_price:
                continue
            g1w = round(growth_1w, 2) if growth_1w is not None else None
            g1m = round(growth_1m, 2) if growth_1m is not None else None
            t = batch[0]
            winners.append((t, cur, price_1w, price_1m, g1w, g1m))

    if not winners:
        return pd.DataFrame(columns=["Current", "1W_Ago", "1M_Ago", "Growth_1W_%", "Growth_1M_%"])

    dfw = pd.DataFrame(winners, columns=["Ticker", "Current", "1W_Ago", "1M_Ago", "Growth_1W_%", "Growth_1M_%"]).set_index("Ticker")
    dfw = dfw.sort_values("Growth_1M_%", ascending=False)
    return dfw

# =========================
# Main
# =========================

def main():
    # A) read whole CSV
    all_tickers = read_all_tickers_from_csv(CSV_PATH, COLUMN)
    print(f"Universe size: {len(all_tickers):,}")
    # B) fast pre-screen based on short-term growth & price
    fast = prescreen_monthly_movers(all_tickers, min_growth_pct=THRESHOLD_1M, min_price=MIN_PRICE_USD)
    print(f"Prescreen matches (±{THRESHOLD_1M:.0f}% in 1W or 1M, Price ≥ ${MIN_PRICE_USD:.0f}): {len(fast):,}")
    if fast.empty:
        # still send an empty report
        winners_csv = OUT_DIR / f"ALL_moves_ge_{int(THRESHOLD_1M)}pct_px_ge_{int(MIN_PRICE_USD)}.csv"
        fast.to_csv(winners_csv)
        send_email_with_attachments(
            subject=f"No matches this run",
            body_html=f"<p style='font-family:Arial'>No symbols met ±{THRESHOLD_1M:.0f}% change in 1W or 1M with price ≥ ${MIN_PRICE_USD:.0f}.</p>",
            attachments=[winners_csv],
        )
        return

    # C) heavy fetch only for those matches to compute 6M/1Y/5Y history
    start_date = pd.Timestamp.today(tz="UTC").tz_localize(None) - pd.Timedelta(days=365 * (LOOKBACK_YEARS + 1))
    history = fetch_history_streaming(fast.index.tolist(), start_date=start_date)

    # D) build full snapshot & growth DataFrames
    snapshot = build_snapshot_table(history)
    growth   = compute_growth_rates(snapshot)
    full     = pd.concat([snapshot, growth], axis=1)

    # Keep only the prescreen matches
    full = full.loc[full.index.intersection(fast.index)].copy()
    # Recompute 1M growth with heavy data for accuracy
    full["Growth_1M_%"] = ((full["Current"] / full["1M_Ago"] - 1.0) * 100.0).round(2)

    winners = full.sort_values("Growth_1M_%", ascending=False)

    # E) save results & send email
    winners_csv = OUT_DIR / f"ALL_moves_ge_{int(THRESHOLD_1M)}pct_px_ge_{int(MIN_PRICE_USD)}.csv"
    winners.to_csv(winners_csv)

    today_str = pd.Timestamp.today(tz="UTC").strftime("%Y-%m-%d")
    subject = f"Weekly Screener – 50% rise or fall in 1W or 1M, Price ≥ ${int(MIN_PRICE_USD)} – {today_str}"
    preview = winners.head(100)
    body = f"""
    <h2 style="font-family:Arial">Weekly Screener</h2>
    <p style="font-family:Arial">
      1W or 1M change ≥ <b>+{THRESHOLD_1M:.0f}%</b> or ≤ <b>-{THRESHOLD_1M:.0f}%</b>, Current ≥ <b>${MIN_PRICE_USD:.0f}</b><br/>
      Universe: {len(all_tickers):,} tickers<br/>
      Matches found: {len(fast):,}
    </p>
    {growth_table_html(preview, "Top matches (first 100)")}
    <p style="font-family:Arial">Full results attached.</p>
    """
    try:
        send_email_with_attachments(subject, body, [winners_csv])
        print(f"Emailed results: {winners_csv.resolve()}")
    except Exception as e:
        print("Email failed/skipped:", e)
        print(f"Results saved to: {winners_csv.resolve()}")

if __name__ == "__main__":
    import pandas as pd
    main()
