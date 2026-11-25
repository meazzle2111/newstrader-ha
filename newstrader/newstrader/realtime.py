"""Echtzeitkurse: Tradegate (Aktien in Euro, 8–22 Uhr) und Kraken (Krypto, rund um die Uhr).

Yahoo liefert Xetra-Kurse nur ~15 Minuten verzögert. Tradegate ist ein Berliner Börsenplatz, den viele
deutsche Neobroker nutzen; dort werden auch US-Aktien in Euro gehandelt. Die Kurse kommen von der öffentlichen
Kursanzeige der Tradegate-Website und werden deshalb zurückhaltend abgefragt (Zwischenspeicher, nur bei Bedarf).

Jeder neue Kurs wird in state/ticks/<symbol>.csv mitgeschrieben, damit der Chart den Echtzeit-Verlauf zeigen kann.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)

TICKS_DIR = STATE_DIR / "ticks"
BERLIN = ZoneInfo("Europe/Berlin")
HEADERS = {"User-Agent": "Mozilla/5.0 (newstrader paper bot)"}
TRADEGATE_URL = "https://www.tradegatebsx.com/refresh.php"          # tradegate.de leitet hierhin um
TRADEGATE_TRADES_URL = "https://www.tradegatebsx.com/umsaetze.php"  # alle Umsätze des Tages (seitenweise)
KRAKEN_URL = "https://api.kraken.com/0/public/Ticker"

_cache: dict[str, tuple[float, "LiveQuote | None"]] = {}
_last_tick: dict[str, float] = {}
_lock = threading.Lock()


@dataclass
class LiveQuote:
    symbol: str
    price: float        # Mittelkurs aus Geld/Brief, in Euro
    bid: float
    ask: float
    time: datetime      # Abrufzeit (UTC) – die Kurse sind Echtzeit
    source: str         # "Tradegate" | "Kraken"
    tradable: bool      # Handelszeit?

    def as_json(self) -> dict:
        return {"symbol": self.symbol, "price": self.price, "bid": self.bid, "ask": self.ask,
                "time": self.time.isoformat(timespec="seconds"), "source": self.source, "tradable": self.tradable}


def _num(v) -> float:
    """Tradegate liefert mal 37.4, mal "37,40" oder "1.234,50"."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("+", "").replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def tradegate_open(now: datetime | None = None) -> bool:
    t = (now or datetime.now(timezone.utc)).astimezone(BERLIN)
    return t.weekday() < 5 and 8 <= t.hour < 22


def has_realtime(cfg: Config, symbol: str) -> bool:
    return symbol in cfg.isins or symbol in cfg.kraken_pairs


def _fetch_tradegate(symbol: str, isin: str) -> LiveQuote | None:
    r = requests.get(TRADEGATE_URL, params={"isin": isin}, headers=HEADERS, timeout=8)
    r.raise_for_status()
    d = r.json()
    bid, ask = d.get("bid"), d.get("ask")
    try:
        bid, ask = _num(bid), _num(ask)
        price = (bid + ask) / 2 if bid > 0 and ask > 0 else _num(d.get("last"))
    except (TypeError, ValueError):
        price = _num(d.get("last"))
        bid = ask = price
    now = datetime.now(timezone.utc)
    return LiveQuote(symbol, price, bid, ask, now, "Tradegate", tradegate_open(now))


_intraday: dict[str, dict] = {}
_intraday_lock = threading.Lock()


def tradegate_intraday(isin: str, max_pages: int = 15, min_age: float = 30.0) -> list[list] | None:
    """Heutige Umsätze auf Tradegate als [[Zeit UTC, Kurs €], …] – für den Tagesverlauf ohne Yahoo-Verzögerung.

    Abgefragt wird nur nachgeladen, was seit dem letzten Abruf dazukam (höchstens alle 30 s je Wert).
    """
    today = datetime.now(BERLIN).date().isoformat()   # Tradegate rechnet in deutscher Ortszeit
    with _intraday_lock:
        st = _intraday.get(isin)
        if not st or st["date"] != today:
            st = _intraday[isin] = {"date": today, "last": None, "rows": [], "fetched": 0.0}
        if time.time() - st["fetched"] < min_age:
            return st["rows"]
        try:
            for _ in range(max_pages):
                params = {"isin": isin} if st["last"] is None else {"isin": isin, "id": st["last"]}
                r = requests.get(TRADEGATE_TRADES_URL, params=params, headers=HEADERS, timeout=8)
                r.raise_for_status()
                d = r.json()
                if not d:
                    break
                for x in d:
                    if x.get("time") and x.get("price") is not None and x.get("date") == today:
                        local = datetime.fromisoformat(f"{x['date']}T{x['time'][:8]}").replace(tzinfo=BERLIN)
                        st["rows"].append([local.astimezone(timezone.utc).isoformat(timespec="seconds"), _num(x["price"])])
                newest = max(int(x["id"]) for x in d)
                if st["last"] is not None and newest <= st["last"]:
                    break
                st["last"] = newest
                if len(d) < 1000:
                    break
        except Exception as e:  # noqa: BLE001 – dann eben Yahoo
            log.debug("Tradegate-Umsätze %s: %s", isin, e)
            return st["rows"] or None
        st["fetched"] = time.time()
        return st["rows"]


def _fetch_kraken(symbol: str, pair: str) -> LiveQuote | None:
    r = requests.get(KRAKEN_URL, params={"pair": pair}, timeout=8)
    r.raise_for_status()
    d = r.json()
    if d.get("error"):
        raise RuntimeError(d["error"])
    t = next(iter(d["result"].values()))
    bid, ask = float(t["b"][0]), float(t["a"][0])
    return LiveQuote(symbol, (bid + ask) / 2, bid, ask, datetime.now(timezone.utc), "Kraken", True)


def _record_tick(q: LiveQuote) -> None:
    """Neuen Kurs anhängen (nur bei Änderung oder spätestens jede Minute)."""
    prev = _last_tick.get(q.symbol)
    now = time.time()
    key = f"{q.symbol}#t"
    if prev == q.price and now - _last_tick.get(key, 0) < 60:
        return
    _last_tick[q.symbol] = q.price
    _last_tick[key] = now
    TICKS_DIR.mkdir(parents=True, exist_ok=True)
    with (TICKS_DIR / f"{q.symbol}.csv").open("a") as f:
        f.write(f"{q.time.isoformat(timespec='seconds')},{q.price:.6f}\n")


def get_live_quote(cfg: Config, symbol: str, max_age: float = 4.0) -> LiveQuote | None:
    """Echtzeitkurs mit kurzem Zwischenspeicher (mehrere Abfragen innerhalb weniger Sekunden teilen sich einen Abruf)."""
    with _lock:
        hit = _cache.get(symbol)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
    q = None
    try:
        if symbol in cfg.kraken_pairs:
            q = _fetch_kraken(symbol, cfg.kraken_pairs[symbol])
        elif symbol in cfg.isins:
            q = _fetch_tradegate(symbol, cfg.isins[symbol])
    except Exception as e:
        log.debug("Echtzeitkurs für %s nicht abrufbar: %s", symbol, e)
    with _lock:
        _cache[symbol] = (time.time(), q)
        if q is not None:
            _record_tick(q)
    return q


def read_ticks(symbol: str, since: datetime | None = None) -> list[list]:
    path = TICKS_DIR / f"{symbol}.csv"
    if not path.exists():
        return []
    out = []
    since_s = since.isoformat(timespec="seconds") if since else ""
    for line in path.read_text().splitlines()[-20000:]:
        ts, _, px = line.partition(",")
        if ts > since_s and px:
            out.append([ts, float(px)])
    return out
