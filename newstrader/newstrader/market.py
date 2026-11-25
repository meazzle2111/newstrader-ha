"""Kursdaten (Yahoo Finance über yfinance), Währungsumrechnung und technisches Signal."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

CURRENCY_SYMBOL = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}


@dataclass
class Quote:
    symbol: str
    price: float            # in Depotwährung
    native_price: float     # in Handelswährung des Wertpapiers
    currency: str           # Handelswährung
    time: datetime          # Zeitpunkt des letzten Kurses (UTC)
    source: str = "Yahoo"
    tradable: bool | None = None   # bei Echtzeitquellen: Handelszeit? (sonst über das Kursalter entschieden)

    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.time).total_seconds() / 60


@dataclass
class TechSignal:
    score: float        # -1 (stark bärisch) .. +1 (stark bullisch)
    rsi: float
    trend: float        # Abstand Kurs zu SMA50
    momentum: float     # Rendite letzte 5 Tage
    sma_cross: float    # +1 wenn SMA20 > SMA50, sonst -1

    def describe(self) -> str:
        return (f"tech={self.score:+.2f} (RSI {self.rsi:.0f}, Trend {self.trend:+.1%}, "
                f"5T {self.momentum:+.1%}, SMA20{'>' if self.sma_cross > 0 else '<'}SMA50)")


# --------------------------------------------------------------------------- Währungen
_currency_cache: dict[str, str] = {}
_fx_cache: dict[tuple[str, str], tuple[float, float]] = {}
from .config import STATE_DIR  # noqa: E402
CACHE_DIR = STATE_DIR / "cache"
SUFFIX_CURRENCY = {".DE": "EUR", ".F": "EUR", ".PA": "EUR", ".AS": "EUR", ".MI": "EUR", ".MC": "EUR",
                   ".HE": "EUR", ".BR": "EUR", ".LS": "EUR", ".VI": "EUR",
                   "-EUR": "EUR", "-USD": "USD", ".L": "GBp", ".SW": "CHF", ".CO": "DKK", ".ST": "SEK", ".OL": "NOK"}


def get_currency(symbol: str) -> str:
    """Handelswährung – meist aus der Endung ablesbar (spart eine Yahoo-Anfrage pro Wert)."""
    if symbol in _currency_cache:
        return _currency_cache[symbol]
    cur = {"^GDAXI": "EUR", "^MDAXI": "EUR", "^STOXX50E": "EUR", "^TECDAX": "EUR"}.get(symbol)
    cur = cur or next((c for suf, c in SUFFIX_CURRENCY.items() if symbol.endswith(suf)), None)
    if cur is None and "." not in symbol and "-" not in symbol and "=" not in symbol:
        cur = "USD"  # US-Börse
    if cur is None:
        try:
            cur = yf.Ticker(symbol).fast_info["currency"]
        except Exception:
            cur = "USD"
    _currency_cache[symbol] = cur
    return cur


def _normalize(currency: str) -> tuple[str, float]:
    """Pence-Notierungen (GBp) usw. auf die Hauptwährung bringen."""
    if currency in ("GBp", "GBX"):
        return "GBP", 0.01
    if currency == "ILA":
        return "ILS", 0.01
    return currency.upper(), 1.0


def _fx_from_kraken(cur: str, base: str) -> float | None:
    """EUR/USD in Echtzeit von Kraken (unabhängig von Yahoo)."""
    if {cur, base} != {"USD", "EUR"}:
        return None
    import requests
    d = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": "EURUSD"}, timeout=8).json()
    t = next(iter(d["result"].values()))
    usd_per_eur = (float(t["a"][0]) + float(t["b"][0])) / 2
    return 1 / usd_per_eur if base == "EUR" else usd_per_eur


def _fx_disk(key: tuple[str, str], value: float | None = None) -> float | None:
    path = CACHE_DIR / "fx.json"
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    k = f"{key[0]}{key[1]}"
    if value is None:
        return data.get(k)
    data[k] = value
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return value


def fx_rate(currency: str, base: str) -> float:
    """Wie viel `base` ist eine Einheit `currency` wert? Kraken → Yahoo → letzter bekannter Kurs."""
    cur, factor = _normalize(currency)
    if cur == base:
        return factor
    key = (cur, base)
    cached = _fx_cache.get(key)
    if cached and time.time() - cached[1] < 300:
        return cached[0] * factor
    rate = None
    try:
        rate = _fx_from_kraken(cur, base)
    except Exception as e:
        log.debug("Kraken-Wechselkurs fehlgeschlagen: %s", e)
    if rate is None:
        try:
            hist = yf.Ticker(f"{base}{cur}=X").history(period="5d", interval="1h")
            if not hist.empty:
                rate = 1.0 / float(hist["Close"].iloc[-1])  # Yahoo liefert cur pro base
        except Exception as e:
            log.debug("Yahoo-Wechselkurs fehlgeschlagen: %s", e)
    if rate is None:
        rate = (cached[0] if cached else None) or _fx_disk(key)
        if rate is None:
            raise RuntimeError(f"Kein Wechselkurs {base}/{cur}")
        log.warning("Wechselkurs %s/%s nicht abrufbar – nutze letzten bekannten Wert", base, cur)
    else:
        _fx_disk(key, rate)
    _fx_cache[key] = (rate, time.time())
    return rate * factor


def fx_history(currency: str, base: str, period: str) -> pd.Series | None:
    """Tägliche Umrechnungskurse (base pro Einheit currency) – für den Backtest."""
    cur, factor = _normalize(currency)
    if cur == base:
        return None
    hist = yf.Ticker(f"{base}{cur}=X").history(period=period, interval="1d")
    s = (1.0 / hist["Close"].dropna()) * factor
    s.index = s.index.tz_localize(None).normalize()  # Handelstag in Ortszeit der Börse
    return s


# --------------------------------------------------------------------------- Kurse
def get_quote(symbol: str, base: str, cfg=None) -> Quote | None:
    """Echtzeitkurs (Tradegate/Kraken), sonst letzter Yahoo-Kurs (Xetra ca. 15 Min. verzögert)."""
    if cfg is not None:
        from .realtime import get_live_quote, has_realtime
        if has_realtime(cfg, symbol):
            live = get_live_quote(cfg, symbol)
            if live is not None:
                cur = get_currency(symbol)
                rate = fx_rate(cur, base) if cur != base else 1.0
                return Quote(symbol, live.price, live.price / rate, cur, live.time, live.source, live.tradable)
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="1d", interval="1m")
        if hist.empty:
            hist = t.history(period="5d", interval="15m")
        if hist.empty:
            return None
        ts = hist.index[-1].to_pydatetime().astimezone(timezone.utc)
        native = float(hist["Close"].iloc[-1])
        cur = get_currency(symbol)
        return Quote(symbol, native * fx_rate(cur, base), native, cur, ts)
    except Exception as e:  # Netzwerk, Yahoo-Ratelimit, ...
        log.warning("Kurs für %s nicht abrufbar: %s", symbol, e)
        return None


_closes_cache: dict[str, tuple[float, pd.Series]] = {}
_closes_lock = threading.Lock()


def get_daily_closes(symbol: str, period: str = "1y", max_age_minutes: float = 120) -> pd.Series | None:
    """Tagesschlusskurse, zwischengespeichert (ändern sich nur einmal am Tag; der aktuelle Kurs kommt live dazu).

    Fällt Yahoo aus, werden die zuletzt geladenen Daten weiterverwendet – auch über einen Neustart hinweg.
    """
    with _closes_lock:
        hit = _closes_cache.get(symbol)
    if hit and time.time() - hit[0] < max_age_minutes * 60:
        return hit[1]
    disk = CACHE_DIR / "closes" / f"{symbol}.json"
    if hit is None and disk.exists():
        try:
            d = json.loads(disk.read_text())
            series = pd.Series(d["close"], index=pd.to_datetime(d["date"]))
            hit = (d["ts"], series)
            if time.time() - d["ts"] < max_age_minutes * 60:
                with _closes_lock:
                    _closes_cache[symbol] = hit
                return series
        except Exception:
            hit = None
    try:
        hist = yf.Ticker(symbol).history(period=period, interval="1d")
        if hist.empty:
            raise RuntimeError("keine Daten")
        series = hist["Close"].dropna()
        series.index = series.index.tz_localize(None).normalize()
        with _closes_lock:
            _closes_cache[symbol] = (time.time(), series)
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_text(json.dumps({"ts": time.time(), "date": [d.strftime("%Y-%m-%d") for d in series.index],
                                    "close": [round(float(v), 6) for v in series.values]}))
        return series
    except Exception as e:
        if hit:
            log.info("Historie für %s nicht abrufbar (%s) – nutze Daten von vor %.0f Min.",
                     symbol, e, (time.time() - hit[0]) / 60)
            return hit[1]
        log.warning("Historie für %s nicht abrufbar: %s", symbol, e)
        return None


def rsi(closes: pd.Series, n: int = 14) -> float:
    delta = closes.diff().dropna()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 0 else np.inf
    return float(100 - 100 / (1 + rs))


def technical_signal(closes: pd.Series, last_price: float | None = None) -> TechSignal | None:
    """Einfacher Mix aus Trend, Momentum und RSI. Bewusst simpel und nachvollziehbar.

    `closes` und `last_price` müssen in derselben Währung sein (live: Handelswährung).
    """
    if closes is None or len(closes) < 55:
        return None
    closes = closes.copy()
    if last_price is not None:
        closes.iloc[-1] = last_price  # heutigen Tagesschluss durch aktuellen Kurs ersetzen

    price = closes.iloc[-1]
    sma20 = closes.rolling(20).mean().iloc[-1]
    sma50 = closes.rolling(50).mean().iloc[-1]
    trend = price / sma50 - 1
    momentum = price / closes.iloc[-6] - 1
    r = rsi(closes)
    cross = 1.0 if sma20 > sma50 else -1.0

    trend_c = np.tanh(trend / 0.05)          # ±5 % vom SMA50 ≈ volles Signal
    mom_c = np.tanh(momentum / 0.04)
    # RSI: überverkauft (<30) leicht bullisch, überkauft (>70) leicht bärisch
    rsi_c = float(np.clip((50 - r) / 25, -1, 1))

    score = 0.35 * trend_c + 0.25 * mom_c + 0.2 * rsi_c + 0.2 * cross
    return TechSignal(float(np.clip(score, -1, 1)), r, float(trend), float(momentum), cross)
