"""Dein echtes Portfolio: Transaktionen, Bestand, Gewinne, Wertentwicklung, Risiko, Alarme.

Die Rechenfunktionen hier sind bewusst „rein“ (bekommen Kurse als Argument, haben keine Seiteneffekte),
damit sie vollständig getestet werden können: siehe tests/test_portfolio.py.

Konventionen
- Alle Beträge in Euro. Kurse fremder Währungen werden mit dem Tageskurs umgerechnet.
- Einstandskurs nach der Durchschnittsmethode (wie die meisten Broker es anzeigen):
  Kauf erhöht Stück und Einstand (inkl. Gebühr); Verkauf mindert beides anteilig, die Differenz ist realisiert.
- Rendite als zeitgewichtete Rendite (TWR): Käufe/Verkäufe verfälschen sie nicht, nur die Kursentwicklung zählt.
"""
from __future__ import annotations

import json
import math
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .config import STATE_DIR

BERLIN = ZoneInfo("Europe/Berlin")
TXN_FILE = STATE_DIR / "my_transactions.json"          # „Mein Depot“: deine echten Käufe
MODEL_TXN_FILE = STATE_DIR / "model_transactions.json" # Musterdepot des Portfoliomanagers
ALERTS_FILE = STATE_DIR / "my_alerts.json"
EPS = 1e-9
TYPES = ("buy", "sell", "dividend")
TRANSFER_NOTE = "Übernahme aus Trade Republic"
_lock = threading.Lock()


# --------------------------------------------------------------------------- Datenmodell
@dataclass
class Txn:
    id: str
    date: str            # YYYY-MM-DD (Handelstag)
    type: str            # buy | sell | dividend
    symbol: str
    qty: float           # Stück (bei Dividende 0)
    price: float         # Kurs je Stück in € (bei Dividende: Bruttobetrag in €)
    fee: float = 0.0     # Gebühr in € (bei Dividende: einbehaltene Steuer)
    note: str = ""
    isin: str = ""
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    @property
    def is_transfer(self) -> bool:
        """Bestand aus Trade Republic übernommen (zum alten Einstand) – für die Rendite zählt der Marktwert."""
        return self.type == "buy" and self.note.startswith(TRANSFER_NOTE)

    @property
    def cash_out(self) -> float:
        """Geldfluss in die Wertpapiere (+) bzw. heraus (−) – Grundlage für die zeitgewichtete Rendite."""
        if self.type == "buy":
            return self.qty * self.price + self.fee
        if self.type == "sell":
            return -(self.qty * self.price - self.fee)
        return -(self.price - self.fee)  # Dividende fließt aus dem Depot heraus (als Ertrag)


@dataclass
class Holding:
    symbol: str
    qty: float = 0.0
    cost: float = 0.0        # Einstand gesamt (inkl. Kaufgebühren) der noch gehaltenen Stücke
    realized: float = 0.0    # realisierte Gewinne/Verluste aus Verkäufen (nach Gebühren)
    dividends: float = 0.0   # Dividenden netto
    fees: float = 0.0        # alle gezahlten Gebühren
    first_buy: str = ""
    last_txn: str = ""

    @property
    def avg_price(self) -> float:
        return self.cost / self.qty if self.qty > EPS else 0.0


# --------------------------------------------------------------------------- Prüfung + Bestand
_THOUSANDS = __import__("re").compile(r"^[+-]?[1-9]\d{0,2}(\.\d{3})+$")


def parse_number(v) -> float:
    """Zahl aus deutscher oder englischer Schreibweise: 10.000 / 1.234,50 / 1,5 / 10000 / 0.010 (= 0,01)."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace("\u00a0", "").replace("€", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")      # deutsch: Punkt = Tausender, Komma = Dezimal
    elif _THOUSANDS.match(s):
        s = s.replace(".", "")                        # 10.000 / 1.234.567 = Tausenderpunkte
    return float(s)


def _parse_float(v, name: str) -> float:
    try:
        v = parse_number(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: keine gültige Zahl")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: keine gültige Zahl")
    if not math.isfinite(x):
        raise ValueError(f"{name}: keine gültige Zahl")
    return x


def sort_key(t: Txn):
    # gleicher Tag: Käufe vor Verkäufen, sonst Reihenfolge der Erfassung
    return (t.date, 0 if t.type == "buy" else 1, t.created)


def validate(raw: dict, existing: list[Txn], symbol_ok, today: date | None = None) -> Txn:
    """Rohdaten aus dem Formular prüfen. Wirft ValueError mit verständlicher Meldung."""
    today = today or datetime.now(BERLIN).date()
    typ = str(raw.get("type", "")).strip().lower()
    if typ not in TYPES:
        raise ValueError("Art muss Kauf, Verkauf oder Dividende sein")
    sym = str(raw.get("symbol", "")).strip().upper()
    if not sym or len(sym) > 20 or any(c.isspace() for c in sym):
        raise ValueError("Bitte ein gültiges Symbol angeben (z. B. SAP.DE, AAPL)")
    try:
        d = date.fromisoformat(str(raw.get("date", "")).strip())
    except ValueError:
        raise ValueError("Datum fehlt oder ist ungültig")
    if d > today:
        raise ValueError("Datum liegt in der Zukunft")
    if d.year < 1990:
        raise ValueError("Datum zu weit in der Vergangenheit")
    fee = _parse_float(raw.get("fee", 0) or 0, "Gebühr")
    if fee < 0:
        raise ValueError("Gebühr darf nicht negativ sein")
    if typ == "dividend":
        qty = 0.0
        price = _parse_float(raw.get("price"), "Betrag")
        if price <= 0:
            raise ValueError("Dividendenbetrag muss größer als 0 sein")
        if fee >= price:
            raise ValueError("Steuer muss kleiner als der Bruttobetrag sein")
    else:
        qty = _parse_float(raw.get("qty"), "Stückzahl")
        price = _parse_float(raw.get("price"), "Kurs")
        if qty <= 0:
            raise ValueError("Stückzahl muss größer als 0 sein")
        if price <= 0:
            raise ValueError("Kurs muss größer als 0 sein")
        if qty * price > 1e9:
            raise ValueError("Betrag unplausibel hoch")
    isin = str(raw.get("isin", "") or "").strip().upper()
    if isin and (len(isin) != 12 or not isin[:2].isalpha() or not isin.isalnum()):
        raise ValueError("ISIN muss 12 Zeichen haben (z. B. DE0007164600) – oder leer lassen")
    if not symbol_ok(sym):
        raise ValueError(f"Für {sym} gibt es keine Kursdaten – Symbol prüfen (Yahoo-Schreibweise, z. B. SAP.DE)")
    t = Txn(id=uuid.uuid4().hex[:12], date=d.isoformat(), type=typ, symbol=sym, qty=qty, price=price, fee=fee,
            note=str(raw.get("note", "") or "")[:200], isin=isin)
    # Gesamtbestand nach Einfügen muss zu jedem Zeitpunkt ≥ 0 sein (kein Verkauf von nicht gehaltenen Stücken)
    compute_holdings(existing + [t])
    if typ == "dividend" and not any(e.symbol == sym and e.type == "buy" and e.date <= t.date for e in existing):
        raise ValueError(f"Dividende für {sym}, aber kein Kauf davor erfasst")
    return t


def compute_holdings(txns: list[Txn]) -> dict[str, Holding]:
    """Bestand nach Durchschnittsmethode. Wirft ValueError, wenn irgendwann mehr verkauft als gehalten wird."""
    hs: dict[str, Holding] = {}
    for t in sorted(txns, key=sort_key):
        h = hs.setdefault(t.symbol, Holding(t.symbol))
        h.last_txn = t.date
        if t.type == "buy":
            h.qty += t.qty
            h.cost += t.qty * t.price + t.fee
            h.fees += t.fee
            h.first_buy = h.first_buy or t.date
        elif t.type == "sell":
            if t.qty > h.qty + 1e-7:
                d = ".".join(reversed(t.date.split("-")))
                raise ValueError(f"Verkauf von {t.qty:g} {t.symbol} am {d}, aber nur {h.qty:g} im Bestand".replace(".", ",", 0))
            avg = h.avg_price
            h.realized += t.qty * t.price - t.fee - t.qty * avg
            h.cost -= t.qty * avg
            h.qty -= t.qty
            h.fees += t.fee
            if h.qty < 1e-7:  # vollständig verkauft -> ein späterer Kauf beginnt eine neue Haltedauer
                h.qty, h.cost, h.first_buy = 0.0, 0.0, ""
        elif t.type == "dividend":
            h.dividends += t.price - t.fee
    return hs


def quantities_by_day(txns: list[Txn], days: pd.DatetimeIndex) -> pd.DataFrame:
    """Stückzahl je Wert am Ende jedes Tages."""
    syms = sorted({t.symbol for t in txns if t.type != "dividend"})
    q = pd.DataFrame(0.0, index=days, columns=syms)
    for t in txns:
        if t.type == "dividend":
            continue
        sign = 1 if t.type == "buy" else -1
        q.loc[q.index >= pd.Timestamp(t.date), t.symbol] += sign * t.qty
    return q.clip(lower=0).where(q > 1e-7, 0.0)


# --------------------------------------------------------------------------- Wertentwicklung
def valuation_history(txns: list[Txn], prices: dict[str, pd.Series], end: date | None = None) -> pd.DataFrame:
    """Tageswerte ab der ersten Transaktion.

    prices: Tagesschlusskurse in € je Symbol (Index = Datum). Fehlende Tage werden mit dem letzten Kurs gefüllt.
    Rückgabe-Spalten: value (Depotwert), flow (Geld rein/raus), ret (Tagesrendite, TWR), index (TWR, Start = 1).
    """
    real = [t for t in txns]
    if not real:
        return pd.DataFrame(columns=["value", "flow", "ret", "index"])
    start = pd.Timestamp(min(t.date for t in real))
    end_ts = pd.Timestamp(end or datetime.now(BERLIN).date())
    days = pd.bdate_range(start, end_ts)
    if len(days) == 0:
        days = pd.DatetimeIndex([start])
    qty = quantities_by_day(real, days)
    px = pd.DataFrame(index=days)
    for sym in qty.columns:
        s = prices.get(sym)
        if s is None or s.empty:
            px[sym] = np.nan
            continue
        s = s.copy()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        px[sym] = s.reindex(days.union(s.index)).ffill().reindex(days)
    # Vor dem ersten bekannten Kurs: Kaufkurs als Näherung, damit der Starttag nicht mit 0 bewertet wird
    for t in sorted(real, key=sort_key):
        if t.type == "buy" and t.symbol in px:
            mask = (px.index >= pd.Timestamp(t.date)) & px[t.symbol].isna()
            px.loc[mask, t.symbol] = t.price
    px = px.ffill().fillna(0.0)
    value = (qty * px[qty.columns]).sum(axis=1)

    flow = pd.Series(0.0, index=days)
    for t in real:
        d = pd.Timestamp(t.date)
        if d < days[0] or d > days[-1]:
            continue
        # Wochenend-Buchungen dem nächsten Handelstag zuordnen
        i = min(days.searchsorted(d), len(days) - 1)
        mkt = px[t.symbol].iloc[i] if t.is_transfer and t.symbol in px else 0.0
        # Übernahme zum alten Einstand: als Einlage zum Marktwert zählen, sonst wäre der alte Gewinn „Rendite“
        flow.iloc[i] += t.qty * mkt if mkt > 0 else t.cash_out

    ret = pd.Series(np.nan, index=days)
    prev = 0.0
    for i, day in enumerate(days):
        v, f = value.iloc[i], flow.iloc[i]
        if prev > EPS:
            ret.iloc[i] = (v - f) / prev - 1
        elif v > EPS and f > EPS:
            ret.iloc[i] = v / f - 1      # erster Tag: Wert gegenüber eingezahltem Betrag (inkl. Gebühr)
        prev = v
    idx = (1 + ret.fillna(0)).cumprod()
    return pd.DataFrame({"value": value, "flow": flow, "ret": ret, "index": idx})


def position_values_by_day(txns: list[Txn], prices: dict[str, pd.Series], end: date | None = None) -> pd.DataFrame:
    """Wert je Position und Tag (Stück × Schlusskurs in €) – Spalten = Symbole, wie in valuation_history bewertet."""
    real = [t for t in txns]
    if not real:
        return pd.DataFrame()
    start = pd.Timestamp(min(t.date for t in real))
    days = pd.bdate_range(start, pd.Timestamp(end or datetime.now(BERLIN).date()))
    if len(days) == 0:
        days = pd.DatetimeIndex([start])
    qty = quantities_by_day(real, days)
    px = pd.DataFrame(index=days)
    for sym in qty.columns:
        s = prices.get(sym)
        if s is None or s.empty:
            px[sym] = np.nan
            continue
        s = s.copy()
        s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
        s = s[~s.index.duplicated(keep="last")].sort_index()
        px[sym] = s.reindex(days.union(s.index)).ffill().reindex(days)
    for t in sorted(real, key=sort_key):
        if t.type == "buy" and t.symbol in px:
            mask = (px.index >= pd.Timestamp(t.date)) & px[t.symbol].isna()
            px.loc[mask, t.symbol] = t.price
    px = px.ffill().fillna(0.0)
    return qty * px[qty.columns]


def benchmark_index(series: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """Vergleichsindex auf dieselben Tage legen, Start = 1."""
    s = series.copy()
    s.index = pd.DatetimeIndex(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s.reindex(days.union(s.index)).ffill().reindex(days).bfill()
    return s / s.iloc[0] if len(s) and s.iloc[0] > 0 else s


def monthly_returns(index: pd.Series) -> list[dict]:
    """Monatsrenditen aus einer Indexreihe (Start = 1)."""
    if index is None or len(index) < 2:
        return []
    m = index.resample("ME").last()
    # Bezug ist 1 = eingesetztes Kapital vor dem ersten Tag (Indizes starten bei 1), nicht der erste Tageswert –
    # sonst fehlt die Rendite des ersten Tages und die Monate passen nicht zur Gesamtrendite.
    out, prev = [], 1.0
    for ts, v in m.items():
        out.append({"month": ts.strftime("%Y-%m"), "ret": float(v / prev - 1) if prev else 0.0})
        prev = v
    return out


def max_drawdown(index: pd.Series) -> float:
    if index is None or len(index) < 2:
        return 0.0
    return float((index / index.cummax() - 1).min())


# --------------------------------------------------------------------------- Risiko
def risk_metrics(weights: dict[str, float], returns: pd.DataFrame, bench: pd.Series | None = None) -> dict:
    """Kennzahlen für das *heutige* Portfolio, rückgerechnet mit den Kursen des letzten Jahres.

    weights: Anteil je Symbol (Summe ≈ 1, nur Wertpapiere). returns: tägliche Renditen je Symbol in €.
    """
    syms = [s for s in weights if s in returns.columns and weights[s] > 0]
    w_sum = sum(weights[s] for s in syms)
    out = {"symbols": syms, "coverage": w_sum}
    if not syms or w_sum <= 0:
        return out
    w = np.array([weights[s] / w_sum for s in syms])
    r = returns[syms].dropna(how="all").fillna(0.0)
    if len(r) < 20:
        return out
    port = r.values @ w
    port_s = pd.Series(port, index=r.index)
    out["volatility"] = float(np.std(port, ddof=1) * math.sqrt(252))
    out["max_drawdown"] = max_drawdown((1 + port_s).cumprod())
    out["var95_1d"] = float(-np.percentile(port, 5))           # Verlust, der an 19 von 20 Tagen nicht überschritten wurde
    out["return_1y"] = float((1 + port_s).prod() - 1)
    out["eff_positions"] = float(1 / np.sum(w ** 2))
    out["largest_weight"] = float(w.max())
    out["top3_weight"] = float(np.sort(w)[::-1][:3].sum())
    corr = r.corr().fillna(0.0)
    out["corr"] = {"symbols": syms, "matrix": [[round(float(corr.loc[a, b]), 3) for b in syms] for a in syms]}
    if bench is not None and len(bench) > 20:
        b = bench.reindex(port_s.index).fillna(0.0)
        var_b = float(np.var(b, ddof=1))
        if var_b > 0:
            out["beta"] = float(np.cov(port_s, b, ddof=1)[0, 1] / var_b)
            out["corr_bench"] = float(np.corrcoef(port_s, b)[0, 1])
    return out


def allocation(values: dict[str, float], meta: dict[str, dict], key: str) -> list[dict]:
    """Verteilung nach Position (key='symbol') oder nach sector/country/currency aus den Metadaten."""
    total = sum(v for v in values.values() if v > 0)
    groups: dict[str, float] = {}
    for sym, v in values.items():
        if v <= 0:
            continue
        g = sym if key == "symbol" else (meta.get(sym, {}).get(key) or "Unbekannt")
        groups[g] = groups.get(g, 0.0) + v
    return sorted(({"group": g, "value": v, "weight": v / total if total else 0.0} for g, v in groups.items()),
                  key=lambda x: -x["value"])


# --------------------------------------------------------------------------- Alarme
def check_alerts(positions: list[dict], cfg_alerts: dict, sent: dict, now: datetime) -> list[dict]:
    """Welche Alarme sind fällig? Jeder Alarm nur einmal (Schlüssel in `sent`).

    positions: je Position {symbol, name, qty, avg_price, price, prev_close, high_since_buy}.
    cfg_alerts: {stop_loss_pct, trailing_pct, move_pct}
    """
    day = now.astimezone(BERLIN).date().isoformat()
    out = []
    for p in positions:
        if p["qty"] <= 0 or not p.get("price"):
            continue
        sym, price = p["symbol"], p["price"]
        stop = p["avg_price"] * (1 - cfg_alerts["stop_loss_pct"])
        trail = (p.get("high_since_buy") or 0) * (1 - cfg_alerts["trailing_pct"])
        level = max(stop, trail)
        if level > 0 and price <= level:
            key = f"stop:{sym}:{round(level, 2)}"
            if key not in sent:
                kind = "Trailing-Stop" if trail >= stop else "Stop-Loss"
                out.append({"key": key, "symbol": sym, "kind": "stop",
                            "title": f"⚠️ {p['name']}: {kind} erreicht",
                            "message": f"{p['name']} steht bei {price:.2f} € – unter deiner {kind}-Marke von "
                                       f"{level:.2f} € (Einstand {p['avg_price']:.2f} €)."})
        prev = p.get("prev_close")
        if prev:
            move = price / prev - 1
            if abs(move) >= cfg_alerts["move_pct"]:
                key = f"move:{sym}:{day}:{'up' if move > 0 else 'down'}"
                if key not in sent:
                    arrow = "📈" if move > 0 else "📉"
                    out.append({"key": key, "symbol": sym, "kind": "move",
                                "title": f"{arrow} {p['name']} {move:+.1%} heute",
                                "message": f"{p['name']} {move:+.1%} gegenüber gestern ({price:.2f} €). "
                                           f"Dein Bestand: {p['qty']:g} Stück, Wert {p['qty'] * price:,.0f} €."})
    return out


# --------------------------------------------------------------------------- Speicher
def _atomic_write(path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".bak"))  # letzte Version als Sicherung
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def _file(book: str):
    if book == "model":
        return MODEL_TXN_FILE
    if book == "real":
        return TXN_FILE
    raise ValueError(f"unbekanntes Depot: {book}")


def load_txns(book: str = "real") -> list[Txn]:
    f = _file(book)
    for p in (f, f.with_suffix(".bak")):
        try:
            return [Txn(**d) for d in json.loads(p.read_text())]
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, TypeError):
            continue  # beschädigt -> Sicherung versuchen
    return []


def save_txns(txns: list[Txn], book: str = "real") -> None:
    compute_holdings(txns)  # nie einen ungültigen Stand speichern
    _atomic_write(_file(book), [asdict(t) for t in sorted(txns, key=sort_key)])


def add_txn(raw: dict, symbol_ok, book: str = "real") -> Txn:
    with _lock:
        txns = load_txns(book)
        t = validate(raw, txns, symbol_ok)
        save_txns(txns + [t], book)
        return t


def delete_txn(txn_id: str, book: str = "real") -> None:
    with _lock:
        txns = load_txns(book)
        rest = [t for t in txns if t.id != txn_id]
        if len(rest) == len(txns):
            raise ValueError("Transaktion nicht gefunden")
        try:
            compute_holdings(rest)
        except ValueError as e:
            raise ValueError(f"Löschen nicht möglich: {e}")
        save_txns(rest, book)


def update_txn(txn_id: str, raw: dict, symbol_ok, book: str = "real") -> Txn:
    """Buchung korrigieren (z. B. dein echter Kurs statt des empfohlenen). Prüft den ganzen Verlauf erneut."""
    with _lock:
        txns = load_txns(book)
        old = next((t for t in txns if t.id == txn_id), None)
        if old is None:
            raise ValueError("Buchung nicht gefunden")
        rest = [t for t in txns if t.id != txn_id]
        merged = {**asdict(old), **{k: v for k, v in raw.items() if k in ("date", "qty", "price", "fee", "note") and v not in (None, "")}}
        t = validate(merged, rest, symbol_ok)
        t.id, t.created = old.id, old.created
        save_txns(rest + [t], book)
        return t


def load_alert_state() -> dict:
    try:
        return json.loads(ALERTS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": {}, "log": []}


def save_alert_state(state: dict) -> None:
    # alte Schlüssel (älter als 30 Tage) entfernen, Log begrenzen
    cutoff = (datetime.now(BERLIN).date().toordinal() - 30)
    state["sent"] = {k: v for k, v in state["sent"].items() if date.fromisoformat(v[:10]).toordinal() >= cutoff}
    state["log"] = state["log"][-100:]
    _atomic_write(ALERTS_FILE, state)
