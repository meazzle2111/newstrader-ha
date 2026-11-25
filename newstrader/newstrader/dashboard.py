"""Grafische Oberfläche: kleiner lokaler Webserver (nur 127.0.0.1) + Dashboard im Browser.

Startet auf Wunsch den Bot im selben Prozess mit. Läuft der Bot schon woanders (z. B. im Terminal),
zeigt die Oberfläche nur dessen Daten an.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _trades(limit: int = 300) -> list[dict]:
    path = STATE_DIR / "trades.csv"
    if not path.exists():
        return []
    with path.open(newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-limit:][::-1]  # neueste zuerst


def _version() -> str:
    try:
        return (Path(__file__).parent / "VERSION").read_text().strip()
    except OSError:
        return "lokal"


def build_state(cfg: Config, in_process: bool) -> dict:
    from .bot import running_pid
    from . import learning, logbuf
    zc = learning.zocker_cfg(cfg)  # Zocker: eigenes Profil + Gelerntes
    pf = _read_json(STATE_DIR / "portfolio.json", None)
    cycle = _read_json(STATE_DIR / "last_cycle.json", {"symbols": {}})
    symbols = cycle.get("symbols", {})
    prices = {s: v["price"] for s, v in symbols.items()}

    from .broker import PaperBroker, direction
    positions, held_value, exposure = [], 0.0, 0.0
    if pf:
        for sym, p in pf["positions"].items():
            p.setdefault("side", "long"); p.setdefault("leverage", 1.0)
            p.setdefault("margin", p["qty"] * p["avg_price"]); p.setdefault("low", p.get("high", p["avg_price"]))
            px = prices.get(sym, p["avg_price"])
            d = direction(p)
            pnl = PaperBroker.pnl(p, px)
            value = PaperBroker.position_value(p, px)
            held_value += value
            exposure += p["qty"] * px
            _b = PaperBroker.__new__(PaperBroker)
            _b.cfg = zc
            lv = _b.levels(p)          # berücksichtigt echte Scheine (Stop vor der Knock-out-Schwelle)
            stop = lv["stop"]
            positions.append({
                "symbol": sym, "name": cfg.names.get(sym, sym), "side": p["side"], "leverage": p["leverage"],
                "qty": p["qty"], "avg_price": p["avg_price"], "price": px, "margin": p["margin"],
                "value": value, "exposure": p["qty"] * px, "pnl": pnl, "pnl_pct": pnl / p["margin"],
                "move_pct": d * (px / p["avg_price"] - 1), "opened": p["opened"], "stop": stop,
                "low": p["low"], "high": p["high"],
                "liquidation": lv["liquidation"] if lv["liquidation"] is not None else p["avg_price"],
                "product": p.get("product"),
            })

    summary = None
    equity_curve = []
    if pf:
        start = pf["start_capital"]
        eq = pf["cash"] + held_value
        rets = [prices[s] / p0 - 1 for s, p0 in pf["benchmark_start"].items() if s in prices]
        summary = {
            "equity": eq, "start_capital": start, "return": eq / start - 1,
            "benchmark": sum(rets) / len(rets) if rets else None,
            "cash": pf["cash"], "invested": held_value, "exposure": exposure,
            "realized_pnl": pf["realized_pnl"], "fees": pf["fees_paid"],
            "financing": pf.get("financing_paid", 0.0), "liquidations": pf.get("liquidations", 0),
            "created": pf["created"], "currency": pf.get("currency", "USD"),
        }
        for row in pf["equity_history"]:
            equity_curve.append({"t": row[0], "equity": row[1], "ret": row[1] / start - 1,
                                 "bench": row[2] if len(row) > 2 else None})

    pid = running_pid()
    return {
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "bot": {"running": pid is not None, "pid": pid, "in_process": in_process,
                "loop_minutes": cfg.loop_minutes},
        "cycle": {k: v for k, v in cycle.items() if k != "symbols"},
        "summary": summary,
        "equity_curve": equity_curve,
        "positions": sorted(positions, key=lambda p: -p["value"]),
        "symbols": [{"symbol": s, "name": cfg.names.get(s, s), "held": pf is not None and s in pf["positions"], **v}
                    for s, v in symbols.items()],
        "trades": _trades(),
        "rules": {"buy": cfg.buy_threshold, "sell": cfg.sell_threshold, "stop_loss": zc.stop_loss_pct,
                  "trailing": zc.trailing_stop_pct, "max_position": zc.max_position_pct,
                  "mode": cfg.trading_mode, "short": zc.short_threshold, "cover": zc.cover_threshold,
                  "shorts": cfg.trading_mode == "leverage" and cfg.allow_short,
                  "max_lev_stocks": zc.max_leverage_stocks, "max_lev_crypto": zc.max_leverage_crypto,
                  "closeout": zc.margin_closeout_pct, "max_gross": zc.max_gross_exposure,
                  "zocker_buy": zc.buy_threshold, "profile": cfg.zocker_profile,
                  "products": [zc.product_min_leverage, zc.product_target_leverage, zc.product_max_leverage]
                  if getattr(zc, "real_products", False) else None},
        "version": _version(), "log_errors": logbuf.error_times(), "learning": learning.learner().summary(cfg.zocker()),
    }


RANGES = {  # Anzeige-Zeitraum -> (Yahoo-Zeitraum, Kerzengröße)
    "1T": ("1d", "5m"), "5T": ("5d", "15m"), "1M": ("1mo", "1h"), "6M": ("6mo", "1d"), "1J": ("1y", "1d"),
}
BUCKET_SECONDS = {"1T": 60, "5T": 300}  # Echtzeit-Ticks werden zu Minuten-/5-Minuten-Punkten zusammengefasst
_chart_cache: dict[tuple[str, str], tuple[float, list]] = {}
_chart_lock = threading.Lock()


def _known_symbol(cfg: Config, sym: str) -> bool:
    """Beobachtungsliste, letzter Durchlauf (inkl. gehaltener Werte) oder offene Zocker-Position."""
    if sym in cfg.watchlist:
        return True
    if sym in _read_json(STATE_DIR / "last_cycle.json", {}).get("symbols", {}):
        return True
    pf = _read_json(STATE_DIR / "portfolio.json", None)
    return bool(pf and sym in pf.get("positions", {}))


def _price_series(cfg: Config, symbol: str, rng: str) -> list[list]:
    """Kursverlauf in Depotwährung (2 Minuten zwischengespeichert, damit Yahoo nicht dauernd gefragt wird)."""
    import yfinance as yf
    from .market import fx_rate, get_currency

    if rng == "1T":  # Tagesverlauf direkt aus den Tradegate-Umsätzen (Echtzeit statt ~15 Min. Verzögerung)
        from .realtime import tradegate_intraday
        isin = cfg.isins.get(symbol)
        if isin and not symbol.endswith(("-EUR", "-USD")):
            rows = tradegate_intraday(isin)
            if rows and len(rows) >= 2:
                out, last_min = [], None   # je Minute der letzte Kurs
                for ts, px in rows:
                    if ts[:16] == last_min:
                        out[-1] = [ts, px]
                    else:
                        out.append([ts, px])
                        last_min = ts[:16]
                return out
    key = (symbol, rng)
    with _chart_lock:
        hit = _chart_cache.get(key)
        if hit and time.time() - hit[0] < 120:
            return hit[1]
    period, interval = RANGES.get(rng, RANGES["5T"])
    hist = yf.Ticker(symbol).history(period=period, interval=interval)
    if rng == "1T" and hist.empty:
        hist = yf.Ticker(symbol).history(period="5d", interval="5m")
    rate = fx_rate(get_currency(symbol), cfg.base_currency)  # aktueller Kurs – für die Anzeige genau genug
    rows = [[ts.to_pydatetime().astimezone(timezone.utc).isoformat(timespec="seconds"), round(float(c) * rate, 6)]
            for ts, c in hist["Close"].dropna().items()]
    with _chart_lock:
        _chart_cache[key] = (time.time(), rows)
    return rows


def _merge_ticks(prices: list[list], ticks: list[list], rng: str) -> list[list]:
    """Yahoo-Verlauf (verzögert) mit eigenen Echtzeit-Ticks danach verlängern."""
    if not ticks:
        return prices
    bucket = BUCKET_SECONDS.get(rng)
    if not bucket:                       # Tages-/Stundenansicht: nur der aktuelle Kurs als letzter Punkt
        return prices + [ticks[-1]]
    out, last_key = list(prices), None
    for ts, px in ticks:
        key = int(datetime.fromisoformat(ts).timestamp()) // bucket
        if key == last_key:
            out[-1] = [ts, px]
        else:
            out.append([ts, px])
            last_key = key
    return out


def build_chart(cfg: Config, symbol: str, rng: str) -> dict:
    from .broker import PaperBroker
    from .realtime import get_live_quote, has_realtime, read_ticks
    prices = list(_price_series(cfg, symbol, rng))
    live = get_live_quote(cfg, symbol) if has_realtime(cfg, symbol) else None
    if prices:
        since = datetime.fromisoformat(prices[-1][0])
        prices = _merge_ticks(prices, read_ticks(symbol, since), rng)
    trades = [t for t in _trades(5000) if t["symbol"] == symbol][::-1]  # älteste zuerst
    levels = _read_json(STATE_DIR / "levels.json", {}).get(symbol, [])
    pf = _read_json(STATE_DIR / "portfolio.json", None)
    position = None
    if pf and symbol in pf["positions"]:
        pos = dict(pf["positions"][symbol])
        pos.setdefault("side", "long"); pos.setdefault("leverage", 1.0)
        pos.setdefault("margin", pos["qty"] * pos["avg_price"]); pos.setdefault("low", pos.get("high", pos["avg_price"]))
        from . import learning
        broker = PaperBroker.__new__(PaperBroker)  # nur für die Rechenhilfen, ohne Datei-Zugriff
        broker.cfg = learning.zocker_cfg(cfg)      # Stops/Margin-Call wie im Zocker
        position = {**pos, **broker.levels(pos)}
    return {"symbol": symbol, "name": cfg.names.get(symbol, symbol), "range": rng, "prices": prices,
            "trades": trades, "levels": levels, "position": position,
            "bucket": BUCKET_SECONDS.get(rng), "live": live.as_json() if live else None}


def _pf_values(cfg: Config, rng: str, state: dict) -> dict:
    """Wertverlauf des Depots (inkl. Cash) für die Grafik oben: 1T/1W aus 5-Min.-Aufzeichnung, sonst Tageswerte."""
    from datetime import datetime, timedelta, timezone
    from . import manager, portfolio as pf, portfolio_service as ps
    now = datetime.now(timezone.utc)
    if rng in ("1T", "1W"):
        raw = manager.load_values()
        if rng == "1T":
            # aktueller Tag nach deutscher Zeit; am Wochenende/vor dem ersten Punkt der letzte Tag mit Daten
            local = lambda t: datetime.fromisoformat(t).astimezone(pf.BERLIN).date().isoformat()  # noqa: E731
            days = sorted({local(r[0]) for r in raw})
            day = days[-1] if days else now.astimezone(pf.BERLIN).date().isoformat()
            pts = [{"t": t, "v": v} for t, v in raw if local(t) == day]
        else:
            cut = (now - timedelta(days=7)).isoformat()
            pts = [{"t": t, "v": v} for t, v in raw if t >= cut]
            pts = pts[::3] + ([pts[-1]] if pts and len(pts) % 3 != 1 else [])  # 15-Minuten-Raster
        return {"range": rng, "points": pts, "change": manager.period_change(pts, state), "intraday": True}
    perf = ps.performance(cfg, "model")
    txns = pf.load_txns("model")
    sec = {r["date"]: r["value"] for r in perf.get("series", [])}
    pts = manager.daily_totals(state, txns, sec)
    start = {"1M": now - timedelta(days=31), "3M": now - timedelta(days=92), "1J": now - timedelta(days=366),
             "YTD": now.replace(month=1, day=1)}.get(rng)
    if start is not None:
        before = [p for p in pts if p["t"] < start.date().isoformat()]
        pts = before[-1:] + [p for p in pts if p["t"] >= start.date().isoformat()]
    return {"range": rng, "points": pts, "change": manager.period_change(pts, state), "intraday": False}


class PriceCheckError(ValueError):
    pass


def _price_check(cfg: Config, sym: str, day, price) -> None:
    """Kurs weit weg vom Börsenkurs des Tages? -> Rückfrage in der Oberfläche (409)."""
    from . import portfolio as pf, portfolio_service as ps
    try:
        px = pf.parse_number(price)
        ref = ps.market_close_on(cfg, sym, str(day or "")[:10]) if sym and day else None
    except (TypeError, ValueError):
        return  # Formatfehler meldet die eigentliche Prüfung
    if ref and px > 0 and abs(px / ref - 1) > 0.20:
        raise PriceCheckError(f"Der Kurs {ps._de(px)} € weicht um {ps._de((px / ref - 1) * 100, 0, True)} % vom "
                              f"Schlusskurs am {ps._de_date(str(day)[:10])} ab ({ps._de(ref)} €). Stimmt er?")


def _add_zocker(perf: dict) -> None:
    """Zockerportfolio (Spielgeld mit Hebel) als Vergleichslinie: Tageswert relativ zum Startdatum des Musterdepots."""
    series = perf.get("series") or []
    if not series:
        return
    st = _read_json(STATE_DIR / "portfolio.json", None)
    if not st or not st.get("equity_history"):
        return
    daily = {}
    for t, eq, *_ in st["equity_history"]:
        daily[t[:10]] = eq  # letzter Wert des Tages (UTC-Datum genügt hier)
    base = None
    last = None
    for row in series:
        v = daily.get(row["date"], last)
        last = v
        if v is None:
            continue
        base = base or v
        row["Zocker"] = round(v / base, 6)


def make_handler(cfg: Config, in_process: bool):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Browser hat die Anfrage abgebrochen (Seitenwechsel) – harmlos

        def _json(self, data, status: int = 200) -> None:
            self._send(json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8", status)

        def _my_get(self) -> None:
            """Dein echtes Portfolio (Tab „Mein Depot“)."""
            from . import mydepot, portfolio as pf, portfolio_service as ps
            path = urlparse(self.path).path
            if path == "/api/my/overview":
                return self._json(ps.overview(cfg, mydepot.load_depot().get("cash")))
            if path == "/api/my/performance":
                return self._json(ps.performance(cfg))
            if path == "/api/my/risk":
                return self._json(ps.risk(cfg))
            if path == "/api/my/transactions.csv":
                body = ("\ufeff" + ps.transactions_csv()).encode("utf-8")  # BOM, damit Excel Umlaute erkennt
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="newstrader-transaktionen.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/my/transactions":
                from dataclasses import asdict
                txns = sorted(pf.load_txns(), key=pf.sort_key, reverse=True)
                syms = sorted(set(cfg.watchlist) | {t.symbol for t in txns})
                return self._json({"transactions": [asdict(t) for t in txns],
                                   "symbols": [{"symbol": x, "name": cfg.names.get(x, x), "isin": cfg.isins.get(x, "")}
                                               for x in syms]})
            if path == "/api/my/alerts":
                st = pf.load_alert_state()
                return self._json({"log": st["log"][-30:][::-1], "settings": {
                    "enabled": cfg.my_alerts_enabled, "stop": cfg.my_alert_stop_pct, "trailing": cfg.my_alert_trailing_pct,
                    "move": cfg.my_alert_move_pct, "report": cfg.my_daily_report_time,
                    "notify": cfg.ha_notify_service or ("Mac-Mitteilung" if not __import__("os").environ.get("SUPERVISOR_TOKEN") else "Home Assistant")}})
            self._json({"error": "nicht gefunden"}, 404)

        def do_GET(self):  # noqa: N802
            try:
                self._do_get()
            except Exception as e:  # nie still abstürzen – Fehler sichtbar machen
                log.exception("Fehler bei %s", self.path)
                self._json({"error": f"Interner Fehler: {e}"}, 500)

        def _pf_get(self) -> None:
            """Portfoliomanager (Musterdepot)."""
            from . import manager, portfolio as pf, portfolio_service as ps
            path = urlparse(self.path).path
            state = manager.load_state()
            cash = manager.cash_of(state["start_capital"], pf.load_txns("model")) if state.get("start_capital") else None
            if path == "/api/pf/state":
                now = __import__("datetime").datetime.now(pf.BERLIN)
                acts = sorted(state.get("actions", []), key=lambda a: (a["time"], a.get("value", 0)), reverse=True)[:150]
                acts = [{**a, "isin": cfg.isins.get(a["symbol"])} for a in acts]
                return self._json({
                    "setup": bool(state.get("start_capital")), "start_capital": state.get("start_capital"),
                    "style": state.get("style"), "created": state.get("created"), "cash": cash,
                    "styles": {k: {"label": v.label, "n": f"{v.n_min}–{v.n_max}", "max_weight": v.max_weight,
                                   "stop": v.stop, "trailing": v.trailing, "cash": v.cash, "crypto": v.crypto}
                               for k, v in manager.STYLES.items()},
                    "actions": acts, "decision_times": manager.decision_times(cfg, state),
                    "strategy": manager.strategy_view(cfg, state) if state.get("start_capital") else None, "build": manager.build_status(),
                    "check": manager.check_status(),
                    "broker": __import__("newstrader.traderepublic", fromlist=["status"]).status(),
                    "spec": __import__("newstrader.spekulation", fromlist=["status"]).status(),
                    "regime": manager._last_regime or None,
                    "review": __import__("newstrader.review", fromlist=["load"]).load() or None,
                    "pf_learning": __import__("newstrader.pf_learning", fromlist=["summary"]).summary(),
                    "weekday": now.weekday()})
            if path == "/api/pf/tr/derivatives":  # Diagnose: welche Hebelprodukte findet TR zu einer ISIN?
                from . import traderepublic as trx
                isin = parse_qs(urlparse(self.path).query).get("isin", [""])[0].upper()
                if len(isin) != 12:
                    return self._json({"error": "ISIN fehlt"}, 400)
                items, raw = trx.find_knockouts(isin, parse_qs(urlparse(self.path).query).get("dir", [None])[0])
                sample = json.loads(json.dumps(raw, default=str))[:3] if isinstance(raw, list) else (
                    {k: (v[:3] if isinstance(v, list) else v) for k, v in raw.items()} if isinstance(raw, dict) else raw)
                return self._json({"items": items[:20], "count": len(items), "raw_sample": sample,
                                   "tries": json.loads(json.dumps(trx.DIAG.get("tries", []), default=str))})
            if not state.get("start_capital"):
                return self._json({"error": "noch kein Musterdepot"}, 404)
            if path == "/api/pf/overview":
                ov = ps.overview(cfg, cash, "model")
                st = manager.style_of(state)
                for p_ in ov["positions"]:  # Stops des Musterdepots (Stil) statt der Alarm-Einstellungen
                    p_["stop_level"] = manager.stop_level(p_["avg_price"], p_["high_since_buy"], st)
                ov["kpi"]["start_capital"] = state["start_capital"]
                ov["kpi"]["since_start"] = ov["kpi"]["total"] / state["start_capital"] - 1
                return self._json(ov)
            if path == "/api/pf/performance":
                perf = ps.performance(cfg, "model")
                _add_zocker(perf)
                return self._json(perf)
            if path == "/api/pf/risk":
                return self._json(ps.risk(cfg, "model"))
            if path == "/api/pf/values":
                rng = parse_qs(urlparse(self.path).query).get("range", ["1M"])[0]
                return self._json(_pf_values(cfg, rng, state))
            if path == "/api/pf/transactions":
                from dataclasses import asdict
                txns = sorted(pf.load_txns("model"), key=pf.sort_key, reverse=True)
                syms = sorted(set(cfg.watchlist) | {t.symbol for t in txns})
                return self._json({"transactions": [asdict(t) for t in txns],
                                   "symbols": [{"symbol": x, "name": cfg.names.get(x, x), "isin": cfg.isins.get(x, "")} for x in syms]})
            self._json({"error": "nicht gefunden"}, 404)

        def _do_get(self):
            if self.path.startswith("/api/pf/"):
                return self._pf_get()
            if self.path.startswith("/api/my/"):
                return self._my_get()
            page = urlparse(self.path).path  # ohne ?parameter
            if page in ("/", "/index.html"):
                self._send((STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif page == "/mine.js":
                self._send((STATIC / "mine.js").read_bytes(), "text/javascript; charset=utf-8")
            elif page == "/api/logs":
                from . import logbuf
                q = parse_qs(urlparse(self.path).query)
                after = int((q.get("after") or ["0"])[0] or 0)
                if after > logbuf.last_seq():   # Server wurde neu gestartet – alles neu schicken
                    after = 0
                self._json({"entries": logbuf.entries(after, 2000 if after == 0 else 500), "last": logbuf.last_seq(),
                            "areas": logbuf.AREAS, "reset": after == 0})
            elif page == "/api/logs/download":
                body = b""
                for f in (STATE_DIR / "bot.log.1", STATE_DIR / "bot.log"):
                    if f.exists():
                        body += f.read_bytes()[-3_000_000:]
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="newstrader-log-{datetime.now():%Y-%m-%d-%H%M}.txt"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif page == "/api/health":  # für den Watchdog von Home Assistant
                from .bot import healthy, HEARTBEAT
                ok = not in_process or healthy()
                body = json.dumps({"ok": ok, "heartbeat_age_s": round(time.time() - HEARTBEAT[0])}).encode()
                self._send(body, "application/json", 200 if ok else 503)
            elif self.path.startswith("/api/chart"):
                q = parse_qs(urlparse(self.path).query)
                sym = q.get("symbol", [""])[0]
                if not _known_symbol(cfg, sym):
                    return self._send(b'{"error": "unbekanntes Symbol"}', "application/json", 400)
                try:
                    body = json.dumps(build_chart(cfg, sym, q.get("range", ["5T"])[0]), ensure_ascii=False).encode()
                except Exception as e:  # Yahoo nicht erreichbar o. Ä.
                    log.warning("Chart für %s fehlgeschlagen: %s", sym, e)
                    return self._send(json.dumps({"error": str(e)}).encode(), "application/json", 502)
                self._send(body, "application/json; charset=utf-8")
            elif self.path.startswith("/api/live"):
                from .realtime import get_live_quote, has_realtime
                q = parse_qs(urlparse(self.path).query)
                syms = [x for x in q.get("symbols", [""])[0].split(",") if x in cfg.watchlist][:8]
                quotes = {}
                for sym in syms:
                    lq = get_live_quote(cfg, sym) if has_realtime(cfg, sym) else None
                    if lq:
                        quotes[sym] = lq.as_json()
                self._send(json.dumps({"quotes": quotes}).encode(), "application/json")
            elif self.path.startswith("/api/mydepot"):
                from . import mydepot
                data = {"depot": mydepot.load_depot(), "reports": mydepot.load_reports()[:20],
                        "build": mydepot.build_status(),
                        "next_slot": mydepot.next_slot(cfg), "times": cfg.my_signal_times,
                        "symbols": [{"symbol": s, "name": cfg.names.get(s, s)} for s in cfg.watchlist]}
                self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif self.path.startswith("/api/state"):
                body = json.dumps(build_state(cfg, in_process), ensure_ascii=False).encode()
                self._send(body, "application/json; charset=utf-8")
            else:
                self._send(b"not found", "text/plain", 404)

        def do_POST(self):  # noqa: N802
            from . import mydepot
            if not self.path.startswith(("/api/mydepot", "/api/my/", "/api/pf/")):
                return self._send(b"not found", "text/plain", 404)
            # Eigener Header erzwingt einen CORS-Preflight -> fremde Webseiten können hier nichts ändern
            if self.headers.get("X-NewsTrader") != "1":
                return self._send(b'{"ok": false, "error": "forbidden"}', "application/json", 403)
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 100_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Ungültige Anfrage")
                if self.path.startswith("/api/my/"):
                    return self._my_post(body)
                if self.path.startswith("/api/pf/"):
                    return self._pf_post(body)
                if self.path.startswith("/api/mydepot/portfolio"):
                    if not mydepot.load_depot().get("value"):
                        raise ValueError("Bitte zuerst den Depotwert eintragen")
                    out = {"ok": True, "started": mydepot.start_portfolio_build(cfg)}
                elif self.path.startswith("/api/mydepot/adopt"):
                    out = {"ok": True, "depot": mydepot.adopt_portfolio(cfg)}
                elif self.path.startswith("/api/mydepot/generate"):
                    rep = mydepot.generate_report(cfg, "manuell")
                    if rep is None:
                        raise ValueError("Noch kein Bot-Durchlauf vorhanden")
                    out = {"ok": True}
                else:
                    first = not mydepot.load_depot().get("value") and not mydepot.load_reports()
                    out = {"ok": True, "depot": mydepot.save_depot(cfg, body.get("value"), body.get("holdings"))}
                    if first and not out["depot"]["holdings"]:  # erster Eintrag ohne Bestand -> Startportfolio bauen
                        out["portfolio_started"] = mydepot.start_portfolio_build(cfg)
                self._send(json.dumps(out, ensure_ascii=False).encode(), "application/json")
            except ValueError as e:
                self._send(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False).encode(), "application/json", 400)
            except Exception as e:
                log.exception("Fehler bei %s", self.path)
                self._json({"ok": False, "error": f"Interner Fehler: {e}"}, 500)

        def _pf_post(self, body: dict) -> None:
            try:
                return self._pf_post_inner(body)
            except PriceCheckError as e:
                return self._json({"ok": False, "code": "price_check", "error": str(e)}, 409)

        def _pf_post_inner(self, body: dict) -> None:
            from . import manager, portfolio_service as ps
            from .mydepot import notify
            path = urlparse(self.path).path
            if path == "/api/pf/setup":
                manager.start_setup(cfg, body.get("capital"), str(body.get("style", "ausgewogen")), notify)
                return self._json({"ok": True})
            if path == "/api/pf/reset":
                if body.get("confirm") != "ZURÜCKSETZEN":
                    raise ValueError("Bitte zur Bestätigung ZURÜCKSETZEN eingeben")
                manager.reset()
                return self._json({"ok": True})
            if path == "/api/pf/action":
                aid = str(body.get("id", ""))
                if body.get("do") == "reject":
                    return self._json({"ok": True, "action": manager.reject_action(aid)})
                if body.get("do") == "confirm":
                    return self._json({"ok": True, "action": manager.confirm_action(aid)})
                if body.get("do") == "confirm_all":
                    sym = str(body.get("symbol") or "").strip() or None
                    return self._json({"ok": True, "confirmed": manager.confirm_all(sym)})
                if body.get("do") == "adjust":
                    act = manager._find(manager.load_state(), aid)
                    if not body.get("force") and act["side"] in ("buy", "sell") and body.get("price"):
                        _price_check(cfg, act["symbol"], body.get("date") or act["time"][:10], body["price"])
                    return self._json({"ok": True, "action": manager.adjust_action(aid, body, ps.symbol_ok)})
                raise ValueError("Unbekannte Aktion")
            if path == "/api/pf/booking":
                if not body.get("force") and body.get("type") in ("buy", "sell"):
                    _price_check(cfg, str(body.get("symbol", "")).strip().upper(), body.get("date"), body.get("price"))
                a = manager.manual_booking(cfg, body, ps.symbol_ok)
                if body.get("isin"):
                    cfg.isins.setdefault(a["symbol"], str(body["isin"]).strip().upper())
                return self._json({"ok": True, "action": a})
            if path == "/api/pf/check":
                from .bot import WAKE, other_bot_pid
                st = manager.request_check()
                if in_process:
                    WAKE.set()                   # Bot im selben Prozess: Durchlauf sofort vorziehen
                elif not other_bot_pid():        # kein Bot läuft: direkt mit den letzten Daten prüfen
                    import threading as _th
                    _th.Thread(target=manager.run_requested_check, daemon=True,
                               args=(cfg, notify, cfg.names, lambda syms: {})).start()
                return self._json({"ok": True, "check": st})
            if path == "/api/pf/export":   # Umzug in das Add-on aus dem Repository: kompletten Stand nach /share kopieren
                import shutil as _sh
                from datetime import datetime as _dt
                from .config import CONFIG_FILE
                share = Path(os.environ.get("NEWSTRADER_SHARE", "/share")) / "newstrader"
                if not share.parent.exists():
                    raise ValueError("Kein /share-Ordner – nur im Home-Assistant-Add-on möglich")
                dst = share / "umzug"
                if dst.exists():
                    dst.rename(share / f"umzug-alt-{_dt.now().strftime('%Y%m%d-%H%M%S')}")
                _sh.copytree(STATE_DIR, dst / "state", ignore=_sh.ignore_patterns("bot.pid", "*.tmp"))
                if CONFIG_FILE.exists():
                    _sh.copy(CONFIG_FILE, dst / "config.json")
                n = sum(1 for p in (dst / "state").rglob("*") if p.is_file())
                return self._json({"ok": True, "files": n, "path": str(dst)})
            if path == "/api/pf/zocker/profile":
                from .config import ZOCKER_PROFILES
                name = str(body.get("profile", ""))
                if name not in ("maximal", "action"):
                    raise ValueError("Unbekanntes Profil")
                (STATE_DIR / "zocker_profile.txt").write_text(name)
                return self._json({"ok": True, "profile": name})
            if path == "/api/pf/spec":
                from . import spekulation
                return self._json({"ok": True, "spec": spekulation.set_enabled(bool(body.get("enabled")))})
            if path == "/api/pf/spec/prepare":
                from . import spekulation
                if spekulation.ANALYST is None:
                    raise ValueError("Der Bot läuft nicht")
                return self._json({"ok": True, "spec": spekulation.prepare_now(cfg, spekulation.ANALYST)})
            if path.startswith("/api/pf/tr/"):
                from . import traderepublic as trx
                op = path.rsplit("/", 1)[1]
                if op == "login":
                    return self._json({"ok": True, "broker": trx.start_login(cfg, notify, body.get("phone"), body.get("pin"))})
                if op == "code":
                    return self._json({"ok": True, "broker": trx.submit_code(cfg, notify, body.get("code"))})
                if op == "cancel":
                    return self._json({"ok": True, "broker": trx.cancel_login()})
                if op == "sync":
                    return self._json({"ok": True, "broker": trx.sync_in_background(cfg, notify)})
                if op == "logout":
                    return self._json({"ok": True, "broker": trx.logout()})
            if path == "/api/pf/strategy":
                return self._json({"ok": True, "strategy": manager.set_strategy(cfg, body)})
            if path == "/api/pf/deposit":
                return self._json({"ok": True, **manager.deposit(body.get("amount"), str(body.get("kind", "deposit")))})
            self._json({"ok": False, "error": "nicht gefunden"}, 404)

        def _my_post(self, body: dict) -> None:
            from . import mydepot, portfolio as pf, portfolio_service as ps
            path = urlparse(self.path).path
            if path == "/api/my/transactions":
                try:
                    return self._json({"ok": True, **ps.add_transaction(cfg, body)})
                except ps.PriceCheck as e:
                    return self._json({"ok": False, "code": "price_check", "error": str(e)}, 409)
            if path == "/api/my/transactions/delete":
                pf.delete_txn(str(body.get("id", "")))
                return self._json({"ok": True})
            if path == "/api/my/cash":
                return self._json({"ok": True, "depot": mydepot.save_cash(body.get("cash"))})
            self._json({"ok": False, "error": "nicht gefunden"}, 404)

        def log_message(self, *args):  # keine Zugriffszeilen im Log
            pass

    return Handler


def serve(cfg: Config, port: int = 8765, with_bot: bool = True, open_browser: bool = True,
          host: str = "127.0.0.1", fixed_port: bool = False) -> None:
    from .bot import other_bot_pid, run_forever

    # Erst den Port belegen (bei Bedarf den nächsten freien nehmen), dann den Bot starten.
    server = None
    for p in range(port, port + (1 if fixed_port else 20)):
        try:
            server = ThreadingHTTPServer((host, p), make_handler(cfg, False))
            break
        except OSError:
            log.info("Port %s ist belegt – versuche %s", p, p + 1)
    if server is None:
        raise SystemExit(f"Kein freier Port zwischen {port} und {port + 19}.")
    port = server.server_address[1]

    stop = threading.Event()
    bot_thread = None
    if with_bot:
        pid = other_bot_pid()  # eigener Prozess (z. B. PID 1 im Container) zählt nicht
        if pid:
            log.info("Bot läuft bereits in Prozess %s – Oberfläche zeigt nur dessen Daten.", pid)
        else:
            bot_thread = threading.Thread(target=run_forever, args=(cfg, stop), name="bot", daemon=True)
            bot_thread.start()

    server.RequestHandlerClass = make_handler(cfg, bot_thread is not None)
    url = f"http://127.0.0.1:{port}/"
    log.info("Oberfläche: %s  (Beenden mit Ctrl+C)", url)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        if bot_thread:
            bot_thread.join(timeout=30)
        print("\nBeendet. Depot ist gespeichert.")
