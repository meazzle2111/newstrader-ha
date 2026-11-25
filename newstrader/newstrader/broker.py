"""Fiktives Depot mit Long/Short und Hebel (wie ein CFD-/Margin-Konto). Zustand in state/portfolio.json.

Eine Position bindet `margin` (eigenes Geld). Mit Hebel h ist das Volumen h × margin.
Gewinn/Verlust = Richtung × Stück × (Kurs − Einstand), Richtung +1 = long, −1 = short.
Depotwert = Cash + Σ (margin + G/V). Ohne Hebel (h = 1, long) ist das genau ein normales Aktiendepot.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timedelta, timezone

from .config import Config, STATE_DIR
from .market import CURRENCY_SYMBOL

log = logging.getLogger(__name__)

PORTFOLIO_FILE = STATE_DIR / "portfolio.json"
TRADES_FILE = STATE_DIR / "trades.csv"
LEVELS_FILE = STATE_DIR / "levels.json"
TRADE_COLUMNS = ["time", "action", "symbol", "side", "leverage", "qty", "price", "fee", "pnl", "reason"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def direction(pos: dict) -> int:
    return -1 if pos.get("side") == "short" else 1


def is_crypto(symbol: str) -> bool:
    return symbol.endswith(("-EUR", "-USD")) and not symbol.endswith("=X")


class PaperBroker:
    def __init__(self, cfg: Config, persist: bool = True):
        self.cfg = cfg
        self.persist = persist
        self.clock = _utcnow           # im Backtest durch simulierte Zeit ersetzt
        self.trades: list[list] = []   # Trades dieser Sitzung (auch im Backtest)
        self.on_close = None           # Rückmeldung an den Lernmodus: (symbol, pos, realized, reason, liquidation, zeit)
        STATE_DIR.mkdir(exist_ok=True)
        self.cur = CURRENCY_SYMBOL.get(cfg.base_currency, cfg.base_currency)
        if persist and PORTFOLIO_FILE.exists():
            self.state = json.loads(PORTFOLIO_FILE.read_text())
            have = self.state.get("currency", "USD")
            if have != cfg.base_currency:
                raise SystemExit(f"Gespeichertes Depot ist in {have}, eingestellt ist {cfg.base_currency}. "
                                 f"Mit 'python -m newstrader reset' neu anfangen.")
            self._migrate()
        else:
            self.state = {
                "created": _utcnow().isoformat(timespec="seconds"),
                "currency": cfg.base_currency,
                "cash": cfg.start_capital,
                "start_capital": cfg.start_capital,
                "positions": {},          # symbol -> {side, leverage, qty, avg_price, margin, high, low, opened, fin_ts}
                "benchmark_start": {},    # symbol -> erster gesehener Kurs (für Buy & Hold)
                "equity_history": [],     # [zeit, depotwert, buy&hold-rendite]
                "fees_paid": 0.0,
                "financing_paid": 0.0,
                "realized_pnl": 0.0,
                "liquidations": 0,
            }
            self.save()

    def _migrate(self) -> None:
        """Ältere Depotdateien (ohne Hebel) auf das neue Format bringen."""
        for key, default in (("financing_paid", 0.0), ("liquidations", 0)):
            self.state.setdefault(key, default)
        for pos in self.positions.values():
            pos.setdefault("side", "long")
            pos.setdefault("leverage", 1.0)
            pos.setdefault("margin", pos["qty"] * pos["avg_price"])
            pos.setdefault("low", pos.get("high", pos["avg_price"]))
            pos.setdefault("fin_ts", self._now())

    def _now(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    # ------------------------------------------------------------------ Zustand
    @property
    def cash(self) -> float:
        return self.state["cash"]

    @property
    def positions(self) -> dict:
        return self.state["positions"]

    def save(self) -> None:
        if not self.persist:
            return
        tmp = PORTFOLIO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(PORTFOLIO_FILE)

    @staticmethod
    def pnl(pos: dict, price: float) -> float:
        return direction(pos) * pos["qty"] * (price - pos["avg_price"])

    @staticmethod
    def position_value(pos: dict, price: float) -> float:
        """Was die Position dem Depot wert ist: gebundene Margin + G/V (nie unter 0, Nachschusspflicht gibt es nicht)."""
        return max(0.0, pos["margin"] + PaperBroker.pnl(pos, price))

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(self.position_value(p, prices.get(s, p["avg_price"])) for s, p in self.positions.items())

    def gross_exposure(self, prices: dict[str, float]) -> float:
        return sum(p["qty"] * prices.get(s, p["avg_price"]) for s, p in self.positions.items())

    def max_leverage(self, symbol: str) -> float:
        if self.cfg.trading_mode != "leverage":
            return 1.0
        return self.cfg.max_leverage_crypto if is_crypto(symbol) else self.cfg.max_leverage_stocks

    def stop_pct(self, pos: dict) -> float:
        """Stop-Abstand; bei echten Scheinen höchstens product_stop_share des Abstands zur Knock-out-Schwelle."""
        p = pos.get("product")
        if not p:
            return self.cfg.stop_loss_pct
        dist = abs(pos["avg_price"] - p["barrier_eur"]) / pos["avg_price"]
        return min(self.cfg.stop_loss_pct, self.cfg.product_stop_share * dist)

    def trailing_pct(self, pos: dict) -> float:
        p = pos.get("product")
        if not p:
            return self.cfg.trailing_stop_pct
        dist = abs(pos["avg_price"] - p["barrier_eur"]) / pos["avg_price"]
        return min(self.cfg.trailing_stop_pct, self.cfg.product_stop_share * dist)

    def liquidation_price(self, pos: dict) -> float:
        """Kurs, bei dem der Verlust die Margin-Close-out-Schwelle erreicht (echter Schein: Knock-out-Schwelle)."""
        if pos.get("product"):
            return pos["product"]["barrier_eur"]
        loss_allowed = pos["margin"] * self.cfg.margin_closeout_pct
        return pos["avg_price"] - direction(pos) * loss_allowed / pos["qty"]

    def levels(self, pos: dict) -> dict:
        """Einstand, fester Stop, Trailing-Stop, wirksamer Stop und Liquidationskurs einer Position."""
        d = direction(pos)
        fixed = pos["avg_price"] * (1 - d * self.stop_pct(pos))
        extreme = pos["high"] if d > 0 else pos.get("low", pos["avg_price"])
        trailing = extreme * (1 - d * self.trailing_pct(pos))
        effective = max(fixed, trailing) if d > 0 else min(fixed, trailing)
        return {"entry": pos["avg_price"], "stop_fixed": fixed, "trailing": trailing, "stop": effective,
                "liquidation": self.liquidation_price(pos) if pos.get("leverage", 1) > 1 or d < 0 else None}

    def record_levels(self) -> None:
        """Pro Durchlauf die Stop-Niveaus offener Positionen speichern – für die Linien im Kurs-Chart."""
        if not self.persist or not self.positions:
            return
        try:
            data = json.loads(LEVELS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        now = self._now()
        for sym, pos in self.positions.items():
            lv = self.levels(pos)
            rows = data.setdefault(sym, [])
            rows.append([now, pos["opened"], pos.get("side", "long"), round(lv["entry"], 6), round(lv["stop"], 6),
                         None if lv["liquidation"] is None else round(lv["liquidation"], 6)])
            del rows[:-3000]
        tmp = LEVELS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(LEVELS_FILE)

    def note_benchmark(self, symbol: str, price: float) -> None:
        # Vergleichskorb wird beim ersten Durchlauf festgelegt; später ergänzte Werte verfälschen ihn sonst
        if not self.state.get("benchmark_fixed"):
            self.state["benchmark_start"].setdefault(symbol, price)

    def benchmark_return(self, prices: dict[str, float]) -> float | None:
        """Rendite, wenn man am Start alles gleichgewichtet gekauft und liegen gelassen hätte."""
        rets = [prices[s] / p0 - 1 for s, p0 in self.state["benchmark_start"].items() if s in prices]
        return sum(rets) / len(rets) if rets else None

    def record_equity(self, prices: dict[str, float]) -> float:
        eq = self.equity(prices)
        bench = self.benchmark_return(prices)
        hist = self.state["equity_history"]
        hist.append([self._now(), round(eq, 2), None if bench is None else round(bench, 6)])
        if self.state["benchmark_start"]:
            self.state["benchmark_fixed"] = True
        if len(hist) > 6000:  # minütliche Punkte: die letzten 2 Tage fein, davor alle 10 Minuten
            cut = (self.clock() - timedelta(days=2)).isoformat(timespec="seconds")
            old, new, last = [], [h for h in hist if h[0] >= cut], None
            for h in hist:
                if h[0] >= cut:
                    break
                if last is None or (datetime.fromisoformat(h[0]) - last).total_seconds() >= 600:
                    old.append(h)
                    last = datetime.fromisoformat(h[0])
            hist[:] = (old + new)[-20000:]
        return eq

    def update_extremes(self, prices: dict[str, float]) -> None:
        for sym, pos in self.positions.items():
            if sym in prices:
                pos["high"] = max(pos["high"], prices[sym])
                pos["low"] = min(pos["low"], prices[sym])

    def accrue_financing(self, prices: dict[str, float]) -> None:
        """Finanzierungskosten laufen stundengenau – auch nachts und am Wochenende, wie bei CFDs."""
        now = self.clock()
        for sym, pos in self.positions.items():
            if pos.get("product"):   # Turbo: Finanzierung steckt im Basispreis
                pos["fin_ts"] = now.isoformat(timespec="seconds")
                continue
            last = datetime.fromisoformat(pos["fin_ts"])
            years = max(0.0, (now - last).total_seconds()) / (365 * 24 * 3600)
            pos["fin_ts"] = now.isoformat(timespec="seconds")
            notional = pos["qty"] * prices.get(sym, pos["avg_price"])
            borrowed = max(0.0, notional - pos["margin"])
            cost = borrowed * self.cfg.financing_pct_pa * years
            if pos.get("side") == "short":
                cost += notional * self.cfg.short_borrow_pct_pa * years
            if cost > 0:
                self.state["cash"] -= cost
                self.state["financing_paid"] += cost

    # ------------------------------------------------------------------ Orders
    def _fee(self, notional: float) -> float:
        return self.cfg.fee_fixed + notional * self.cfg.fee_pct

    def open(self, symbol: str, side: str, price: float, margin: float, leverage: float, reason: str,
             entry: dict | None = None, product: dict | None = None) -> bool:
        if symbol in self.positions:
            return False
        sign = -1 if side == "short" else 1
        fill = price * (1 + sign * self.cfg.slippage_pct)   # long kauft etwas teurer, short verkauft etwas billiger
        notional = margin * leverage
        fee = self._fee(notional)
        if margin <= 0 or margin + fee > self.cash:
            return False
        qty = notional / fill
        self.positions[symbol] = {
            "side": side, "leverage": leverage, "qty": qty, "avg_price": fill, "margin": margin,
            "high": fill, "low": fill, "opened": self._now(), "fin_ts": self._now(),
        }
        if entry:
            self.positions[symbol]["entry"] = entry   # womit eingestiegen wurde (für den Lernmodus)
        if product:   # echter Knock-out-Schein: Knock-out-Schwelle in € (zum Einstiegskurs umgerechnet)
            self.positions[symbol]["product"] = product
        self.state["cash"] -= margin + fee
        self.state["fees_paid"] += fee
        self._log_trade("OPEN", symbol, side, leverage, qty, fill, fee, 0.0, reason)
        icon = "🟢 LONG " if side == "long" else "🟣 SHORT"
        log.info("%s %-8s %gx  %10.4f Stk @ %10.2f %s  (Margin %.0f, Volumen %.0f %s)  – %s",
                 icon, symbol, leverage, qty, fill, self.cur, margin, notional, self.cur, reason)
        self.save()
        return True

    def close(self, symbol: str, price: float, reason: str, liquidation: bool = False) -> bool:
        pos = self.positions.get(symbol)
        if not pos:
            return False
        sign = direction(pos)
        fill = price * (1 - sign * self.cfg.slippage_pct)
        fee = self._fee(pos["qty"] * fill)
        gross = self.pnl({**pos}, fill)
        payout = max(0.0, pos["margin"] + gross)            # Verlust höchstens die Margin (Negativsaldoschutz)
        self.state["cash"] += payout - fee
        self.state["fees_paid"] += fee
        realized = payout - pos["margin"] - fee
        self.state["realized_pnl"] += realized
        if liquidation:
            self.state["liquidations"] += 1
        del self.positions[symbol]
        if self.on_close:
            try:
                self.on_close(symbol, pos, realized, reason, liquidation, self.clock())
            except Exception:  # noqa: BLE001 – Lernen darf das Schließen nie verhindern
                log.exception("Lernmodus: Trade konnte nicht ausgewertet werden")
        self._log_trade("CLOSE", symbol, pos["side"], pos["leverage"], pos["qty"], fill, fee, realized, reason)
        log.info("🔴 ZU    %-8s %s %gx @ %10.2f %s  G/V %+.2f %s  – %s",
                 symbol, pos["side"], pos["leverage"], fill, self.cur, realized, self.cur, reason)
        self.save()
        return True

    # Alte Namen, damit bestehender Code weiter funktioniert
    def buy(self, symbol: str, price: float, budget: float, reason: str) -> bool:
        return self.open(symbol, "long", price, budget, 1.0, reason)

    def sell(self, symbol: str, price: float, reason: str) -> bool:
        return self.close(symbol, price, reason)

    def _log_trade(self, action, symbol, side, leverage, qty, price, fee, pnl, reason) -> None:
        row = [self._now(), action, symbol, side, f"{leverage:g}", f"{qty:.6f}", f"{price:.4f}",
               f"{fee:.2f}", f"{pnl:.2f}", reason]
        self.trades.append(row)
        if not self.persist:
            return
        if TRADES_FILE.exists():
            with TRADES_FILE.open(newline="") as f:
                header = next(csv.reader(f), [])
            if header != TRADE_COLUMNS:  # altes Format beiseitelegen
                TRADES_FILE.rename(TRADES_FILE.with_name("trades_alt.csv"))
        new = not TRADES_FILE.exists()
        with TRADES_FILE.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(TRADE_COLUMNS)
            w.writerow(row)
