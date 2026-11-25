"""Portfolio-Logik von oben nach unten (rein rechnend, testbar).

1. Marktlage (MSCI World): Trend + Schwankung -> wie viel investiert sein soll.
2. Auswahl: beste geprüfte Signale, je Branche höchstens ~30 % der Plätze; Bestand bleibt, solange das Signal
   nicht klar kippt (Hysterese gegen Hin und Her).
3. Gewichte: Signalstärke / Schwankung (ruhige Werte mehr, wilde weniger), gedeckelt je Wert und für Krypto.
4. Aktionen nur bei deutlicher Abweichung vom Ziel (Bänder) – spart Gebühren und Pushes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MIN_ORDER = 100.0
VOL_FLOOR = 0.15            # Jahresschwankung mindestens (sonst bekämen sehr ruhige Werte zu viel)
HOLD_BONUS = 0.05           # Bestand bevorzugen (weniger Umschichten)
SECTOR_SHARE = 0.30         # höchstens so viele der Plätze je Branche
VOL_PENALTY = {"defensiv": 0.6, "ausgewogen": 0.3, "offensiv": 0.0}


@dataclass
class Regime:
    state: str              # "positiv" | "neutral" | "negativ"
    vol: float              # Jahresschwankung des Markts
    invest: float           # Zielanteil investiert (0..1)
    text: str


def market_regime(closes, cash_reserve: float) -> Regime:
    """closes: Tagesschlusskurse des Marktindex (MSCI World). Ohne Daten: neutral."""
    base = 1 - cash_reserve
    if closes is None or len(closes) < 200:
        return Regime("neutral", 0.0, base - 0.05, "Marktlage unbekannt – leicht vorsichtig")
    c = closes.astype(float)
    px, s50, s200 = float(c.iloc[-1]), float(c.rolling(50).mean().iloc[-1]), float(c.rolling(200).mean().iloc[-1])
    vol = float(c.pct_change().iloc[-20:].std() * math.sqrt(252))
    if px > s200 and s50 > s200:
        state, cut, txt = "positiv", 0.0, "Aufwärtstrend"
    elif px < s200 and s50 < s200:
        state, cut, txt = "negativ", 0.25, "Abwärtstrend – mehr Cash"
    else:
        state, cut, txt = "neutral", 0.10, "seitwärts – etwas mehr Cash"
    if vol > 0.25:
        cut += 0.05
        txt += ", hohe Schwankung"
    return Regime(state, vol, max(0.3, base - cut), f"Markt {txt} (MSCI World {px / s200 - 1:+.1%} zum 200-Tage-Schnitt)")


def plan_targets(held: dict[str, float], signals: dict[str, dict], vols: dict[str, float], sectors: dict[str, str],
                 style, style_name: str, rules: dict, regime: Regime, is_crypto,
                 veto: dict[str, str] | None = None) -> tuple[dict[str, float], dict[str, str]]:
    """Zielgewichte je Wert (Anteil am Gesamtdepot) + Begründung für Ausschlüsse.

    held: Symbol -> aktuelles Gewicht. signals: {score, tradable, checked(bool)}.
    """
    why: dict[str, str] = {}
    pen = VOL_PENALTY.get(style_name, 0.3)
    entry = max(style.min_score, rules["buy"])

    def comp(sym):
        v = max(vols.get(sym, 0.3), VOL_FLOOR)
        return signals[sym]["score"] - pen * (v - 0.25) + (HOLD_BONUS if sym in held else 0.0)

    keep = []
    for sym in held:
        s = signals.get(sym)
        if not s or not s.get("tradable") or not s.get("checked"):
            keep.append(sym)                      # ohne frisches, geprüftes Signal nichts tun
        elif (veto or {}).get(sym) and s["score"] < 0:
            why[sym] = f"KI-Durchsicht rät zum Verkauf: {veto[sym]}"
        elif s["score"] >= rules["sell"]:
            keep.append(sym)
        else:
            why[sym] = f"Signal {s['score']:+.2f} unter {rules['sell']:+.2f}"
    cands = [s for s, v in signals.items() if s not in held and v.get("tradable") and v.get("checked")
             and v["score"] > entry and not (is_crypto(s) and style.crypto <= 0)]
    cands.sort(key=comp, reverse=True)
    cap = max(2, math.ceil(style.n_max * SECTOR_SHARE))
    per_sector: dict[str, int] = {}
    for sym in keep:
        per_sector[sectors.get(sym, "?")] = per_sector.get(sectors.get(sym, "?"), 0) + 1
    picks = list(keep)
    for sym in cands:
        if len(picks) >= style.n_max:
            break
        sec = sectors.get(sym, "?")
        if sec != "?" and per_sector.get(sec, 0) >= cap:
            why[sym] = f"Branche {sec} schon {cap}× vertreten"
            continue
        picks.append(sym)
        per_sector[sec] = per_sector.get(sec, 0) + 1

    # Gewichte: Stärke / Schwankung, normiert auf den Zielanteil, gedeckelt (überschüssiges umverteilen)
    raw = {}
    for sym in picks:
        sc = signals.get(sym, {}).get("score", entry)
        strength = max(0.0, min(1.0, (sc - rules["sell"]) / max(0.05, 0.6 - rules["sell"])))
        raw[sym] = (0.7 + 0.6 * strength) / max(vols.get(sym, 0.3), VOL_FLOOR)
    target = {}
    budget, free = regime.invest, dict(raw)
    for _ in range(6):
        tot = sum(free.values())
        if not free or tot <= 0 or budget <= 1e-9:
            break
        over = {}
        for sym, r in free.items():
            w = budget * r / tot
            cap_w = style.max_weight
            if is_crypto(sym):
                cap_w = min(cap_w, max(0.0, style.crypto - sum(target.get(c, 0) for c in target if is_crypto(c))))
            if w > cap_w:
                over[sym] = cap_w
        if not over:
            for sym, r in free.items():
                target[sym] = budget * r / tot
            break
        for sym, w in over.items():
            target[sym] = w
            budget -= w
            del free[sym]
    for sym in picks:
        target.setdefault(sym, 0.0)
    return target, why


def plan_trades(held: dict[str, dict], target: dict[str, float], prices: dict[str, float], total: float, cash: float,
                regime: Regime, style, why: dict[str, str], qty_fn, extra_reserve: float = 0.0) -> list[dict]:
    """Aus Ist und Ziel konkrete Aufträge: Verkäufe (raus / zu groß) zuerst, dann Käufe bis zum Cash-Rahmen.

    held: Symbol -> {qty, price}. Gibt [{side, symbol, qty, price, kind, reason}].
    """
    out = []
    cash_left = cash
    for sym, h in held.items():
        px = prices.get(sym) or h["price"]
        if sym not in target:
            if sym in why:   # nur echte Verkaufsgründe – ohne Grund (z. B. Markt zu) bleibt die Position
                out.append({"side": "sell", "symbol": sym, "qty": h["qty"], "price": px, "kind": "signal", "reason": why[sym]})
                cash_left += h["qty"] * px
            continue
        w, tw = h["qty"] * px / total, target[sym]
        if w > max(style.max_weight * 1.25, tw * 1.6) and tw > 0:
            q = qty_fn(sym, (w - tw) * total, px)
            if q > 0 and q * px >= MIN_ORDER:
                out.append({"side": "sell", "symbol": sym, "qty": q, "price": px, "kind": "trim",
                            "reason": f"Anteil {w:.0%} deutlich über dem Ziel von {tw:.0%} – Gewinne sichern, Risiko streuen"})
                cash_left += q * px
    reserve = total * (1 - regime.invest) + extra_reserve   # + Einsatz für den geplanten Hebel-Trade
    buys = []
    for sym, tw in target.items():
        px = prices.get(sym)
        if not px or tw <= 0:
            continue
        cur = held.get(sym, {}).get("qty", 0) * px / total
        gap = tw - cur
        if sym in held and gap < max(0.03, tw * 0.35):   # Nachkaufen nur bei klarer Untergewichtung
            continue
        buys.append((sym, gap * total, px, sym in held))
    buys.sort(key=lambda b: -b[1])
    for sym, amount, px, top_up in buys:
        room = cash_left - reserve
        if room < MIN_ORDER:
            break
        q = qty_fn(sym, min(amount, room), px)
        if q <= 0 or q * px < MIN_ORDER:
            continue
        out.append({"side": "buy", "symbol": sym, "qty": q, "price": px, "kind": "rebalance" if top_up else "signal",
                    "reason": ("Nachkauf: deutlich unter dem Zielgewicht" if top_up else "Neu im Portfolio")
                    + f" ({target[sym]:.0%} Ziel)"})
        cash_left -= q * px
    return out
