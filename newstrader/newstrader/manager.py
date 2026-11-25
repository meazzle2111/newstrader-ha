"""Portfoliomanager: führt ein Musterdepot mit deinem Startkapital und schickt jeden Schritt als Empfehlung.

- Aufbau: aus den stärksten, von Claude geprüften Long-Signalen, gewichtet nach Signalstärke.
- Stop-Loss / Trailing-Stop: jederzeit (jede Minute mit Echtzeitkursen).
- Neue Käufe, signalbedingte Verkäufe, Umschichtungen: nur zu festen Zeiten (pm_decision_times), Mo–Fr.
- Rasante Kursbewegung bei einer Position + schlechtes Signal: sofortiger Verkauf.
Die Empfehlungen sind freiwillig – „Umgesetzt“ überträgt sie in „Mein Depot“, sonst einfach ignorieren.

Die Planungsfunktionen (plan_*) sind rein und in tests/test_manager.py abgedeckt.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
STATE_FILE = STATE_DIR / "model.json"
_lock = threading.RLock()


@dataclass(frozen=True)
class Style:
    label: str
    n_min: int
    n_max: int
    max_weight: float      # höchstens so viel vom Depot je Wert
    stop: float            # Stop-Loss unter Einstand
    trailing: float        # Trailing-Stop unter Hoch seit Kauf
    cash: float            # Cash-Reserve
    crypto: float          # Krypto zusammen höchstens
    min_score: float       # Mindestsignal für Käufe


STYLES = {
    "ausgewogen": Style("Ausgewogen", 10, 15, 0.10, 0.10, 0.15, 0.07, 0.05, 0.15),
    "defensiv": Style("Defensiv", 15, 20, 0.07, 0.07, 0.12, 0.15, 0.0, 0.20),
    "offensiv": Style("Offensiv", 6, 10, 0.15, 0.15, 0.20, 0.03, 0.10, 0.12),
}
MIN_ORDER = 100.0  # kleinere Orders lohnen wegen der Gebühr nicht


def is_crypto(sym: str) -> bool:
    return sym.endswith(("-EUR", "-USD"))


def checked(sig: dict) -> bool:
    """Signal verlässlich genug zum Handeln: Claude hat die Nachrichten gelesen, oder es gibt (geladen) keine."""
    if sig.get("analyst") == "claude":
        return True
    if sig.get("analyst") == "lexicon_only":  # Claude ist komplett aus -> Wörterbuch zählt
        return True
    return not sig.get("headlines") and sig.get("news_loaded", True)


def cash_of(start_capital: float, txns: list[pf.Txn]) -> float:
    return start_capital - sum(t.cash_out for t in txns)


def stop_level(avg: float, high: float, st: Style) -> float:
    return max(avg * (1 - st.stop), high * (1 - st.trailing))


def _qty(sym: str, amount: float, price: float) -> float:
    """Ganze Stücke (wie bei den meisten Brokern), Krypto auf 4 Nachkommastellen."""
    if price <= 0:
        return 0.0
    if is_crypto(sym):
        return math.floor(amount / price * 1e4 + 1e-6) / 1e4
    return float(math.floor(amount / price + 1e-9))  # 1999,9999… / 100 soll 20 ergeben, nicht 19


def _action(kind: str, side: str, sym: str, name: str, qty: float, price: float, reason: str, now: datetime) -> dict:
    return {"id": uuid.uuid4().hex[:10], "time": now.isoformat(timespec="seconds"), "kind": kind, "side": side,
            "symbol": sym, "name": name, "qty": qty, "price": round(price, 4), "value": round(qty * price, 2),
            "reason": reason, "status": "angenommen"}


# --------------------------------------------------------------------------- Planung (rein)
def plan_stops(positions: list[dict], prices: dict[str, float], st: Style, names: dict, now: datetime) -> list[dict]:
    """positions: {symbol, qty, avg_price, high}. Verkauft komplett, wenn der Kurs die Stop-Marke erreicht."""
    out = []
    for p in positions:
        px = prices.get(p["symbol"])
        if not px or p["qty"] <= 0:
            continue
        high = max(p["high"], px)
        level = stop_level(p["avg_price"], high, st)
        if px <= level:
            kind = "Trailing-Stop" if high * (1 - st.trailing) >= p["avg_price"] * (1 - st.stop) else "Stop-Loss"
            out.append(_action("stop", "sell", p["symbol"], names.get(p["symbol"], p["symbol"]), p["qty"], px,
                               f"{kind} bei {level:.2f} € erreicht (Einstand {p['avg_price']:.2f} €, Hoch {high:.2f} €)", now))
    return out


def plan_decisions(positions: list[dict], signals: dict[str, dict], cash: float, st: Style, cfg_rules: dict,
                   names: dict, now: datetime, building: bool = False, blocked: set[str] | None = None) -> list[dict]:
    """Verkäufe bei schlechtem Signal, Kürzen zu großer Positionen, Käufe neuer Werte.

    positions: {symbol, qty, avg_price, price}; signals: {score, price, tradable, analyst, headlines, summary, tech}.
    cfg_rules: {buy, sell}. building=True: Erstaufbau (alle Käufe auf einmal, Mindestsignal des Stils).
    """
    actions = []
    held = {p["symbol"]: p for p in positions if p["qty"] > 0}
    total = cash + sum(p["qty"] * p["price"] for p in held.values())
    if total <= 0:
        return []
    cash_left = cash

    # 1) Verkaufen bei schlechtem Signal
    for sym, p in held.items():
        s = signals.get(sym)
        if not s or not s.get("tradable") or not checked(s):
            continue
        if s["score"] < cfg_rules["sell"]:
            actions.append(_action("signal", "sell", sym, names.get(sym, sym), p["qty"], s["price"],
                                   f"Signal {s['score']:+.2f} unter {cfg_rules['sell']:+.2f} – {s.get('summary') or s.get('tech', '')}", now))
            cash_left += p["qty"] * s["price"]
    sold = {a["symbol"] for a in actions}

    # 2) Zu große Positionen kürzen (mehr als 1,5 × Zielgewicht)
    for sym, p in held.items():
        s = signals.get(sym)
        if sym in sold or not s or not s.get("tradable"):
            continue
        w = p["qty"] * s["price"] / total
        if w > st.max_weight * 1.5:
            q = _qty(sym, (w - st.max_weight) * total, s["price"])
            if q > 0 and q * s["price"] >= MIN_ORDER:
                actions.append(_action("trim", "sell", sym, names.get(sym, sym), q, s["price"],
                                       f"Anteil {w:.0%} über dem Ziel von {st.max_weight:.0%} – Gewinne sichern, Risiko streuen", now))
                cash_left += q * s["price"]

    # 3) Kaufen: stärkste geprüfte Signale, nicht im Bestand
    remaining = [s for s in held if s not in sold]
    slots = st.n_max - len(remaining)
    reserve = total * st.cash
    crypto_now = sum(held[s]["qty"] * held[s]["price"] for s in remaining if is_crypto(s)) / total
    min_score = st.min_score if building else max(st.min_score, cfg_rules["buy"])
    cands = sorted(((sym, s) for sym, s in signals.items()
                    if sym not in held and sym not in (blocked or set()) and s.get("tradable") and checked(s)
                    and s["score"] > min_score
                    and s.get("price", 0) > 0 and not (is_crypto(sym) and st.crypto <= 0)
                    # eine ganze Aktie muss ins Positionsbudget passen, sonst blockiert sie nur einen Platz
                    and (is_crypto(sym) or s["price"] <= st.max_weight * total)),
                   key=lambda x: -x[1]["score"])
    # Auswahl: stärkste zuerst, bis die freien Plätze voll sind (Krypto nur bis zur Obergrenze)
    picks, crypto_room = [], st.crypto - crypto_now
    for sym, s in cands:
        if len(picks) >= slots:
            break
        if is_crypto(sym) and crypto_room <= 0:
            continue
        strength = max(0.0, min(1.0, (s["score"] - min_score) / max(0.05, 0.6 - min_score)))
        picks.append((sym, s, strength))
        if is_crypto(sym):
            crypto_room -= st.max_weight
    # Gewichte: Kapital (ohne Reserve) gleichmäßig auf mind. n_min Plätze, stärkere Signale etwas mehr (×0,8 … ×1,2)
    n_target = max(st.n_min, len(remaining) + len(picks))
    base = min(st.max_weight, (1 - st.cash) / n_target)
    raw = [0.8 + 0.4 * strength for _, _, strength in picks]
    raw = [base * r * len(raw) / sum(raw) for r in raw] if raw else []  # Mittelwert = base
    available = max(0.0, (cash_left - reserve) / total)
    scale = min(1.0, available / sum(raw)) if raw else 0.0
    for (sym, s, _), w in zip(picks, raw):
        weight = min(st.max_weight, w * scale)
        if is_crypto(sym):
            weight = min(weight, st.crypto - crypto_now)
            if weight <= 0:
                continue
        q = _qty(sym, min(weight * total, cash_left - reserve), s["price"])
        if q <= 0 or q * s["price"] < MIN_ORDER:
            continue
        actions.append(_action("setup" if building else "signal", "buy", sym, names.get(sym, sym), q, s["price"],
                               f"Signal {s['score']:+.2f} – {s.get('summary') or s.get('tech', '')}", now))
        cash_left -= q * s["price"]
        if is_crypto(sym):
            crypto_now += q * s["price"] / total
    return actions


# --------------------------------------------------------------------------- Zustand
def load_state() -> dict:
    for p in (STATE_FILE, STATE_FILE.with_suffix(".bak")):
        try:
            return json.loads(p.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return {}


def save_state(state: dict) -> None:
    state["actions"] = state.get("actions", [])[-500:]
    pf._atomic_write(STATE_FILE, state)


# Einstellbare Werte der Strategie: Schlüssel -> (Bezeichnung, Minimum, Maximum, Prozentwert?)
PARAMS = {
    "n_min": ("Positionen mindestens", 3, 40, False),
    "n_max": ("Positionen höchstens", 3, 40, False),
    "max_weight": ("Höchstanteil je Wert", 0.02, 0.5, True),
    "stop": ("Stop-Loss unter Einstand", 0.02, 0.5, True),
    "trailing": ("Trailing-Stop unter Hoch", 0.02, 0.5, True),
    "cash": ("Cash-Reserve", 0.0, 0.8, True),
    "crypto": ("Krypto höchstens", 0.0, 0.5, True),
    "min_score": ("Mindestsignal für Käufe", 0.05, 0.8, False),
}


def style_of(state: dict) -> Style:
    """Gewählter Stil plus deine eigenen Abweichungen (state["custom"])."""
    base = STYLES.get(state.get("style", "ausgewogen"), STYLES["ausgewogen"])
    custom = {k: v for k, v in (state.get("custom") or {}).items() if k in PARAMS}
    st = replace(base, **custom) if custom else base
    try:   # gelernt: Stops etwas weiter, wenn sich Werte nach Stop-Loss oft erholten
        from .pf_learning import adjustments
        widen = adjustments()["stop_widen"]
        if widen:
            st = replace(st, stop=min(st.stop + widen, 0.25))
    except Exception:  # noqa: BLE001
        pass
    return st


def decision_times(cfg: Config, state: dict) -> list[str]:
    return state.get("decision_times") or list(cfg.pm_decision_times)


def strategy_view(cfg: Config, state: dict) -> dict:
    st = style_of(state)
    return {"style": state.get("style", "ausgewogen"), "custom": state.get("custom") or {},
            "values": {k: getattr(st, k) for k in PARAMS}, "decision_times": decision_times(cfg, state),
            "params": {k: {"label": v[0], "min": v[1], "max": v[2], "pct": v[3]} for k, v in PARAMS.items()},
            "defaults": {name: {k: getattr(sty, k) for k in PARAMS} for name, sty in STYLES.items()}}


def set_strategy(cfg: Config, raw: dict) -> dict:
    """Stil wechseln und/oder einzelne Werte anpassen. Prozentwerte kommen als Prozent (z. B. „10“ oder „7,5“)."""
    style = str(raw.get("style", "")).strip()
    if style not in STYLES:
        raise ValueError("Unbekannter Stil")
    base = STYLES[style]
    custom: dict = {}
    for k, (label, lo, hi, is_pct) in PARAMS.items():
        v = (raw.get("values") or {}).get(k)
        if v in (None, ""):
            continue
        try:
            x = pf.parse_number(v)
        except ValueError:
            raise ValueError(f"{label}: keine gültige Zahl")
        if x is None:
            raise ValueError(f"{label}: keine gültige Zahl")
        if is_pct:
            x = round(x / 100, 4)
        if k in ("n_min", "n_max"):
            if x != int(x):
                raise ValueError(f"{label}: bitte eine ganze Zahl")
            x = int(x)
        if not lo <= x <= hi:
            f = (lambda y: f"{y * 100:g} %") if is_pct else (lambda y: f"{y:g}")
            raise ValueError(f"{label}: erlaubt sind {f(lo)} bis {f(hi)}".replace(".", ","))
        if x != getattr(base, k):
            custom[k] = x
    st = replace(base, **custom)
    if st.n_min > st.n_max:
        raise ValueError("Positionen: „mindestens“ darf nicht größer als „höchstens“ sein")
    if st.max_weight * st.n_max < 1 - st.cash - 0.05:
        raise ValueError(f"Mit höchstens {st.n_max} Werten zu je {st.max_weight * 100:g} % wird das Depot nicht voll"
                         " investiert – Höchstanteil oder Anzahl erhöhen".replace(".", ","))
    times = raw.get("decision_times")
    if times is not None:
        if isinstance(times, str):
            times = [t for t in times.replace(";", ",").split(",")]
        clean = set()
        for t in times:
            t = str(t).strip()
            if not t:
                continue
            try:
                h, m = (int(x) for x in t.split(":"))
            except ValueError:
                raise ValueError(f"Prüfzeit „{t}“: bitte als HH:MM angeben")
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"Prüfzeit „{t}“ gibt es nicht")
            if not 8 <= h <= 21:
                raise ValueError(f"Prüfzeit „{t}“: Aktien werden nur zwischen 08:00 und 22:00 gehandelt")
            clean.add(f"{h:02d}:{m:02d}")
        if not 1 <= len(clean) <= 14:
            raise ValueError("Bitte 1 bis 14 Prüfzeiten angeben")
        times = sorted(clean)
    with _lock:
        state = load_state()
        if not state.get("start_capital"):
            raise ValueError("Erst das Portfolio anlegen")
        state["style"] = style
        state["custom"] = custom
        if times is not None:
            state["decision_times"] = times if times != sorted(cfg.pm_decision_times) else None
        save_state(state)
    log.info("Strategie geändert: %s %s, Prüfzeiten %s", style, custom, times)
    return strategy_view(cfg, state)


def signals_from_cycle() -> dict[str, dict]:
    try:
        cyc = json.loads((STATE_DIR / "last_cycle.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    claude_off = cyc.get("analyst") != "claude"
    out = {}
    for sym, v in cyc.get("symbols", {}).items():
        out[sym] = {"score": v.get("combined", 0.0), "tech_score": v.get("tech", 0.0), "news_score": v.get("news", 0.0),
                    "n_headlines": len(v.get("headlines") or []),
                    "price": v.get("price", 0.0), "tradable": v.get("tradable", False),
                    "analyst": "lexicon_only" if claude_off and v.get("news_loaded", True) else v.get("analyst"),
                    "headlines": v.get("headlines", []), "news_loaded": v.get("news_loaded", True),
                    "summary": v.get("news_summary", ""), "tech": v.get("tech_detail", ""), "rapid": v.get("rapid")}
    return out


def positions_now(cfg: Config, prices: dict[str, float] | None = None) -> tuple[list[dict], dict]:
    """Bestand des Musterdepots mit Einstand, Hoch seit Kauf (Tageskurse + aktuell) und aktuellem Kurs."""
    from .portfolio_service import _period_for, history_eur
    txns = pf.load_txns("model")
    hs = pf.compute_holdings(txns)
    out = []
    for sym, h in hs.items():
        if h.qty <= 0:
            continue
        hist = history_eur(cfg, sym, _period_for(h.first_buy))
        high = h.avg_price
        if hist is not None:
            since = hist[hist.index >= __import__("pandas").Timestamp(h.first_buy)]
            if not since.empty:
                high = max(high, float(since.max()))
        px = (prices or {}).get(sym)
        out.append({"symbol": sym, "qty": h.qty, "avg_price": h.avg_price, "high": max(high, px or 0), "price": px})
    return out, hs


# --------------------------------------------------------------------------- Ausführen
def _execute(cfg: Config, actions: list[dict], notify) -> list[dict]:
    """Aktionen im Musterdepot buchen (zum genannten Kurs, 1 Order = Gebühr) und als Empfehlung pushen."""
    if not actions:
        return []
    done = []
    with _lock:
        state = load_state()
        txns = pf.load_txns("model")
        today = datetime.now(pf.BERLIN).date().isoformat()
        for a in actions:
            t = pf.Txn(id=uuid.uuid4().hex[:12], date=today, type=a["side"], symbol=a["symbol"], qty=a["qty"],
                       price=a["price"], fee=cfg.fee_fixed, note=f"{a['kind']}: {a['reason']}"[:200])
            try:
                pf.compute_holdings(txns + [t])
            except ValueError as e:
                log.warning("Aktion %s übersprungen: %s", a["symbol"], e)
                continue
            if a["side"] == "buy" and cash_of(state["start_capital"], txns + [t]) < -0.01:
                log.warning("Aktion %s übersprungen: nicht genug Cash", a["symbol"])
                continue
            txns.append(t)
            a["txn_id"] = t.id
            done.append(a)
        if done:
            pf.save_txns(txns, "model")
            state.setdefault("actions", []).extend(done)
            save_state(state)
    for a in done:
        log.info("Musterdepot: %s %g %s @ %.2f € (%s)", "KAUF" if a["side"] == "buy" else "VERKAUF",
                 a["qty"], a["symbol"], a["price"], a["kind"])
    _push_actions(cfg, done, notify)
    return done


def _fmt(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def _push_actions(cfg: Config, actions: list[dict], notify) -> None:
    if not actions or notify is None:
        return
    st = style_of(load_state())
    lines = []
    for a in actions[:8]:
        q = f"{a['qty']:g}".replace(".", ",")
        if a["side"] == "buy":
            stop = a["price"] * (1 - st.stop)
            lines.append(f"🟢 Kaufen: {q} × {a['name']} zu ca. {_fmt(a['price'])} € (≈ {_fmt(a['value'], 0)} €), "
                         f"Stop-Loss {_fmt(stop)} €")
        else:
            label = {"stop": "Stop-Loss", "trim": "Teilverkauf", "rapid": "Kurssturz"}.get(a["kind"], "Verkaufen")
            lines.append(f"🔴 {label}: {q} × {a['name']} zu ca. {_fmt(a['price'])} € (≈ {_fmt(a['value'], 0)} €)")
    if len(actions) > 8:
        lines.append(f"… und {len(actions) - 8} weitere")
    title = "📋 Depot: " + ("Aufbau – bitte kaufen" if actions[0]["kind"] == "setup" else
                                "Stop-Loss" if all(a["kind"] == "stop" for a in actions) else "Empfehlungen")
    from .mydepot import tr_url
    link = tr_url(getattr(cfg, "isins", {}).get(actions[0]["symbol"]))
    text = "\n".join(lines) + "\nIm Depot als ausgeführt gebucht – anders gemacht? In der App anpassen."
    try:
        notify(cfg, title, text, url=link)
    except TypeError:   # ältere notify-Funktion ohne Link
        notify(cfg, title, text)


# --------------------------------------------------------------------------- Ablauf
def setup(cfg: Config, capital: float, style: str, signals: dict, names: dict, notify, now: datetime | None = None) -> list[dict]:
    """Musterdepot anlegen und sofort aufbauen."""
    if style not in STYLES:
        raise ValueError("Unbekannter Anlagestil")
    if not 500 <= capital <= 1e8:
        raise ValueError("Startkapital bitte zwischen 500 € und 100 Mio. €")
    now = now or datetime.now(timezone.utc)
    with _lock:
        if pf.load_txns("model"):
            raise ValueError("Es gibt bereits ein Musterdepot – erst zurücksetzen")
        save_state({"start_capital": round(capital, 2), "style": style, "created": now.isoformat(timespec="seconds"),
                    "actions": [], "decisions": {}})
    acts = plan_decisions([], signals, capital, STYLES[style], {"buy": cfg.buy_threshold, "sell": cfg.sell_threshold},
                          names, now, building=True)
    return _execute(cfg, acts, notify)


def reset() -> None:
    with _lock:
        for f in (STATE_FILE, pf.MODEL_TXN_FILE):
            if f.exists():
                f.rename(f.with_name(f.stem + f"-{datetime.now():%Y%m%d-%H%M%S}.old.json"))


COOLDOWN_DAYS = {"stop": 7, "rapid": 7, "signal": 3, "trim": 0}


def recently_sold(state: dict, now: datetime) -> set[str]:
    """Kein Rückkauf kurz nach einem Verkauf (vermeidet Hin und Her): 7 Tage nach Stop-Loss/Kurssturz, 3 nach Signal."""
    out = set()
    for a in state.get("actions", []):
        if a["side"] != "sell":
            continue
        days = COOLDOWN_DAYS.get(a["kind"], 3)
        if days and now - datetime.fromisoformat(a["time"]) < timedelta(days=days):
            out.add(a["symbol"])
    return out


def due_slot(cfg: Config, state: dict, now: datetime) -> str | None:
    """Fälliger Entscheidungstermin (Mo–Fr), der heute noch nicht gelaufen ist, höchstens 90 Min. verspätet."""
    local = now.astimezone(pf.BERLIN)
    if local.weekday() >= 5:
        return None
    done = state.get("decisions", {})
    slots = []
    for t in sorted(decision_times(cfg, state)):
        h, m = (int(x) for x in t.split(":"))
        slots.append(local.replace(hour=h, minute=m, second=0, microsecond=0))
    for i, slot in enumerate(slots):
        # nachholen höchstens 90 Min. – und nie über den nächsten Termin hinaus (bei stündlichen Prüfungen)
        end = min(slot + timedelta(minutes=90), slots[i + 1] if i + 1 < len(slots) else slot + timedelta(minutes=90))
        key = slot.isoformat(timespec="minutes")
        if slot <= local < end and key not in done:
            return key
    return None


_last_regime: dict = {}


def plan_holistic(cfg: Config, positions: list[dict], signals: dict[str, dict], cash: float, st: Style, state: dict,
                  names: dict, now: datetime) -> list[dict]:
    """Ganzheitliche Entscheidung (Marktlage, Branchen, Schwankung, Bänder) – siehe allocator.py."""
    import math
    from . import allocator as A
    from .market import get_daily_closes
    from .portfolio_service import history_eur, meta_for
    from . import pf_learning
    adj = pf_learning.adjustments()          # gelernt aus früheren Empfehlungen
    rules = {"buy": cfg.buy_threshold + adj["buy_shift"], "sell": cfg.sell_threshold + adj["sell_shift"]}
    signals = pf_learning.rescore(signals, adj["tech_weight"])
    blocked = recently_sold(state, now)
    held = {p["symbol"]: {"qty": p["qty"], "price": p["price"]} for p in positions if p["qty"] > 0}
    total = cash + sum(h["qty"] * h["price"] for h in held.values())
    if total <= 0:
        return []
    sig = {s: {"score": v["score"], "tradable": v.get("tradable"), "checked": checked(v)} for s, v in signals.items()
           if v.get("price") and (s in held or s not in blocked)}
    # Kandidaten begrenzen: Bestand + die 25 stärksten geprüften Signale (Branchen/Schwankung nur für diese laden)
    top = sorted((s for s, v in sig.items() if s not in held and v["tradable"] and v["checked"]),
                 key=lambda s: -sig[s]["score"])[:25]
    universe = list(held) + top
    vols = {}
    for s in universe:
        c = get_daily_closes(s)
        if c is not None and len(c) > 30:
            vols[s] = float(c.pct_change().iloc[-60:].std() * math.sqrt(252))
    meta = meta_for(cfg, universe)
    sectors = {s: (meta.get(s) or {}).get("sector") or "?" for s in universe}
    regime = A.market_regime(history_eur(cfg, "EUNL.DE", "2y"), st.cash)
    _last_regime.update(state=regime.state, text=regime.text, invest=regime.invest, time=now.isoformat(timespec="minutes"))
    target, why = A.plan_targets({s: h["qty"] * h["price"] / total for s, h in held.items()},
                                 {s: sig[s] for s in universe if s in sig}, vols, sectors, st,
                                 state.get("style", "ausgewogen"), rules, regime, is_crypto,
                                 veto=__import__("newstrader.review", fromlist=["sell_views"]).sell_views() if adj["veto_ok"] else {})
    prices = {s: v["price"] for s, v in signals.items() if v.get("price")}
    from . import spekulation
    spec_reserve = spekulation.cash_needed(cfg, total)   # Cash für den nächsten Hebel-Trade freihalten
    trades = A.plan_trades(held, target, prices, total, cash, regime, st, why, _qty, extra_reserve=spec_reserve)
    out = []
    for t in trades:
        s = signals.get(t["symbol"], {})
        detail = s.get("summary") or s.get("tech", "")
        reason = t["reason"] + (f" · Signal {s['score']:+.2f} – {detail}" if s.get("score") is not None else "")
        act = _action(t["kind"], t["side"], t["symbol"], names.get(t["symbol"], t["symbol"]), t["qty"], t["price"],
                      reason, now)
        act["signal"] = {"score": s.get("score"), "tech": s.get("tech_score"), "news": s.get("news_score"),
                         "n_news": s.get("n_headlines", 0), "veto": "KI-Durchsicht" in t["reason"]}
        out.append(act)
    return out


def decide(cfg: Config, notify, names: dict, prices: dict[str, float], key: str) -> list[dict]:
    """Eine Entscheidungsrunde (wie zu den festen Zeiten): verkaufen, kürzen, kaufen – mit den neuesten Signalen."""
    state = load_state()
    if not state.get("start_capital"):
        return []
    st = style_of(state)
    now = datetime.now(timezone.utc)
    signals = signals_from_cycle()
    positions, _ = positions_now(cfg, {s: v["price"] for s, v in signals.items()})
    for p in positions:
        p["price"] = p["price"] or prices.get(p["symbol"]) or p["avg_price"]
    cash = cash_of(state["start_capital"], pf.load_txns("model"))
    plan = plan_holistic(cfg, positions, signals, cash, st, state, names, now)
    acts = _execute(cfg, plan, notify)
    with _lock:
        state = load_state()
        state.setdefault("decisions", {})[key] = len(acts)
        state["decisions"] = dict(sorted(state["decisions"].items())[-200:])
        save_state(state)
    log.info("Portfolio-Entscheidung %s: %d Aktionen", key, len(acts))
    return acts


def tick(cfg: Config, notify, names: dict, live_prices) -> list[dict]:
    """Vom Bot jede Minute aufgerufen: Stops prüfen, zu den festen Zeiten entscheiden."""
    state = load_state()
    if not state.get("start_capital"):
        return []
    st = style_of(state)
    now = datetime.now(timezone.utc)
    positions, _ = positions_now(cfg)
    prices = live_prices([p["symbol"] for p in positions])
    for p in positions:
        p["price"] = prices.get(p["symbol"])
    acts = _execute(cfg, plan_stops(positions, prices, st, names, now), notify)
    slot = due_slot(cfg, state, now)
    if slot:
        acts += decide(cfg, notify, names, prices, slot)
    return acts


# --------------------------------------------------------------------------- „Jetzt prüfen“
_check = {"requested": False, "running": False, "last": 0.0, "result": None, "step": ""}
CHECK_COOLDOWN = 120  # Sekunden


def request_check() -> dict:
    """Vom Knopf „Jetzt prüfen“: der Bot zieht den nächsten Durchlauf vor und entscheidet danach sofort."""
    import time as _t
    if _check["running"]:
        raise ValueError("Die Prüfung läuft gerade")
    wait = CHECK_COOLDOWN - (_t.time() - _check["last"])
    if wait > 0:
        raise ValueError(f"Eben erst geprüft – bitte in {int(wait) + 1} Sekunden noch einmal")
    _check.update(requested=True, running=True, result=None, step="Kurse und Nachrichten werden aktualisiert …")
    return check_status()


def check_status() -> dict:
    return {k: v for k, v in _check.items() if k != "requested"}


def run_requested_check(cfg: Config, notify, names: dict, live_prices) -> list[dict] | None:
    """Nach einem frischen Bot-Durchlauf aufgerufen: angeforderte Prüfung ausführen."""
    import time as _t
    if not _check["requested"]:
        return None
    _check.update(requested=False, step="Stop-Loss und Empfehlungen werden geprüft …")
    try:
        from . import traderepublic
        if traderepublic._load().get("connected"):
            _check.update(step="Trade Republic wird abgeglichen …")
            try:
                traderepublic.sync(cfg, notify)
            except ValueError:
                pass
            _check.update(step="Stop-Loss und Empfehlungen werden geprüft …")
        state = load_state()
        if not state.get("start_capital"):
            _check.update(result="Noch kein Portfolio angelegt")
            return []
        positions, _ = positions_now(cfg)
        prices = live_prices([p["symbol"] for p in positions])
        for p in positions:
            p["price"] = prices.get(p["symbol"])
        now = datetime.now(timezone.utc)
        acts = _execute(cfg, plan_stops(positions, prices, style_of(state), names, now), notify)
        acts += decide(cfg, notify, names, prices, "manuell-" + now.astimezone(pf.BERLIN).isoformat(timespec="minutes"))
        n_buy = sum(1 for a in acts if a["side"] == "buy")
        n_sell = len(acts) - n_buy
        closed = not any(v.get("tradable") for sym, v in signals_from_cycle().items() if not is_crypto(sym))
        _check.update(result=(f"{len(acts)} neue Empfehlung(en): {n_buy} Kauf, {n_sell} Verkauf – Push ist unterwegs" if acts
                              else "Keine neuen Empfehlungen – dein Portfolio ist im Plan"
                              + (" (Börsen gerade geschlossen)" if closed else "")))
        return acts
    except Exception as e:
        log.exception("Manuelle Prüfung fehlgeschlagen")
        _check.update(result=f"Fehler bei der Prüfung: {e}")
        return []
    finally:
        _check.update(running=False, last=_t.time(), step="")


def on_rapid(cfg: Config, notify, names: dict) -> list[dict]:
    """Nach einem Bot-Durchlauf: rasante Bewegung bei einer Position + Signal unter der Verkaufsschwelle -> sofort raus."""
    state = load_state()
    if not state.get("start_capital"):
        return []
    signals = signals_from_cycle()
    positions, _ = positions_now(cfg)
    now = datetime.now(timezone.utc)
    acts = []
    for p in positions:
        s = signals.get(p["symbol"])
        if s and s.get("rapid") and s.get("tradable") and checked(s) and s["score"] < cfg.sell_threshold:
            acts.append(_action("rapid", "sell", p["symbol"], names.get(p["symbol"], p["symbol"]), p["qty"], s["price"],
                                f"Kurs {s['rapid']:+.1%} in kurzer Zeit, Nachrichtenlage neu bewertet: Signal {s['score']:+.2f} – {s['summary']}", now))
    return _execute(cfg, acts, notify)


def _find(state: dict, action_id: str) -> dict:
    a = next((x for x in state.get("actions", []) if x["id"] == action_id), None)
    if a is None:
        raise ValueError("Empfehlung nicht gefunden")
    return a


def adjust_action(action_id: str, raw: dict, symbol_ok) -> dict:
    """Du hast anders ausgeführt: Kurs/Stück/Datum/Gebühr der Buchung korrigieren."""
    with _lock:
        state = load_state()
        a = _find(state, action_id)
        if a["status"] in ("abgelehnt",) or not a.get("txn_id"):
            raise ValueError("Diese Empfehlung ist nicht gebucht")
        t = pf.update_txn(a["txn_id"], raw, symbol_ok, "model")
        if a["side"] == "buy" and cash_of(state["start_capital"], pf.load_txns("model")) < -0.01:
            # zu teuer für das vorhandene Cash -> Änderung zurücknehmen
            pf.update_txn(a["txn_id"], {"qty": a["qty"], "price": a["price"]}, symbol_ok, "model")
            raise ValueError("Dafür reicht das Cash im Depot nicht – Stückzahl oder Kurs prüfen")
        a.update(qty=t.qty, price=t.price, value=round(t.qty * t.price, 2), status="angepasst")
        save_state(state)
        return a


def confirm_action(action_id: str) -> dict:
    """Du hast die Empfehlung genau so umgesetzt."""
    with _lock:
        state = load_state()
        a = _find(state, action_id)
        if a["status"] in ("abgelehnt", "eigene"):
            raise ValueError("Diese Empfehlung kann nicht bestätigt werden")
        if a["status"] == "angenommen":
            a["status"] = "bestätigt"
            save_state(state)
        return a


def confirm_all(symbol: str | None = None) -> int:
    """Alle unbestätigten Empfehlungen (optional nur eines Werts) bestätigen. Gibt die Anzahl zurück."""
    with _lock:
        state = load_state()
        n = 0
        for a in state.get("actions", []):
            if a["status"] == "angenommen" and (symbol is None or a["symbol"] == symbol):
                a["status"] = "bestätigt"
                n += 1
        if n:
            save_state(state)
        return n


def reject_action(action_id: str) -> dict:
    """Nicht gemacht: Buchung rückgängig machen. Geht nicht, wenn spätere Buchungen darauf aufbauen."""
    with _lock:
        state = load_state()
        a = _find(state, action_id)
        if a["status"] == "abgelehnt":
            return a
        if a.get("txn_id"):
            try:
                pf.delete_txn(a["txn_id"], "model")
            except ValueError as e:
                raise ValueError(f"Rückgängig nicht möglich – {e}. Erst die späteren Buchungen zu {a['name']} rückgängig machen.")
        a["status"] = "abgelehnt"
        save_state(state)
        return a


def manual_booking(cfg: Config, raw: dict, symbol_ok) -> dict:
    """Eigene Buchung (Kauf/Verkauf/Dividende, die du selbst gemacht hast) im Portfolio."""
    with _lock:
        state = load_state()
        if not state.get("start_capital"):
            raise ValueError("Erst das Portfolio mit Startkapital anlegen")
        txns = pf.load_txns("model")
        t = pf.validate(raw, txns, symbol_ok)
        if t.type == "buy" and cash_of(state["start_capital"], txns + [t]) < -0.01:
            raise ValueError("Dafür reicht das Cash im Depot nicht – ggf. erst Geld einzahlen")
        pf.save_txns(txns + [t], "model")
        if t.isin:
            cfg.isins.setdefault(t.symbol, t.isin)
        a = _action("manual", t.type if t.type != "dividend" else "sell", t.symbol, cfg.names.get(t.symbol, t.symbol),
                    t.qty, t.price, "Eigene Buchung" + (f": {t.note}" if t.note else ""), datetime.now(timezone.utc))
        a.update(txn_id=t.id, status="eigene", side=t.type)
        state.setdefault("actions", []).append(a)
        save_state(state)
        return a


def deposit(amount, kind: str = "deposit") -> dict:
    """Einzahlung (+) oder Entnahme (−): verändert das eingesetzte Kapital."""
    amount = pf.parse_number(amount)
    if not amount or abs(amount) > 1e8:
        raise ValueError("Ungültiger Betrag")
    if kind == "withdraw":
        amount = -abs(amount)
    with _lock:
        state = load_state()
        if not state.get("start_capital"):
            raise ValueError("Erst das Portfolio anlegen")
        if cash_of(state["start_capital"] + amount, pf.load_txns("model")) < -0.01:
            raise ValueError("So viel Cash ist nicht im Depot")
        state["start_capital"] = round(state["start_capital"] + amount, 2)
        state.setdefault("flows", []).append({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "amount": amount})
        save_state(state)
        return {"start_capital": state["start_capital"]}


# --------------------------------------------------------------------------- Aufbau im Hintergrund
_build = {"running": False, "step": "", "error": None}


def build_status() -> dict:
    return dict(_build)


def start_setup(cfg: Config, capital, style: str, notify) -> None:
    """Startkapital -> Kandidaten von Claude prüfen lassen -> Musterdepot aufbauen (dauert ca. 1–2 Minuten)."""
    try:
        capital = pf.parse_number(capital)
    except (TypeError, ValueError):
        raise ValueError("Startkapital muss eine Zahl sein")
    if style not in STYLES:
        raise ValueError("Unbekannter Anlagestil")
    if not 500 <= capital <= 1e8:
        raise ValueError("Startkapital bitte zwischen 500 € und 100 Mio. €")
    if pf.load_txns("model"):
        raise ValueError("Es gibt bereits ein Musterdepot – erst zurücksetzen")
    if _build["running"]:
        raise ValueError("Der Aufbau läuft bereits")
    _build.update(running=True, step="Kandidaten auswählen …", error=None)
    threading.Thread(target=_run_setup, args=(cfg, capital, style, notify), daemon=True, name="pm-setup").start()


def _run_setup(cfg: Config, capital: float, style: str, notify) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from .analyst import NewsAnalyst
    from .news import get_headlines
    try:
        signals = signals_from_cycle()
        if not signals:
            raise ValueError("Noch kein Bot-Durchlauf – bitte ein paar Minuten warten")
        st = STYLES[style]
        pool = sorted((s for s in signals.items() if s[1]["price"] > 0 and s[1]["score"] > st.min_score * 0.5),
                      key=lambda x: -x[1]["score"])[: st.n_max + 6]
        analyst = NewsAnalyst(cfg)
        _build["step"] = f"Claude liest Nachrichten zu {len(pool)} Kandidaten …"
        w = cfg.weight_technical + cfg.weight_news

        def deep(item):
            sym, s = item
            hl = get_headlines(sym, cfg.news_max_age_hours, cfg.names.get(sym), cfg.german_news)
            news = analyst.analyze(sym, hl, f"Kurs {s['price']:.2f} EUR, {s['tech']}", use_claude=analyst.mode == "claude")
            return sym, news, hl

        with ThreadPoolExecutor(max(1, cfg.claude_parallel)) as ex:
            for sym, news, hl in ex.map(deep, pool):
                s = signals[sym]
                s["score"] = s["tech_score"] if news.n_headlines == 0 else \
                    (cfg.weight_technical * s["tech_score"] + cfg.weight_news * news.score) / w
                s["analyst"] = news.analyst if analyst.mode == "claude" else "lexicon_only"
                s["headlines"] = [h.title for h in hl]
                s["summary"] = news.summary
        for s in signals.values():
            s["tradable"] = True  # Aufbau auch außerhalb der Handelszeit: zum letzten Kurs
        _build["step"] = "Portfolio wird zusammengestellt …"
        acts = setup(cfg, capital, style, signals, cfg.names, notify)
        _build.update(running=False, step=f"Fertig: {len(acts)} Käufe", error=None)
    except Exception as e:
        log.exception("Aufbau des Musterdepots fehlgeschlagen")
        _build.update(running=False, step="", error=str(e))


# --------------------------------------------------------------------------- Wertverlauf
VALUES_FILE = STATE_DIR / "model_values.json"
_last_record = [0.0]


def record_value(cfg: Config, every_seconds: int = 300) -> float | None:
    """Gesamtwert (Wertpapiere zu Live-Kursen + Cash) alle 5 Minuten aufzeichnen – für die 1T/1W-Grafik."""
    import time as _t
    if _t.time() - _last_record[0] < every_seconds:
        return None
    state = load_state()
    if not state.get("start_capital"):
        return None
    from .portfolio_service import overview
    txns = pf.load_txns("model")
    ov = overview(cfg, cash_of(state["start_capital"], txns), "model", txns)
    # Ohne aktuelle Kurse für einen nennenswerten Teil lieber keinen Punkt (sonst Zacken im Chart). Kleinstpositionen
    # ohne Kurs (z. B. vom Handel ausgesetzt) laufen mit dem letzten bekannten Kurs mit – sonst stünde die Kurve still.
    total_val = sum(p["value"] for p in ov["positions"]) or 1.0
    if sum(p["value"] for p in ov["positions"] if p["stale"]) / total_val > 0.05:
        return None
    total = round(ov["kpi"]["total"], 2)
    _last_record[0] = _t.time()
    try:
        data = json.loads(VALUES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        data = []
    data.append([datetime.now(timezone.utc).isoformat(timespec="seconds"), total])
    del data[:-20000]  # ca. 70 Tage in 5-Minuten-Schritten
    tmp = VALUES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(VALUES_FILE)
    return total


def load_values() -> list[list]:
    try:
        return json.loads(VALUES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def capital_at(state: dict, day: str) -> float:
    """Eingesetztes Kapital an einem Tag (Startkapital ± Ein-/Auszahlungen bis dahin)."""
    flows = state.get("flows", [])
    initial = state["start_capital"] - sum(f["amount"] for f in flows)
    return initial + sum(f["amount"] for f in flows if f["time"][:10] <= day)


def daily_totals(state: dict, txns: list[pf.Txn], securities_by_day: dict[str, float]) -> list[dict]:
    """Tageswerte inkl. Cash. securities_by_day: Wertpapierwert je Tag (YYYY-MM-DD)."""
    out = []
    for day in sorted(securities_by_day):
        spent = sum(t.cash_out for t in txns if t.date <= day)
        cap = capital_at(state, day)
        out.append({"t": day, "v": round(securities_by_day[day] + cap - spent, 2), "capital": cap})
    return out


def period_change(points: list[dict], state: dict) -> dict:
    """Veränderung im Zeitraum: € ohne Ein-/Auszahlungen, % bezogen auf den Startwert des Zeitraums."""
    if len(points) < 2:
        return {"abs": None, "pct": None}
    a, b = points[0], points[-1]
    # Tagespunkte (nur Datum) stehen für den Tagesschluss -> Zeitpunkt 23:59:59 für den Vergleich
    key = lambda t: (t if len(t) > 10 else t + "T23:59:59")[:19]  # noqa: E731
    flows = sum(f.get("market", f["amount"]) for f in state.get("flows", []) if key(a["t"]) < f["time"][:19] <= key(b["t"]))
    abs_ = b["v"] - a["v"] - flows
    return {"abs": round(abs_, 2), "pct": abs_ / a["v"] if a["v"] else None}
