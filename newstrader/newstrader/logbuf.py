"""Log für die Oberfläche: die letzten Einträge im Speicher, je Eintrag Zeit, Stufe, Bereich und Text.

Beim Start werden die letzten Zeilen aus bot.log übernommen, damit die Log-Seite nach einem Neustart nicht leer ist.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from pathlib import Path

MAX = 3000
AREAS = {"bot": "Bot", "depot": "Depot", "ki": "KI", "tr": "Trade Republic", "spek": "Spekulation",
         "daten": "Kursdaten", "system": "System"}
_AREA_OF = {
    "bot": "bot", "strategy": "bot", "broker": "bot", "learning": "bot",
    "manager": "depot", "allocator": "depot", "portfolio": "depot", "portfolio_service": "depot",
    "pf_learning": "depot", "review": "depot", "mydepot": "depot", "reports": "depot",
    "analyst": "ki", "gemini": "ki",
    "traderepublic": "tr",
    "spekulation": "spek",
    "market": "daten", "realtime": "daten", "news": "daten",
}
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_buf: deque[dict] = deque(maxlen=MAX)
_lock = threading.Lock()
_seq = [0]


def area_of(name: str) -> str:
    if name == "newstrader":
        return "bot"
    if name.startswith("newstrader."):
        return _AREA_OF.get(name.split(".", 2)[1], "system")
    if name.startswith("pytr"):
        return "tr"
    return "system" if name else ""


def _guess(msg: str) -> str:
    """Ältere Logzeilen ohne Absender: Bereich am Text erkennen."""
    low = msg.lower()
    for area, words in (("ki", ("gemini", "claude", "ki-")), ("tr", ("trade republic", "tr-", "knock-out-suche")),
                        ("spek", ("spekulation", "hebel-vorschlag", "vorauswahl")),
                        ("depot", ("portfolio", "depot", "empfehlung")), ("daten", ("tradegate", "kurs"))):
        if any(w in low for w in words):
            return area
    return "bot"


def _add(ts: float, level: str, name: str, msg: str) -> None:
    with _lock:
        _seq[0] += 1
        _buf.append({"seq": _seq[0], "ts": round(ts, 3), "lvl": level, "area": area_of(name) or _guess(msg), "msg": msg})


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + logging.Formatter().formatException(record.exc_info)
            _add(record.created, record.levelname, record.name, msg)
        except Exception:  # noqa: BLE001 – Logging darf nie den Bot stören
            pass


_LINE = re.compile(r"^(\d\d)\.(\d\d)\. (\d\d):(\d\d):(\d\d) (DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+(?:\[([\w.]+)\] )?(.*)$")


_prefilled = [False]


def prefill(path: Path, max_bytes: int = 400_000, now: float | None = None, force: bool = False) -> int:
    """Letzte Zeilen aus der Logdatei übernehmen (Format: „25.11. 12:22:17 INFO    [newstrader.x] Text“).
    Nur einmal pro Prozess – sonst stünden die alten Zeilen doppelt da (die Einrichtung läuft beim Add-on zweimal)."""
    if (_prefilled[0] and not force) or not path.exists():
        return 0
    _prefilled[0] = True
    with path.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - max_bytes))
        lines = f.read().decode("utf-8", errors="replace").splitlines()
    now = now or time.time()
    year = time.localtime(now).tm_year
    rows: list[list] = []
    for line in lines:
        m = _LINE.match(line)
        if not m:
            if rows and line.strip():
                rows[-1][3] += "\n" + line   # Fortsetzung, z. B. Traceback
            continue
        d, mo, h, mi, s, lvl, name, msg = m.groups()
        try:
            ts = time.mktime((year, int(mo), int(d), int(h), int(mi), int(s), 0, 0, -1))
        except (OverflowError, ValueError):
            continue
        if ts > now + 86400:           # Eintrag vom Vorjahr (Jahreswechsel)
            ts = time.mktime((year - 1, int(mo), int(d), int(h), int(mi), int(s), 0, 0, -1))
        rows.append([ts, lvl, name or "", msg])
    for ts, lvl, name, msg in rows[-MAX:]:
        _add(ts, lvl, name, msg)
    return len(rows)


def entries(after: int = 0, limit: int = MAX) -> list[dict]:
    with _lock:
        out = [e for e in _buf if e["seq"] > after]
    return out[-limit:]


def error_times(hours: float = 24, limit: int = 99) -> list[float]:
    """Zeitpunkte der Fehler (ERROR/CRITICAL) der letzten Stunden – für das Zeichen am Menüpunkt „Log“."""
    since = time.time() - hours * 3600
    with _lock:
        out = [e["ts"] for e in _buf if e["ts"] >= since and LEVELS.get(e["lvl"], 0) >= 40]
    return out[-limit:]


def last_seq() -> int:
    return _seq[0]


def clear() -> None:
    with _lock:
        _buf.clear()
