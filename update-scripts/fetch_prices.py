#!/usr/bin/env python3
"""
Daily Brief — Prices fetcher.

Pulls latest prices + ~1 year of daily closes for each tracked asset, then
computes SMA50, SMA200, RSI(14), and a simple bull / bear / neutral signal.

Sources (free, no key):
  - CoinGecko /coins/{id}/market_chart   → BTC, ETH (daily, 365d)
  - Stooq /q/d/l/?s=TICKER&i=d           → equities, indices, ETFs, commodities

Signal rules:
  - bull    if last_price > SMA200 AND RSI(14) in (40, 70)
  - bear    if last_price < SMA200 AND RSI(14) < 45
  - neutral otherwise

Failures degrade gracefully: an asset that can't be fetched keeps its prior
JSON entry (or an "unknown" placeholder) rather than blocking the run.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

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
    source: str        # "coingecko" | "stooq"
    source_id: str     # coingecko coin id, or stooq symbol
    note: str = ""

# Categories follow the dashboard layout 1:1.
ASSETS: dict[str, list[AssetSpec]] = {
    "crypto": [
        AssetSpec("BTC", "Bitcoin",  "USD", "coingecko", "bitcoin"),
        AssetSpec("ETH", "Ethereum", "USD", "coingecko", "ethereum"),
    ],
    "commodities": [
        AssetSpec("XAUUSD", "Gold (spot, oz)",      "USD", "stooq", "xauusd"),
        AssetSpec("CL=F",   "Crude Oil (WTI, bbl)", "USD", "stooq", "cl.f"),
    ],
    "indices": [
        AssetSpec("^GSPC",   "S&P 500",                 "USD", "stooq", "^spx"),
        AssetSpec("VWCE.DE", "Vanguard FTSE All-World", "EUR", "stooq", "vwce.de"),
        AssetSpec("URTH",    "iShares MSCI World",      "USD", "stooq", "urth.us"),
    ],
    "ai_tech": [
        AssetSpec("NVDA", "NVIDIA",                   "USD", "stooq", "nvda.us"),
        AssetSpec("MSFT", "Microsoft (OpenAI proxy)", "USD", "stooq", "msft.us",
                  note="MSFT held as a proxy for OpenAI exposure: Microsoft holds an "
                       "economic interest of roughly 49% in OpenAI."),
    ],
    "defense": [
        AssetSpec("PLTR",   "Palantir",      "USD", "stooq", "pltr.us"),
        AssetSpec("LDO.MI", "Leonardo SpA",  "EUR", "stooq", "ldo.it"),
        AssetSpec("RHM.DE", "Rheinmetall AG","EUR", "stooq", "rhm.de"),
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

def fetch_coingecko(coin_id: str) -> list[float] | None:
    """Daily close history for the last ~365 days from CoinGecko (free tier)."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        "?vs_currency=usd&days=365&interval=daily"
    )
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        prices = data.get("prices") or []
        # CoinGecko returns [[ts, price], ...] sorted oldest→newest.
        return [float(p[1]) for p in prices if p and p[1] is not None]
    except Exception as exc:
        log.warning("coingecko %s failed: %s", coin_id, exc)
        return None

def fetch_stooq(symbol: str) -> list[float] | None:
    """Daily closes from Stooq CSV. Free, no key, but rate-limit-friendly."""
    # Stooq CSV: Date,Open,High,Low,Close,Volume
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    try:
        r = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        text = r.text.strip()
        if not text or text.lower().startswith("<!doctype") or "no data" in text.lower():
            log.warning("stooq %s returned no data", symbol)
            return None
        reader = csv.DictReader(io.StringIO(text))
        closes = []
        for row in reader:
            try:
                v = float(row["Close"])
                if math.isfinite(v):
                    closes.append(v)
            except (KeyError, TypeError, ValueError):
                continue
        # Stooq returns oldest→newest; trim to last ~400 trading days for SMA200.
        return closes[-450:] if len(closes) > 450 else closes
    except Exception as exc:
        log.warning("stooq %s failed: %s", symbol, exc)
        return None

def fetch_history(spec: AssetSpec) -> list[float] | None:
    if spec.source == "coingecko":
        return fetch_coingecko(spec.source_id)
    if spec.source == "stooq":
        return fetch_stooq(spec.source_id)
    return None

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def build_asset_payload(spec: AssetSpec) -> dict:
    closes = fetch_history(spec)

    if not closes or len(closes) < 2:
        log.warning("%s: insufficient history; placeholder entry", spec.ticker)
        return {
            "ticker": spec.ticker,
            "name": spec.name,
            "currency": spec.currency,
            "price": None,
            "change_pct": None,
            "sma50": None,
            "sma200": None,
            "rsi14": None,
            "signal": "unknown",
            **({"note": spec.note} if spec.note else {}),
        }

    last = closes[-1]
    prev = closes[-2]
    change_pct = ((last - prev) / prev) * 100.0 if prev else None
    sma50_v = sma(closes, 50)
    sma200_v = sma(closes, 200)
    rsi14 = rsi(closes, 14)
    sig = classify(last, sma200_v, rsi14)

    out = {
        "ticker": spec.ticker,
        "name": spec.name,
        "currency": spec.currency,
        "price": round(last, 4),
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
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
