"""Abendbericht (Mo–Fr) und Wochenbericht (freitags) per Push: Musterdepot, Zockerportfolio, Mein Depot."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import manager
from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
REPORT_STATE = STATE_DIR / "reports.json"


def _fmt(x: float, d: int = 0) -> str:
    return f"{x:,.{d}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _pct(x: float | None, d: int = 1) -> str:
    return "–" if x is None else f"{x * 100:+.{d}f} %".replace(".", ",")


def _zocker() -> dict | None:
    """Spielgeld-Hebelportfolio: Depotwert, Rendite seit Start und seit gestern (aus dem Depot-Verlauf)."""
    try:
        st = json.loads((STATE_DIR / "portfolio.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    hist = st.get("equity_history", [])
    if not hist:
        return None
    eq = hist[-1][1]
    today = datetime.now(pf.BERLIN).date().isoformat()
    before = [h for h in hist if h[0][:10] < today]
    week_ago = (datetime.now(pf.BERLIN) - timedelta(days=7)).date().isoformat()
    wk = [h for h in hist if h[0][:10] <= week_ago]
    return {"equity": eq, "total": eq / st["start_capital"] - 1,
            "day": eq / before[-1][1] - 1 if before else None,
            "week": eq / wk[-1][1] - 1 if wk else None}


def build_report(cfg: Config, weekly: bool) -> tuple[str, str]:
    from .portfolio_service import overview, performance
    lines = []
    state = manager.load_state()
    if state.get("start_capital"):
        txns = pf.load_txns("model")
        cash = manager.cash_of(state["start_capital"], txns)
        ov = overview(cfg, cash, "model", txns)
        k = ov["kpi"]
        total = k["total"]
        lines.append(f"📋 Portfolio {_fmt(total)} € · heute {_pct(k['day_change'])} · seit Start "
                     f"{_pct(total / state['start_capital'] - 1)}")
        perf = performance(cfg, "model")
        if weekly and perf.get("series"):
            ser = perf["series"]
            cut = (datetime.now(pf.BERLIN) - timedelta(days=7)).date().isoformat()
            base = [r for r in ser if r["date"] <= cut] or ser[:1]
            b, e = base[-1], ser[-1]
            parts = [f"Woche: Depot {_pct(e['index'] / b['index'] - 1)}"]
            for name in cfg.my_benchmarks:
                if b.get(name) and e.get(name):
                    parts.append(f"{name} {_pct(e[name] / b[name] - 1)}")
            lines.append(" · ".join(parts))
        pos = [p for p in ov["positions"] if p["day_change"] is not None]
        if pos:
            pos.sort(key=lambda p: p["day_change"])
            lines.append(f"Top: {pos[-1]['name']} {_pct(pos[-1]['day_change'])} · Flop: {pos[0]['name']} {_pct(pos[0]['day_change'])}")
        weak = [p["name"] for p in ov["positions"] if p["signal"] is not None and p["signal"] < cfg.sell_threshold]
        if weak:
            lines.append("⚠️ Schwaches Signal: " + ", ".join(weak[:4]))
        day = datetime.now(pf.BERLIN).date().isoformat()
        acts = [a for a in state.get("actions", []) if a["time"][:10] >= (day if not weekly else
                (datetime.now(pf.BERLIN) - timedelta(days=7)).date().isoformat())]
        if acts:
            lines.append(f"{len(acts)} Empfehlung(en) {'diese Woche' if weekly else 'heute'}, "
                         f"{sum(1 for a in acts if a['status'] == 'offen')} noch offen")
    z = _zocker()
    if z:
        lines.append(f"🎲 Zocker {_fmt(z['equity'])} € · heute {_pct(z['day'])} · "
                     + (f"Woche {_pct(z['week'])} · " if weekly else "") + f"seit Start {_pct(z['total'])}")
    real = pf.load_txns("real")
    if real:
        from .mydepot import load_depot
        ov = overview(cfg, load_depot().get("cash"), "real", real)
        lines.append(f"💼 Mein Depot {_fmt(ov['kpi']['total'])} € · heute {_pct(ov['kpi']['day_change'])}")
    title = "📊 NewsTrader – " + ("Wochenbericht" if weekly else "Abendbericht")
    return title, "\n".join(lines) or "Noch keine Daten."


def maybe_send(cfg: Config, notify) -> bool:
    """Mo–Fr zur eingestellten Uhrzeit genau einmal; freitags als Wochenbericht."""
    if not cfg.my_daily_report_time:
        return False
    now = datetime.now(pf.BERLIN)
    h, m = (int(x) for x in cfg.my_daily_report_time.split(":"))
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now.weekday() >= 5 or now < due or (now - due).total_seconds() > 7200:
        return False
    try:
        last = json.loads(REPORT_STATE.read_text()).get("last")
    except (FileNotFoundError, json.JSONDecodeError):
        last = None
    if last == now.date().isoformat():
        return False
    title, text = build_report(cfg, weekly=now.weekday() == 4)
    notify(cfg, title, text)
    REPORT_STATE.write_text(json.dumps({"last": now.date().isoformat()}))
    log.info("%s gesendet", title)
    return True
