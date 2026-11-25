"""Täglicher spekulativer Hebel-Vorschlag für dein echtes Depot (per Schalter).

Ablauf (Mo–Fr):
- Tagsüber (9:30–22:30) wächst die Vorauswahl für den nächsten Handelstag: die aussichtsreichsten Basiswerte
  (deutliche Signale, DAX, Nasdaq-100) werden nach und nach von der KI (Gemini, sonst Claude) bewertet.
- 22:30 Uhr steht der Plan (nach US-Schluss): bestes Setup mit gutem Chance-Risiko-Verhältnis.
  Bewusst nicht abends einsteigen: Übernacht-Lücken mit Hebel und breite Abend-Spannen.
- 09:00 Uhr: Kurs prüfen, bei Trade Republic handelbaren Knock-out-Schein suchen (Knock-out-Schwelle jenseits
  des Stop-Loss, damit der Stop zuerst greift), Einsatz vorschlagen, Push.
- Danach wird verfolgt, ob Kursziel oder Stop erreicht werden (Push), und die Trefferquote mitgeschrieben.
Es wird nichts gekauft – du entscheidest und handelst selbst.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import numpy as np

from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
SPEC_FILE = STATE_DIR / "spekulation.json"
INDICES = {"^GDAXI": ("DAX", "DE0008469008"), "^NDX": ("Nasdaq-100", "US6311011026"),
           "^GSPC": ("S&P 500", "US78378X1072"), "GC=F": ("Gold", "XC0009655157"), "BZ=F": ("Öl (Brent)", "XC0009677409")}
N_STOCKS = 6                # so viele Einzelwerte kommen zu den festen Basiswerten dazu
POOL = 25                   # aus so vielen der stärksten Signale wird nach Bewegung ausgewählt
MIN_VOL_DAY = 0.008         # unter 0,8 % typischer Tagesschwankung: zu wenig Bewegung für ein Kursziel
EARNINGS_FILE = STATE_DIR / "cache" / "earnings.json"
PREP_AT = (22, 30)          # Plan steht (nach US-Schluss und Abendnachrichten)
PUBLISH_AT = (9, 0)         # Vorschlag um
MIN_RR = 1.5                # Chance-Risiko mindestens
MIN_CONVICTION = 0.40       # darunter kein Vorschlag; Qualität A/B/C steuert den Einsatz
GRADES = ((0.70, "A", 1.0), (0.55, "B", 0.75), (0.0, "C", 0.5))   # Überzeugung ab, Note, Anteil am Risiko-Budget
_running = {"prep": False}
PROGRESS: dict = {"text": "", "done": 0, "total": 0, "results": []}   # live für die Oberfläche
_lock = threading.Lock()

SYSTEM = """Du bist ein erfahrener, disziplinierter Kurzfrist-Trader (Haltedauer 1–10 Handelstage).
Du bekommst Kursdaten und Schlagzeilen zu einem Basiswert. Wähle für den nächsten Handelstag die bessere Richtung
für einen spekulativen Hebel-Trade (Knock-out-Schein): long oder short.
Regeln:
- Nur handeln, wenn Trend/Momentum und Nachrichtenlage zusammenpassen oder ein klarer Katalysator ansteht.
- stop: Kurs des Basiswerts (in der angegebenen Währung), an dem die Idee widerlegt ist – technisch sinnvoll
  (z. B. unter Unterstützung bzw. über Widerstand), nicht enger als die normale Tagesschwankung.
- target: realistisches Kursziel des Basiswerts innerhalb der Haltedauer.
- entry_max (long) bzw. entry_min (short): schlechtester Einstiegskurs, ab dem der Trade nicht mehr lohnt.
- Nenne immer die bessere Richtung (long oder short) mit ehrlicher conviction (0–1) – auch ein mittelmäßiges Setup
  bekommt eine Richtung, nur mit niedrigerer conviction. "none" nur, wenn ein Trade unvertretbar wäre
  (z. B. völlig richtungslos, extreme Unsicherheit, Zahlen morgen früh ohne klare Erwartung).
- Stehen Quartalszahlen innerhalb der Haltedauer an: Kurslücken-Risiko mit Hebel beachten – entweder bewusst auf
  die Zahlen spekulieren (dann begründen) oder die Haltedauer davor enden lassen; im Zweifel "none".
- rationale: 2–3 kurze deutsche Sätze mit dem Kern der Idee. risks: der wichtigste Grund, warum es schiefgehen kann.
Antworte nur im JSON-Format."""

SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["long", "short", "none"]},
        "conviction": {"type": "number"},
        "entry_limit": {"type": "number"},
        "stop": {"type": "number"},
        "target": {"type": "number"},
        "holding_days": {"type": "integer"},
        "rationale": {"type": "string"},
        "risks": {"type": "string"},
    },
    "required": ["direction", "conviction", "entry_limit", "stop", "target", "holding_days", "rationale", "risks"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- Zustand
def load() -> dict:
    try:
        d = json.loads(SPEC_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("plans", {})       # Handelstag -> Plan (vorbereitet am Vortag)
    d.setdefault("history", [])     # veröffentlichte Vorschläge mit Ergebnis
    return d


def save(d: dict) -> None:
    d["history"] = d["history"][-200:]
    d["plans"] = dict(sorted(d["plans"].items())[-20:])
    pf._atomic_write(SPEC_FILE, d)


def set_enabled(on: bool) -> dict:
    with _lock:
        d = load()
        d["enabled"] = bool(on)
        save(d)
    return status()


def next_trading_day(day) -> str:
    d = day + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def status() -> dict:
    d = load()
    today = datetime.now(pf.BERLIN).date()
    hist = d["history"]
    done = [h for h in hist if h.get("result") in ("Ziel", "Stop", "Knock-out", "abgelaufen")]
    wins = [h for h in done if (h.get("product_pct") or 0) > 0]
    nxt = next_trading_day(today)
    w = d.get("watch", {}).get(nxt) or d.get("watch", {}).get(today.isoformat())
    watch = None
    if w:
        rows = sorted(w["items"].values(), key=lambda e: e.get("rank", 99))
        watch = {"for_day": w["for_day"], "updated": w.get("updated"), "leader": w.get("leader"),
                 "items": [{k: e.get(k) for k in ("symbol", "name", "score", "direction", "conviction", "rr", "ok", "why",
                                                   "evaluated", "stop", "target", "currency", "rationale", "provider",
                                                   "earnings", "vol_day")}
                           for e in rows]}
    return {"enabled": d["enabled"], "preparing": _running["prep"], "watch": watch, "progress": dict(PROGRESS),
            "plan_next": d["plans"].get(next_trading_day(today)), "plan_today": d["plans"].get(today.isoformat()),
            "active": [h for h in hist if not h.get("result")], "history": hist[-15:][::-1],
            "stats": {"n": len(done), "wins": len(wins),
                      "avg_product_pct": (sum(h.get("product_pct") or 0 for h in done) / len(done)) if done else None}}


# --------------------------------------------------------------------------- Kennzahlen
def features(closes) -> dict | None:
    if closes is None or len(closes) < 60:
        return None
    c = closes.astype(float)
    px = float(c.iloc[-1])
    rets = c.pct_change().dropna()
    delta = c.diff()
    up, down = delta.clip(lower=0).rolling(14).mean().iloc[-1], (-delta.clip(upper=0)).rolling(14).mean().iloc[-1]
    rsi = 100 - 100 / (1 + up / down) if down else 100.0
    return {"price": px, "sma20": float(c.rolling(20).mean().iloc[-1]), "sma50": float(c.rolling(50).mean().iloc[-1]),
            "sma200": float(c.rolling(200).mean().iloc[-1]) if len(c) >= 200 else None,
            "hi20": float(c.iloc[-20:].max()), "lo20": float(c.iloc[-20:].min()),
            "hi60": float(c.iloc[-60:].max()), "lo60": float(c.iloc[-60:].min()),
            "ret5": px / float(c.iloc[-6]) - 1, "ret20": px / float(c.iloc[-21]) - 1,
            "vol_day": float(rets.iloc[-20:].std()), "rsi": float(rsi)}


def _fmt_features(f: dict, cur: str) -> str:
    g = lambda x: "–" if x is None else f"{x:,.2f}"  # noqa: E731
    return (f"Kurs {g(f['price'])} {cur}; SMA20 {g(f['sma20'])}, SMA50 {g(f['sma50'])}, SMA200 {g(f['sma200'])}; "
            f"20-Tage-Hoch/Tief {g(f['hi20'])}/{g(f['lo20'])}; 60-Tage-Hoch/Tief {g(f['hi60'])}/{g(f['lo60'])}; "
            f"5 Tage {f['ret5']:+.1%}, 20 Tage {f['ret20']:+.1%}; RSI {f['rsi']:.0f}; "
            f"typische Tagesschwankung {f['vol_day']:.1%}")


def validate(idea: dict, f: dict) -> tuple[bool, str, float]:
    """Plausibilität: Stop/Ziel auf der richtigen Seite, Stop nicht im Rauschen, Chance-Risiko ≥ MIN_RR."""
    d, px = idea.get("direction"), f["price"]
    if d not in ("long", "short"):
        return False, "kein Setup", 0.0
    stop, target = float(idea["stop"]), float(idea["target"])
    sign = 1 if d == "long" else -1
    if sign * (px - stop) <= 0 or sign * (target - px) <= 0:
        return False, "Stop/Ziel auf der falschen Seite", 0.0
    risk, reward = abs(px - stop) / px, abs(target - px) / px
    if risk < 0.5 * f["vol_day"]:
        return False, "Stop zu eng (im normalen Tagesrauschen)", 0.0
    if risk > 5 * f["vol_day"] or risk > 0.15:
        return False, "Stop zu weit", 0.0
    rr = reward / risk
    if rr < MIN_RR:
        return False, f"Chance-Risiko nur {rr:.1f}", rr
    if float(idea.get("conviction", 0)) < MIN_CONVICTION:
        return False, "zu wenig überzeugend", rr
    return True, "", rr


# --------------------------------------------------------------------------- Vorbereitung (Vortag)
def earnings_date(sym: str) -> str | None:
    """Nächster Termin für Quartalszahlen (Yahoo), einen Tag zwischengespeichert. None = unbekannt/keiner."""
    import time as _t
    try:
        cache = json.loads(EARNINGS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    hit = cache.get(sym)
    if hit and _t.time() - hit[0] < 86400:
        return hit[1]
    date = None
    try:
        import yfinance as yf
        cal = yf.Ticker(sym).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        today = datetime.now(pf.BERLIN).date()
        future = sorted(d for d in (dates or []) if hasattr(d, "isoformat") and d >= today)
        date = future[0].isoformat() if future else None
    except Exception:  # noqa: BLE001 – ohne Termin weiter
        pass
    cache[sym] = [_t.time(), date]
    EARNINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EARNINGS_FILE.write_text(json.dumps(cache))
    return date


def candidates(cfg: Config) -> list[dict]:
    """Einzelwerte mit deutlichem Signal UND genug Bewegung (+ Quartalszahlen-Termin), dazu feste Basiswerte
    (DAX, Nasdaq-100, S&P 500, Gold, Öl). Nur mit ISIN, damit TR-Scheine suchbar sind."""
    from .market import get_daily_closes
    try:
        cyc = json.loads((STATE_DIR / "last_cycle.json").read_text()).get("symbols", {})
    except (FileNotFoundError, json.JSONDecodeError):
        cyc = {}
    pool = []
    for sym, v in cyc.items():
        isin = cfg.isins.get(sym)
        if not isin or sym.endswith(("-EUR", "-USD")) or v.get("combined") is None or sym in INDICES:
            continue
        pool.append({"symbol": sym, "name": cfg.names.get(sym, sym), "isin": isin, "score": v["combined"],
                     "summary": v.get("news_summary", "")})
    pool.sort(key=lambda x: -abs(x["score"]))
    picked = []
    for c in pool[:POOL]:
        f = features(get_daily_closes(c["symbol"]))
        if not f or f["vol_day"] < MIN_VOL_DAY:
            continue
        c["vol_day"] = f["vol_day"]
        c["rank_score"] = abs(c["score"]) * min(f["vol_day"] / 0.015, 2.0)   # Signal × Bewegung
        picked.append(c)
    picked.sort(key=lambda x: -x["rank_score"])
    out = picked[:N_STOCKS]
    for c in out:
        c["earnings"] = earnings_date(c["symbol"])
    for sym, (name, isin) in INDICES.items():
        out.append({"symbol": sym, "name": name, "isin": isin, "score": cyc.get(sym, {}).get("combined", 0.0),
                    "summary": "", "earnings": None})
    return out


def _headlines(sym: str) -> list[str]:
    try:
        data = json.loads((STATE_DIR / "cache" / "headlines.json").read_text())
        return [f"[{h['source']}] {h['title']}" for h in data.get(sym, [0, []])[1][:12]]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return []


def evaluate(cfg: Config, analyst, c: dict, for_day: str) -> dict | None:
    """Einen Kandidaten gründlich von der KI bewerten lassen. Gibt den Eintrag (ok oder verworfen mit Grund)."""
    from .market import get_currency, get_daily_closes
    closes = get_daily_closes(c["symbol"], max_age_minutes=180)
    f = features(closes)
    if not f:
        return None
    cur = get_currency(c["symbol"])
    hl = _headlines(c["symbol"])
    prompt = (f"Basiswert: {c['name']} ({c['symbol']}), Währung {cur}\nKurslage: {_fmt_features(f, cur)}\n"
              f"Bot-Signal (−1…+1): {c['score']:+.2f}\n"
              + (f"Nachrichtenlage kurz: {c['summary']}\n" if c.get("summary") else "")
              + (f"Quartalszahlen: am {c['earnings']}\n" if c.get("earnings") else "")
              + ("Schlagzeilen:\n" + "\n".join(f"- {h}" for h in hl) if hl else "Keine aktuellen Schlagzeilen.")
              + f"\n\nDer Trade würde am {for_day} um 9 Uhr eröffnet.")
    idea, provider = analyst.ask_json(SYSTEM, prompt, SCHEMA)
    ok, why, rr = validate(idea, f)
    return {**c, **{k: idea.get(k) for k in SCHEMA["properties"]}, "price_ref": f["price"], "currency": cur,
            "vol_day": f["vol_day"], "rr": round(rr, 2), "provider": provider, "ok": ok, "why": why,
            "evaluated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "score_at_eval": c["score"]}


def _best(entries: list[dict]) -> dict | None:
    return max((e for e in entries if e.get("ok")), key=lambda i: float(i["conviction"]) * min(i["rr"], 4.0), default=None)


def prepare(cfg: Config, analyst, for_day: str, reuse: dict | None = None, manual: bool = False) -> dict:
    """Plan für `for_day` festlegen. reuse: schon tagsüber bewertete Kandidaten (Vorauswahl) – nur Fehlendes wird gefragt."""
    entries = []
    cands = candidates(cfg)
    PROGRESS.update(text="Kandidaten werden zusammengestellt …", done=0, total=len(cands), results=[])
    for i, c in enumerate(cands):
        PROGRESS.update(text=f"Bewerte {c['name']} ({i + 1}/{len(cands)}) …", done=i)
        e = (reuse or {}).get(c["symbol"])
        stale = e and e.get("evaluated") and (
            datetime.now(timezone.utc) - datetime.fromisoformat(e["evaluated"])).total_seconds() > STALE_S
        if not e or not e.get("evaluated") or stale:
            try:
                e = evaluate(cfg, analyst, c, for_day)
            except Exception as ex:  # noqa: BLE001
                log.warning("Spekulation: KI-Bewertung %s fehlgeschlagen: %s", c["symbol"], ex)
                continue
        if e:
            entries.append(e)
            PROGRESS["results"].append({"name": e["name"], "ok": e.get("ok"), "direction": e.get("direction"),
                                        "why": e.get("why"), "rr": e.get("rr")})
        else:
            PROGRESS["results"].append({"name": c["name"], "ok": False, "why": "KI nicht erreichbar"})
    best = _best(entries)
    PROGRESS.update(text=(f"Fertig: {best['name']} ({best['direction']})" if best else "Fertig: kein lohnendes Setup"),
                    done=len(cands))
    plan = {"for_day": for_day, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "idea": best, "checked": len(entries), "manual": manual,
            "rejected": [{"name": r["name"], "why": r["why"]} for r in entries if not r.get("ok")][:10]}
    with _lock:
        d = load()
        d["plans"][for_day] = plan
        # Ergebnisse auch in die Vorauswahl-Liste übernehmen (sonst zeigt sie alte Stände)
        w = d.setdefault("watch", {}).setdefault(for_day, {"for_day": for_day, "items": {}})
        for rank, e in enumerate(entries):
            w["items"][e["symbol"]] = {**w["items"].get(e["symbol"], {}), **e, "rank": rank}
        w["leader"] = best["symbol"] if best else None
        w["updated"] = plan["created"]
        save(d)
    log.info("Spekulation für %s festgelegt: %s", for_day,
             f"{best['direction']} {best['name']} (Überzeugung {float(best['conviction']):.0%}, CRV {best['rr']})" if best
             else f"kein Setup ({len(entries)} geprüft)")
    return plan


# --------------------------------------------------------------------------- Vorauswahl über den Tag
WATCH_REFRESH_S = 30 * 60   # Kandidatenliste neu zusammenstellen
EVAL_EVERY_S = 10 * 60      # je ein Kandidat pro 10 Minuten zur KI
STALE_S = 4 * 3600          # Bewertung gilt so lange (außer das Signal ändert sich deutlich)
_watch_mem = {"refreshed": 0.0, "evaluated": 0.0}


def watch_step(cfg: Config, analyst, for_day: str) -> None:
    """Ein Schritt der Vorauswahl: Liste auffrischen, den dringendsten Kandidaten bewerten."""
    import time as _t
    with _lock:
        d = load()
        w = d.setdefault("watch", {}).get(for_day) or {"for_day": for_day, "items": {}}
    now = _t.time()
    if now - _watch_mem["refreshed"] >= WATCH_REFRESH_S or not w["items"]:
        _watch_mem["refreshed"] = now
        fresh = {c["symbol"]: c for c in candidates(cfg)}
        items = {}
        for sym, c in fresh.items():
            old = w["items"].get(sym, {})
            items[sym] = {**old, **c, "rank": list(fresh).index(sym)}
        w["items"] = items
        w["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # dringendster Kandidat: noch nie bewertet > Bewertung alt > Signal deutlich verändert
    def need(e):
        if not e.get("evaluated"):
            return 3
        age = now - datetime.fromisoformat(e["evaluated"]).timestamp()
        if abs(e.get("score", 0) - (e.get("score_at_eval") or 0)) > 0.15:
            return 2
        return 1 if age > STALE_S else 0
    todo = sorted((e for e in w["items"].values() if need(e)), key=lambda e: (-need(e), e.get("rank", 99)))
    if todo and now - _watch_mem["evaluated"] >= EVAL_EVERY_S:
        _watch_mem["evaluated"] = now
        c = todo[0]
        PROGRESS.update(text=f"Vorauswahl: bewerte {c['name']} …", done=0, total=0, results=[])
        try:
            e = evaluate(cfg, analyst, {k: c[k] for k in ("symbol", "name", "isin", "score", "summary", "earnings", "vol_day")
                                        if k in c}, for_day)
            if e:
                w["items"][c["symbol"]] = {**c, **e}
        except Exception as ex:  # noqa: BLE001
            log.warning("Spekulation: Vorauswahl %s nicht bewertet: %s", c["symbol"], ex)
            w["items"][c["symbol"]] = {**c, "why": "KI kurz nicht erreichbar – neuer Versuch folgt", "retry": True}
    best = _best(list(w["items"].values()))
    w["leader"] = best["symbol"] if best else None
    with _lock:
        d = load()
        d.setdefault("watch", {})[for_day] = w
        d["watch"] = dict(sorted(d["watch"].items())[-5:])
        save(d)


def grade(conviction) -> tuple[str, float]:
    """Qualitätsnote und Anteil am Risiko-Budget: A volles Risiko, B ¾, C ½."""
    c = float(conviction or 0)
    return next((g, share) for lim, g, share in GRADES if c >= lim)


def stake_for(cfg: Config, idea: dict, depot: float, lev: float | None = None) -> float:
    """Einsatz so, dass am Stop höchstens spec_risk_pct des Depots verloren gehen (bei Note B/C weniger)."""
    px, stop = float(idea.get("price_ref") or idea.get("entry") or 0), float(idea.get("stop") or 0)
    lev = lev or cfg.spec_target_leverage
    loss = min(1.0, lev * abs(px - stop) / px) if px and stop else 0.25
    return depot * cfg.spec_risk_pct * grade(idea.get("conviction"))[1] / max(loss, 0.05)


def cash_plan(cfg: Config, depot: float) -> dict:
    """Wie viel Cash das Portfolio für den nächsten Hebel-Trade braucht – und woher die Zahl kommt.

    1. fester (oder als Vorschau erstellter) Plan, noch nicht veröffentlicht -> sein Einsatz
    2. sonst der führende Kandidat der Vorauswahl für den nächsten Handelstag
    3. sonst der mittlere Einsatz der letzten Vorschläge, ganz ohne Erfahrung 3 % des Depots
    """
    d = load()
    if not d["enabled"]:
        return {"need": 0.0, "source": "aus", "for_day": None}
    today = datetime.now(pf.BERLIN).date()
    nxt = next_trading_day(today)
    for day in (today.isoformat(), nxt):
        plan = d["plans"].get(day)
        if plan and not plan.get("published"):
            if not plan.get("idea"):
                return {"need": 0.0, "source": "Plan: kein Trade", "for_day": day, "final": not plan.get("manual")}
            return {"need": round(stake_for(cfg, plan["idea"], depot), 2), "source": f"Plan {plan['idea'].get('name') or plan['idea'].get('symbol', '')}".strip(),
                    "for_day": day, "final": not plan.get("manual"), "plan_created": plan.get("created")}
    w = (d.get("watch") or {}).get(nxt) or {}
    lead = (w.get("items") or {}).get(w.get("leader") or "")
    if lead and lead.get("ok") and lead.get("stop"):
        return {"need": round(stake_for(cfg, lead, depot), 2), "source": f"Vorauswahl: {lead.get('name')}", "for_day": nxt}
    stakes = sorted(float(h["stake"]) for h in d["history"][-10:] if h.get("stake"))
    if stakes:
        return {"need": stakes[len(stakes) // 2], "source": "Schnitt der letzten Vorschläge", "for_day": nxt}
    return {"need": round(depot * 0.03, 2), "source": "Schätzung (3 % des Depots)", "for_day": nxt}


def cash_needed(cfg: Config, depot: float) -> float:
    """Wie viel Cash das Portfolio für den nächsten Hebel-Trade freihalten soll (0, wenn ausgeschaltet)."""
    return cash_plan(cfg, depot)["need"]


# --------------------------------------------------------------------------- Veröffentlichen (09:00)
def native_price(cfg: Config, sym: str) -> float | None:
    """Aktueller Kurs in der Handelswährung des Basiswerts (Stop/Ziel sind darin angegeben)."""
    from .market import fx_rate, get_currency, get_daily_closes, get_quote
    q = get_quote(sym, cfg.base_currency, cfg)
    cur = get_currency(sym)
    if q is not None and q.price:
        return q.price / (fx_rate(cur, cfg.base_currency) if cur != cfg.base_currency else 1.0)
    closes = get_daily_closes(sym)
    return float(closes.iloc[-1]) if closes is not None and len(closes) else None


def _fmt(x: float, d: int = 2) -> str:
    return f"{x:,.{d}f}".replace(",", "§").replace(".", ",").replace("§", ".")


def publish(cfg: Config, notify, day: str) -> dict | None:
    from . import manager, traderepublic
    d = load()
    plan = d["plans"].get(day)
    if not plan or plan.get("published"):
        return None
    idea = plan.get("idea")
    if not idea:
        notify(cfg, "🎲 Spekulation heute", "Kein Setup mit gutem Chance-Risiko-Verhältnis gefunden – heute kein Trade. "
                                            f"({plan.get('checked', 0)} Basiswerte geprüft)")
        plan["published"] = True
        with _lock:
            d2 = load(); d2["plans"][day] = plan; save(d2)
        return None
    px = native_price(cfg, idea["symbol"]) or idea["price_ref"]
    sign = 1 if idea["direction"] == "long" else -1
    stop, target, limit = float(idea["stop"]), float(idea["target"]), float(idea["entry_limit"] or px)
    invalid = None
    if sign * (px - stop) <= 0:
        invalid = "Kurs steht schon am/hinter dem Stop"
    elif sign * (px - limit) > 0 and sign * (target - px) / (abs(px - stop) or 1) < MIN_RR:
        invalid = "Kurs ist dem Ziel schon zu nahe gelaufen"
    prop = {"day": day, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "symbol": idea["symbol"],
            "name": idea["name"], "isin": idea["isin"], "direction": idea["direction"], "currency": idea["currency"],
            "entry": px, "stop": stop, "target": target, "rr": round(abs(target - px) / abs(px - stop), 2) if px != stop else None,
            "rationale": idea["rationale"], "risks": idea["risks"], "conviction": idea["conviction"],
            "holding_days": int(idea.get("holding_days") or 5), "provider": idea.get("provider")}
    if invalid:
        notify(cfg, "🎲 Spekulation heute", f"Geplant war {('Long' if sign > 0 else 'Short')} {idea['name']}, aber: {invalid}. "
                                            "Heute kein Trade.")
        prop.update(result="verworfen", note=invalid)
    else:
        try:
            items, _ = traderepublic.find_knockouts(idea["isin"], idea["direction"])
        except Exception as e:  # noqa: BLE001
            log.warning("Spekulation: TR-Scheinsuche fehlgeschlagen: %s", e)
            items = []
        ko = traderepublic.pick_knockout(items, idea["direction"], stop, cfg.spec_target_leverage,
                                         max_leverage=cfg.spec_max_leverage, price=px)
        state = manager.load_state()
        depot = state.get("start_capital") or 0
        try:
            from .portfolio_service import overview
            txns = pf.load_txns("model")
            depot = overview(cfg, manager.cash_of(state["start_capital"], txns), "model", txns)["kpi"]["total"]
        except Exception:  # noqa: BLE001
            pass
        if ko and (ko.get("strike") or ko.get("barrier")):   # Hebel mit aktuellem Kurs neu rechnen
            k = ko.get("strike") or ko["barrier"]
            gap = (px - k) if idea["direction"] == "long" else (k - px)
            if gap > 0:
                ko = {**ko, "leverage": round(px / gap, 2)}
        lev = (ko or {}).get("leverage") or cfg.spec_target_leverage
        loss_frac = min(1.0, lev * abs(px - stop) / px)         # Verlust auf den Schein am Stop (ungefähr)
        gain_frac = lev * abs(target - px) / px
        note, share = grade(idea.get("conviction"))
        stake = depot * cfg.spec_risk_pct * share / loss_frac if loss_frac else 0
        prop.update(ko=ko, leverage=lev, product_stop_pct=-loss_frac, product_target_pct=gain_frac,
                    stake=round(stake, -1), depot=depot, grade=note)
        side = "Long" if sign > 0 else "Short"
        cur = idea["currency"]
        lines = [f"{side} {idea['name']} · Basiswert {_fmt(px)} {cur}",
                 f"Stop-Loss {_fmt(stop)} {cur} · Kursziel {_fmt(target)} {cur} · Chance/Risiko {prop['rr']}"]
        if ko:
            lines.append(f"Schein: {ko['name']} · ISIN {ko['isin']} · Hebel {_fmt(lev, 1)} · Knock-out "
                         f"{_fmt(ko.get('barrier') or ko.get('strike'))} {cur}")
            if ko.get("currency", "EUR") == "EUR" and cur == "EUR":
                pe, ps, pt = (traderepublic.product_price(ko, x, idea["direction"]) for x in (px, stop, target))
                if pe:
                    prop.update(product_entry=pe, product_stop=ps, product_target=pt)
                    lines.append(f"Schein ca. {_fmt(pe)} € · Stop ca. {_fmt(ps)} € · Ziel ca. {_fmt(pt)} €")
        else:
            lines.append(f"Schein: in der TR-App „{idea['name']}“ → Derivate → Knock-out {side}, Hebel ca. "
                         f"{_fmt(cfg.spec_target_leverage, 0)}, Knock-out-Schwelle {'unter' if sign > 0 else 'über'} "
                         f"{_fmt(stop)} {cur} wählen")
        lines.append(f"Qualität {note} (Überzeugung {float(idea['conviction']):.0%}) · Einsatz höchstens ~{_fmt(stake, 0)} € "
                     f"(am Stop ca. {_fmt(loss_frac * 100, 0)} % auf den Schein = {_fmt(cfg.spec_risk_pct * share * 100, 2)} % "
                     f"deines Depots), am Ziel ca. +{_fmt(gain_frac * 100, 0)} %")
        lines.append(f"Kauf am besten nach den ersten Minuten (Spannen) mit Limit – nur solange der Basiswert "
                     f"{'unter' if sign > 0 else 'über'} {_fmt(limit)} {cur} steht.")
        if idea.get("earnings"):
            lines.append(f"⚠️ Quartalszahlen am {idea['earnings']} – Kurslücken-Risiko mit Hebel")
        lines.append(idea["rationale"])
        try:
            cash_now = manager.cash_of(state["start_capital"], pf.load_txns("model")) if state.get("start_capital") else None
        except Exception:  # noqa: BLE001
            cash_now = None
        if cash_now is not None and cash_now < stake:
            lines.append(f"⚠️ Im Portfolio sind nur {_fmt(cash_now, 0)} € Cash – für diesen Einsatz fehlen ca. "
                         f"{_fmt(stake - cash_now, 0)} € (kleiner einsteigen oder Cash nachlegen).")
            prop["cash_short"] = round(stake - cash_now, 2)
        # Zweistufige KI-Prüfung vor dem Push: Gemini prüft vor, Claude entscheidet
        chk = None
        try:
            from . import pruefung
            hl = _headlines(idea["symbol"])[:8]
            chk = pruefung.review_spec(ANALYST, "\n".join(lines) + ("\nAktuelle Schlagzeilen:\n" + "\n".join(f"- {h}" for h in hl)
                                                                   if hl else ""))
        except Exception:  # noqa: BLE001 – die Prüfung darf den Vorschlag nie verschlucken
            log.exception("Spekulation: KI-Prüfung fehlgeschlagen – Vorschlag geht ungeprüft raus")
        f = (chk or {}).get("final") or {}
        who = "Claude" if (chk or {}).get("final_by") == "claude" else "Gemini"
        if chk:
            prop["check"] = chk
        if f.get("verdict") == "verwerfen":
            prop.update(result="verworfen", note=f"KI-Prüfung ({who}): {f.get('reason', '')}")
            notify(cfg, "🎲 Spekulation heute: kein Trade", f"Geplant war {side} {idea['name']}, aber die Prüfung "
                   f"(Gemini → Claude) hat den Trade verworfen: {f.get('reason', '')}")
        else:
            if f.get("verdict") == "bedenken":
                lines.insert(2, f"⚠️ {who}: {f.get('reason', '')}")
            elif f.get("verdict") == "ok":
                lines.append(f"✓ Geprüft von {'Gemini + Claude' if chk.get('pre') and who == 'Claude' else who}: {f.get('reason', '')}")
            else:
                lines.append("(ungeprüft – keine KI erreichbar)")
            from .mydepot import tr_url
            link = tr_url((ko or {}).get("isin") or idea["isin"])
            try:
                notify(cfg, f"🎲 Spekulation ({note}): {side} {idea['name']}", "\n".join(lines), url=link)
            except TypeError:
                notify(cfg, f"🎲 Spekulation ({note}): {side} {idea['name']}", "\n".join(lines))
    with _lock:
        d = load()
        d["plans"][day] = {**plan, "published": True}
        d["history"].append(prop)
        save(d)
    log.info("Spekulation veröffentlicht: %s %s", prop["direction"], prop["name"])
    return prop


# --------------------------------------------------------------------------- Verfolgen
def _trading_days_since(day: str, now) -> int:
    d, n = datetime.fromisoformat(day).date(), 0
    while d < now.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def track(cfg: Config, notify, now=None) -> None:
    now = now or datetime.now(pf.BERLIN)
    d = load()
    changed = False
    for h in d["history"]:
        if h.get("result"):
            continue
        px = native_price(cfg, h["symbol"])
        if not px:
            continue
        sign = 1 if h["direction"] == "long" else -1
        move = sign * (px / h["entry"] - 1)
        lev = h.get("leverage") or cfg.spec_target_leverage
        barrier = (h.get("ko") or {}).get("barrier")
        result = None
        if barrier and sign * (px - barrier) <= 0:
            result, prod = "Knock-out", -1.0
        elif sign * (px - h["stop"]) <= 0:
            result, prod = "Stop", max(-1.0, lev * move)
        elif sign * (px - h["target"]) >= 0:
            result, prod = "Ziel", lev * move
        elif _trading_days_since(h["day"], now) >= h.get("holding_days", 5):
            result, prod = "abgelaufen", max(-1.0, lev * move)
        h["last"] = px
        h["product_pct_now"] = max(-1.0, lev * move)
        # Gewinn sichern: halber Weg zum Ziel -> Stop auf Einstand nachziehen (einmal melden)
        way = sign * (px - h["entry"]) / (abs(h["target"] - h["entry"]) or 1)
        if not result and way >= 0.5 and not h.get("lock_sent"):
            h["lock_sent"] = True
            notify(cfg, f"🔒 Spekulation {h['name']}: Gewinn sichern",
                   f"Halber Weg zum Ziel geschafft ({move:+.1%}, Schein ca. {lev * move * 100:+.0f} %). "
                   f"Stop-Loss jetzt auf den Einstand {_fmt(h['entry'])} {h['currency']} nachziehen – dann kann der Trade "
                   "nicht mehr ins Minus laufen.")
            changed = True
        if result:
            h.update(result=result, product_pct=prod, closed=now.isoformat(timespec="minutes"), exit=px)
            icon = {"Ziel": "✅", "Stop": "🛑", "Knock-out": "💥", "abgelaufen": "⌛"}[result]
            notify(cfg, f"{icon} Spekulation {h['name']}: {result}",
                   f"Basiswert {_fmt(px)} {h['currency']} ({move:+.1%}) · Schein ca. {prod * 100:+.0f} %"
                   + (" – jetzt verkaufen" if result in ("Ziel", "abgelaufen") else ""))
        changed = True
    if changed:
        with _lock:
            d2 = load()
            d2["history"] = d["history"]
            save(d2)


# --------------------------------------------------------------------------- Takt (jede Minute aus dem Bot)
_last_track = [0.0]
_prep_tried: dict[str, float] = {}
ANALYST = None   # vom Bot gesetzt (für „Jetzt vorbereiten“ aus der Oberfläche)


def tick(cfg: Config, notify, analyst) -> None:
    import time as _t
    d = load()
    if not d["enabled"] or analyst is None:
        return
    now = datetime.now(pf.BERLIN)
    today = now.date()
    hm = (now.hour, now.minute)
    if today.weekday() < 5:
        nxt = next_trading_day(today)
        final = lambda day: day in d["plans"] and not d["plans"][day].get("manual")  # noqa: E731 – Vorschau zählt nicht
        # tagsüber: Vorauswahl für morgen wächst Schritt für Schritt (in der Oberfläche sichtbar)
        if (9, 30) <= hm < PREP_AT and not final(nxt) and not _running["prep"]:
            _bg(lambda: watch_step(cfg, analyst, nxt))
        # ab 22:30 festlegen – mit dem, was tagsüber bewertet wurde; morgens nachholen, falls der Bot abends aus war
        target_day = nxt if hm >= PREP_AT else (today.isoformat() if hm < PUBLISH_AT else None)
        if (target_day and not final(target_day) and not _running["prep"]
                and _t.time() - _prep_tried.get(target_day, 0) > 3600):   # nach einem Fehler höchstens stündlich
            _prep_tried[target_day] = _t.time()
            reuse = (d.get("watch", {}).get(target_day) or {}).get("items")
            _bg(lambda: prepare(cfg, analyst, target_day, reuse=reuse))
        if PUBLISH_AT <= hm < (11, 0) and today.isoformat() in d["plans"] and not d["plans"][today.isoformat()].get("published"):
            publish(cfg, notify, today.isoformat())
    if _t.time() - _last_track[0] >= 300:
        _last_track[0] = _t.time()
        track(cfg, notify, now)


def _bg(fn) -> None:
    """KI-Arbeit im Hintergrund, damit die minütliche Stop-Prüfung nie wartet."""
    _running["prep"] = True

    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("Spekulation: Vorbereitung fehlgeschlagen")
        finally:
            _running["prep"] = False
    threading.Thread(target=run, daemon=True, name="spekulation").start()


def prepare_now(cfg: Config, analyst) -> dict:
    """Aus der Oberfläche: sofort (neu) vorbereiten – für den nächsten Handelstag."""
    if _running["prep"]:
        raise ValueError("Die Vorbereitung läuft schon")
    day = next_trading_day(datetime.now(pf.BERLIN).date())

    reuse = (load().get("watch", {}).get(day) or {}).get("items")
    _running["prep"] = False
    _bg(lambda: prepare(cfg, analyst, day, reuse=reuse, manual=True))   # nur Vorschau – 22:30 legt neu fest
    return status()
