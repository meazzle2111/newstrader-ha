"""Backtest der Technik-Regeln auf Tageskursen (ohne News – historische Schlagzeilen gibt es gratis nicht).

Zeigt, ob die Regeln + Gebühren + Stops überhaupt besser sind als einfach Kaufen und Liegenlassen.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

from .broker import PaperBroker
from .config import Config
from .market import CURRENCY_SYMBOL, fx_history, get_currency, technical_signal
from .strategy import Snapshot, run_strategy


def run_backtest(cfg: Config, period: str = "2y", learn: bool = True) -> None:
    logging.getLogger("newstrader.broker").setLevel(logging.WARNING)

    data = {}
    for sym in cfg.watchlist:
        h = yf.Ticker(sym).history(period=period, interval="1d")
        if not h.empty:
            s = h["Close"].dropna()
            s.index = s.index.tz_localize(None).normalize()  # Handelstag in Ortszeit der Börse
            s = s[~s.index.duplicated(keep="last")]
            fx = fx_history(get_currency(sym), cfg.base_currency, period)
            if fx is not None:  # in Depotwährung umrechnen (Wechselkurs des jeweiligen Tages)
                fx = fx[~fx.index.duplicated(keep="last")]
                s = (s * fx.reindex(s.index, method="ffill")).dropna()
            data[sym] = s
    if not data:
        print("Keine Daten.")
        return
    closes = pd.DataFrame(data).sort_index().ffill()
    days = closes.index[60:]  # Vorlauf für SMA50

    from .learning import Learner
    base_cfg = cfg.zocker()
    base_cfg.min_hold_minutes = 0  # Tagesschritte – Haltedauer-Sperre irrelevant
    lrn = Learner(persist=False) if learn else None
    cfg = base_cfg
    broker = PaperBroker(cfg, persist=False)
    if lrn:
        broker.on_close = lrn.record
    equity = []
    for day in days:
        broker.clock = lambda d=day: d.to_pydatetime().replace(tzinfo=timezone.utc)
        window = closes.loc[:day]
        snaps = {}
        for sym in closes.columns:
            col = window[sym].dropna()
            tech = technical_signal(col)
            if tech is not None:
                snaps[sym] = Snapshot(sym, float(col.iloc[-1]), True, tech)
        if lrn:
            broker.cfg = cfg = lrn.tune(base_cfg)
        run_strategy(broker, cfg, snaps, learner=lrn)
        equity.append(broker.equity({s: v.price for s, v in snaps.items()}))

    start = cfg.start_capital
    c = CURRENCY_SYMBOL.get(cfg.base_currency, cfg.base_currency)
    curve = pd.Series(equity, index=days)
    first, last = closes.loc[days[0]], closes.loc[days[-1]]
    bh = ((last / first) - 1).mean()
    dd = (curve / curve.cummax() - 1).min()
    sells = [t for t in broker.trades if t[1] == "CLOSE"]
    wins = [t for t in sells if float(t[8]) > 0]
    shorts = sum(1 for t in broker.trades if t[1] == "OPEN" and t[3] == "short")

    print(f"\nBacktest {days[0]:%d.%m.%Y} – {days[-1]:%d.%m.%Y}  ({len(closes.columns)} Werte, nur Technik, in {cfg.base_currency}, "
          f"Modus {cfg.trading_mode}, Profil {cfg.zocker_profile}{', lernend' if lrn else ''})")
    print(f"  Strategie:        {curve.iloc[-1]:>10,.2f} {c}  ({curve.iloc[-1] / start - 1:+.1%})")
    print(f"  Buy & Hold (gl.): {start * (1 + bh):>10,.2f} {c}  ({bh:+.1%})")
    print(f"  Max. Drawdown:    {dd:.1%}")
    print(f"  Trades:           {len(broker.trades)} (davon {len(sells)} Verkäufe, Trefferquote "
          f"{len(wins) / len(sells):.0%})" if sells else f"  Trades:           {len(broker.trades)}")
    print(f"  Gebühren:         {broker.state['fees_paid']:,.2f} {c}")
    if cfg.trading_mode == "leverage":
        print(f"  Finanzierung:     {broker.state['financing_paid']:,.2f} {c}")
        print(f"  Shorts eröffnet:  {shorts}")
        print(f"  Margin-Calls:     {broker.state['liquidations']}")
    print("\nHinweis: Vergangenheit ≠ Zukunft. Bei so wenigen Werten und Jahren ist das Ergebnis stark vom Zufall geprägt.")
