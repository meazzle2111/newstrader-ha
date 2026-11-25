"""Verbindet die Portfolio-Rechnungen mit Kursdaten, Firmendaten, Bot-Signalen und Benachrichtigungen."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yfinance as yf

from . import portfolio as pf
from .config import Config, STATE_DIR
from .market import fx_history, fx_rate, get_currency, get_daily_closes, get_quote

log = logging.getLogger(__name__)
META_FILE = STATE_DIR / "cache" / "meta.json"
REPORT_STATE = STATE_DIR / "my_daily_report.json"

SECTORS = {
    "Technology": "Technologie", "Financial Services": "Finanzen", "Healthcare": "Gesundheit",
    "Consumer Cyclical": "Konsum (zyklisch)", "Consumer Defensive": "Konsum (Basis)", "Industrials": "Industrie",
    "Basic Materials": "Grundstoffe", "Energy": "Energie", "Utilities": "Versorger",
    "Communication Services": "Kommunikation", "Real Estate": "Immobilien",
}
COUNTRIES = {"Germany": "Deutschland", "United States": "USA", "Netherlands": "Niederlande", "France": "Frankreich",
             "Switzerland": "Schweiz", "United Kingdom": "Großbritannien", "Ireland": "Irland", "Luxembourg": "Luxemburg",
             "Denmark": "Dänemark", "Japan": "Japan", "China": "China", "Canada": "Kanada", "Taiwan": "Taiwan"}

_cache: dict = {}
_cache_lock = threading.Lock()
_meta_lock = threading.Lock()


def _cached(key, ttl, fn):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (time.time(), val)
    return val


# --------------------------------------------------------------------------- Kurse
def symbol_ok(sym: str) -> bool:
    return get_daily_closes(sym) is not None


def _period_for(start: str) -> str:
    days = (datetime.now().date() - datetime.fromisoformat(start).date()).days
    for p, d in (("1y", 360), ("2y", 720), ("5y", 1800)):
        if days < d:
            return p
    return "max"


def history_eur(cfg: Config, sym: str, period: str) -> pd.Series | None:
    """Tagesschlusskurse in € (Fremdwährung mit dem jeweiligen Tageskurs umgerechnet), 30 Min. zwischengespeichert."""
    def load():
        try:
            h = yf.Ticker(sym).history(period=period, interval="1d")
        except Exception as e:
            log.warning("Historie %s nicht abrufbar: %s", sym, e)
            return None
        if h.empty:
            return None
        s = h["Close"].dropna()
        s.index = s.index.tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")]
        cur = get_currency(sym)
        if cur != cfg.base_currency:
            fx = fx_history(cur, cfg.base_currency, period)
            if fx is None or fx.empty:
                s = s * fx_rate(cur, cfg.base_currency)
            else:
                fx = fx[~fx.index.duplicated(keep="last")]
                s = (s * fx.reindex(s.index.union(fx.index)).ffill().reindex(s.index)).dropna()
        return s
    return _cached(("hist", sym, period), 1800, load)


def _isin_map(cfg: Config, txns) -> None:
    """ISINs aus deinen Transaktionen für Echtzeitkurse nutzbar machen."""
    for t in txns:
        if t.isin and t.symbol not in cfg.isins:
            cfg.isins[t.symbol] = t.isin


def live_price(cfg: Config, sym: str) -> tuple[float | None, str, bool]:
    q = get_quote(sym, cfg.base_currency, cfg)
    if q is None:
        return None, "", False
    tradable = q.tradable if q.tradable is not None else q.age_minutes() <= cfg.max_quote_age_minutes
    return q.price, q.source, tradable


def prev_close_eur(cfg: Config, sym: str) -> float | None:
    """Schlusskurs des letzten Handelstags vor heute (für die Tagesveränderung)."""
    closes = get_daily_closes(sym)
    if closes is None or closes.empty:
        return None
    today = pd.Timestamp(datetime.now(pf.BERLIN).date())
    idx = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    before = closes[idx < today]
    if before.empty:
        return None
    cur = get_currency(sym)
    rate = fx_rate(cur, cfg.base_currency) if cur != cfg.base_currency else 1.0
    return float(before.iloc[-1]) * rate


# --------------------------------------------------------------------------- Firmendaten
def _load_meta() -> dict:
    try:
        return json.loads(META_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def meta_for(cfg: Config, symbols: list[str], fetch_missing: bool = True) -> dict:
    """Name, Branche, Land, Währung je Wert. Fehlendes wird im Hintergrund nachgeladen (Yahoo ist langsam)."""
    meta = _load_meta()
    missing = [s for s in symbols if s not in meta]
    for s in missing:
        if s.endswith(("-EUR", "-USD")):
            meta[s] = {"name": cfg.names.get(s, s), "sector": "Krypto", "country": "—", "currency": get_currency(s)}
    missing = [s for s in symbols if s not in meta]
    if missing and fetch_missing:
        threading.Thread(target=_fetch_meta, args=(cfg, missing), daemon=True).start()
    out = {}
    for s in symbols:
        m = dict(meta.get(s, {}))
        m.setdefault("name", cfg.names.get(s, s))
        m.setdefault("currency", get_currency(s))
        m["currency"] = {"GBp": "GBP"}.get(m["currency"], m["currency"])
        out[s] = m
    return out


def _fetch_meta(cfg: Config, symbols: list[str]) -> None:
    if not _meta_lock.acquire(blocking=False):
        return
    try:
        meta = _load_meta()
        for s in symbols:
            if s in meta:
                continue
            try:
                info = yf.Ticker(s).info or {}
            except Exception:
                info = {}
            meta[s] = {
                "name": cfg.names.get(s) or info.get("shortName") or s,
                "sector": SECTORS.get(info.get("sector"), info.get("sector") or ("ETF/Fonds" if info.get("quoteType") == "ETF" else None)),
                "country": COUNTRIES.get(info.get("country"), info.get("country")),
                "currency": get_currency(s),
            }
            time.sleep(0.5)
        META_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = META_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        tmp.replace(META_FILE)
    finally:
        _meta_lock.release()


# --------------------------------------------------------------------------- Übersicht
def _bot_signals() -> dict:
    try:
        return json.loads((STATE_DIR / "last_cycle.json").read_text()).get("symbols", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def overview(cfg: Config, cash: float | None, book: str = "real", txns=None) -> dict:
    """Bestand mit Echtzeitkursen, Tagesveränderung, Gewinnen, Gewichten, Bot-Signalen."""
    txns = pf.load_txns(book) if txns is None else txns
    _isin_map(cfg, txns)
    hs = pf.compute_holdings(txns)
    open_syms = [s for s, h in hs.items() if h.qty > 0]
    meta = meta_for(cfg, list(hs))
    sig = _bot_signals()
    positions = []
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(4) as ex:
        live = dict(zip(open_syms, ex.map(lambda s: (live_price(cfg, s), prev_close_eur(cfg, s)), open_syms)))
    for s in open_syms:
        h = hs[s]
        (price, source, tradable), prev = live[s]
        stale = price is None
        price = price if price is not None else (prev or h.avg_price)
        value = h.qty * price
        # Tagesveränderung: ab gestrigem Schluss – bei heute gekauften Werten ab dem Kaufkurs
        if h.first_buy == datetime.now(pf.BERLIN).date().isoformat():
            day_pnl = value - h.cost
            day_change = value / h.cost - 1 if h.cost else None
            prev_for_day = h.avg_price
        else:
            day_pnl = (price - prev) * h.qty if prev else None
            day_change = (price / prev - 1) if prev else None
            prev_for_day = prev
        hist = history_eur(cfg, s, _period_for(h.first_buy))
        high = max([price] + ([float(hist[hist.index >= pd.Timestamp(h.first_buy)].max())]
                              if hist is not None and not hist[hist.index >= pd.Timestamp(h.first_buy)].empty else []))
        b = sig.get(s, {})
        positions.append({
            "symbol": s, "name": meta[s]["name"], "qty": h.qty, "avg_price": h.avg_price, "cost": h.cost,
            "price": price, "price_source": source or ("letzter Schluss" if stale else ""), "stale": stale,
            "tradable": tradable, "value": value, "prev_close": prev,
            "day_change": day_change, "day_pnl": day_pnl,
            "pnl": value - h.cost, "pnl_pct": (value / h.cost - 1) if h.cost else None,
            "realized": h.realized, "dividends": h.dividends, "first_buy": h.first_buy, "high_since_buy": high,
            "stop_level": max(h.avg_price * (1 - cfg.my_alert_stop_pct), high * (1 - cfg.my_alert_trailing_pct)),
            "signal": b.get("combined"), "signal_summary": b.get("news_summary"), "signal_tech": b.get("tech_detail"),
            "sector": meta[s].get("sector"), "country": meta[s].get("country"), "currency": meta[s].get("currency"),
        })
    total_sec = sum(p["value"] for p in positions)
    for p in positions:
        p["weight"] = p["value"] / total_sec if total_sec else 0.0
    positions.sort(key=lambda p: -p["value"])
    closed = [{"symbol": s, "name": meta[s]["name"], "realized": h.realized, "dividends": h.dividends,
               "last_txn": h.last_txn} for s, h in hs.items() if h.qty <= 0 and (h.realized or h.dividends)]
    realized = sum(h.realized for h in hs.values())
    dividends = sum(h.dividends for h in hs.values())
    cost = sum(p["cost"] for p in positions)
    day_pnl = sum(p["day_pnl"] or 0 for p in positions)
    day_base = sum(p["value"] - (p["day_pnl"] or 0) for p in positions)
    values = {p["symbol"]: p["value"] for p in positions}
    return {
        "has_txns": bool(txns),
        "positions": positions, "closed": closed,
        "kpi": {
            "securities": total_sec, "cash": cash, "total": total_sec + (cash or 0), "cost": cost,
            "unrealized": total_sec - cost, "unrealized_pct": (total_sec / cost - 1) if cost else None,
            "realized": realized, "dividends": dividends, "total_pnl": total_sec - cost + realized + dividends,
            "day_pnl": day_pnl, "day_change": (day_pnl / day_base) if day_base else None,
            "fees": sum(h.fees for h in hs.values()),
        },
        "allocation": {k: pf.allocation(values, meta, k) for k in ("symbol", "sector", "country", "currency")},
        "names": {s: meta[s]["name"] for s in meta},
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- Wertentwicklung + Risiko
def performance(cfg: Config, book: str = "real") -> dict:
    txns = pf.load_txns(book)
    if not txns:
        return {"series": [], "monthly": []}
    start = min(t.date for t in txns)
    period = _period_for(start)
    syms = sorted({t.symbol for t in txns if t.type != "dividend"})
    prices = {s: history_eur(cfg, s, period) for s in syms}
    prices = {s: p for s, p in prices.items() if p is not None}
    hist = pf.valuation_history(txns, prices)
    if hist.empty:
        return {"series": [], "monthly": []}
    days = hist.index
    bench = {}
    for name, sym in cfg.my_benchmarks.items():
        b = history_eur(cfg, sym, period)
        if b is not None and not b.empty:
            bench[name] = pf.benchmark_index(b, days)
    # Live: heutigen Wert mit Echtzeitkursen ersetzen (der Tagesschluss steht erst abends fest)
    series = []
    for i, d in enumerate(days):
        row = {"date": d.strftime("%Y-%m-%d"), "value": round(float(hist["value"].iloc[i]), 2),
               "index": round(float(hist["index"].iloc[i]), 6)}
        for name, s in bench.items():
            row[name] = round(float(s.iloc[i]), 6) if pd.notna(s.iloc[i]) else None
        series.append(row)
    idx = hist["index"]
    summary = {"twr": float(idx.iloc[-1] - 1), "max_drawdown": pf.max_drawdown(idx), "start": start,
               "bench": {n: float(s.iloc[-1] - 1) for n, s in bench.items() if pd.notna(s.iloc[-1])}}
    return {"series": series, "monthly": pf.monthly_returns(idx),
            "bench_monthly": {n: pf.monthly_returns(s) for n, s in bench.items()}, "summary": summary}


def risk(cfg: Config, book: str = "real") -> dict:
    txns = pf.load_txns(book)
    hs = pf.compute_holdings(txns)
    open_syms = [s for s, h in hs.items() if h.qty > 0]
    if not open_syms:
        return {}
    prices = {s: history_eur(cfg, s, "1y") for s in open_syms}
    values = {}
    for s in open_syms:
        p = prices.get(s)
        values[s] = hs[s].qty * (float(p.iloc[-1]) if p is not None and len(p) else hs[s].avg_price)
    total = sum(values.values())
    weights = {s: v / total for s, v in values.items()} if total else {}
    # Gemeinsamer Börsenkalender (Mo–Fr): Krypto-Wochenenden fließen in den Montag ein, statt Nullzeilen zu erzeugen
    frame = pd.DataFrame({s: p for s, p in prices.items() if p is not None})
    days = pd.bdate_range(frame.index.min(), frame.index.max()) if not frame.empty else pd.DatetimeIndex([])
    aligned = frame.reindex(frame.index.union(days)).ffill().reindex(days)
    rets = aligned.pct_change().iloc[1:]
    bench_sym = cfg.my_benchmarks.get("MSCI World") or next(iter(cfg.my_benchmarks.values()), None)
    bench = None
    if bench_sym:
        b = history_eur(cfg, bench_sym, "1y")
        if b is not None and not b.empty:
            bench = b.reindex(b.index.union(days)).ffill().reindex(days).pct_change().iloc[1:]
    m = pf.risk_metrics(weights, rets, bench)
    m["missing"] = [s for s in open_syms if prices.get(s) is None]
    m["bench_name"] = next((n for n, s in cfg.my_benchmarks.items() if s == bench_sym), "")
    if bench is not None and len(bench) > 20:
        m["bench_volatility"] = float(np.std(bench, ddof=1) * np.sqrt(252))
    return m


# --------------------------------------------------------------------------- Alarme + Abendbericht
def watch(cfg: Config, notify) -> list[dict]:
    """Vom Bot jede Minute aufgerufen: Stop-Loss und starke Bewegungen deiner Positionen -> Push."""
    if not cfg.my_alerts_enabled:
        return []
    txns = pf.load_txns()
    if not txns:
        return []
    ov = overview(cfg, None)
    live_positions = [p for p in ov["positions"] if not p["stale"] and p["tradable"]]
    state = pf.load_alert_state()
    now = datetime.now(timezone.utc)
    alerts = pf.check_alerts(live_positions, {"stop_loss_pct": cfg.my_alert_stop_pct,
                                              "trailing_pct": cfg.my_alert_trailing_pct,
                                              "move_pct": cfg.my_alert_move_pct}, state["sent"], now)
    for a in alerts:
        notify(cfg, a["title"], a["message"])
        state["sent"][a["key"]] = now.isoformat(timespec="seconds")
        state["log"].append({"time": now.isoformat(timespec="seconds"), "title": a["title"], "message": a["message"]})
        log.info("Alarm: %s", a["title"])
    if alerts:
        pf.save_alert_state(state)
    return alerts


def maybe_daily_report(cfg: Config, notify) -> bool:
    """Abendbericht (Mo–Fr zur eingestellten Uhrzeit, genau einmal pro Tag)."""
    if not cfg.my_daily_report_time or not pf.load_txns():
        return False
    now = datetime.now(pf.BERLIN)
    h, m = (int(x) for x in cfg.my_daily_report_time.split(":"))
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now.weekday() >= 5 or now < due or (now - due).total_seconds() > 7200:
        return False
    try:
        done = json.loads(REPORT_STATE.read_text()).get("last")
    except (FileNotFoundError, json.JSONDecodeError):
        done = None
    if done == now.date().isoformat():
        return False
    ov = overview(cfg, None)
    k = ov["kpi"]
    pos = [p for p in ov["positions"] if p["day_change"] is not None]
    pos.sort(key=lambda p: p["day_change"])
    lines = [f"Depot {k['securities']:,.0f} € · heute {k['day_pnl']:+,.0f} € ({(k['day_change'] or 0):+.1%})"
             .replace(",", ".")]
    if pos:
        top, flop = pos[-1], pos[0]
        lines.append(f"Top: {top['name']} {top['day_change']:+.1%} · Flop: {flop['name']} {flop['day_change']:+.1%}")
    weak = [p["name"] for p in ov["positions"] if p["signal"] is not None and p["signal"] < cfg.sell_threshold]
    if weak:
        lines.append("Schwaches Signal: " + ", ".join(weak[:4]))
    notify(cfg, "📊 NewsTrader – Tagesbericht", "\n".join(lines))
    REPORT_STATE.write_text(json.dumps({"last": now.date().isoformat()}))
    log.info("Tagesbericht gesendet")
    return True


# --------------------------------------------------------------------------- Buchen
def _de(x: float, d: int = 2, sign: bool = False) -> str:
    s = f"{x:+,.{d}f}" if sign else f"{x:,.{d}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _de_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


class PriceCheck(ValueError):
    """Kurs weicht stark vom Börsenkurs ab – Oberfläche fragt nach, ob trotzdem gespeichert werden soll."""


def market_close_on(cfg: Config, sym: str, day: str) -> float | None:
    hist = history_eur(cfg, sym, _period_for(day))
    if hist is None or hist.empty:
        return None
    before = hist[hist.index <= pd.Timestamp(day)]
    return float(before.iloc[-1]) if not before.empty else float(hist.iloc[0])


def add_transaction(cfg: Config, raw: dict, book: str = "real") -> dict:
    force = bool(raw.get("force"))
    with pf._lock:
        txns = pf.load_txns(book)
        t = pf.validate(raw, txns, symbol_ok)
        if t.type in ("buy", "sell") and not force:
            ref = market_close_on(cfg, t.symbol, t.date)
            if ref and abs(t.price / ref - 1) > 0.20:
                raise PriceCheck(f"Der Kurs {_de(t.price)} € weicht um {_de((t.price / ref - 1) * 100, 0, True)} % vom "
                                 f"Schlusskurs am {_de_date(t.date)} ab ({_de(ref)} €). Stimmt er (z. B. Kurs in €, nicht in $)?")
        pf.save_txns(txns + [t], book)
    if t.isin:
        cfg.isins.setdefault(t.symbol, t.isin)
    meta_for(cfg, [t.symbol])  # Firmendaten im Hintergrund holen
    with _cache_lock:          # neue Daten -> Zwischenergebnisse verwerfen
        for k in [k for k in _cache if k[0] != "hist"]:
            del _cache[k]
    return {"id": t.id}


def transactions_csv(book: str = "real") -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Datum", "Art", "Symbol", "Stück", "Kurs/Betrag €", "Gebühr/Steuer €", "ISIN", "Notiz"])
    art = {"buy": "Kauf", "sell": "Verkauf", "dividend": "Dividende"}
    for t in sorted(pf.load_txns(book), key=pf.sort_key):
        w.writerow([t.date, art[t.type], t.symbol, f"{t.qty:g}".replace(".", ","), f"{t.price:.4f}".replace(".", ","),
                    f"{t.fee:.2f}".replace(".", ","), t.isin, t.note])
    return buf.getvalue()
