"""Handelsentscheidung: Technik + Nachrichten -> Gesamtsignal -> Long / Short / Schließen / Halten."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .analyst import NewsSignal, NEUTRAL
from .broker import PaperBroker, direction
from .config import Config
from .market import TechSignal


@dataclass
class Snapshot:
    symbol: str
    price: float
    tradable: bool          # Kurs frisch genug (Markt offen)?
    tech: TechSignal
    news: NewsSignal = field(default_factory=lambda: NEUTRAL)
    news_loaded: bool = True   # False = Schlagzeilen noch nicht abgerufen (nicht dasselbe wie „keine Meldungen“)

    def combined(self, cfg: Config) -> float:
        # Ohne Meldungen zählt nur die Technik (sonst würde ein fehlendes Signal alles verwässern).
        if self.news.n_headlines == 0:
            return self.tech.score
        w = cfg.weight_technical + cfg.weight_news
        return (cfg.weight_technical * self.tech.score + cfg.weight_news * self.news.score) / w


def _strength(score: float, threshold: float) -> float:
    """0 an der Schwelle, 1 ab |Signal| 0,6."""
    return max(0.0, min(1.0, (abs(score) - abs(threshold)) / (0.6 - abs(threshold))))


def choose_leverage(broker: PaperBroker, symbol: str, score: float, threshold: float) -> float:
    """Hebel wächst mit der Signalstärke: an der Schwelle 1x, bei starkem Signal bis zum Maximum. In 0,5er-Schritten."""
    max_lev = broker.max_leverage(symbol)
    if broker.cfg.lev_always_max:
        return max_lev
    lev = 1 + (max_lev - 1) * _strength(score, threshold)
    return max(1.0, round(lev * 2) / 2)


def _trailing(cfg: Config, pos: dict, d: int) -> float:
    """Trailing-Abstand: normal – oder eng, sobald die Position deutlich im Plus war (Gewinnsicherung)."""
    if cfg.lock_after_pct and cfg.lock_trailing_pct:
        extreme = pos["high"] if d > 0 else pos["low"]
        if d * (extreme / pos["avg_price"] - 1) >= cfg.lock_after_pct:
            return min(cfg.trailing_stop_pct, cfg.lock_trailing_pct)
    return cfg.trailing_stop_pct


def check_risk(broker: PaperBroker, cfg: Config, prices: dict[str, float]) -> int:
    """Margin-Call, Stop-Loss und Trailing-Stop (Short spiegelbildlich). `prices` nur handelbare Kurse.

    Läuft im vollen Durchlauf und zusätzlich alle paar Sekunden mit Echtzeitkursen. Gibt die Zahl geschlossener Positionen zurück.
    """
    closed = 0
    for sym, pos in list(broker.positions.items()):
        if sym not in prices:
            continue
        p, d = prices[sym], direction(pos)
        move = d * (p / pos["avg_price"] - 1)                       # positiv = Gewinn-Richtung
        extreme = pos["high"] if d > 0 else pos["low"]
        from_extreme = d * (p / extreme - 1)                        # negativ = Rücklauf vom besten Kurs
        prod = pos.get("product")
        stop_pct = broker.stop_pct(pos) if hasattr(broker, "stop_pct") else cfg.stop_loss_pct
        if prod and d * (p - prod["barrier_eur"]) <= 0:
            closed += broker.close(sym, p, f"Knock-out: Schwelle {prod['barrier']:g} {prod.get('currency', '')} erreicht – "
                                           f"Schein {prod.get('name', '')} wertlos", liquidation=True)
        elif not prod and broker.pnl(pos, p) <= -pos["margin"] * cfg.margin_closeout_pct:
            closed += broker.close(sym, p, f"Margin-Call: {cfg.margin_closeout_pct:.0%} der Margin verloren "
                                           f"({move:+.1%} Kursbewegung bei {pos['leverage']:g}x)", liquidation=True)
        elif move <= -stop_pct:
            closed += broker.close(sym, p, f"Stop-Loss ({move:+.1%} gegen die Position, "
                                           f"{move * pos['leverage']:+.1%} auf die Margin)")
        elif from_extreme <= -min(_trailing(cfg, pos, d), broker.trailing_pct(pos) if hasattr(broker, "trailing_pct") else 1.0):
            locked = _trailing(cfg, pos, d) < cfg.trailing_stop_pct
            best = d * (extreme / pos["avg_price"] - 1)
            closed += broker.close(sym, p, (f"Gewinn gesichert (bestes Plus {best:+.1%}, enger Trailing-Stop "
                                            f"{from_extreme:+.1%} vom besten Kurs, {move * pos['leverage']:+.0%} auf die Margin)")
                                   if locked else f"Trailing-Stop ({from_extreme:+.1%} vom besten Kurs)")
        elif cfg.take_profit_pct and move >= cfg.take_profit_pct:
            closed += broker.close(sym, p, f"Gewinnmitnahme ({move:+.1%} Kursbewegung, {move * pos['leverage']:+.0%} auf die Margin)")
        elif cfg.max_hold_days and (broker.clock() - datetime.fromisoformat(pos["opened"])).total_seconds() > cfg.max_hold_days * 86400:
            closed += broker.close(sym, p, f"Haltedauer {cfg.max_hold_days:g} Tage erreicht ({move:+.1%}) – Geld für neue Trades")
    return closed


def _weakest(broker: PaperBroker, cfg: Config, snaps: dict[str, Snapshot], now) -> tuple[str, float] | None:
    """Offene Position mit dem schwächsten Signal in ihrer Richtung (nur handelbar und nach Mindesthaltedauer)."""
    best = None
    for sym, pos in broker.positions.items():
        snap = snaps.get(sym)
        if not snap or not snap.tradable:
            continue
        if (now - datetime.fromisoformat(pos["opened"])).total_seconds() / 60 < cfg.min_hold_minutes:
            continue
        strength = direction(pos) * snap.combined(cfg)
        if best is None or strength < best[1]:
            best = (sym, strength)
    return best


def _real_product(cfg: Config, snap, side: str, searches: list) -> dict | None:
    """Passenden echten Knock-out-Schein suchen (Trade Republic, 6 h zwischengespeichert, begrenzt pro Durchlauf)."""
    try:
        from . import traderepublic as tr
        from .market import fx_rate, get_currency
    except Exception:  # noqa: BLE001
        return None
    isin = (cfg.isins or {}).get(snap.symbol)
    if not isin or snap.symbol.endswith(("-EUR", "-USD")):
        return None
    if (isin, side) not in tr._ko_cache:
        if searches[0] >= cfg.product_searches_per_cycle:
            return None
        searches[0] += 1
    items, _ = tr.find_knockouts(isin, side, cached_hours=6)
    if not items:
        return None
    cur = get_currency(snap.symbol)
    rate = fx_rate(cur, cfg.base_currency) if cur != cfg.base_currency else 1.0
    native = snap.price / rate if rate else snap.price
    items = [i for i in items if not i.get("currency") or i["currency"] == cur]
    ko = tr.pick_for_zocker(items, side, native, cfg)
    if not ko:
        return None
    to_eur = snap.price / native
    return {"isin": ko["isin"], "name": ko["name"], "issuer": ko.get("issuer", ""), "strike": ko.get("strike"),
            "barrier": ko.get("barrier") or ko.get("strike"), "currency": cur, "leverage": ko["leverage"],
            "barrier_eur": round((ko.get("barrier") or ko["strike"]) * to_eur, 6), "size": ko.get("size")}


def run_strategy(broker: PaperBroker, cfg: Config, snaps: dict[str, Snapshot], claude_active: bool = False,
                 learner=None) -> None:
    prices = {s: snap.price for s, snap in snaps.items()}
    broker.accrue_financing(prices)
    broker.update_extremes(prices)
    now = broker.clock()
    shorts_allowed = cfg.trading_mode == "leverage" and cfg.allow_short

    # 1) Risiko: Margin-Call, Stop-Loss, Trailing-Stop
    check_risk(broker, cfg, {s: snap.price for s, snap in snaps.items() if snap.tradable})

    # 1b) Zu große Positionen (z. B. nach Profilwechsel) schließen – damit Geld für neue Trades frei wird
    if cfg.max_margin_factor:
        eq = broker.equity(prices)
        for sym, pos in list(broker.positions.items()):
            snap = snaps.get(sym)
            if snap and snap.tradable and pos["margin"] > eq * cfg.max_position_pct * cfg.max_margin_factor:
                broker.close(sym, snap.price, f"Position zu groß ({pos['margin'] / eq:.0%} Einsatz) – Umstellung auf "
                                              f"kleinere Positionen ({cfg.max_position_pct:.0%}), Gewinn/Verlust realisiert")

    # 2) Schließen, wenn das Signal gegen die Position dreht (nach Mindesthaltedauer)
    for sym, pos in list(broker.positions.items()):
        snap = snaps.get(sym)
        if not snap or not snap.tradable:
            continue
        held_min = (now - datetime.fromisoformat(pos["opened"])).total_seconds() / 60
        if held_min < cfg.min_hold_minutes:
            continue
        score = snap.combined(cfg)
        detail = f"Signal {score:+.2f} | {snap.tech.describe()} | {snap.news.summary}"
        if pos.get("side") == "short" and score > cfg.cover_threshold:
            broker.close(sym, snap.price, "Short eindecken: " + detail)
        elif pos.get("side", "long") == "long" and score < cfg.sell_threshold:
            broker.close(sym, snap.price, "Long schließen: " + detail)

    # 3) Neue Positionen, stärkstes Signal zuerst
    equity = broker.equity(prices)
    candidates = []
    for s in snaps.values():
        if not s.tradable or s.symbol in broker.positions or (cfg.entry_requires_news and not s.news_loaded):
            continue
        # Neue Positionen nur, wenn Claude die Nachrichten gelesen hat (sofern Claude aktiv ist und es Meldungen gibt)
        if (cfg.entry_requires_claude and cfg.analyst != "lexicon" and s.news.n_headlines > 0
                and s.news.analyst != "claude" and claude_active):
            continue
        if learner is not None and not learner.allows(s.symbol, now):
            continue  # nach zwei Verlusten in Folge mit diesem Wert: Pause
        score = s.combined(cfg) * (learner.factor(s.symbol) if learner is not None else 1.0)
        if score > cfg.buy_threshold:
            candidates.append((s, "long", score, cfg.buy_threshold))
        elif shorts_allowed and score < cfg.short_threshold:
            candidates.append((s, "short", score, cfg.short_threshold))
    candidates.sort(key=lambda c: abs(c[2]), reverse=True)

    searches = [0]
    for snap, side, score, threshold in candidates:
        strength = 0.5 + 0.5 * _strength(score, threshold)          # Margin: an der Schwelle halb, stark = voll
        lev = choose_leverage(broker, snap.symbol, score, threshold)
        if cfg.lev_needs_claude and claude_active and snap.news.analyst != "claude" and snap.news.n_headlines > 0:
            lev = 1.0  # Hebel nur, wenn Claude die Nachrichtenlage bestätigt hat
        product = None
        if cfg.real_products:
            product = _real_product(cfg, snap, side, searches)
            if product is None:
                continue          # nur echte, bei Trade Republic handelbare Scheine
            lev = product["leverage"]
        margin = equity * cfg.max_position_pct * strength
        margin = min(margin, broker.cash - equity * cfg.min_cash_reserve_pct)
        # Gesamtvolumen begrenzen (Summe aller Positionen ≤ max_gross_exposure × Depotwert)
        room = equity * cfg.max_gross_exposure - broker.gross_exposure(prices)
        margin = min(margin, room / lev)
        if margin < 50 and cfg.rotate_gap > 0:
            # Geld verplant: schwächste Position gegen dieses deutlich stärkere Signal tauschen
            weakest = _weakest(broker, cfg, snaps, now)
            if weakest and abs(score) - weakest[1] >= cfg.rotate_gap:
                w_sym, w_strength = weakest
                broker.close(w_sym, snaps[w_sym].price,
                             f"Umschichten: {snap.symbol} hat das stärkere Signal ({score:+.2f} statt {w_strength:+.2f})")
                equity = broker.equity(prices)
                margin = min(equity * cfg.max_position_pct * strength, broker.cash - equity * cfg.min_cash_reserve_pct,
                             (equity * cfg.max_gross_exposure - broker.gross_exposure(prices)) / lev)
        if margin < 50:  # Kleinstorders lohnen wegen Fixgebühr nicht (50 in Depotwährung)
            continue
        label = "Long" if side == "long" else "Short"
        via = f" via {product['name']} ({product['isin']}), KO {product['barrier']:g} {product.get('currency', '')}" if product else ""
        broker.open(snap.symbol, side, snap.price, margin, lev,
                    f"{label} {lev:g}x{via}: Signal {score:+.2f} | {snap.tech.describe()} | {snap.news.summary}",
                    product=product,
                    entry={"score": round(score, 3), "tech": round(snap.tech.score, 3), "news": round(snap.news.score, 3),
                           "n_news": snap.news.n_headlines, "analyst": snap.news.analyst, "lev": lev})
