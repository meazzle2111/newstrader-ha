"""Zweistufige KI-Prüfung der Vorschläge fürs echte Depot, bevor sie per Push rausgehen.

1. Gemini prüft vor (schnell, eigenes Kontingent).
2. Claude prüft danach – mit Geminis Einschätzung in der Hand – und entscheidet.

Urteile je Vorschlag: „ok“ (geht raus), „bedenken“ (geht mit Warnhinweis raus), „verwerfen“ (geht nicht raus, wird
mit Begründung gespeichert). Ist Claude nicht erreichbar, zählt Geminis Urteil (sichtbar markiert); ist gar keine KI
erreichbar, geht der Vorschlag als „ungeprüft“ raus – die Regeln des Portfoliomanagers bleiben die Grundlage.
Stop-Loss und Kurssturz-Verkäufe werden nicht geprüft (zeitkritisch).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
FILE = STATE_DIR / "pruefung.json"           # verworfene Vorschläge (für die Anzeige und spätere Auswertung)
SKIP_KINDS = ("stop", "rapid", "setup", "manual")
VERDICTS = ("ok", "bedenken", "verwerfen")
KIND_TXT = {"signal": "Signal", "rebalance": "Nachkauf", "trim": "Teilverkauf (Übergewicht)", "cash": "Teilverkauf (Cash für Hebel-Trade)"}

SYSTEM = """Du bist die Risikoprüfung für Kauf- und Verkaufsvorschläge eines regelbasierten Portfoliomanagers.
Das Depot ist ein ECHTES Privatdepot (Trade Republic), ohne Hebel, mittel- bis langfristig. Die Vorschläge entstehen aus
festen Regeln: Gesamtsignal aus Technik und Nachrichtenlage, Marktlage (MSCI World), Branchengrenzen, Schwankung,
Bänder gegen Hin-und-Her, Cash-Planung für einen täglichen Hebel-Trade.
Prüfe jeden Vorschlag auf Plausibilität: Passt er zur Nachrichten- und Kurslage? Gibt es einen KONKRETEN Grund dagegen?
Zum Beispiel: Übernahmeangebot (Kurs fix), Gewinnwarnung/Prognosesenkung, Quartalszahlen unmittelbar bevor, Klumpenrisiko
(gleiche Branche/gleicher Treiber), Verkauf direkt nach einem Kauf, Verkauf nur wegen kurzfristigem Rauschen, Kauf in
einen gerade gebrochenen Trend, ein deutlich besserer Kandidat für einen Cash-Verkauf.
Urteile: "ok" = plausibel; "bedenken" = umsetzbar, aber mit einem Hinweis, den der Anleger kennen sollte;
"verwerfen" = nur bei konkretem, gewichtigem Grund – die Regeln sind getestet, du bist die Plausibilitätsprüfung, nicht
der Entscheider. reason: 1–2 deutsche Sätze, konkret. summary: ein Satz zum Gesamtbild. Antworte nur im JSON-Format."""

FINAL_ADD = """
Eine erste Prüfung durch Gemini liegt bei. Prüfe sie kritisch und unabhängig: Übernimm sie nicht ungeprüft – du triffst
das endgültige Urteil."""

SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)},
                           "reason": {"type": "string"}},
            "required": ["id", "verdict", "reason"], "additionalProperties": False}},
        "summary": {"type": "string"},
    },
    "required": ["reviews", "summary"],
    "additionalProperties": False,
}


def load() -> dict:
    try:
        d = json.loads(FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("rejected", [])
    d.setdefault("last", None)
    return d


def _save(d: dict) -> None:
    d["rejected"] = d["rejected"][-100:]
    pf._atomic_write(FILE, d)


def build_prompt(actions: list[dict], context: dict) -> str:
    """Depot-Lage + Vorschläge (+ Schlagzeilen/Termine je Wert) als Text."""
    lines = [f"Depot: {context.get('total', 0):.0f} € gesamt, Cash {context.get('cash', 0):.0f} €. "
             f"Marktlage: {context.get('regime') or 'unbekannt'}."]
    if context.get("review"):
        lines.append(f"Heutige Depot-Durchsicht: {context['review']}")
    if context.get("positions"):
        lines.append("Positionen (Gewicht, Gewinn/Verlust, Signal −1…+1, Branche):")
        for p in context["positions"]:
            lines.append(f"- {p['name']} ({p['symbol']}): {p['weight']:.0%}, G/V {p.get('pnl_pct') or 0:+.0%}, "
                         f"Signal {p['signal'] if p.get('signal') is not None else 0:+.2f}, {p.get('sector') or '?'}")
    lines.append("\nVorschläge:")
    for a in actions:
        extra = context.get("per_symbol", {}).get(a["symbol"], {})
        lines.append(f"[{a['id']}] {'KAUFEN' if a['side'] == 'buy' else 'VERKAUFEN'} {a['qty']:g} × {a['name']} ({a['symbol']}) "
                     f"zu ca. {a['price']:.2f} € (≈ {a['qty'] * a['price']:.0f} €), Art: {KIND_TXT.get(a['kind'], a['kind'])}")
        lines.append(f"    Begründung der Regeln: {a['reason']}")
        if extra.get("earnings"):
            lines.append(f"    Nächste Quartalszahlen: {extra['earnings']}")
        for h in (extra.get("headlines") or [])[:8]:
            lines.append(f"    · {h}")
    return "\n".join(lines)


def _parse(data: dict, ids: set[str]) -> dict[str, dict]:
    out = {}
    for r in (data or {}).get("reviews") or []:
        if r.get("id") in ids and r.get("verdict") in VERDICTS:
            out[r["id"]] = {"verdict": r["verdict"], "reason": str(r.get("reason", ""))[:300]}
    return out


def two_stage(analyst, system: str, prompt: str, ids: set[str], progress=None) -> dict:
    """Gemini prüft vor, Claude entscheidet (mit Geminis Urteil). Gibt pre, final, final_by, errors."""
    pre, pre_err = {}, None
    gem = getattr(analyst, "gemini", None)
    if gem is not None and gem.available():
        if progress:
            progress("Gemini prüft die Vorschläge vor …")
        try:
            pre = _parse(gem.ask_json(system, prompt, SCHEMA), ids)
        except Exception as e:  # noqa: BLE001
            pre_err = str(e)[:120]
            log.info("Vorprüfung durch Gemini nicht möglich: %s", e)
    else:
        pre_err = "Gemini nicht verfügbar"
    final, final_by, final_err = {}, "claude", None
    if progress:
        progress("Claude prüft die Vorschläge …")
    prompt2 = prompt + ("\n\nVorprüfung durch Gemini:\n" + "\n".join(
        f"[{i}] {r['verdict']}: {r['reason']}" for i, r in pre.items()) if pre else "\n\n(Keine Vorprüfung durch Gemini verfügbar.)")
    try:
        final = _parse(analyst.ask_claude_json(system + FINAL_ADD, prompt2, SCHEMA), ids)
    except Exception as e:  # noqa: BLE001 – Claude nicht erreichbar: Geminis Urteil zählt
        final_err = str(e)[:120]
        log.info("Prüfung durch Claude nicht möglich: %s – Geminis Urteil zählt", e)
        final, final_by = pre, "gemini"
    return {"pre": pre, "final": final, "final_by": final_by, "errors": [e for e in (pre_err, final_err) if e]}


def _check_of(res: dict, i: str, now: str) -> dict:
    f = res["final"].get(i)
    return {"time": now, "pre": res["pre"].get(i), "pre_by": "gemini" if res["pre"].get(i) else None,
            "final": f, "final_by": res["final_by"] if f else None,
            "note": None if f and res["final_by"] == "claude" else
            ("nur von Gemini geprüft (Claude nicht erreichbar)" if f else "ungeprüft – keine KI erreichbar"),
            "errors": res["errors"]}


def review(cfg: Config, analyst, actions: list[dict], context: dict, progress=None) -> tuple[list[dict], list[dict]]:
    """Gibt (rausgehen, verworfen) zurück; jede Aktion bekommt a["check"] mit Vor- und Endprüfung."""
    todo = [a for a in actions if a.get("kind") not in SKIP_KINDS]
    if not todo or analyst is None:
        return actions, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ids = {a["id"] for a in todo}
    res = two_stage(analyst, SYSTEM, build_prompt(todo, context), ids, progress)
    final, final_by = res["final"], res["final_by"]
    out, rejected = [], []
    for a in actions:
        if a["id"] not in ids:
            out.append(a)
            continue
        a["check"] = _check_of(res, a["id"], now)
        f = a["check"]["final"]
        if f and f["verdict"] == "verwerfen":
            rejected.append(a)
        else:
            out.append(a)
    d = load()
    d["last"] = {"time": now, "n": len(todo), "rejected": len(rejected), "by": final_by if final else None}
    for a in rejected:
        d["rejected"].append({k: a.get(k) for k in ("id", "time", "side", "kind", "symbol", "name", "qty", "price", "reason", "check")})
    _save(d)
    log.info("Vorschlagsprüfung: %d geprüft (%s), %d verworfen", len(todo),
             "Gemini → Claude" if final_by == "claude" and res["pre"] else final_by if final else "ungeprüft", len(rejected))
    return out, rejected


SPEC_SYSTEM = """Du bist die Risikoprüfung für einen spekulativen Hebel-Trade (Knock-out-Schein) in einem ECHTEN Depot.
Der Vorschlag wurde am Vorabend von einer KI vorbereitet und soll jetzt um 9 Uhr an den Anleger gehen. Einsatz und
Stop sind so bemessen, dass am Stop höchstens ~1 % des Depots verloren geht.
Prüfe: Ist die These noch aktuell (Kurs seit gestern, neue Meldungen)? Liegt der Stop sinnvoll (nicht im normalen
Tagesrauschen, aber vor der Knock-out-Schwelle)? Ist das Chance-Risiko realistisch? Steht ein Ereignis mit
Kurslücken-Risiko bevor? Widerspricht etwas Konkretes dem Trade?
Urteile: "ok", "bedenken" (Hinweis an den Anleger) oder "verwerfen" (heute kein Trade – nur bei konkretem Grund).
reason: 1–2 deutsche Sätze. summary: ein Satz. Die id ist "spec". Antworte nur im JSON-Format."""


def review_spec(analyst, text: str, progress=None) -> dict | None:
    """Hebel-Vorschlag (fertiger Text mit allen Zahlen) zweistufig prüfen. Gibt die Prüfung (wie a["check"])."""
    if analyst is None:
        return None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    res = two_stage(analyst, SPEC_SYSTEM, "[spec] Vorschlag:\n" + text, {"spec"}, progress)
    chk = _check_of(res, "spec", now)
    log.info("Hebel-Vorschlag geprüft: %s", (chk["final"] or {}).get("verdict", "ungeprüft"))
    return chk


def context_for(cfg: Config, actions: list[dict]) -> dict:
    """Depot-Lage und Schlagzeilen/Termine zu den betroffenen Werten (für den Prompt)."""
    from . import manager, review as rv
    from .portfolio_service import overview
    from .spekulation import _headlines, earnings_date
    ctx: dict = {"per_symbol": {}}
    try:
        state = manager.load_state()
        txns = pf.load_txns("model")
        ov = overview(cfg, manager.cash_of(state["start_capital"], txns), "model", txns)
        k = ov["kpi"]
        ctx.update(total=k["total"], cash=k.get("cash") or 0,
                   positions=[{"name": p["name"], "symbol": p["symbol"], "weight": p["value"] / k["total"] if k["total"] else 0,
                               "pnl_pct": p.get("pnl_pct"), "signal": p.get("signal"), "sector": p.get("sector")}
                              for p in ov["positions"]])
        ctx["regime"] = (manager._last_regime or state.get("regime") or {}).get("text")
        r = rv.load()
        if r.get("day") == datetime.now(pf.BERLIN).date().isoformat():
            ctx["review"] = r.get("summary")
    except Exception as e:  # noqa: BLE001
        log.warning("Prüfung: Depot-Lage nicht vollständig: %s", e)
    for a in actions:
        try:
            ctx["per_symbol"][a["symbol"]] = {"headlines": _headlines(a["symbol"]), "earnings": earnings_date(a["symbol"])}
        except Exception:  # noqa: BLE001
            ctx["per_symbol"][a["symbol"]] = {}
    return ctx


def notify_rejected(cfg: Config, notify, rejected: list[dict]) -> None:
    if not rejected or notify is None:
        return
    lines = [f"✕ {'Kauf' if a['side'] == 'buy' else 'Verkauf'} {a['name']}: {(a['check']['final'] or {}).get('reason', '')}"
             for a in rejected[:5]]
    notify(cfg, "🧠 Vorschlag verworfen", "Die KI-Prüfung (Gemini → Claude) hat diese Vorschläge nicht freigegeben:\n"
           + "\n".join(lines))


def recent_rejected(days: int = 7) -> list[dict]:
    from datetime import timedelta
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [r for r in load()["rejected"] if (r.get("time") or "") >= cut][::-1]
