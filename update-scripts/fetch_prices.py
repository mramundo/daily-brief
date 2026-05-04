#!/usr/bin/env python3
"""
Daily Brief — Prices fetcher.

Pulls latest prices + ~1 year of daily closes for each tracked asset, then
computes SMA50, SMA200, RSI(14), and a simple bull / bear / neutral signal.

Sources (free, no key):
  - CoinGecko /coins/{id}/market_chart   → BTC, ETH (daily, 365d)
  - Yahoo Finance v8 chart endpoint      → equities, indices, ETFs, commodities

Signal rules:
  - bull    if last_price > SMA200 AND RSI(14) in (40, 70)
  - bear    if last_price < SMA200 AND RSI(14) < 45
  - neutral otherwise

Failures degrade gracefully: an asset that can't be fetched keeps its prior
JSON entry (or an "unknown" placeholder) rather than blocking the run.
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from yahooquery import Ticker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_FILE = DATA_DIR / "prices.json"
SEED_FILE = DATA_DIR / "prices.seed.json"

USER_AGENT = "DailyBriefBot/1.0 (+https://github.com/mramundo/daily-brief)"
HTTP_TIMEOUT = 25

# Signal thresholds — kept conservative.
RSI_BULL_LOW, RSI_BULL_HIGH = 40.0, 70.0
RSI_BEAR_HIGH = 45.0

@dataclass
class AssetSpec:
    ticker: str
    name: str
    currency: str
    source: str        # "coingecko" | "yahoo"
    source_id: str     # coingecko coin id, or yahoo symbol
    note: str = ""

# Categories follow the dashboard layout 1:1.
ASSETS: dict[str, list[AssetSpec]] = {
    "crypto": [
        AssetSpec("BTC", "Bitcoin",  "USD", "coingecko", "bitcoin"),
        AssetSpec("ETH", "Ethereum", "USD", "coingecko", "ethereum"),
    ],
    "commodities": [
        AssetSpec("XAUUSD", "Gold (spot, oz)",      "USD", "yahoo", "GC=F"),
        AssetSpec("CL=F",   "Crude Oil (WTI, bbl)", "USD", "yahoo", "CL=F"),
    ],
    "indices": [
        AssetSpec("^GSPC",   "S&P 500",                 "USD", "yahoo", "^GSPC"),
        AssetSpec("VWCE.DE", "Vanguard FTSE All-World", "EUR", "yahoo", "VWCE.DE"),
        AssetSpec("URTH",    "iShares MSCI World",      "USD", "yahoo", "URTH"),
    ],
    "ai_tech": [
        AssetSpec("NVDA",  "NVIDIA",    "USD", "yahoo", "NVDA"),
        AssetSpec("MSFT",  "Microsoft", "USD", "yahoo", "MSFT"),
        AssetSpec("GOOGL", "Alphabet",  "USD", "yahoo", "GOOGL"),
        AssetSpec("AAPL",  "Apple",     "USD", "yahoo", "AAPL"),
    ],
    "defense": [
        AssetSpec("PLTR",   "Palantir",      "USD", "yahoo", "PLTR"),
        AssetSpec("LDO.MI", "Leonardo SpA",  "EUR", "yahoo", "LDO.MI"),
        AssetSpec("RHM.DE", "Rheinmetall AG","EUR", "yahoo", "RHM.DE"),
    ],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_prices")

# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------

def sma(values: list[float], window: int) -> float | None:
    """Simple moving average of the last `window` closes."""
    if len(values) < window:
        return None
    return sum(values[-window:]) / window

def rsi(values: list[float], period: int = 14) -> float | None:
    """Wilder's RSI on a list of close prices."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def change_over(dates: list[str], closes: list[float], days: int) -> float | None:
    """% change of the last close vs. the close `days` calendar days ago.
    Walks back to the most recent date <= (last_date - days). Source-agnostic:
    works for daily-trading (yahoo, weekends missing) and daily-calendar
    (coingecko) alike."""
    if not closes or len(closes) < 2:
        return None
    last_close = closes[-1]
    if last_close == 0:
        return None
    try:
        last_date = datetime.fromisoformat(dates[-1].replace("Z", "+00:00")).date()
    except Exception:
        return None
    cutoff = last_date.toordinal() - days
    target_close: float | None = None
    for i in range(len(dates) - 1, -1, -1):
        try:
            d_ord = datetime.fromisoformat(dates[i].replace("Z", "+00:00")).date().toordinal()
        except Exception:
            continue
        if d_ord <= cutoff:
            target_close = closes[i]
            break
    if target_close is None or target_close == 0:
        return None
    return ((last_close - target_close) / target_close) * 100.0

def classify(price: float | None, sma200_v: float | None, rsi_v: float | None) -> str:
    if price is None or sma200_v is None or rsi_v is None:
        return "unknown"
    if price > sma200_v and RSI_BULL_LOW < rsi_v < RSI_BULL_HIGH:
        return "bull"
    if price < sma200_v and rsi_v < RSI_BEAR_HIGH:
        return "bear"
    return "neutral"

# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

Series = tuple[list[str], list[float]]   # (iso dates, closes), aligned

def fetch_coingecko(coin_id: str) -> Series | None:
    """Daily close history (~365 days) from CoinGecko (free tier)."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        "?vs_currency=usd&days=365&interval=daily"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        prices = data.get("prices") or []
        # CoinGecko returns [[ts_ms, price], ...] sorted oldest→newest.
        dates, closes = [], []
        for ts, p in prices:
            if p is None:
                continue
            d = datetime.fromtimestamp(ts / 1000.0, timezone.utc).date().isoformat()
            dates.append(d)
            closes.append(float(p))
        return (dates, closes) if len(closes) >= 2 else None
    except Exception as exc:
        log.warning("coingecko %s failed: %s", coin_id, exc)
        return None

def fetch_yahoo(symbol: str) -> Series | None:
    """Daily (date, close) series via yahooquery.

    yfinance/v8 chart endpoint 429s from cloud IPs; yahooquery's quoteSummary
    + chart endpoints use a different code path that's not on the blocklist.
    """
    try:
        hist = Ticker(symbol, asynchronous=False).history(period="2y", interval="1d")
        if not hasattr(hist, "shape") or hist.empty or "close" not in hist.columns:
            log.warning("yahoo %s: empty history (%s)", symbol, type(hist).__name__)
            return None
        # Multi-index: (symbol, date). We only fetched one symbol.
        dates, closes = [], []
        for (_sym, dt), close in hist["close"].items():
            if close is None or not math.isfinite(float(close)):
                continue
            dates.append(dt.isoformat() if hasattr(dt, "isoformat") else str(dt))
            closes.append(float(close))
        return (dates, closes) if len(closes) >= 2 else None
    except Exception as exc:
        log.warning("yahoo %s failed: %s", symbol, exc)
        return None

def fetch_history(spec: AssetSpec) -> Series | None:
    if spec.source == "coingecko":
        return fetch_coingecko(spec.source_id)
    if spec.source == "yahoo":
        return fetch_yahoo(spec.source_id)
    return None

# ---------------------------------------------------------------------------
# FX (EUR→USD, etc.) — cached per run
# ---------------------------------------------------------------------------

_FX_CACHE: dict[str, dict[str, float]] = {}

def fx_series(base: str, quote: str = "USD") -> dict[str, float]:
    """date → rate map for `base`/`quote` (e.g. EUR→USD via EURUSD=X).

    Empty dict if fetch fails — caller should fall back to leaving the price
    in its native currency rather than producing wrong USD numbers.
    """
    if base == quote:
        return {}
    key = f"{base}{quote}"
    if key in _FX_CACHE:
        return _FX_CACHE[key]
    series = fetch_yahoo(f"{base}{quote}=X")
    if not series:
        log.warning("fx %s/%s: no data; assets in %s will stay native", base, quote, base)
        _FX_CACHE[key] = {}
        return _FX_CACHE[key]
    dates, closes = series
    _FX_CACHE[key] = {d: c for d, c in zip(dates, closes)}
    return _FX_CACHE[key]

def to_usd(series: Series, base_ccy: str) -> Series | None:
    """Convert a (dates, closes) series from `base_ccy` to USD via daily FX
    rates. Forward-fills missing FX days (holidays where stock trades but
    FX feed has a gap) using the last known rate.
    """
    if base_ccy == "USD":
        return series
    fx = fx_series(base_ccy, "USD")
    if not fx:
        return None
    dates, closes = series
    out_dates, out_closes = [], []
    last_rate: float | None = None
    for d, c in zip(dates, closes):
        rate = fx.get(d)
        if rate is None or not math.isfinite(rate):
            rate = last_rate
        else:
            last_rate = rate
        if rate is None:
            continue   # no FX yet at start of series
        out_dates.append(d)
        out_closes.append(c * rate)
    return (out_dates, out_closes) if len(out_closes) >= 2 else None

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_asset_payload(spec: AssetSpec) -> dict:
    series = fetch_history(spec)

    # Convert to USD if the asset is denominated in another currency. We do
    # this on the full series (not just the spot) so SMA/RSI reflect the USD
    # trajectory rather than being mixed-currency artefacts.
    display_currency = "USD"
    if series is not None and spec.currency != "USD":
        converted = to_usd(series, spec.currency)
        if converted is None:
            # FX fetch failed — keep native currency rather than emitting wrong numbers.
            display_currency = spec.currency
        else:
            series = converted

    if not series or len(series[1]) < 2:
        log.warning("%s: insufficient history; placeholder entry", spec.ticker)
        return {
            "ticker": spec.ticker,
            "name": spec.name,
            "currency": display_currency,
            "price": None,
            "change_pct": None,
            "change_1w": None,
            "change_1m": None,
            "change_3m": None,
            "change_6m": None,
            "change_1y": None,
            "sma50": None,
            "sma200": None,
            "rsi14": None,
            "signal": "unknown",
            **({"note": spec.note} if spec.note else {}),
        }

    dates, closes = series
    last = closes[-1]
    prev = closes[-2]
    change_pct = ((last - prev) / prev) * 100.0 if prev else None
    change_1w = change_over(dates, closes, 7)
    change_1m = change_over(dates, closes, 30)
    change_3m = change_over(dates, closes, 90)
    change_6m = change_over(dates, closes, 180)
    change_1y = change_over(dates, closes, 365)
    sma50_v = sma(closes, 50)
    sma200_v = sma(closes, 200)
    rsi14 = rsi(closes, 14)
    sig = classify(last, sma200_v, rsi14)

    out = {
        "ticker": spec.ticker,
        "name": spec.name,
        "currency": display_currency,
        "price": round(last, 4),
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "change_1w": round(change_1w, 2) if change_1w is not None else None,
        "change_1m": round(change_1m, 2) if change_1m is not None else None,
        "change_3m": round(change_3m, 2) if change_3m is not None else None,
        "change_6m": round(change_6m, 2) if change_6m is not None else None,
        "change_1y": round(change_1y, 2) if change_1y is not None else None,
        "sma50":  round(sma50_v, 4) if sma50_v is not None else None,
        "sma200": round(sma200_v, 4) if sma200_v is not None else None,
        "rsi14":  round(rsi14, 2) if rsi14 is not None else None,
        "signal": sig,
    }
    if spec.note:
        out["note"] = spec.note
    return out

def build_payload() -> dict:
    now = datetime.now(timezone.utc)
    categories: dict[str, list[dict]] = {}
    for cat, specs in ASSETS.items():
        items = []
        for spec in specs:
            items.append(build_asset_payload(spec))
            log.info("[%s] %s done (signal=%s)", cat, spec.ticker, items[-1].get("signal"))
        categories[cat] = items
    return {
        "updated": now.isoformat(),
        "categories": categories,
    }

def write_output(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s", OUT_FILE)

def main() -> int:
    try:
        payload = build_payload()
        write_output(payload)
        return 0
    except Exception as exc:
        log.exception("fatal: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
