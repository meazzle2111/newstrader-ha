"""Schlagzeilen pro Ticker aus Yahoo Finance (yfinance-News + RSS)."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import requests
import yfinance as yf

log = logging.getLogger(__name__)

RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US"
GOOGLE_NEWS_DE = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (newstrader paper bot)"}


@dataclass
class Headline:
    symbol: str
    title: str
    summary: str
    source: str
    published: datetime

    @property
    def id(self) -> str:
        return hashlib.sha1(self.title.strip().lower().encode()).hexdigest()[:16]

    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.published).total_seconds() / 3600


def _from_yfinance(symbol: str) -> list[Headline]:
    out = []
    try:
        for item in yf.Ticker(symbol).news or []:
            c = item.get("content", item)
            title = c.get("title")
            ts = c.get("pubDate") or c.get("displayTime")
            if not title or not ts:
                continue
            out.append(Headline(
                symbol, title, (c.get("summary") or "")[:400],
                (c.get("provider") or {}).get("displayName", "Yahoo"),
                datetime.fromisoformat(ts.replace("Z", "+00:00")),
            ))
    except Exception as e:
        log.debug("yfinance-News für %s fehlgeschlagen: %s", symbol, e)
    return out


def _from_rss(symbol: str) -> list[Headline]:
    out = []
    try:
        resp = requests.get(RSS_URL.format(symbol=symbol), timeout=10, headers=HEADERS)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
        for e in feed.entries:
            if not e.get("title") or not e.get("published"):
                continue
            out.append(Headline(
                symbol, e.title, (e.get("summary") or "")[:400], "Yahoo RSS",
                parsedate_to_datetime(e.published).astimezone(timezone.utc),
            ))
    except Exception as e:
        log.debug("RSS für %s fehlgeschlagen: %s", symbol, e)
    return out


def _from_google_news_de(symbol: str, name: str) -> list[Headline]:
    """Deutschsprachige Schlagzeilen (Börsenportale, Handelsblatt, ...) über Google News."""
    out = []
    word = "Kurs" if symbol.endswith("-EUR") else "Aktie"
    params = {"q": f'"{name}" {word} when:2d', "hl": "de", "gl": "DE", "ceid": "DE:de"}
    try:
        resp = requests.get(GOOGLE_NEWS_DE, params=params, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        for e in feedparser.parse(resp.content).entries:
            if not e.get("title") or not e.get("published"):
                continue
            source = (e.get("source") or {}).get("title", "Google News")
            title = e.title
            if title.endswith(f" - {source}"):  # Google hängt die Quelle an den Titel
                title = title[: -len(source) - 3]
            out.append(Headline(symbol, title, "", source,
                                parsedate_to_datetime(e.published).astimezone(timezone.utc)))
    except Exception as e:
        log.debug("Google News für %s fehlgeschlagen: %s", symbol, e)
    return out


def get_headlines(symbol: str, max_age_hours: float, name: str | None = None,
                  german: bool = True) -> list[Headline]:
    """Neueste zuerst, ohne Duplikate, nur innerhalb des Zeitfensters."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items = _from_yfinance(symbol) + _from_rss(symbol)
    if german and name:
        items += _from_google_news_de(symbol, name)
    seen: dict[str, Headline] = {}
    for h in items:
        if h.published >= cutoff and h.id not in seen:
            seen[h.id] = h
    return sorted(seen.values(), key=lambda h: h.published, reverse=True)[:30]
