"""Zocker-Strategie „Momentum“: wenige große Positionen in den Aktien mit der stärksten Kursentwicklung.

Regeln (Einstellungen in config.ZOCKER):
- Rangliste: Kursveränderung der letzten ~6 Monate ohne die letzte Woche, nur Werte mit positivem Wert.
- Gekauft werden die besten 3 (je ⅓ des Kontos), über echte Knock-out-Scheine mit Hebel um 5 – gibt es keinen
  passenden Schein, die Aktie selbst.
- Verkauft wird, wenn ein Wert aus den besten 6 fällt, oder über Stop-Loss / Trailing-Stop (bei Scheinen 60 % des
  Abstands zur Knock-out-Schwelle, bei 5x also ~12 %; bei Aktien 25 %).
- Nach einem Stop wird derselbe Wert ~1 Tag nicht wieder gekauft.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
FILE = STATE_DIR / "momentum.json"


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
        # 1) Raus, was nicht mehr unter den Besten ist
        for sym in list(broker.positions):
            snap = snaps.get(sym)
            if sym in keep or not snap or not snap.tradable:
                continue
            place = order.index(sym) + 1 if sym in order else None
            broker.close(sym, snap.price, f"Momentum: {f'nur noch Platz {place}' if place else 'Kursstärke nicht mehr positiv'}"
                                          f" – nicht mehr unter den besten {cfg.mom_keep_rank}")
        # 2) Die Besten kaufen (freie Plätze, je ⅓ des Kontos)
        equity = broker.equity(prices)
        searches = [0]
        for i, r in enumerate(ranking[:cfg.mom_positions]):
            sym = r["symbol"]
            snap = snaps.get(sym)
            if len(broker.positions) >= cfg.mom_positions:
                break
            if sym in broker.positions or sym in d["cooldown"] or not snap or not snap.tradable:
                continue
            isin = (cfg.isins or {}).get(sym)
            if cfg.real_products and isin:
                from . import traderepublic as tr
                if (isin, "long") not in tr._ko_cache and searches[0] >= cfg.product_searches_per_cycle:
                    continue    # Scheinsuche im nächsten Durchlauf
            product = _real_product(cfg, snap, "long", searches) if cfg.real_products else None
            lev = product["leverage"] if product else 1.0
            margin = min(equity / cfg.mom_positions, (broker.cash - cfg.fee_fixed - 0.5) / (1 + cfg.fee_pct * lev))
            if margin < 50:
                continue
            via = (f"via {product['name']} ({product['isin']}), KO {product['barrier']:g} {product.get('currency', '')}"
                   if product else f"Aktie direkt (kein passender Schein mit Hebel um {cfg.product_target_leverage:g})")
            broker.open(sym, "long", snap.price, margin, lev,
                        f"Momentum Platz {i + 1}: {r['mom']:+.0%} in 6 Monaten · {lev:g}x {via}",
                        product=product, entry={"strategy": "momentum", "mom": r["mom"], "rank": i + 1, "lev": lev})
    d["prev"] = list(broker.positions)
    _save(d)


def view(cfg: Config, held: set[str]) -> dict:
    """Für die Oberfläche: Rangliste und Regeln."""
    d = load()
    return {"time": d.get("time"), "n": d.get("n", 0),
            "ranking": [{**r, "rank": i + 1, "held": r["symbol"] in held} for i, r in enumerate(d["ranking"][:10])],
            "cooldown": sorted(d["cooldown"]),
            "rules": {"positions": cfg.mom_positions, "keep": cfg.mom_keep_rank, "stop": cfg.stop_loss_pct,
                      "stop_product": min(cfg.stop_loss_pct, cfg.product_stop_share / cfg.product_target_leverage),
                      "trailing": cfg.trailing_stop_pct, "lev": [cfg.product_min_leverage, cfg.product_target_leverage,
                                                                 cfg.product_max_leverage]}}
