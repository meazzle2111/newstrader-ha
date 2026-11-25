"""Zocker-Strategie „Momentum + Nachrichten“: gestreut in die stärksten Aktien, deren Nachrichtenlage passt.

Regeln (Einstellungen in config.ZOCKER):
- Rangliste: Kursveränderung der letzten ~6 Monate ohne die letzte Woche, nur Werte mit positivem Wert.
- Die 10 stärksten bekommen eine KI-Bewertung ihrer Nachrichten. Gekauft wird nur bei neutraler oder positiver Lage;
  Reihenfolge nach Gesamtwertung: 60 % Nachrichtenlage, 40 % Momentum-Platz.
- Bis zu 6 Positionen (je ~1/6 von 85 % des Kontos) über echte Knock-out-Scheine mit Hebel um 5 – gibt es keinen
  passenden Schein, die Aktie selbst. 15 % bleiben immer Cash; werktags 14:15–16:00 keine Käufe (US-Daten, US-Start).
- Verkauft wird, wenn ein Wert aus den besten 20 fällt, wenn seine Nachrichtenlage klar kippt, oder über Stop-Loss /
  Trailing-Stop (bei Scheinen 60 % des Abstands zur Knock-out-Schwelle, bei Aktien 25 %).
- Kaufprüfung („erst nachdenken“): Vor jedem Kauf ein KI-Gutachten zu genau diesem Kauf (Übernahme statt Trend?
  Gewinnwarnung? anstehendes Ereignis? Trend gebrochen?) und eine harte Regel: keine Käufe kurz vor Quartalszahlen.
- Nach einem Stop wird derselbe Wert ~1 Tag nicht wieder gekauft. Übersprungene Käufe werden weiter beobachtet.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
FILE = STATE_DIR / "momentum.json"
NEWS_VETO = -0.30          # KI-Bewertung (−1…+1, mit Sicherheit gewichtet) ab hier: klar negativ -> nicht kaufen
LEXICON_VETO = -0.50       # nur Wörterbuch (KI nicht erreichbar): erst bei sehr negativer Lage
NEWS_WAIT_H = 3.0          # so lange auf die KI-Bewertung warten, danach notfalls das Wörterbuch
CHECKS_FILE = STATE_DIR / "momentum_checks.json"
CHECK_TTL_H = 12.0         # ein Gutachten gilt so lange
CHECK_RETRY_MIN = 30       # nach einem Fehler frühestens wieder
EARNINGS_DAYS = 3          # Quartalszahlen in so vielen Kalendertagen: nicht jetzt kaufen (Kurslücke bei Hebel 5)

CHECK_SYSTEM = """Du prüfst für einen Momentum-Trader einen geplanten Kauf, bevor Geld eingesetzt wird.
Die Strategie: die Aktien mit der stärksten Kursentwicklung der letzten 6 Monate kaufen und halten, solange der Trend
trägt (Wochen bis Monate). Umsetzung mit einem Knock-out-Schein, Hebel etwa 5: Stop rund 12 % unter dem Einstieg,
Knock-out rund 20 % darunter – ein Rückgang von 12 % kostet 60 % des Einsatzes.
Suche gezielt nach Gründen, die gegen GENAU DIESEN Kauf jetzt sprechen:
- Kommt der Anstieg von einem Übernahmeangebot (Kurs klebt am Angebotspreis, kein Trend mehr)?
- Gewinnwarnung, gesenkte Prognose, Herabstufungen, Ermittlungen, Bilanzprobleme, Klagen mit großem Risiko?
- Steht ein Ereignis mit Kurslücken-Risiko unmittelbar bevor (Zahlen, Studiendaten, Zulassung, Urteil)?
- Ist der Trend schon gebrochen (deutlich unter dem 20- und 50-Tage-Schnitt, letzte Tage stark gefallen)?
- Extreme Übertreibung ohne Nachricht (z. B. +30 % in wenigen Tagen)?
Hohe Bewertung oder ein allgemein schwankender Markt sind KEIN Grund – Momentum kauft bewusst Stärke.
decision: "kaufen" wenn keine konkrete Warnflagge vorliegt; "warten" bei einem unmittelbar bevorstehenden Ereignis
oder unklarer Lage; "nicht kaufen" bei einer klaren Warnflagge. reason: 1–2 deutsche Sätze, konkret.
Antworte nur im JSON-Format."""
CHECK_SCHEMA = {
    "type": "object",
    "properties": {"decision": {"type": "string", "enum": ["kaufen", "warten", "nicht kaufen"]},
                   "reason": {"type": "string"}, "red_flags": {"type": "array", "items": {"type": "string"}},
                   "confidence": {"type": "number"}},
    "required": ["decision", "reason", "red_flags", "confidence"],
    "additionalProperties": False,
}
_checking = {"on": False}


def eligible(symbol: str) -> bool:
    """Nur Aktien: keine Kryptowährungen, Indizes, Rohstoff-Futures oder Devisen."""
    return not (symbol.startswith("^") or symbol.endswith(("-EUR", "-USD", "=F", "=X")))


def score(closes: pd.Series | None, look: int, skip: int, today=None) -> float | None:
    """Kursveränderung von vor `look` bis vor `skip` Handelstagen."""
    if closes is None or len(closes) < look + 1:
        return None
    today = pd.Timestamp(today or datetime.now(timezone.utc).date())
    if (today - pd.Timestamp(closes.index[-1]).tz_localize(None)).days > 10:
        return None                                  # keine aktuellen Kurse mehr (z. B. vom Handel genommen)
    a, b = float(closes.iloc[-1 - look]), float(closes.iloc[-1 - skip])
    return b / a - 1 if a > 0 else None


def load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("ranking", [])
    d.setdefault("cooldown", {})
    d.setdefault("prev", [])
    d.setdefault("vetoes", [])       # übersprungene Käufe (Nachrichten-Filter) – werden weiter beobachtet
    d.setdefault("news_wait", {})    # Symbol -> seit wann auf die KI-Bewertung gewartet wird
    d.setdefault("check_wait", {})   # Symbol -> seit wann auf das Kauf-Gutachten gewartet wird
    d.setdefault("status", {})       # Symbol -> [Zustand, Text] der Kaufkandidaten im letzten Durchlauf
    return d


def _save(d: dict) -> None:
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1))
    tmp.replace(FILE)


def update(cfg: Config, closes: dict[str, pd.Series], today=None) -> list[str]:
    """Rangliste neu berechnen und speichern. Gibt die Symbole in Rangfolge zurück."""
    rows = []
    for sym, s in closes.items():
        if not eligible(sym):
            continue
        m = score(s, cfg.mom_lookback_days, cfg.mom_skip_days, today)
        if m is not None and m > 0:
            rows.append({"symbol": sym, "name": cfg.names.get(sym, sym), "mom": round(m, 4)})
    rows.sort(key=lambda r: -r["mom"])
    d = load()
    old_top = [r["symbol"] for r in d["ranking"][:cfg.mom_positions]]
    d["ranking"] = rows[:30]
    d["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    d["n"] = len(rows)
    _save(d)
    top = [r["symbol"] for r in rows[:cfg.mom_positions]]
    if top != old_top and top:
        log.info("Momentum-Rangliste: %s", " · ".join(f"{r['symbol']} {r['mom']:+.0%}" for r in rows[:cfg.mom_keep_rank]))
    return [r["symbol"] for r in rows]


# --------------------------------------------------------------------------- Kaufprüfung („erst nachdenken“)
def load_checks() -> dict:
    try:
        return json.loads(CHECKS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_checks(c: dict) -> None:
    tmp = CHECKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1))
    tmp.replace(CHECKS_FILE)


def check_candidate(cfg: Config, analyst, r: dict, now: datetime | None = None) -> dict:
    """Ein Kauf-Gutachten: harte Regel (Quartalszahlen) und KI-Einschätzung zu genau diesem Kauf."""
    from .market import get_currency, get_daily_closes
    from .spekulation import _fmt_features, _headlines, earnings_date, features
    now = now or datetime.now(timezone.utc)
    sym = r["symbol"]
    out = {"time": now.isoformat(timespec="seconds"), "mom": r["mom"]}
    earn = earnings_date(sym)
    out["earnings"] = earn
    if earn:
        days = (datetime.fromisoformat(earn).date() - now.date()).days
        if 0 <= days <= EARNINGS_DAYS:
            return {**out, "decision": "warten", "provider": "Regel", "red_flags": ["Quartalszahlen"],
                    "reason": f"Quartalszahlen am {datetime.fromisoformat(earn):%d.%m.} – mit Hebel 5 erst danach kaufen."}
    f = features(get_daily_closes(sym))
    hl = _headlines(sym)
    prompt = (f"Kandidat: {r.get('name', sym)} ({sym}), Währung {get_currency(sym)}\n"
              f"Momentum: {r['mom']:+.0%} in den letzten 6 Monaten (ohne letzte Woche), Platz {r.get('rank', '?')} von "
              f"rund 230 beobachteten Werten\n"
              + (f"Kurslage: {_fmt_features(f, get_currency(sym))}\n" if f else "")
              + (f"Nächste Quartalszahlen: {earn}\n" if earn else "Nächste Quartalszahlen: unbekannt\n")
              + ("Schlagzeilen (neueste zuerst):\n" + "\n".join(f"- {h}" for h in hl) if hl else "Keine aktuellen Schlagzeilen."))
    data, provider = analyst.ask_json(CHECK_SYSTEM, prompt, CHECK_SCHEMA)
    dec = data.get("decision") if data.get("decision") in ("kaufen", "warten", "nicht kaufen") else "warten"
    return {**out, "decision": dec, "reason": str(data.get("reason", ""))[:300], "provider": provider,
            "red_flags": [str(x)[:80] for x in (data.get("red_flags") or [])][:4],
            "confidence": float(data.get("confidence") or 0)}


def due_checks(cfg: Config, held: set[str], now: datetime) -> list[dict]:
    """Kandidaten, die ein (neues) Gutachten brauchen: die nächsten Käufe laut Rangliste (mit Reserve)."""
    d, checks = load(), load_checks()
    out = []
    sc = d.get("scores", {})
    cands = [dict(r, rank=i + 1) for i, r in enumerate(d["ranking"][:cfg.mom_pool]) if r["symbol"] not in held]
    # zu schwache Nachrichten brauchen kein Gutachten; sonst die besten nach Gesamtwertung (unbewertete zuletzt)
    cands = [r for r in cands if sc.get(r["symbol"], {}).get("news", 0.0) >= cfg.mom_news_min]
    cands.sort(key=lambda r: -sc.get(r["symbol"], {}).get("score", -1))
    free = max(0, cfg.mom_positions - len(held))
    for r in cands[:max(2, free + 2)]:
        c = checks.get(r["symbol"]) or {}
        t = c.get("time") or c.get("failed")
        age_h = (now - datetime.fromisoformat(t)).total_seconds() / 3600 if t else 1e9
        if c.get("failed") and not c.get("decision"):
            if age_h * 60 >= CHECK_RETRY_MIN:
                out.append(r)
        elif age_h >= CHECK_TTL_H:
            out.append(r)
    return out


def start_checks(cfg: Config, analyst, held: set[str]) -> int:
    """Im Hintergrund Gutachten für die anstehenden Käufe holen (die Stop-Prüfung wartet nie auf die KI)."""
    if analyst is None or _checking["on"]:
        return 0
    todo = due_checks(cfg, held, datetime.now(timezone.utc))[:3]
    if not todo:
        return 0
    _checking["on"] = True

    def go():
        try:
            for r in todo:
                try:
                    res = check_candidate(cfg, analyst, r)
                    log.info("Kaufprüfung %s: %s – %s (%s)", r["symbol"], res["decision"], res["reason"][:160], res["provider"])
                except Exception as e:  # noqa: BLE001 – KI nicht erreichbar: später neu versuchen
                    log.warning("Kaufprüfung %s fehlgeschlagen: %s", r["symbol"], e)
                    res = {"failed": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": str(e)[:160]}
                c = load_checks()
                c[r["symbol"]] = res
                _save_checks({k: v for k, v in c.items() if (v.get("time") or v.get("failed", "")) >=
                              (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()})
        finally:
            _checking["on"] = False
    threading.Thread(target=go, daemon=True, name="kaufpruefung").start()
    return len(todo)


def buy_verdict(snap, d: dict, checks: dict, now: datetime) -> tuple[str, str]:
    """ok · wait (Prüfung/Nachrichten stehen aus, Platz bleibt reserviert) · hold (KI: jetzt nicht, der nächste rückt
    nach) · veto (klare Warnflagge oder klar negative Nachrichten, der nächste rückt nach)."""
    v, text = news_check(snap, d, now)
    if v == "veto":
        return v, text
    c = checks.get(snap.symbol) or {}
    fresh = c.get("decision") and now - datetime.fromisoformat(c["time"]) < timedelta(hours=CHECK_TTL_H)
    if fresh:
        d["check_wait"].pop(snap.symbol, None)
        why = f"Kaufprüfung ({c.get('provider', '')}): {c.get('reason', '')}"
        if c["decision"] == "nicht kaufen":
            return "veto", why
        if c["decision"] == "warten":
            return "hold", why
        return ("wait", text) if v == "wait" else ("ok", why)
    first = d["check_wait"].setdefault(snap.symbol, now.isoformat(timespec="seconds"))
    if now - datetime.fromisoformat(first) < timedelta(hours=NEWS_WAIT_H):
        return "wait", "Kaufprüfung durch die KI läuft"
    return v, f"{text} (ohne Kaufprüfung – KI über {NEWS_WAIT_H:g} Std. nicht erreichbar)"


def news_check(snap, d: dict, now: datetime) -> tuple[str, str]:
    """„ok“, „wait“ (Nachrichten/KI-Bewertung stehen noch aus) oder „veto“ (klar negative Nachrichtenlage)."""
    n = snap.news
    if not snap.news_loaded:
        return "wait", "Nachrichten werden geladen"
    if n.n_headlines == 0:
        return "ok", "keine aktuellen Meldungen"
    if n.analyst == "claude":                                   # von einer KI (Gemini/Claude) geprüft
        return ("veto" if n.score <= NEWS_VETO else "ok"), n.summary
    first = d["news_wait"].setdefault(snap.symbol, now.isoformat(timespec="seconds"))
    if now - datetime.fromisoformat(first) < timedelta(hours=NEWS_WAIT_H):
        return "wait", "wartet auf die KI-Bewertung der Nachrichten"
    return ("veto" if n.score <= LEXICON_VETO else "ok"), f"(Wörterbuch, KI nicht erreichbar) {n.summary}"


def _note_veto(d: dict, r: dict, snap, now: datetime, text: str) -> None:
    sym = r["symbol"]
    day_ago = (now - timedelta(hours=24)).isoformat()
    if any(v["symbol"] == sym and v["time"] >= day_ago for v in d["vetoes"]):
        return
    d["vetoes"].append({"symbol": sym, "name": r.get("name", sym), "time": now.isoformat(timespec="seconds"),
                        "price": snap.price, "price_now": snap.price, "news": round(snap.news.score, 3),
                        "mom": r["mom"], "why": text[:200]})
    d["vetoes"] = d["vetoes"][-50:]
    log.info("Momentum: %s übersprungen – %s", sym, text[:200])


def combined_score(cfg: Config, rank0: int, pool_size: int, news: float) -> float:
    """0…1: Nachrichtenlage (−1…+1 -> 0…1) mit mom_news_weight, Momentum-Platz im Pool (bester = 1) mit dem Rest."""
    m = 1 - rank0 / max(1, pool_size - 1)
    return cfg.mom_news_weight * (news + 1) / 2 + (1 - cfg.mom_news_weight) * m


def in_buy_pause(cfg: Config, now: datetime) -> bool:
    """Kaufpause werktags (deutsche Zeit), z. B. „14:15-16:00“: US-Wirtschaftsdaten und die unruhige erste US-Handelszeit."""
    if not cfg.mom_buy_pause or "-" not in cfg.mom_buy_pause:
        return False
    from . import portfolio as pf
    local = now.astimezone(pf.BERLIN)
    if local.weekday() >= 5:
        return False
    a, b = (tuple(int(x) for x in t.strip().split(":")) for t in cfg.mom_buy_pause.split("-"))
    return a <= (local.hour, local.minute) < b


def run(broker, cfg: Config, snaps: dict) -> None:
    """Ein Durchlauf des Zockers: Stops prüfen, aus der Rangliste Gefallene verkaufen, die Besten kaufen."""
    from .strategy import _real_product, check_risk
    prices = {s: v.price for s, v in snaps.items()}
    broker.accrue_financing(prices)
    broker.update_extremes(prices)
    now = broker.clock()
    d = load()
    before = set(d["prev"]) | set(broker.positions)
    check_risk(broker, cfg, {s: v.price for s, v in snaps.items() if v.tradable})
    # Was seit dem letzten Durchlauf oder eben per Stop/Knock-out geschlossen wurde: kurz nicht wieder kaufen
    for sym in before:
        if sym not in broker.positions:
            d["cooldown"][sym] = (now + timedelta(hours=cfg.mom_cooldown_hours)).isoformat(timespec="seconds")
    d["cooldown"] = {s: t for s, t in d["cooldown"].items() if datetime.fromisoformat(t) > now}

    ranking = d["ranking"]
    fresh = d.get("time") and now - datetime.fromisoformat(d["time"]) < timedelta(days=3)
    if ranking and fresh:
        order = [r["symbol"] for r in ranking]
        keep = set(order[:cfg.mom_keep_rank])
        # 1) Raus: Momentum verblasst – oder die Nachrichtenlage kippt klar ins Negative (von der KI bewertet)
        for sym in list(broker.positions):
            snap = snaps.get(sym)
            if not snap or not snap.tradable:
                continue
            n = snap.news
            if sym not in keep:
                place = order.index(sym) + 1 if sym in order else None
                broker.close(sym, snap.price, f"Momentum: {f'nur noch Platz {place}' if place else 'Kursstärke nicht mehr positiv'}"
                                              f" – nicht mehr unter den besten {cfg.mom_keep_rank}")
            elif n.analyst == "claude" and n.n_headlines and n.score <= cfg.mom_news_exit:
                broker.close(sym, snap.price, f"Nachrichtenlage kippt ({n.score:+.2f}): {n.summary[:160]}")
        # 2) Kandidaten: die stärksten Momentum-Werte, bewertet nach Nachrichtenlage (60 %) und Momentum (40 %)
        equity = broker.equity(prices)
        searches = [0]
        status: dict[str, list] = {}
        scores: dict[str, dict] = {}
        checks = load_checks()
        pool = ranking[:cfg.mom_pool]
        eligible = []
        for j, r in enumerate(pool):
            sym = r["symbol"]
            snap = snaps.get(sym)
            if sym in broker.positions:
                continue
            if sym in d["cooldown"]:
                status[sym] = ["pause", "Pause nach Stop"]
                continue
            if not snap or not snap.tradable:
                status[sym] = ["closed", "Börse gerade zu"]
                continue
            n = snap.news
            news = n.score if n.n_headlines else 0.0
            sc = combined_score(cfg, j, len(pool), news)
            scores[sym] = {"news": round(news, 3), "score": round(sc, 3), "ai": n.analyst == "claude", "n": n.n_headlines}
            verdict, text = buy_verdict(snap, d, checks, now)
            if verdict == "veto":
                _note_veto(d, r, snap, now, text)
            elif verdict == "ok" and n.n_headlines and news < cfg.mom_news_min:
                verdict, text = "weak", f"Nachrichtenlage {news:+.2f} – für einen Kauf nicht gut genug ({n.summary[:120]})"
            status[sym] = [verdict, text[:240]]
            if verdict == "ok":
                eligible.append((sc, j, r, snap))
        eligible.sort(key=lambda e: -e[0])
        # 3) Kaufen: bestes Gesamtbild zuerst, höchstens mom_positions, immer mom_cash_reserve Cash übrig lassen
        pause = in_buy_pause(cfg, now)
        budget = equity * (1 - cfg.mom_cash_reserve)
        invested = sum(p["margin"] for p in broker.positions.values())
        for sc, j, r, snap in eligible:
            sym = r["symbol"]
            if pause:
                status[sym] = ["timepause", f"Kaufpause {cfg.mom_buy_pause} Uhr (US-Daten, US-Börsenstart) – danach"]
                continue
            if len(broker.positions) >= cfg.mom_positions:
                status[sym] = ["full", "alle Plätze belegt"]
                continue
            isin = (cfg.isins or {}).get(sym)
            if cfg.real_products and isin:
                from . import traderepublic as tr
                if (isin, "long") not in tr._ko_cache and searches[0] >= cfg.product_searches_per_cycle:
                    continue    # Scheinsuche im nächsten Durchlauf
            product = _real_product(cfg, snap, "long", searches) if cfg.real_products else None
            lev = product["leverage"] if product else 1.0
            margin = min(budget / cfg.mom_positions, budget - invested,
                         (broker.cash - cfg.fee_fixed - 0.5) / (1 + cfg.fee_pct * lev))
            if margin < 50:
                status[sym] = ["cash", f"Cash-Reserve ({cfg.mom_cash_reserve:.0%}) bleibt – kein Geld für eine weitere Position"]
                continue
            via = (f"via {product['name']} ({product['isin']}), KO {product['barrier']:g} {product.get('currency', '')}"
                   if product else f"Aktie direkt (kein passender Schein mit Hebel um {cfg.product_target_leverage:g})")
            n = snap.news
            if broker.open(sym, "long", snap.price, margin, lev,
                           f"Momentum Platz {j + 1} ({r['mom']:+.0%} in 6 Monaten), Nachrichten {n.score:+.2f}, "
                           f"Gesamtwertung {sc:.2f} · {lev:g}x {via}",
                           product=product, entry={"strategy": "momentum", "mom": r["mom"], "rank": j + 1, "lev": lev,
                                                   "score": round(sc, 3), "news": round(n.score, 3), "n_news": n.n_headlines,
                                                   "analyst": n.analyst, "provider": n.provider,
                                                   "news_summary": n.summary[:160],
                                                   "check": (checks.get(sym) or {}).get("reason", "")[:200]}):
                d["news_wait"].pop(sym, None)
                invested += margin
                status[sym] = ["bought", "gekauft"]
        d["status"] = status
        d["scores"] = scores
        d["news_wait"] = {s: t for s, t in d["news_wait"].items() if status.get(s, [""])[0] == "wait"}
        d["check_wait"] = {s: t for s, t in d["check_wait"].items() if status.get(s, [""])[0] == "wait"}
    # übersprungene Käufe weiter beobachten: wie liefen sie seitdem?
    for v in d["vetoes"]:
        snap = snaps.get(v["symbol"])
        if snap and snap.price:
            v["price_now"] = snap.price
            v["updated"] = now.isoformat(timespec="seconds")
    d["prev"] = list(broker.positions)
    _save(d)


def _status(d: dict, sym: str, rank: int, held: set[str], cfg: Config, now: datetime) -> list | None:
    if sym in held:
        return None
    day_ago = (now - timedelta(hours=24)).isoformat()
    veto = next((v for v in reversed(d["vetoes"]) if v["symbol"] == sym and v["time"] >= day_ago), None)
    st = d["status"].get(sym)
    if st and st[0] != "ok":
        return st
    if veto:
        return ["veto", veto["why"]]
    return st


def view(cfg: Config, held: set[str]) -> dict:
    """Für die Oberfläche: Rangliste und Regeln."""
    d = load()
    now = datetime.now(timezone.utc)
    vetoes = [{**v, "change": (v["price_now"] / v["price"] - 1) if v.get("price") else None,
               "days": (now - datetime.fromisoformat(v["time"])).total_seconds() / 86400} for v in d["vetoes"]]
    judged = [v for v in vetoes if v["days"] >= 5 and v["change"] is not None]   # erst nach ein paar Tagen aussagekräftig
    checks = load_checks()
    fresh = lambda c: c.get("decision") and now - datetime.fromisoformat(c["time"]) < timedelta(hours=CHECK_TTL_H)  # noqa: E731
    return {"time": d.get("time"), "n": d.get("n", 0),
            "ranking": [{**r, "rank": i + 1, "held": r["symbol"] in held, "status": _status(d, r["symbol"], i + 1, held, cfg, now),
                         "check": {k: (checks.get(r["symbol"]) or {}).get(k) for k in ("decision", "reason", "provider", "time", "earnings")}
                         if fresh(checks.get(r["symbol"]) or {}) else None,
                         **{k: (d.get("scores", {}).get(r["symbol"]) or {}).get(k) for k in ("news", "score", "ai")}}
                        for i, r in enumerate(d["ranking"][:cfg.mom_pool])],
            "checking": _checking["on"],
            "cooldown": sorted(d["cooldown"]),
            "vetoes": vetoes[-6:][::-1],
            "filter": {"n": len(judged), "avg": sum(v["change"] for v in judged) / len(judged) if judged else None,
                       "threshold": NEWS_VETO},
            "rules": {"positions": cfg.mom_positions, "keep": cfg.mom_keep_rank, "stop": cfg.stop_loss_pct,
                      "pool": cfg.mom_pool, "news_weight": cfg.mom_news_weight, "news_min": cfg.mom_news_min,
                      "news_exit": cfg.mom_news_exit, "reserve": cfg.mom_cash_reserve, "pause": cfg.mom_buy_pause,
                      "paused": in_buy_pause(cfg, now),
                      "stop_product": min(cfg.stop_loss_pct, cfg.product_stop_share / cfg.product_target_leverage),
                      "trailing": cfg.trailing_stop_pct, "lev": [cfg.product_min_leverage, cfg.product_target_leverage,
                                                                 cfg.product_max_leverage]}}
