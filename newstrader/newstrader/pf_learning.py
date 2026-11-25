"""Portfolio lernt aus seinen Empfehlungen.

Jede umgesetzte Empfehlung des Bots (nicht deine eigenen Trades) wird nach 5 und 20 Handelstagen gegen den
MSCI World gemessen:
- Kauf gut  = Aktie lief danach besser als der Markt.
- Verkauf/Stop gut = Aktie lief danach schlechter als der Markt (Verlust vermieden).
Daraus werden – in festen Grenzen und erst ab genug Erfahrung – nachgeführt:
- Kaufschwelle (strenger bei schlechten, etwas lockerer bei guten Käufen),
- Verkaufsschwelle (länger halten, wenn nach Verkäufen oft weiter steigt),
- Stop-Loss (etwas weiter, wenn sich Werte nach Stops oft erholen),
- Gewichtung Technik/Nachrichten (was lag öfter richtig),
- KI-Durchsicht als Verkaufsgrund (abschalten, wenn ihre Verkäufe danebenlagen).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
FILE = STATE_DIR / "pf_learning.json"
HORIZONS = (5, 20)          # Handelstage
MIN_N = 8                   # ab so vielen Bewertungen wird angepasst
BENCH = "EUNL.DE"           # MSCI World
LEARN_KINDS = ("signal", "rebalance", "trim", "stop", "setup")


def load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("results", {})   # Aktions-ID -> {side, kind, symbol, name, time, h5, h20, signal}
    d.setdefault("day", None)
    return d


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _ret_after(series, t0, days: int) -> float | None:
    """Rendite von `t0` (Tag der Empfehlung) bis `days` Handelstage später. None, wenn noch zu früh."""
    import pandas as pd
    if series is None or series.empty:
        return None
    s = series.copy()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    t0 = pd.Timestamp(t0.date())
    after = s[s.index >= t0]
    if len(after) <= days:
        return None
    return float(after.iloc[days] / after.iloc[0] - 1)


def evaluate(cfg: Config, actions: list[dict], history, now: datetime | None = None) -> dict:
    """Empfehlungen auswerten (history: Symbol -> Tageskurse in €; muss BENCH enthalten)."""
    d = load()
    bench = history(BENCH)
    for a in actions:
        if a.get("kind") not in LEARN_KINDS or a.get("status") in ("abgelehnt", "eigene") or a.get("side") not in ("buy", "sell"):
            continue
        r = d["results"].setdefault(a["id"], {"side": a["side"], "kind": a["kind"], "symbol": a["symbol"],
                                             "name": a["name"], "time": a["time"], "signal": a.get("signal"),
                                             "veto": bool((a.get("signal") or {}).get("veto"))})
        t0 = datetime.fromisoformat(a["time"])
        for h in HORIZONS:
            key = f"h{h}"
            if r.get(key) is not None:
                continue
            rs, rb = _ret_after(history(a["symbol"]), t0, h), _ret_after(bench, t0, h)
            if rs is None or rb is None:
                continue
            excess = rs - rb
            r[key] = {"ret": round(rs, 4), "bench": round(rb, 4), "excess": round(excess, 4),
                      "good": excess > 0 if a["side"] == "buy" else excess < 0}
    d["day"] = (now or datetime.now(timezone.utc)).date().isoformat()
    pf._atomic_write(FILE, d)
    return d


def _scored(d: dict, side: str | None = None, kind: str | None = None, veto: bool | None = None) -> list[dict]:
    out = []
    for r in d["results"].values():
        res = r.get("h20") or r.get("h5")
        if not res or (side and r["side"] != side) or (kind and r["kind"] != kind) or (veto is not None and r.get("veto") != veto):
            continue
        out.append({**r, "res": res})
    return out


def adjustments(d: dict | None = None) -> dict:
    """Was das Portfolio aus seinen Empfehlungen gelernt hat (alles 0/Standard, solange zu wenig Erfahrung)."""
    d = d or load()
    adj = {"buy_shift": 0.0, "sell_shift": 0.0, "stop_widen": 0.0, "tech_weight": None, "veto_ok": True, "n": 0}
    buys, sells = _scored(d, "buy"), [r for r in _scored(d, "sell") if r["kind"] in ("signal", "trim")]
    stops, vetos = _scored(d, "sell", "stop"), _scored(d, "sell", veto=True)
    adj["n"] = len(buys) + len(sells) + len(stops)
    if len(buys) >= MIN_N:
        m = sum(r["res"]["excess"] for r in buys) / len(buys)
        adj["buy_shift"] = round(_clamp(-m * 2, -0.05, 0.10), 3)            # −2 % Rendite ggü. Markt -> +0,04 strenger
    if len(sells) >= MIN_N:
        m = sum(r["res"]["excess"] for r in sells) / len(sells)             # > 0: stieg nach dem Verkauf weiter
        adj["sell_shift"] = round(_clamp(-m * 2, -0.10, 0.05), 3)           # schlechte Verkäufe -> später verkaufen
    if len(stops) >= 4:
        rebound = sum(1 for r in stops if r["res"]["excess"] > 0.03) / len(stops)
        adj["stop_widen"] = 0.02 if rebound > 0.5 else 0.0
    if len(vetos) >= 3:
        adj["veto_ok"] = sum(1 for r in vetos if r["res"]["good"]) / len(vetos) >= 0.5
    # Technik oder Nachrichten: wessen Vorzeichen passte öfter zum Ergebnis?
    hits = {"tech": [0, 0], "news": [0, 0]}
    for r in buys + sells:
        sig = r.get("signal") or {}
        up_was_right = (r["side"] == "buy") == r["res"]["good"]    # „Aktie lief besser als Markt“
        for k in ("tech", "news"):
            v = sig.get(k)
            if not v or (k == "news" and not sig.get("n_news")):
                continue
            hits[k][1] += 1
            hits[k][0] += 1 if (v > 0) == up_was_right else 0
    if hits["tech"][1] >= MIN_N and hits["news"][1] >= MIN_N:
        at, an = hits["tech"][0] / hits["tech"][1], hits["news"][0] / hits["news"][1]
        adj["tech_weight"] = round(_clamp(at / max(1e-9, at + an), 0.2, 0.8), 2)
        adj["acc"] = {"tech": round(at, 2), "news": round(an, 2)}
    return adj


def rescore(signals: dict[str, dict], tech_weight: float | None) -> dict[str, dict]:
    """Gesamtsignal mit der gelernten Gewichtung neu berechnen (nur wo Nachrichten vorliegen)."""
    if tech_weight is None:
        return signals
    out = {}
    for sym, v in signals.items():
        if v.get("n_headlines") and v.get("news_score") is not None and v.get("tech_score") is not None:
            v = {**v, "score": tech_weight * v["tech_score"] + (1 - tech_weight) * v["news_score"]}
        out[sym] = v
    return out


def summary() -> dict:
    d = load()
    adj = adjustments(d)
    buys, sells = _scored(d, "buy"), _scored(d, "sell")
    rate = lambda xs: (sum(1 for r in xs if r["res"]["good"]) / len(xs)) if xs else None  # noqa: E731
    avg = lambda xs: (sum(r["res"]["excess"] for r in xs) / len(xs)) if xs else None  # noqa: E731
    lessons = []
    if adj["n"] < MIN_N:
        lessons.append(f"Sammelt noch Erfahrung: {adj['n']} Empfehlungen ausgewertet (nach 5 und 20 Handelstagen) – "
                       f"angepasst wird ab {MIN_N}.")
    if adj["buy_shift"] > 0:
        lessons.append(f"Käufe liefen schlechter als der Markt – kauft jetzt wählerischer (Schwelle +{adj['buy_shift']:.2f}).")
    elif adj["buy_shift"] < 0:
        lessons.append(f"Käufe schlugen den Markt – darf etwas früher kaufen (Schwelle {adj['buy_shift']:.2f}).")
    if adj["sell_shift"] < 0:
        lessons.append(f"Nach Verkäufen stieg es oft weiter – hält jetzt länger (Verkaufsschwelle {adj['sell_shift']:.2f}).")
    if adj["stop_widen"]:
        lessons.append("Nach Stop-Loss erholten sich die Werte oft – Stop 2 Prozentpunkte weiter.")
    if adj["tech_weight"] is not None:
        lessons.append(f"Gewichtung jetzt {adj['tech_weight']:.0%} Technik / {1 - adj['tech_weight']:.0%} Nachrichten "
                       f"(Trefferquote Technik {adj['acc']['tech']:.0%}, Nachrichten {adj['acc']['news']:.0%}).")
    if not adj["veto_ok"]:
        lessons.append("Verkaufsempfehlungen der KI-Durchsicht lagen oft daneben – werden ignoriert.")
    recent = sorted(_scored(d), key=lambda r: r["time"], reverse=True)[:8]
    return {"adj": adj, "buys": {"n": len(buys), "hit": rate(buys), "excess": avg(buys)},
            "sells": {"n": len(sells), "hit": rate(sells), "excess": avg(sells)}, "lessons": lessons,
            "pending": sum(1 for r in d["results"].values() if not r.get("h5")),
            "recent": [{"name": r["name"], "side": r["side"], "kind": r["kind"], "time": r["time"],
                        "excess": r["res"]["excess"], "good": r["res"]["good"]} for r in recent]}


def tick(cfg: Config) -> None:
    """Einmal am Tag (nach Börsenschluss) auswerten."""
    from . import manager
    from .portfolio_service import history_eur
    now = datetime.now(pf.BERLIN)
    if now.hour < 22 or load().get("day") == now.date().isoformat():
        return
    try:
        evaluate(cfg, manager.load_state().get("actions", []), lambda s: history_eur(cfg, s, "1y"))
        log.info("Portfolio-Lernen: %s", "; ".join(summary()["lessons"]) or "ausgewertet")
    except Exception:  # noqa: BLE001
        log.exception("Portfolio-Lernen fehlgeschlagen")
