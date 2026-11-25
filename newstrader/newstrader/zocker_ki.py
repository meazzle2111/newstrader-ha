"""Zocker mit freier KI-Entscheidung: Ziel maximale Rendite bei minimalem Verlust.

Alle 30 Minuten (werktags 9–22 Uhr) bekommt die KI (Gemini, sonst Claude) das komplette Lagebild – Konto, Positionen,
Kandidaten (stärkstes Momentum + beste Signale) mit Kurslage, Nachrichten, Schlagzeilen und Zahlen-Terminen, Marktlage,
letzte Trades – und entscheidet frei: kaufen (Größe, Hebel), verkaufen, abwarten. Sie darf Positionen verkaufen, um Geld
für bessere Chancen frei zu machen, und so viel Cash halten, wie sie für richtig hält.

Sicherheitsnetz (bleibt immer): Stops vor jeder Knock-out-Schwelle (mechanisch, sofort), höchstens 30 % des Kontos je
Position, Hebel höchstens 5, nur echte bei Trade Republic handelbare Produkte. Ohne KI keine neuen Käufe.

Die Entscheidung wird im Hintergrund geholt (die Stop-Prüfung wartet nie) und im nächsten Durchlauf mit frischen Kursen
ausgeführt. Jede Runde wird mit Begründung gespeichert.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
FILE = STATE_DIR / "zocker_ki.json"
EVERY_MIN = 30
HOURS = (9, 22)              # werktags, deutsche Zeit
MAX_WEIGHT = 0.30            # Einsatz je Position höchstens 30 % des Kontos
MAX_LEVERAGE = 5.0
MAX_POSITIONS = 8
MIN_ORDER = 100.0
PENDING_MAX_MIN = 45         # nicht ausgeführte Anweisungen verfallen danach (Lage hat sich geändert)

SYSTEM = """Du verwaltest ein Spielgeld-Konto (Paper-Trading) für einen privaten Anleger. Vorgabe: maximale Rendite bei
minimalem Verlust. Du entscheidest frei: welche Aktien, wie viel, mit wie viel Hebel, wann verkaufen, wie viel Cash.
Du darfst Positionen verkaufen, um Geld für bessere Chancen frei zu machen, und darfst ganz oder teilweise in Cash bleiben.

Instrumente: Aktien direkt (Hebel 1) oder echte Knock-out-Scheine (Hebel 2–5). Bei Hebel L sitzt ein automatischer Stop bei
etwa 60 % des Abstands zur Knock-out-Schwelle (Hebel 5: Stop ~12 % unter Einstieg, Knock-out ~20 %). Ein Rückgang bis zum Stop
kostet bei Hebel 5 rund 60 % des Einsatzes – Hebel nur, wenn die Lage klar ist. Aktien ohne Hebel: Stop 25 %.
Grenzen: Einsatz je Position höchstens 30 % des Kontos (weight 0.02–0.30), höchstens 8 Positionen, nur Werte aus der Liste.
Kosten: ~1 € je Order plus Spanne – kein Hin und Her ohne Grund. Kurz vor Quartalszahlen oder anderen Ereignissen mit
Kurslücken-Risiko ist Hebel besonders gefährlich.

Denk wie ein disziplinierter Trader: Trends und Nachrichten nutzen, Verluste klein halten, Gewinner laufen lassen,
Übertreibungen und gebrochene Trends meiden, Klumpenrisiken vermeiden. Lieber eine gute Gelegenheit als fünf mittelmäßige.
Gib nur Aktionen an, die du jetzt wirklich willst (leere Liste = alles bleibt, wie es ist).
actions: action "buy" (symbol, weight = Anteil des Kontos als Einsatz, leverage 1–5) oder "sell" (symbol, ganze Position).
reason je Aktion: 1–2 konkrete deutsche Sätze. cash_target: welcher Cash-Anteil dir gerade richtig erscheint (0–1).
summary: 2–3 Sätze zur Lage und deiner Strategie. Antworte nur im JSON-Format."""

SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "items": {
            "type": "object",
            "properties": {"action": {"type": "string", "enum": ["buy", "sell"]}, "symbol": {"type": "string"},
                           "weight": {"type": "number"}, "leverage": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["action", "symbol", "weight", "leverage", "reason"], "additionalProperties": False}},
        "cash_target": {"type": "number"},
        "summary": {"type": "string"},
    },
    "required": ["actions", "cash_target", "summary"],
    "additionalProperties": False,
}

_thinking = {"on": False}
_lock = threading.Lock()


def load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("rounds", [])        # letzte Entscheidungsrunden (für Anzeige und Auswertung)
    d.setdefault("pending", None)     # noch auszuführende Runde
    d.setdefault("last_ask", None)
    return d


def _save(d: dict) -> None:
    d["rounds"] = d["rounds"][-60:]
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(FILE)


def due(now: datetime, d: dict, force: bool = False) -> bool:
    from . import portfolio as pf
    local = now.astimezone(pf.BERLIN)
    if local.weekday() >= 5 or not HOURS[0] <= local.hour < HOURS[1]:
        return False
    if force or not d.get("last_ask"):
        return True
    return now - datetime.fromisoformat(d["last_ask"]) >= timedelta(minutes=EVERY_MIN)


# --------------------------------------------------------------------------- Lagebild
def candidates(cfg: Config, snaps: dict, held: set[str], momentum_ranking: list[dict], n_mom: int = 10, n_sig: int = 6) -> list[str]:
    """Stärkstes Momentum + beste aktuelle Signale (mit KI-geprüften Nachrichten) – ohne Krypto/Indizes."""
    from .momentum import eligible
    out = [r["symbol"] for r in momentum_ranking[:n_mom] if r["symbol"] not in held]
    sig = sorted((s for s, v in snaps.items() if eligible(s) and s not in held and s not in out and v.tradable
                  and v.news.analyst == "claude" and v.news.n_headlines),
                 key=lambda s: -snaps[s].combined(cfg))[:n_sig]
    return out + sig


def _line_features(sym: str) -> str:
    from .market import get_daily_closes
    from .spekulation import features
    f = features(get_daily_closes(sym))
    if not f:
        return "Kurslage unbekannt"
    rel = lambda a: "–" if not a else f"{f['price'] / a - 1:+.1%}"  # noqa: E731
    return (f"5 Tage {f['ret5']:+.1%}, 20 Tage {f['ret20']:+.1%}, zum 20-Tage-Schnitt {rel(f['sma20'])}, zum 50-Tage-Schnitt "
            f"{rel(f['sma50'])}, RSI {f['rsi']:.0f}, Tagesschwankung {f['vol_day']:.1%}")


def build_prompt(cfg: Config, broker, snaps: dict, cands: list[str], momentum_ranking: list[dict], market: str,
                 recent: list[dict]) -> str:
    from .spekulation import _headlines, earnings_date
    prices = {s: v.price for s, v in snaps.items()}
    eq = broker.equity(prices)
    mom = {r["symbol"]: (i + 1, r["mom"]) for i, r in enumerate(momentum_ranking)}
    L = [f"Konto: {eq:,.0f} € Gesamtwert, Cash {broker.cash:,.0f} € ({broker.cash / eq:.0%}). Marktlage: {market or 'unbekannt'}.",
         "", "OFFENE POSITIONEN:" if broker.positions else "OFFENE POSITIONEN: keine"]
    for sym, p in broker.positions.items():
        px = prices.get(sym, p["avg_price"])
        pnl = broker.pnl(p, px)
        lv = broker.levels(p)
        n = snaps[sym].news if sym in snaps else None
        L.append(f"- {cfg.names.get(sym, sym)} ({sym}): Hebel {p['leverage']:g}, Einsatz {p['margin']:.0f} € "
                 f"({p['margin'] / eq:.0%}), Einstieg {p['avg_price']:.2f} €, jetzt {px:.2f} €, G/V {pnl:+.0f} € "
                 f"({pnl / p['margin']:+.0%} auf den Einsatz), Stop {lv['stop']:.2f} €"
                 + (f", Knock-out {lv['liquidation']:.2f} €" if lv.get("liquidation") else "")
                 + f", seit {p['opened'][:10]}")
        L.append(f"  Kurslage: {_line_features(sym)}" + (f"; Momentum Platz {mom[sym][0]} ({mom[sym][1]:+.0%} in 6 Mon.)" if sym in mom else ""))
        if n and n.n_headlines:
            L.append(f"  Nachrichten {n.score:+.2f}: {n.summary[:200]}")
    L += ["", "KANDIDATEN (Kurse in €):"]
    for sym in cands:
        v = snaps.get(sym)
        if not v:
            continue
        n = v.news
        earn = earnings_date(sym)
        L.append(f"- {cfg.names.get(sym, sym)} ({sym}): Kurs {v.price:.2f} €, {'handelbar' if v.tradable else 'Börse zu'}"
                 + (f", Momentum Platz {mom[sym][0]} ({mom[sym][1]:+.0%} in 6 Mon.)" if sym in mom else "")
                 + f", Bot-Signal {v.combined(cfg):+.2f}" + (f", Quartalszahlen {earn}" if earn else ""))
        L.append(f"  Kurslage: {_line_features(sym)}")
        if n.n_headlines:
            L.append(f"  Nachrichten {n.score:+.2f} ({'KI' if n.analyst == 'claude' else 'Wörterbuch'}): {n.summary[:200]}")
        for h in _headlines(sym)[:3]:
            L.append(f"    · {h}")
    if recent:
        L += ["", "LETZTE ABGESCHLOSSENE TRADES:"]
        for r in recent[:6]:
            L.append(f"- {r['symbol']} Hebel {r.get('lev')}: {r['ret']:+.0%} auf den Einsatz ({r['pnl']:+.0f} €) – {r['reason'][:80]}")
    return "\n".join(L)


# --------------------------------------------------------------------------- Entscheidung holen (Hintergrund)
def ask(analyst, prompt: str, now: datetime) -> None:
    """KI fragen und die Antwort als „ausstehend“ speichern (wird im nächsten Durchlauf ausgeführt)."""
    try:
        data, provider = analyst.ask_json(SYSTEM, prompt, SCHEMA)
        acts = []
        for a in data.get("actions") or []:
            if a.get("action") in ("buy", "sell") and a.get("symbol"):
                acts.append({"action": a["action"], "symbol": str(a["symbol"]).strip(),
                             "weight": float(a.get("weight") or 0), "leverage": float(a.get("leverage") or 1),
                             "reason": str(a.get("reason", ""))[:300], "status": "offen"})
        rnd = {"time": now.isoformat(timespec="seconds"), "provider": provider, "summary": str(data.get("summary", ""))[:500],
               "cash_target": max(0.0, min(1.0, float(data.get("cash_target") or 0))), "actions": acts}
        log.info("Zocker-KI (%s): %s – %s", provider, rnd["summary"][:160],
                 "; ".join(f"{a['action']} {a['symbol']}" for a in acts) or "keine Aktion")
    except Exception as e:  # noqa: BLE001 – ohne KI keine neuen Käufe; Stops laufen weiter
        log.warning("Zocker-KI nicht erreichbar: %s", e)
        rnd = {"time": now.isoformat(timespec="seconds"), "provider": None, "error": str(e)[:200], "actions": [],
               "summary": "KI nicht erreichbar – keine neuen Käufe, Stops laufen weiter."}
    with _lock:
        d = load()
        d["pending"] = rnd if rnd["actions"] else None
        d["rounds"].append(rnd)
        _save(d)
    if rnd["actions"]:                    # gleich umsetzen: Bot zieht den nächsten Durchlauf vor (frische Kurse)
        try:
            from . import bot
            bot.WAKE.set()
        except Exception:  # noqa: BLE001
            pass


def start(analyst, prompt: str, now: datetime) -> bool:
    if analyst is None or _thinking["on"]:
        return False
    _thinking["on"] = True

    def go():
        try:
            ask(analyst, prompt, now)
        finally:
            _thinking["on"] = False
    threading.Thread(target=go, daemon=True, name="zocker-ki").start()
    return True


# --------------------------------------------------------------------------- Ausführen
def execute(broker, cfg: Config, snaps: dict, now: datetime) -> int:
    """Ausstehende Entscheidung mit frischen Kursen umsetzen (erst Verkäufe, dann Käufe). Gibt die Zahl der Orders."""
    from .strategy import _real_product
    with _lock:
        d = load()
        rnd = d.get("pending")
        if not rnd:
            return 0
        if now - datetime.fromisoformat(rnd["time"]) > timedelta(minutes=PENDING_MAX_MIN):
            for a in rnd["actions"]:
                if a["status"] == "offen":
                    a["status"] = "verfallen (Lage veraltet)"
            d["pending"] = None
            _sync_round(d, rnd)
            _save(d)
            return 0
    prices = {s: v.price for s, v in snaps.items()}
    done = 0
    searches = [0]
    for a in sorted(rnd["actions"], key=lambda x: x["action"] != "sell"):      # Verkäufe zuerst (machen Cash frei)
        if a["status"] != "offen":
            continue
        sym, snap = a["symbol"], snaps.get(a["symbol"])
        if a["action"] == "sell":
            if sym not in broker.positions:
                a["status"] = "nicht im Depot"
            elif snap and snap.tradable:
                broker.close(sym, snap.price, f"KI: {a['reason']}")
                a["status"] = "verkauft"
                done += 1
            continue
        # Kauf
        if sym in broker.positions:
            a["status"] = "schon im Depot"
            continue
        if not snap:
            a["status"] = "unbekannter Wert"
            continue
        if not snap.tradable:
            continue                                   # Börse zu: im nächsten Durchlauf (bis zum Verfall)
        if len(broker.positions) >= MAX_POSITIONS:
            a["status"] = f"höchstens {MAX_POSITIONS} Positionen"
            continue
        eq = broker.equity(prices)
        lev_wish = max(1.0, min(MAX_LEVERAGE, round(a["leverage"])))
        product = None
        if lev_wish >= 2 and cfg.real_products:
            isin = (cfg.isins or {}).get(sym)
            if isin:
                from . import traderepublic as tr
                if (isin, "long") not in tr._ko_cache and searches[0] >= cfg.product_searches_per_cycle:
                    continue                           # Scheinsuche im nächsten Durchlauf
            pc = replace(cfg, product_target_leverage=lev_wish, product_min_leverage=max(1.5, lev_wish * 0.75),
                         product_max_leverage=min(MAX_LEVERAGE * 1.3, lev_wish * 1.3))
            product = _real_product(pc, snap, "long", searches)
        lev = product["leverage"] if product else 1.0
        weight = max(0.0, min(MAX_WEIGHT, a["weight"]))
        margin = min(weight * eq, (broker.cash - cfg.fee_fixed - 0.5) / (1 + cfg.fee_pct * lev))
        if margin < MIN_ORDER:
            a["status"] = "zu wenig Cash"
            continue
        via = (f"{lev:g}x via {product['name']} ({product['isin']}), KO {product['barrier']:g} {product.get('currency', '')}"
               if product else ("Aktie direkt" + (f" (kein Schein mit Hebel um {lev_wish:g})" if lev_wish >= 2 else "")))
        n = snap.news
        if broker.open(sym, "long", snap.price, margin, lev, f"KI: {a['reason']} · {via}", product=product,
                       entry={"strategy": "ki", "weight": round(weight, 3), "lev_wish": lev_wish, "lev": lev,
                              "news": round(n.score, 3), "n_news": n.n_headlines, "analyst": n.analyst,
                              "reason": a["reason"][:200]}):
            a["status"] = f"gekauft ({lev:g}x, {margin:.0f} €)"
            done += 1
    with _lock:
        d = load()
        if (d.get("pending") or {}).get("time") == rnd["time"]:      # inzwischen keine neuere Entscheidung da
            d["pending"] = None if all(a["status"] != "offen" for a in rnd["actions"]) else rnd
        _sync_round(d, rnd)
        _save(d)
    return done


def _sync_round(d: dict, rnd: dict) -> None:
    for i, r in enumerate(d["rounds"]):
        if r["time"] == rnd["time"]:
            d["rounds"][i] = rnd


def run(broker, cfg: Config, snaps: dict, analyst, momentum_ranking: list[dict], market: str = "",
        recent: list[dict] | None = None) -> None:
    """Ein Durchlauf: Stops (mechanisch), ausstehende KI-Entscheidung ausführen, bei Bedarf neue Entscheidung holen."""
    from .strategy import check_risk
    prices = {s: v.price for s, v in snaps.items()}
    broker.accrue_financing(prices)
    broker.update_extremes(prices)
    now = broker.clock()
    before = set(broker.positions)
    check_risk(broker, cfg, {s: v.price for s, v in snaps.items() if v.tradable})
    stopped = before - set(broker.positions)
    execute(broker, cfg, snaps, now)
    d = load()
    if due(now, d, force=bool(stopped)) and analyst is not None and not _thinking["on"]:
        cands = candidates(cfg, snaps, set(broker.positions), momentum_ranking)
        prompt = build_prompt(cfg, broker, snaps, cands, momentum_ranking, market, recent or [])
        with _lock:
            d = load()
            d["last_ask"] = now.isoformat(timespec="seconds")
            d["candidates"] = cands
            _save(d)
        start(analyst, prompt, now)


def view() -> dict:
    d = load()
    last = d["rounds"][-1] if d["rounds"] else None
    nxt = None
    if d.get("last_ask"):
        nxt = (datetime.fromisoformat(d["last_ask"]) + timedelta(minutes=EVERY_MIN)).isoformat(timespec="minutes")
    return {"last": last, "history": d["rounds"][-8:][::-1], "thinking": _thinking["on"], "next": nxt,
            "pending": bool(d.get("pending")), "candidates": d.get("candidates", []),
            "rules": {"every": EVERY_MIN, "hours": HOURS, "max_weight": MAX_WEIGHT, "max_leverage": MAX_LEVERAGE,
                      "max_positions": MAX_POSITIONS}}
