#!/usr/bin/env python3
"""
Daily Brief — News fetcher.

Pulls today's stories from authoritative outlets (RSS), filters per category
via keyword rules, scores them, deduplicates across all categories, and writes
data/news.json with the **5 highest-scoring stories of the day** total — one
hero (lead) + four briefs. Each item carries its category tag.

Score model
-----------
score = authority * recency_decay * (1 + ln(1 + cluster_size)) * keyword_strength

  authority      — weight assigned per source (Reuters/AP/AFP top, BBC/AJ/DW
                   editorial, UN/IMF/ECB/NASA/USGS/IEA primary)
  recency_decay  — exp(-age_hours / 24); story 24h old halves to ~0.37
  cluster_size   — number of items judged "the same story" via fuzzy title
                   matching; rewards cross-source coverage
  keyword_strength — # category-specific keyword hits in title/summary

Dedup
-----
Two items are merged if rapidfuzz.fuzz.token_set_ratio(title_a, title_b) >= 78.
Cluster keeps the highest-scoring item; cluster_size is recorded.

Hero & briefs
-------------
The hero item is the single highest-scoring item across all categories.
The next four items (regardless of category) become the briefs grid. We keep
a per-category cap so a single huge cluster cannot crowd out the brief.

The script is best-effort: a feed that 4xx/5xxs is logged and skipped; a fully
empty fetch falls back to data/news.seed.json so the dashboard never breaks.
"""
from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import feedparser
import requests
from dateutil import parser as dateparser
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_FILE = DATA_DIR / "news.json"
SEED_FILE = DATA_DIR / "news.seed.json"

USER_AGENT = "DailyBriefBot/1.0 (+https://github.com/mramundo/daily-brief)"
HTTP_TIMEOUT = 20
TOTAL_STORIES = 5            # 1 hero + 4 briefs
MAX_PER_CATEGORY = 2         # don't let one topic dominate the brief
MAX_AGE_HOURS = 36           # ignore items older than this
DEDUP_THRESHOLD = 78         # fuzz token-set-ratio threshold

# Authority weights — primary wires + national broadcasters + official bodies.
AUTHORITY: dict[str, float] = {
    # Wires
    "Reuters": 1.00, "AP News": 1.00, "AFP": 0.98, "Bloomberg": 0.95,
    # Broadcasters / dailies
    "BBC World": 0.92, "BBC Science": 0.92, "BBC Business": 0.92, "BBC Tech": 0.90,
    "Al Jazeera": 0.85, "Deutsche Welle": 0.85, "France 24": 0.82,
    "Nikkei Asia": 0.85, "The Guardian": 0.82,
    # Tech press
    "The Verge": 0.80, "TechCrunch": 0.78, "Ars Technica": 0.82,
    # Official / institutional
    "UN News": 0.95, "IMF": 0.95, "ECB": 0.95, "Federal Reserve": 0.95,
    "NASA": 0.95, "ESA": 0.92, "USGS": 0.92, "IEA": 0.92,
    "ACLED": 0.90,
    # Default fallback
    "_default": 0.70,
}

# Per-category feed list. Each entry: (feed_url, source_label).
# Mix of editorial broadcasters + Google News mirrors of wires that no longer
# expose direct RSS (Reuters, AP, AFP). Mirrors give us the headline + link
# back to the original wire — good enough for ranking and display.
def _gnews(query: str, when_days: int = 1) -> str:
    q = requests.utils.quote(f"{query} when:{when_days}d")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

FEEDS: dict[str, list[tuple[str, str]]] = {
    "politics": [
        (_gnews("site:reuters.com world OR politics"), "Reuters"),
        (_gnews("site:apnews.com world OR politics"), "AP News"),
        (_gnews("site:afp.com world"), "AFP"),
        ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
        ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
        ("https://rss.dw.com/rdf/rss-en-all", "Deutsche Welle"),
        ("https://www.france24.com/en/rss", "France 24"),
        ("https://news.un.org/feed/subscribe/en/news/all/rss.xml", "UN News"),
    ],
    "finance": [
        (_gnews("site:reuters.com markets OR economy OR central bank"), "Reuters"),
        (_gnews("site:apnews.com economy OR markets OR earnings"), "AP News"),
        (_gnews("site:bloomberg.com markets"), "Bloomberg"),
        ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business"),
        ("https://www.imf.org/en/News/RSS?Language=ENG", "IMF"),
        ("https://www.ecb.europa.eu/rss/press.html", "ECB"),
        ("https://asia.nikkei.com/rss/feed/nar", "Nikkei Asia"),
    ],
    "conflicts": [
        (_gnews("site:reuters.com Ukraine OR Gaza OR Sudan OR conflict OR ceasefire"), "Reuters"),
        (_gnews("site:apnews.com conflict OR ceasefire OR airstrike OR offensive"), "AP News"),
        (_gnews("site:afp.com conflict OR war OR ceasefire"), "AFP"),
        ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
        ("https://www.aljazeera.com/xml/rss/all.xml", "Al Jazeera"),
        ("https://news.un.org/feed/subscribe/en/news/all/rss.xml", "UN News"),
        ("https://acleddata.com/feed/", "ACLED"),
    ],
    "science": [
        (_gnews("site:reuters.com science OR research OR study"), "Reuters"),
        ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "BBC Science"),
        ("https://www.nasa.gov/news-release/feed/", "NASA"),
        ("https://www.esa.int/rssfeed/Our_Activities", "ESA"),
        ("https://www.usgs.gov/news/feed.xml", "USGS"),
        (_gnews("site:nature.com news"), "Nature"),
    ],
    "resources": [
        (_gnews("site:reuters.com energy OR commodities OR oil OR lng OR copper OR lithium"), "Reuters"),
        (_gnews("site:apnews.com oil OR energy OR commodities"), "AP News"),
        ("https://www.iea.org/news/rss", "IEA"),
        (_gnews("OPEC OR \"rare earths\" OR \"critical minerals\""), "Wires"),
        ("https://feeds.bbci.co.uk/news/business/rss.xml", "BBC Business"),
    ],
    "tech": [
        (_gnews("site:reuters.com technology OR AI OR chip OR semiconductor"), "Reuters"),
        (_gnews("site:apnews.com technology OR AI"), "AP News"),
        (_gnews("site:bloomberg.com technology"), "Bloomberg"),
        ("https://feeds.bbci.co.uk/news/technology/rss.xml", "BBC Tech"),
        ("https://www.theverge.com/rss/index.xml", "The Verge"),
        ("https://techcrunch.com/feed/", "TechCrunch"),
        ("https://feeds.arstechnica.com/arstechnica/technology-lab", "Ars Technica"),
        (_gnews("\"OpenAI\" OR \"Anthropic\" OR \"NVIDIA\" OR \"Google DeepMind\""), "Wires"),
    ],
}

# Category keyword filters — applied to (title + summary). Hits also boost
# the keyword_strength multiplier in the scoring formula.
KEYWORDS: dict[str, list[str]] = {
    "politics": [
        "election", "president", "prime minister", "parliament", "summit",
        "diplomat", "treaty", "sanction", "embassy", "coup", "minister",
        "geopolitic", "alliance", "g7", "g20", "nato", "eu ", "european union",
        "white house", "kremlin", "beijing", "washington",
    ],
    "finance": [
        "fed", "federal reserve", "rate", "inflation", "cpi", "ppi", "yield",
        "bond", "treasury", "stocks", "equities", "earnings", "imf", "ecb",
        "central bank", "currency", "fx", "forex", "recession", "gdp",
        "market", "wall street", "nasdaq", "s&p",
    ],
    "conflicts": [
        "war", "conflict", "ceasefire", "airstrike", "offensive", "missile",
        "drone strike", "front line", "casualt", "killed", "wounded",
        "ukraine", "russia", "gaza", "israel", "hamas", "lebanon", "houthi",
        "sudan", "yemen", "syria", "myanmar", "armed", "militia", "hostilit",
        "battle", "siege", "naval", "coalition",
    ],
    "science": [
        "research", "study", "scientist", "nasa", "esa", "telescope", "space",
        "mars", "moon", "satellite", "physics", "biology", "genome",
        "microbiome", "fusion", "quantum", "ai model", "vaccine", "trial",
        "earthquake", "volcano", "climate", "ipcc", "discovery",
    ],
    "resources": [
        "oil", "crude", "wti", "brent", "opec", "lng", "gas pipeline",
        "uranium", "lithium", "copper", "nickel", "cobalt", "rare earth",
        "critical mineral", "gold", "silver", "platinum", "wheat", "corn",
        "commodity", "commodities", "supply chain", "stockpile", "barrel",
    ],
    "tech": [
        "ai", "artificial intelligence", "machine learning", "llm",
        "openai", "anthropic", "deepmind", "gemini", "chatgpt", "claude",
        "nvidia", "amd", "intel", "tsmc", "semiconductor", "chip", "gpu",
        "cloud", "aws", "azure", "google cloud", "datacenter", "data center",
        "startup", "venture", "funding round", "ipo", "acquisition",
        "smartphone", "iphone", "android", "apple", "microsoft", "alphabet",
        "meta", "amazon", "tesla", "robotics", "autonomous", "quantum",
        "cybersecurity", "breach", "ransomware", "open source", "kernel",
    ],
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fetch_news")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published_at: str
    summary: str = ""
    category: str = ""
    score: float = 0.0
    cluster_size: int = 1
    keyword_hits: int = 0

    def for_payload(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": self.published_at,
            "summary": self.summary,
            "category": self.category,
            "score": round(self.score, 3),
        }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch_feed(url: str) -> feedparser.FeedParserDict | None:
    """Fetch + parse a feed; return None on error so callers can skip cleanly."""
    try:
        resp = requests.get(url, timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        return feedparser.parse(resp.content)
    except Exception as exc:
        log.warning("feed failed %s: %s", url, exc)
        return None

def parse_dt(entry) -> datetime | None:
    """Best-effort extraction of a published datetime from a feed entry."""
    for key in ("published", "updated", "pubDate", "created"):
        val = entry.get(key)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    # Fallback to feedparser's struct_time
    pp = entry.get("published_parsed") or entry.get("updated_parsed")
    if pp:
        return datetime.fromtimestamp(time.mktime(pp), tz=timezone.utc)
    return None

def clean_text(t: str) -> str:
    """Decode HTML entities and collapse whitespace."""
    if not t:
        return ""
    # Strip any leftover HTML tags first, then decode entities (twice for
    # double-encoded feeds like `&amp;nbsp;`).
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(html.unescape(t))
    # Replace non-breaking spaces and other unicode whitespace with regular spaces.
    t = t.replace("\u00a0", " ").replace("\u200b", "")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def normalise_title(t: str) -> str:
    """Normalise a title for keyword matching and dedup hashing."""
    t = clean_text(t)
    # Strip Google News' "Title - Source" suffix if present.
    t = re.sub(r"\s+-\s+[^-]+$", "", t)
    return t

def detect_source_from_entry(entry, default_source: str) -> str:
    """Use Google News' embedded source when available; else fall back."""
    src = (entry.get("source") or {}).get("title") if isinstance(entry.get("source"), dict) else None
    if not src:
        # Some feeds put source in "author" or in the title suffix.
        suffix = re.search(r"-\s+([^-]+)$", entry.get("title", ""))
        if suffix:
            src = suffix.group(1).strip()
    if not src:
        return default_source
    # Map well-known mirror names back to canonical labels.
    norm = src.strip()
    aliases = {
        "Reuters": "Reuters", "Associated Press": "AP News", "AP": "AP News",
        "AFP": "AFP", "Agence France-Presse": "AFP",
        "Bloomberg": "Bloomberg", "BBC News": "BBC World", "BBC": "BBC World",
        "Al Jazeera English": "Al Jazeera", "Al Jazeera": "Al Jazeera",
        "Deutsche Welle": "Deutsche Welle", "DW": "Deutsche Welle",
        "France 24": "France 24",
        "The Guardian": "The Guardian", "Guardian": "The Guardian",
        "Nikkei Asia": "Nikkei Asia",
        "United Nations": "UN News", "UN News": "UN News",
        "Nature": "Nature",
    }
    return aliases.get(norm, norm or default_source)

def authority_for(source: str) -> float:
    return AUTHORITY.get(source, AUTHORITY["_default"])

def keyword_hits(text: str, keywords: list[str]) -> int:
    if not text:
        return 0
    t = text.lower()
    return sum(1 for k in keywords if k in t)

def recency_decay(published: datetime | None, now: datetime) -> float:
    if not published:
        return 0.5  # unknown → middling weight
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    if age_hours > MAX_AGE_HOURS:
        return 0.0
    # 24h half-life: exp(-age / 24)
    return math.exp(-age_hours / 24.0)

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def collect_category(category: str, now: datetime) -> list[NewsItem]:
    feeds = FEEDS.get(category, [])
    keywords = KEYWORDS.get(category, [])
    items: list[NewsItem] = []

    for feed_url, default_source in feeds:
        parsed = fetch_feed(feed_url)
        if not parsed or not parsed.entries:
            continue

        for entry in parsed.entries[:40]:
            title = normalise_title(entry.get("title", ""))
            if not title:
                continue
            url = entry.get("link") or ""
            if not url:
                continue
            published = parse_dt(entry)
            decay = recency_decay(published, now)
            if decay <= 0:
                continue

            summary = clean_text(entry.get("summary", ""))
            haystack = f"{title} {summary}".lower()
            hits = keyword_hits(haystack, keywords)

            # Drop items with zero keyword overlap UNLESS source is an
            # institutional one (UN/IMF/ECB/NASA/...) where the whole feed
            # is already on-topic.
            source = detect_source_from_entry(entry, default_source)
            inst = source in ("UN News", "IMF", "ECB", "Federal Reserve",
                              "NASA", "ESA", "USGS", "IEA", "ACLED")
            if hits == 0 and not inst:
                continue

            authority = authority_for(source)
            base = authority * decay
            keyword_strength = 1.0 + 0.25 * hits
            score = base * keyword_strength  # cluster_size folded in later

            items.append(NewsItem(
                title=title,
                url=url,
                source=source,
                published_at=published.isoformat() if published else "",
                summary=summary[:280].strip(),
                category=category,
                score=score,
                keyword_hits=hits,
            ))
    log.info("[%s] collected %d raw items", category, len(items))
    return items

def dedup_cluster(items: list[NewsItem]) -> list[NewsItem]:
    """Merge near-duplicate titles. Keep the highest-scoring representative;
    record cluster_size. Final score = repr.score * (1 + ln(1+cluster_size))."""
    items = sorted(items, key=lambda i: i.score, reverse=True)
    kept: list[NewsItem] = []
    for it in items:
        merged = False
        for k in kept:
            if fuzz.token_set_ratio(it.title, k.title) >= DEDUP_THRESHOLD:
                k.cluster_size += 1
                merged = True
                break
        if not merged:
            kept.append(it)
    for k in kept:
        k.score = k.score * (1.0 + math.log(1.0 + k.cluster_size))
    kept.sort(key=lambda i: i.score, reverse=True)
    return kept

def build_payload() -> dict:
    now = datetime.now(timezone.utc)

    # Per-category fetch + intra-category dedup. Cap each category so one
    # topic cannot monopolise the brief; final cross-category dedup runs after.
    pool: list[NewsItem] = []
    per_cat_kept: dict[str, int] = {}
    for cat in FEEDS:
        raw = collect_category(cat, now)
        clustered = dedup_cluster(raw)[:max(TOTAL_STORIES, 4)]
        per_cat_kept[cat] = len(clustered)
        pool.extend(clustered)
        log.info("[%s] kept %d after intra dedup", cat, len(clustered))

    # Cross-category dedup so the same wire story doesn't appear under two cats.
    pool = dedup_cluster(pool)

    # Pick top stories with a per-category cap.
    pool.sort(key=lambda i: i.score, reverse=True)
    picked: list[NewsItem] = []
    cat_count: dict[str, int] = {}
    for it in pool:
        if cat_count.get(it.category, 0) >= MAX_PER_CATEGORY:
            continue
        picked.append(it)
        cat_count[it.category] = cat_count.get(it.category, 0) + 1
        if len(picked) >= TOTAL_STORIES:
            break

    # Backfill if cap left us short (e.g. only 2 categories produced anything).
    if len(picked) < TOTAL_STORIES:
        seen_urls = {p.url for p in picked}
        for it in pool:
            if it.url in seen_urls:
                continue
            picked.append(it)
            if len(picked) >= TOTAL_STORIES:
                break

    payload = {
        "updated": now.isoformat(),
        "hero": None,
        "items": [],
    }

    if picked:
        hero_item = picked[0]
        payload["hero"] = {
            **hero_item.for_payload(),
            "summary": hero_item.summary or
                "A focused recap of the day's biggest story, sourced from authoritative outlets.",
        }
        payload["items"] = [it.for_payload() for it in picked[1:TOTAL_STORIES]]

    return payload

def write_output(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("wrote %s", OUT_FILE)

TARGET_HOUR_ROME = 8

def main() -> int:
    # Cron fires at both 06:00 and 07:00 UTC year-round to cover DST; only
    # the firing where Europe/Rome is at TARGET_HOUR_ROME actually runs.
    # Manual workflow_dispatch sets SKIP_TIME_GUARD=1 to bypass.
    if os.getenv("SKIP_TIME_GUARD") != "1" and os.getenv("GITHUB_EVENT_NAME") == "schedule":
        rome_hour = datetime.now(ZoneInfo("Europe/Rome")).hour
        if rome_hour != TARGET_HOUR_ROME:
            log.info("skipping: Rome hour=%s, target=%s", rome_hour, TARGET_HOUR_ROME)
            return 0

    try:
        payload = build_payload()
        if payload.get("hero") is None and not payload.get("items"):
            log.warning("no items collected; leaving previous data in place")
            return 0
        write_output(payload)
        return 0
    except Exception as exc:
        log.exception("fatal: %s", exc)
        return 1

if __name__ == "__main__":
    sys.exit(main())
