"""Tägliche KI-Durchsicht des ganzen Portfolios (Mo–Fr 8:30): Zusammenhang statt Einzelaktien.

Die KI (Gemini, sonst Claude) sieht alle Positionen mit Signal, Nachrichtenlage, Gewicht und die Marktlage und gibt
je Position halten / reduzieren / verkaufen mit Begründung. „verkaufen“ wird nur zusammen mit einem negativen
Signal zur Empfehlung – die KI allein verkauft nichts.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
REVIEW_FILE = STATE_DIR / "review.json"
AT = (8, 30)
_running = {"on": False}

SYSTEM = """Du bist Portfoliomanager und schaust jeden Morgen vor Börsenstart auf ein privates Aktiendepot.
Du bekommst alle Positionen (Gewicht, Gewinn/Verlust, Bot-Signal −1…+1, Nachrichtenlage) und die Marktlage.
Beurteile das Depot als Ganzes: Klumpenrisiken (Branche, Land, gleiche Treiber), Positionen mit gekippter Story,
anstehende Risiken. Je Position: "halten", "reduzieren" oder "verkaufen" – nur bei klarem Grund nicht "halten".
summary: 2–3 deutsche Sätze für eine Push-Nachricht. Antworte nur im JSON-Format."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "positions": {"type": "array", "items": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}, "view": {"type": "string", "enum": ["halten", "reduzieren", "verkaufen"]},
                           "reason": {"type": "string"}},
            "required": ["symbol", "view", "reason"], "additionalProperties": False}},
    },
    "required": ["summary", "risks", "positions"],
    "additionalProperties": False,
}


def load() -> dict:
    try:
        return json.loads(REVIEW_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def sell_views() -> dict[str, str]:
    """Symbol -> Begründung, wo die heutige Durchsicht „verkaufen“ sagt."""
    r = load()
    if r.get("day") != datetime.now(pf.BERLIN).date().isoformat():
        return {}
    return {p["symbol"]: p["reason"] for p in r.get("positions", []) if p.get("view") == "verkaufen"}


def run(cfg: Config, analyst, notify) -> dict | None:
    from . import manager
    from .portfolio_service import overview
    state = manager.load_state()
    if not state.get("start_capital"):
        return None
    txns = pf.load_txns("model")
    ov = overview(cfg, manager.cash_of(state["start_capital"], txns), "model", txns)
    k = ov["kpi"]
    reg = manager._last_regime or {}
    lines = [f"Depot {k['total']:.0f} €, Cash {k.get('cash') or 0:.0f} €. Marktlage: {reg.get('text', 'unbekannt')}."]
    for p in ov["positions"]:
        lines.append(f"- {p['name']} ({p['symbol']}), {p.get('sector') or '?'}/{p.get('country') or '?'}: Gewicht "
                     f"{p['value'] / k['total']:.0%}, G/V {p['pnl_pct'] or 0:+.0%}, Signal "
                     f"{p['signal'] if p['signal'] is not None else 0:+.2f}; {p.get('signal_summary') or 'keine Meldungen'}")
    data, provider = analyst.ask_json(SYSTEM, "\n".join(lines), SCHEMA)
    rev = {**data, "day": datetime.now(pf.BERLIN).date().isoformat(), "provider": provider,
           "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    pf._atomic_write(REVIEW_FILE, rev)
    flagged = [p for p in rev.get("positions", []) if p.get("view") != "halten"]
    msg = rev["summary"] + ("\n" + "\n".join(f"• {p['symbol']}: {p['view']} – {p['reason']}" for p in flagged[:4]) if flagged else "")
    notify(cfg, "☀️ Depot-Durchsicht", msg)
    log.info("Depot-Durchsicht (%s): %d Hinweise", provider, len(flagged))
    return rev


def tick(cfg: Config, analyst, notify) -> None:
    now = datetime.now(pf.BERLIN)
    if analyst is None or now.weekday() >= 5 or (now.hour, now.minute) < AT or now.hour >= 12 or _running["on"]:
        return
    if load().get("day") == now.date().isoformat():
        return
    _running["on"] = True

    def go():
        try:
            run(cfg, analyst, notify)
        except Exception as e:  # noqa: BLE001
            log.warning("Depot-Durchsicht fehlgeschlagen: %s", e)
            pf._atomic_write(REVIEW_FILE, {**load(), "day": now.date().isoformat(), "error": str(e)[:200]})
        finally:
            _running["on"] = False
    threading.Thread(target=go, daemon=True, name="review").start()
