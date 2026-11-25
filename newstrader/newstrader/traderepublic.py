"""Trade Republic: Anmeldung wie im Browser (Bestätigung in der TR-App), Abruf der Käufe, Verkäufe und Dividenden,
Abgleich mit dem Portfolio.

Nur lesend: Es werden ausschließlich Timeline, Buchungsdetails und Cash abgefragt – niemals Orders.
Die PIN wird nicht gespeichert, nur die Sitzungs-Cookies (in /data, nur für das Add-on lesbar).
Grundlage ist das inoffizielle Projekt „pytr“; ändert Trade Republic etwas, kann der Abruf ausfallen.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from . import manager
from . import portfolio as pf
from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
TR_DIR = STATE_DIR / "tr"
COOKIES = TR_DIR / "cookies.txt"
SYNC_FILE = TR_DIR / "sync.json"
SYNC_EVERY_MIN = 30          # Mo–Fr tagsüber
SYNC_EVERY_MIN_QUIET = 180   # nachts und am Wochenende
MATCH_BEFORE = timedelta(days=1)    # Trade darf so viel vor der Empfehlung liegen …
MATCH_AFTER = timedelta(days=14)    # … und so viel danach

_login: dict = {"status": "idle", "msg": "", "api": None, "phone": ""}
_AVG: dict[str, float] = {}   # Einstand je ISIN laut Trade Republic (letzter Abruf)
CRYPTO_ISINS = {"XF000BTC0017": "BTC-EUR", "XF000ETH0019": "ETH-EUR", "XF000SOL0012": "SOL-EUR", "XF000XRP0018": "XRP-EUR"}
MIN_IMPORT_EUR = 1.0          # kleinere Reste (z. B. Saveback-Bruchteile) nicht übernehmen
_sync = {"running": False}
_api_lock = threading.Lock()        # pytr teilt Zustand zwischen Instanzen – nie zwei Abfragen gleichzeitig
_ko_cache: dict[tuple[str, str], tuple[float, list]] = {}
_sync_lock = threading.Lock()


# --------------------------------------------------------------------------- Zustand
def _load() -> dict:
    try:
        d = json.loads(SYNC_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("connected", False)
    d.setdefault("done", {})        # TR-Ereignis-ID -> Buchungs-ID (nie doppelt buchen)
    d.setdefault("symbols", {})     # ISIN -> Symbol (Yahoo)
    return d


def _save(d: dict) -> None:
    TR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SYNC_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(SYNC_FILE)


def _mask(phone: str) -> str:
    return phone[:5] + "•" * max(0, len(phone) - 7) + phone[-2:] if len(phone) > 7 else "•••"


def status() -> dict:
    d = _load()
    return {"connected": d["connected"], "phone": d.get("phone", ""), "expired": d.get("expired", False),
            "login": {"status": _login["status"], "msg": _login["msg"]},
            "running": _sync["running"], "last": d.get("last"), "last_ok": d.get("last_ok"),
            "error": d.get("error"), "report": d.get("report"), "cash": d.get("cash"),
            "changes": d.get("changes"), "compare": d.get("compare"),
            "synced": len(d["done"])}


# --------------------------------------------------------------------------- Anmeldung
def _api(phone: str = "+490000000000", pin: str = "0000"):
    from pytr.api import TradeRepublicApi  # erst hier laden – fehlt pytr, läuft der Rest weiter
    TR_DIR.mkdir(parents=True, exist_ok=True)
    return TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=True, cookies_file=str(COOKIES),
                            credentials_file=str(TR_DIR / "keine-zugangsdaten"), use_v2_login=True)


def _clean_phone(raw: str) -> str:
    p = re.sub(r"[\s/()-]", "", str(raw or ""))
    if p.startswith("00"):
        p = "+" + p[2:]
    elif p.startswith("0"):
        p = "+49" + p[1:]
    if not re.fullmatch(r"\+\d{8,15}", p):
        raise ValueError("Telefonnummer bitte wie in Trade Republic, z. B. +49 171 1234567")
    return p


def _friendly(e: Exception) -> str:
    s = str(e)
    if "TOO_MANY" in s or "Too many" in s:
        return "Zu viele Versuche – bitte ein paar Minuten warten"
    if "not confirmed in time" in s or isinstance(e, TimeoutError):
        return "Nicht rechtzeitig in der App bestätigt – bitte noch einmal"
    if "401" in s or "403" in s or "Unauthorized" in s or "PIN" in s.upper():
        return "Anmeldung abgelehnt – Telefonnummer und PIN prüfen"
    if "authenticator" in s.lower():
        return "Der Code stimmt nicht"
    return f"Anmeldung fehlgeschlagen ({s[:120]})"


def start_login(cfg: Config, notify, phone: str, pin: str) -> dict:
    """Schritt 1: Telefonnummer + PIN an Trade Republic. Danach Bestätigung in der App (oder Code)."""
    phone = _clean_phone(phone)
    pin = str(pin or "").strip()
    if not re.fullmatch(r"\d{4}", pin):
        raise ValueError("Die PIN hat 4 Ziffern")
    if _login["status"] in ("starting", "waiting", "code"):
        raise ValueError("Eine Anmeldung läuft bereits – bitte erst abschließen oder abbrechen")
    _login.update(status="starting", msg="Verbinde mit Trade Republic …", api=None, phone=phone)

    def run(pin=pin):
        try:
            api = _api(phone, pin)
            api.initiate_weblogin()
            api.pin = None  # PIN nicht länger im Speicher halten als nötig
            pin = None
            if api.weblogin_needs_authenticator:
                _login.update(status="code", msg="Bitte den Code aus deiner Authenticator-App eingeben", api=api)
                return
            _login.update(status="waiting", msg="Bitte bestätige die Anmeldung jetzt in deiner Trade-Republic-App", api=api)
            api.complete_weblogin()
            _finish(cfg, notify, api)
        except Exception as e:  # noqa: BLE001 – alles landet als Meldung in der Oberfläche
            log.warning("Trade-Republic-Anmeldung fehlgeschlagen: %s", type(e).__name__)
            _login.update(status="error", msg=_friendly(e), api=None)

    threading.Thread(target=run, daemon=True, name="tr-login").start()
    return status()


def submit_code(cfg: Config, notify, code: str) -> dict:
    code = str(code or "").strip()
    if _login["status"] != "code" or _login["api"] is None:
        raise ValueError("Es wartet keine Anmeldung auf einen Code")
    if not re.fullmatch(r"\d{4,8}", code):
        raise ValueError("Bitte den Zahlencode eingeben")
    api = _login["api"]
    _login.update(status="starting", msg="Prüfe Code …")

    def run():
        try:
            api.complete_weblogin(code)
            _finish(cfg, notify, api)
        except Exception as e:  # noqa: BLE001
            _login.update(status="error", msg=_friendly(e), api=None)

    threading.Thread(target=run, daemon=True, name="tr-code").start()
    return status()


def cancel_login() -> dict:
    _login.update(status="idle", msg="", api=None)
    return status()


def _finish(cfg: Config, notify, api) -> None:
    try:
        os.chmod(COOKIES, 0o600)
    except OSError:
        pass
    _backup()
    d = _load()
    d.update(connected=True, expired=False, phone=_mask(_login.get("phone", "")), error=None,
             connected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _save(d)
    _login.update(status="connected", msg="Verbunden ✓ – erster Abgleich läuft", api=None, phone="")
    log.info("Trade Republic verbunden")
    threading.Thread(target=lambda: sync(cfg, notify, quiet=False), daemon=True, name="tr-sync").start()


def _backup() -> None:
    """Vor dem ersten Abgleich nach dem Verbinden: Portfolio sichern (falls etwas schiefgeht)."""
    import shutil
    dst = TR_DIR / f"sicherung-{datetime.now(pf.BERLIN).strftime('%Y%m%d-%H%M%S')}"
    dst.mkdir(parents=True, exist_ok=True)
    for f in (manager.STATE_FILE, pf.MODEL_TXN_FILE):
        if f.exists():
            shutil.copy2(f, dst / f.name)


def logout() -> dict:
    try:
        COOKIES.unlink()
    except FileNotFoundError:
        pass
    d = _load()
    d.update(connected=False, expired=False, phone="")  # "done" bleibt: nach erneutem Verbinden nichts doppelt
    _save(d)
    _login.update(status="idle", msg="", api=None)
    return status()


# --------------------------------------------------------------------------- Abruf
def _ts(raw: str) -> datetime:
    s = raw.replace("Z", "+0000")
    if re.search(r"[+-]\d{4}$", s):
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def _wanted(e: dict) -> bool:
    """Nur Ereignisse, die Käufe, Verkäufe oder Dividenden sein können (keine Kartenzahlungen usw.)."""
    from pytr.event import ConditionalEventType, PPEventType, tr_event_type_mapping
    typ = (e.get("eventType") or "").upper()
    if typ not in tr_event_type_mapping:
        return not typ.startswith("CARD_")
    return tr_event_type_mapping[typ] in (ConditionalEventType.TRADE_INVOICE, PPEventType.BUY, PPEventType.SELL,
                                          PPEventType.DIVIDEND)


async def _fetch(api, since: datetime, skip: set[str]) -> tuple[list[dict], float | None, dict | None]:
    async def one(fut, timeout=20.0):
        sid = await fut
        try:
            return await asyncio.wait_for(api._recv_subscription(sid), timeout)
        finally:
            await api.unsubscribe(sid)

    try:
        items, after = [], None
        for _ in range(40):  # höchstens 40 Seiten
            resp = await one(api.timeline_transactions(after))
            stop = False
            for e in resp.get("items", []):
                if _ts(e["timestamp"]) < since:
                    stop = True
                    break
                items.append(e)
            after = (resp.get("cursors") or {}).get("after")
            if stop or not after:
                break
        out = []
        for e in items:
            if e["id"] in skip or not _wanted(e):
                continue
            try:
                e["details"] = await one(api.timeline_detail_v2(e["id"]))
            except Exception as ex:  # noqa: BLE001
                log.warning("TR-Details zu %s nicht abrufbar: %s", e.get("title"), ex)
                continue
            out.append(e)
        cash = None
        try:
            c = await one(api.cash())
            cash = round(sum(float(x.get("amount", 0)) for x in c if x.get("currencyId", "EUR") == "EUR"), 2)
        except Exception:  # noqa: BLE001
            pass
        holdings = None
        try:
            r = await one(api.compact_portfolio())
            holdings = {}
            for cat in r.get("categories", []):
                for pos in cat.get("positions", []):
                    isin = pos.get("isin") or pos.get("instrumentId")
                    if isin:
                        holdings[isin] = holdings.get(isin, 0.0) + float(pos.get("netSize", 0) or 0)
                        if pos.get("averageBuyIn"):
                            _AVG[isin] = float(pos["averageBuyIn"])
        except Exception as ex:  # noqa: BLE001
            log.warning("TR-Bestand nicht abrufbar: %s", ex)
        return out, cash, holdings
    finally:
        await api.close()


def parse_event(e: dict) -> dict | None:
    """TR-Ereignis -> {id, time, side (buy|sell|dividend), isin, name, qty, price, fee}. None = nicht relevant."""
    from pytr.event import ConditionalEventType, Event, PPEventType
    ev = Event.from_dict(e)
    t = ev.event_type
    base = {"id": e["id"], "time": ev.date.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "isin": ev.isin, "name": e.get("title") or ev.title}
    if t in (ConditionalEventType.TRADE_INVOICE, PPEventType.BUY, PPEventType.SELL):
        if not ev.isin or not ev.shares or ev.value is None:
            return {**base, "side": "unknown"}
        val, fee, tax, qty = abs(ev.value), abs(ev.fees or 0), abs(ev.taxes or 0), abs(ev.shares)
        buy = t == PPEventType.BUY or (t != PPEventType.SELL and ev.value < 0)
        price = (val - fee) / qty if buy else (val + fee + tax) / qty
        return {**base, "side": "buy" if buy else "sell", "qty": qty, "price": round(price, 4), "fee": round(fee, 2)}
    if t == PPEventType.DIVIDEND and ev.isin and ev.value:
        tax = abs(ev.taxes or 0)
        return {**base, "side": "dividend", "qty": 0.0, "price": round(abs(ev.value) + tax, 2), "fee": round(tax, 2)}
    return None


# --------------------------------------------------------------------------- ISIN -> Symbol
def resolve_symbol(cfg: Config, isin: str, known: dict, prefer: set[str], symbol_ok) -> str | None:
    if isin in known:
        return known[isin]
    if isin in CRYPTO_ISINS:
        return CRYPTO_ISINS[isin]
    cands = [s for s, i in cfg.isins.items() if i == isin]
    for s in sorted(cands, key=lambda s: (s not in prefer, s)):
        return s
    try:
        import yfinance as yf
        quotes = yf.Search(isin, max_results=8).quotes
    except Exception as e:  # noqa: BLE001
        log.warning("Symbolsuche für %s fehlgeschlagen: %s", isin, e)
        return None
    syms = [q["symbol"] for q in quotes if q.get("symbol") and q.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND")]
    order = {s: i for i, s in enumerate(syms)}
    syms.sort(key=lambda s: (not (isin.startswith("DE") and s.endswith(".DE")), order[s]))
    names = {q["symbol"]: (q.get("longname") or q.get("shortname") or "").strip() for q in quotes if q.get("symbol")}
    for s in syms:
        if symbol_ok(s):
            if names.get(s):
                cfg.names.setdefault(s, names[s].title() if names[s].isupper() else names[s])
            return s
    return None


# --------------------------------------------------------------------------- Abgleich
def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(pf.BERLIN).date().isoformat()


def reconcile(cfg: Config, trades: list[dict], done: dict, symbol_ok, now: datetime | None = None) -> dict:
    """TR-Trades ins Portfolio übernehmen.

    1. Passt ein Trade zu einer gebuchten Empfehlung (gleicher Wert, gleiche Richtung, zeitlich nah), werden Kurs,
       Stück, Gebühr und Datum exakt aus Trade Republic übernommen und die Empfehlung abgehakt.
    2. Sonst ist es dein eigener Trade und wird als „eigene“ Buchung angelegt.
    Gibt einen Bericht zurück; `done` (TR-ID -> Buchungs-ID) wird ergänzt.
    """
    now = now or datetime.now(timezone.utc)
    rep = {"matched": [], "corrected": [], "new": [], "dividends": [], "problems": [], "open": [],
           "imported": [], "deposits": []}
    with manager._lock:
        state = manager.load_state()
        created = datetime.fromisoformat(state["created"])
        acts = state.setdefault("actions", [])
        for tr in sorted(trades, key=lambda t: t["time"]):
            if tr["id"] in done:
                continue
            when = datetime.fromisoformat(tr["time"])
            if when < created - timedelta(minutes=10):
                done[tr["id"]] = None  # vor dem Portfolio-Start: gehört nicht dazu
                continue
            sym, name = tr.get("symbol"), tr.get("name") or tr.get("isin")
            if tr["side"] == "unknown" or not sym:
                rep["problems"].append(f"{name}: {'nicht lesbar' if tr['side'] == 'unknown' else 'kein Kurssymbol zur ISIN ' + str(tr.get('isin'))}")
                continue
            if tr["side"] in ("buy", "sell") and not tr.get("price"):
                if tr["side"] == "buy":   # Gratisaktien, Spin-off o. Ä. ohne Kaufpreis: nicht als Kauf buchen
                    done[tr["id"]] = None
                    continue
                tr = {**tr, "price": 0.0001}  # „wertlos“ ausgebucht: Verkauf praktisch zu 0
            raw = {"date": _day(tr["time"]), "qty": tr["qty"], "price": tr["price"], "fee": tr["fee"]}
            if tr["side"] == "dividend":
                try:
                    t = pf.validate({**raw, "type": "dividend", "symbol": sym, "isin": tr["isin"], "note": "Trade Republic"},
                                    pf.load_txns("model"), symbol_ok)
                except ValueError as e:
                    rep["problems"].append(f"Dividende {name}: {e}")
                    continue
                pf.save_txns(pf.load_txns("model") + [t], "model")
                a = manager._action("manual", "sell", sym, cfg.names.get(sym, name), 0, t.price, "Dividende (Trade Republic)", when)
                a.update(txn_id=t.id, status="eigene", side="dividend", tr_id=tr["id"])
                acts.append(a)
                done[tr["id"]] = t.id
                rep["dividends"].append(cfg.names.get(sym, name))
                continue
            cands = [a for a in acts if a.get("txn_id") and not a.get("tr_id") and a["symbol"] == sym
                     and a["side"] == tr["side"] and a["status"] in ("angenommen", "bestätigt", "angepasst", "eigene")
                     and -MATCH_BEFORE <= when - datetime.fromisoformat(a["time"]) <= MATCH_AFTER
                     # Stückzahl muss ungefähr passen – Saveback-/Sparplan-Bruchteile haken keine Empfehlung ab
                     and 0.5 <= tr["qty"] / max(a["qty"], 1e-9) <= 2.0]
            if cands:
                a = min(cands, key=lambda a: (a["status"] != "angenommen", abs(a["qty"] - tr["qty"]),
                                              abs((when - datetime.fromisoformat(a["time"])).total_seconds())))
                try:
                    t = pf.update_txn(a["txn_id"], raw, symbol_ok, "model")
                except ValueError as e:
                    rep["problems"].append(f"{a['name']}: {e}")
                    continue
                same = abs(a["qty"] - t.qty) < 1e-6 and abs(t.price / a["price"] - 1) < 0.003
                was = a["status"]
                if was == "angenommen":
                    a["status"] = "bestätigt" if same else "angepasst"
                elif not same and was == "bestätigt":
                    a["status"] = "angepasst"
                a.update(qty=t.qty, price=t.price, value=round(t.qty * t.price, 2), tr_id=tr["id"])
                done[tr["id"]] = t.id
                (rep["matched"] if was == "angenommen" else rep["corrected"]).append(a["name"])
                continue
            # eigener Trade
            txns = pf.load_txns("model")
            try:
                t = pf.validate({**raw, "type": tr["side"], "symbol": sym, "isin": tr["isin"], "note": "Trade Republic"},
                                txns, symbol_ok)
            except ValueError as e:
                # TR ist die Vorgabe: Passt der Trade nicht ins Portfolio (z. B. mehr verkauft als gebucht), ersetzt er
                # einen noch nicht verknüpften Vorschlag zum selben Wert – mit Stück, Kurs und Datum von TR.
                absorb = sorted((a for a in acts if a.get("txn_id") and not a.get("tr_id") and a["symbol"] == sym
                                 and a["side"] == tr["side"] and a["status"] in ("angenommen", "bestätigt", "angepasst")
                                 and -MATCH_BEFORE <= when - datetime.fromisoformat(a["time"]) <= MATCH_AFTER),
                                key=lambda a: abs((when - datetime.fromisoformat(a["time"])).total_seconds()))
                for a in absorb:
                    try:
                        t = pf.update_txn(a["txn_id"], raw, symbol_ok, "model")
                    except ValueError:
                        continue
                    a.update(status="angepasst", qty=t.qty, price=t.price, value=round(t.qty * t.price, 2), tr_id=tr["id"])
                    done[tr["id"]] = t.id
                    rep["corrected"].append(f"{a['name']} (wie bei Trade Republic: {t.qty:g} Stk)")
                    break
                else:
                    rep["problems"].append(f"{name}: {e}")
                continue
            short = -manager.cash_of(state["start_capital"], txns + [t]) if t.type == "buy" else 0
            if short > 0.005:  # Geld war bei TR offensichtlich da -> als Einzahlung nachbuchen
                _flow(state, short, f"Einzahlung bei Trade Republic (für Kauf {name})")
                rep["deposits"].append(round(short, 2))
            pf.save_txns(txns + [t], "model")
            if tr.get("isin"):
                cfg.isins.setdefault(sym, tr["isin"])
            cfg.names.setdefault(sym, name)
            a = manager._action("manual", t.type, sym, cfg.names.get(sym, name), t.qty, t.price,
                                "Eigener Trade (aus Trade Republic übernommen)", when)
            a.update(txn_id=t.id, status="eigene", tr_id=tr["id"])
            acts.append(a)
            done[tr["id"]] = t.id
            rep["new"].append(f"{'Kauf' if t.type == 'buy' else 'Verkauf'} {a['name']}")
        rep["open"] = [a["name"] for a in acts if a["status"] == "angenommen"
                       and now - datetime.fromisoformat(a["time"]) > timedelta(hours=20)]
        manager.save_state(state)
    return rep


def _flow(state: dict, amount: float, note: str, market: float | None = None) -> None:
    """Ein-/Auszahlung im Portfolio (verändert das eingesetzte Kapital, nicht die Rendite).
    market: Marktwert bei Übernahmen zum alten Einstand – zählt für die Wertveränderung statt des Betrags."""
    state["start_capital"] = round(state["start_capital"] + amount, 2)
    f = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "amount": round(amount, 2), "note": note}
    if market is not None and abs(market - amount) > 0.005:
        f["market"] = round(market, 2)
    state.setdefault("flows", []).append(f)


def import_positions(cfg: Config, tr_holdings: dict[str, float], symbols: dict[str, str], skip: set[str],
                     symbol_ok, price_of, rep: dict, avg: dict[str, float] | None = None) -> None:
    """Alles, was bei Trade Republic mehr im Depot liegt als im Portfolio, übernehmen – mit dem Einstandskurs von TR.

    Gebucht als Einlage + Kauf zum Durchschnitts-Einstand laut TR (ohne zusätzliche Gebühr): Einstand, Gewinn und
    Verlust entsprechen dann der TR-App. Fehlt der Einstand, wird der aktuelle Kurs genommen.
    """
    avg = _AVG if avg is None else avg
    today = datetime.now(pf.BERLIN).date().isoformat()
    with manager._lock:
        state = manager.load_state()
        txns = pf.load_txns("model")
        book = {s_: h.qty for s_, h in pf.compute_holdings(txns).items() if h.qty > 1e-9}
        pending: dict[str, float] = {}
        for a in state.get("actions", []):
            if a["status"] == "angenommen" and a.get("txn_id") and a["side"] in ("buy", "sell"):
                pending[a["symbol"]] = pending.get(a["symbol"], 0.0) + (a["qty"] if a["side"] == "buy" else -a["qty"])
        isin_of = {s_: i for i, s_ in symbols.items()}
        for t in txns:
            if t.isin:
                isin_of.setdefault(t.symbol, t.isin)
        for s_, i in cfg.isins.items():
            isin_of.setdefault(s_, i)
        changed = False
        for isin, trq in sorted(tr_holdings.items()):
            if isin in skip or trq <= 1e-9:
                continue
            syms = [s_ for s_ in set(book) | set(pending) if isin_of.get(s_) == isin]
            have = sum(book.get(s_, 0.0) - pending.get(s_, 0.0) for s_ in syms)
            diff = round(trq - have, 6)
            if diff <= 1e-4:
                continue
            try:
                sym = syms[0] if syms else resolve_symbol(cfg, isin, symbols, set(), symbol_ok)
            except Exception as e:  # noqa: BLE001 – ein Problemwert darf den Abgleich nicht stoppen
                log.warning("Symbol zu %s: %s", isin, e)
                sym = None
            if not sym:
                rep["problems"].append(f"{isin}: kein Kurssymbol gefunden – kann nicht übernommen werden")
                continue
            symbols[isin] = sym
            name = cfg.names.get(sym) or sym
            buy_in = avg.get(isin)
            try:
                market = price_of(sym)
            except Exception:  # noqa: BLE001
                market = None
            # nur übernehmen, was einen aktuellen Kurs hat und mindestens 1 € wert ist (TR meldet z. B. Bruchteile
            # nach Kapitalmaßnahmen, die die App gar nicht mehr anzeigt)
            if not market or market <= 0 or diff * market < MIN_IMPORT_EUR:
                continue
            price = buy_in or market
            note = pf.TRANSFER_NOTE + (" zum Einstandskurs" if buy_in else " zum aktuellen Kurs (kein Einstand von TR)")
            try:
                t = pf.validate({"type": "buy", "symbol": sym, "date": today, "qty": diff, "price": round(price, 4),
                                 "fee": 0, "isin": isin, "note": note}, txns, symbol_ok)
            except ValueError as e:
                rep["problems"].append(f"{name}: {e}")
                continue
            txns.append(t)
            book[sym] = book.get(sym, 0.0) + diff
            isin_of.setdefault(sym, isin)
            cfg.isins.setdefault(sym, isin)
            now_px = market
            _flow(state, diff * t.price, f"Übernahme {name} aus Trade Republic", market=diff * now_px if now_px else None)
            a = manager._action("manual", "buy", sym, name, diff, t.price, note, datetime.now(timezone.utc))
            a.update(txn_id=t.id, status="eigene", tr_id=f"bestand-{isin}-{today}")
            state.setdefault("actions", []).append(a)
            rep["imported"].append(f"{name} {str(round(diff, 6)).rstrip('0').rstrip('.').replace('.', ',')} Stk")
            changed = True
        if changed:
            pf.save_txns(txns, "model")
            manager.save_state(state)


def drop_worthless_imports(cfg: Config, price_of, rep: dict) -> None:
    """Früher übernommene Kleinstreste ohne aktuellen Kurs wieder entfernen (samt Einlage)."""
    with manager._lock:
        state = manager.load_state()
        txns = {t.id: t for t in pf.load_txns("model")}
        changed = False
        for a in state.get("actions", []):
            if not str(a.get("tr_id", "")).startswith("bestand-") or a.get("status") != "eigene":
                continue
            t = txns.get(a.get("txn_id"))
            if not t:
                continue
            try:
                market = price_of(t.symbol)
            except Exception:  # noqa: BLE001
                market = None
            if market and t.qty * market >= MIN_IMPORT_EUR:
                continue
            try:
                pf.delete_txn(t.id, "model")
            except ValueError as e:
                log.warning("Kleinstrest %s nicht entfernbar: %s", t.symbol, e)
                continue
            amount = round(t.qty * t.price, 2)
            flows = state.setdefault("flows", [])
            for i, f in enumerate(flows):
                if f.get("note", "").startswith("Übernahme") and abs(f["amount"] - amount) < 0.01:
                    del flows[i]
                    break
            state["start_capital"] = round(state["start_capital"] - amount, 2)
            a.update(status="abgelehnt", reason=a.get("reason", "") + " – entfernt: kein Kurs / Kleinstrest")
            rep.setdefault("removed", []).append(a["name"])
            changed = True
        if changed:
            manager.save_state(state)


def align_to_tr(cfg: Config, tr_holdings: dict[str, float], tr_cash: float | None, symbols: dict[str, str],
                unsettled: set[str], price_of, rep: dict) -> None:
    """Trade Republic ist die Vorgabe: Liegt im Portfolio mehr als bei TR, wird der Rest ausgebucht; weicht das Cash ab,
    wird es per Ein-/Auszahlung angeglichen (ändert das eingesetzte Kapital, nicht die Rendite). Nur wenn bei TR keine
    Buchung mehr unterwegs ist und keine Empfehlung auf Umsetzung wartet (die sind im Portfolio vorab gebucht)."""
    cmp = compare_holdings(cfg, tr_holdings, symbols)
    with manager._lock:
        state = manager.load_state()
        acts = state.setdefault("actions", [])
        waiting = any(a["status"] == "angenommen" and a.get("txn_id") and a["side"] in ("buy", "sell") for a in acts)
        now = datetime.now(timezone.utc)
        changed = False
        for r in cmp["rows"]:
            if r["ok"] or r["pending"] or r["tr"] is None or r["isin"] in unsettled or r["book"] <= r["tr"] + 1e-4:
                continue
            qty = round(r["book"] - r["tr"], 6)
            txns = pf.load_txns("model")
            px = price_of(r["symbol"]) or next((t.price for t in sorted(txns, key=pf.sort_key, reverse=True)
                                                if t.symbol == r["symbol"] and t.price > 0), None)
            if not px:
                rep["problems"].append(f"{r['name']}: kein Kurs für den Abgleich")
                continue
            try:
                t = pf.validate({"date": now.astimezone(pf.BERLIN).date().isoformat(), "type": "sell", "symbol": r["symbol"],
                                 "qty": qty, "price": px, "fee": 0, "isin": r["isin"],
                                 "note": "Abgleich mit Trade Republic"}, txns, lambda s_: True)
            except ValueError as e:
                rep["problems"].append(f"{r['name']}: {e}")
                continue
            pf.save_txns(txns + [t], "model")
            a = manager._action("manual", "sell", r["symbol"], r["name"], qty, px,
                                f"Abgleich mit Trade Republic: dort {r['tr']:g} Stk, im Portfolio waren {r['book']:g}", now)
            a.update(txn_id=t.id, status="eigene")
            acts.append(a)
            rep.setdefault("aligned", []).append(f"{r['name']} −{qty:g} Stk")
            changed = True
        if tr_cash is not None and not unsettled and not waiting:
            diff = round(tr_cash - manager.cash_of(state["start_capital"], pf.load_txns("model")), 2)
            if abs(diff) >= 1.0:
                _flow(state, diff, "Cash-Abgleich mit Trade Republic")
                rep["cash_aligned"] = diff
                changed = True
        if changed:
            manager.save_state(state)


def compare_holdings(cfg: Config, tr_holdings: dict[str, float], symbols: dict[str, str]) -> dict:
    """Stückzahlen im Portfolio gegen Trade Republic. Positionen nur bei TR sind evtl. Altbestand außerhalb des Portfolios."""
    txns = pf.load_txns("model")
    book = {s: h.qty for s, h in pf.compute_holdings(txns).items() if h.qty > 1e-9}
    isin_of = {s: i for i, s in symbols.items()}
    for t in txns:
        if t.isin:
            isin_of.setdefault(t.symbol, t.isin)
    for s, i in cfg.isins.items():
        isin_of.setdefault(s, i)
    state = manager.load_state()
    names = {a["symbol"]: a["name"] for a in state.get("actions", [])}
    # offene Empfehlungen sind im Portfolio schon gebucht, bei TR aber evtl. noch nicht ausgeführt
    pending: dict[str, float] = {}
    for a in state.get("actions", []):
        if a["status"] == "angenommen" and a.get("txn_id") and a["side"] in ("buy", "sell"):
            pending[a["symbol"]] = pending.get(a["symbol"], 0.0) + (a["qty"] if a["side"] == "buy" else -a["qty"])
    rows, seen = [], set()
    for sym in sorted(set(book) | set(pending)):
        qty = book.get(sym, 0.0)
        isin = isin_of.get(sym)
        trq = tr_holdings.get(isin, 0.0) if isin else None
        seen.add(isin)
        ok = trq is not None and abs(trq - qty) < 1e-4
        waiting = not ok and trq is not None and sym in pending and abs(trq - (qty - pending[sym])) < 1e-4
        if qty < 1e-9 and (trq or 0) < 1e-9:
            continue
        rows.append({"symbol": sym, "name": names.get(sym) or cfg.names.get(sym, sym), "isin": isin,
                     "book": round(qty, 6), "tr": None if trq is None else round(trq, 6), "ok": ok, "pending": waiting})
    only_tr = [{"isin": i, "symbol": symbols.get(i), "name": cfg.names.get(symbols.get(i, ""), i), "tr": round(q, 6)}
               for i, q in tr_holdings.items() if i not in seen and q >= 0.01]
    # Für „nicht gekauft“: die eine noch nicht mit TR verknüpfte Buchung, die den ganzen Bestand ausmacht
    for r in rows:
        if not r["ok"] and not r["pending"] and r["tr"] == 0:
            cand = [a for a in state.get("actions", []) if a["symbol"] == r["symbol"] and a.get("txn_id")
                    and not a.get("tr_id") and a["side"] == "buy" and a["status"] != "abgelehnt"]
            if len(cand) == 1 and abs(cand[0]["qty"] - r["book"]) < 1e-6:
                r["action_id"] = cand[0]["id"]
    return {"rows": rows, "only_tr": only_tr,
            "diff": sum(1 for r in rows if not r["ok"] and not r["pending"]), "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def _summary(rep: dict) -> str:
    parts = []
    if rep["matched"]:
        parts.append(f"{len(rep['matched'])} Empfehlung(en) abgehakt: {', '.join(rep['matched'][:5])}")
    if rep["corrected"]:
        parts.append(f"{len(rep['corrected'])} Buchung(en) auf Cent genau korrigiert")
    if rep["new"]:
        parts.append(f"{len(rep['new'])} eigene(r) Trade(s) übernommen: {', '.join(rep['new'][:5])}")
    if rep["dividends"]:
        parts.append(f"Dividende: {', '.join(rep['dividends'][:5])}")
    if rep.get("imported"):
        parts.append(f"{len(rep['imported'])} Position(en) aus dem Depot übernommen: {', '.join(rep['imported'][:5])}")
    if rep.get("removed"):
        parts.append(f"Kleinstreste ohne Kurs entfernt: {', '.join(rep['removed'])}")
    if rep.get("deposits"):
        parts.append(f"Einzahlung nachgebucht: {sum(rep['deposits']):.2f} €".replace(".", ","))
    if rep.get("aligned"):
        parts.append(f"An Trade Republic angeglichen: {', '.join(rep['aligned'][:5])}")
    if rep.get("cash_aligned"):
        parts.append(f"Cash an Trade Republic angeglichen ({rep['cash_aligned']:+.2f} €)".replace(".", ","))
    if rep["problems"]:
        parts.append("⚠️ " + "; ".join(rep["problems"][:3]))
    return "\n".join(parts)


def sync(cfg: Config, notify=None, quiet: bool = True) -> dict:
    """Einmal abgleichen. Wirft ValueError, wenn es nicht geht (nicht verbunden, läuft schon …)."""
    if not _sync_lock.acquire(blocking=False):
        raise ValueError("Der Abgleich läuft gerade")
    _sync["running"] = True
    d = _load()
    try:
        _sync_once(cfg, notify, d)
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("TR-Abgleich fehlgeschlagen")
        d["error"] = f"Abgleich fehlgeschlagen: {e}"[:200]
    finally:
        _save(d)
        _sync["running"] = False
        _sync_lock.release()
    return status()


def _sync_once(cfg: Config, notify, d: dict) -> None:
    from .portfolio_service import symbol_ok
    if not d["connected"]:
        raise ValueError("Trade Republic ist nicht verbunden")
    state = manager.load_state()
    if not state.get("start_capital"):
        raise ValueError("Erst das Portfolio anlegen")
    d["last"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    api = _api()
    try:
        ok = api.resume_websession()
    except Exception as e:  # noqa: BLE001 – Netzwerk o. Ä.: später nochmal
        d["error"] = f"Trade Republic nicht erreichbar ({type(e).__name__})"
        return
    if not ok:
        d.update(expired=True, error="Anmeldung abgelaufen – bitte neu verbinden")
        if notify and not d.get("expired_notified"):
            notify(cfg, "Trade Republic", "Die Anmeldung ist abgelaufen. Bitte im NewsTrader unter "
                                          "Einstellungen → Trade Republic neu verbinden.")
            d["expired_notified"] = True
        return
    d.update(expired=False, expired_notified=False)
    since = datetime.fromisoformat(state["created"]) - timedelta(days=1)
    with _api_lock:
        events, cash, tr_holdings = asyncio.run(_fetch(api, since, set(d["done"])))
    trades = []
    for e in events:
        try:
            tr = parse_event(e)
        except Exception as ex:  # noqa: BLE001
            log.warning("TR-Ereignis %s nicht lesbar: %s", e.get("title"), ex)
            tr = {"id": e["id"], "time": _ts(e["timestamp"]).isoformat(), "side": "unknown", "name": e.get("title")}
        if tr is None:
            d["done"][e["id"]] = None  # keine Buchung (z. B. Zinsen) – nicht erneut abrufen
            continue
        trades.append(tr)
    held = {a["symbol"] for a in state.get("actions", [])}
    for tr in trades:
        if tr.get("isin") and tr["side"] != "unknown":
            tr["symbol"] = resolve_symbol(cfg, tr["isin"], d["symbols"], held, symbol_ok)
            if tr["symbol"]:
                d["symbols"][tr["isin"]] = tr["symbol"]
    rep = reconcile(cfg, trades, d["done"], symbol_ok)
    if tr_holdings is not None:
        from .portfolio_service import live_price
        unsettled = {t["isin"] for t in trades if t["id"] not in d["done"] and t.get("isin")}
        import_positions(cfg, tr_holdings, d["symbols"], unsettled, symbol_ok,
                         lambda sym: live_price(cfg, sym)[0], rep)
        drop_worthless_imports(cfg, lambda sym: live_price(cfg, sym)[0], rep)
        unsettled = {t["isin"] for t in trades if t["id"] not in d["done"] and t.get("isin")}
        align_to_tr(cfg, tr_holdings, cash, d["symbols"], unsettled, lambda sym: live_price(cfg, sym)[0], rep)
    d.update(report=rep, cash=cash, error=None, last_ok=d["last"])
    if (rep["matched"] or rep["corrected"] or rep["new"] or rep["dividends"] or rep["imported"] or rep["deposits"]
            or rep.get("removed") or rep.get("aligned") or rep.get("cash_aligned")):
        d["changes"] = {"time": d["last"], "report": rep}
    if tr_holdings is not None:
        d["compare"] = compare_holdings(cfg, tr_holdings, d["symbols"])
    text = _summary(rep)
    if text and notify and (rep["matched"] or rep["corrected"] or rep["new"] or rep["dividends"] or rep["imported"]):
        notify(cfg, "Trade Republic abgeglichen", text)
    log.info("TR-Abgleich: %s", text.replace("\n", " | ") or "nichts Neues")


def maybe_sync(cfg: Config, notify) -> None:
    """Aus der Bot-Schleife (jede Minute): im Hintergrund abgleichen, wenn es Zeit ist."""
    if _sync["running"]:
        return
    d = _load()
    if not d["connected"]:
        return
    now = datetime.now(pf.BERLIN)
    every = SYNC_EVERY_MIN if now.weekday() < 5 and 7 <= now.hour < 23 else SYNC_EVERY_MIN_QUIET
    if d.get("expired"):
        every = max(every, 120)
    last = d.get("last")
    if last and (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() < every * 60:
        return

    def run():
        try:
            sync(cfg, notify)
        except ValueError:
            pass

    threading.Thread(target=run, daemon=True, name="tr-sync").start()


def sync_in_background(cfg: Config, notify) -> dict:
    if _sync["running"]:
        raise ValueError("Der Abgleich läuft gerade")
    if not _load()["connected"]:
        raise ValueError("Trade Republic ist nicht verbunden")
    threading.Thread(target=lambda: _safe(cfg, notify), daemon=True, name="tr-sync").start()
    time.sleep(0.05)
    return status()


def _safe(cfg, notify):
    try:
        sync(cfg, notify, quiet=False)
    except ValueError as e:
        d = _load()
        d["error"] = str(e)
        _save(d)


# --------------------------------------------------------------------------- Hebelprodukte (nur lesen)
KO_CATEGORIES = ("knockOutProduct",)   # bestätigt; „knockOut“/„turbo“ lehnt TR ab


def _find_items(obj) -> list[dict]:
    """Ergebnisliste aus der TR-Antwort fischen (Format nicht dokumentiert): alle Dicts mit einer ISIN."""
    out = []
    if isinstance(obj, dict):
        ident = next((obj[k] for k in ("isin", "instrumentId", "id") if isinstance(obj.get(k), str)), "")
        if len(ident) == 12 and ident[:2].isalpha() and ident.isalnum():
            out.append({**obj, "isin": ident})
            return out
        for v in obj.values():
            out += _find_items(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _find_items(v)
    return out


def _num_field(d: dict, *names) -> float | None:
    for k, v in d.items():
        if any(n in k.lower() for n in names) and isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        if any(n in k.lower() for n in names) and isinstance(v, str):
            try:
                return float(v.replace(",", "."))
            except ValueError:
                pass
    return None


def _direction(d: dict) -> str | None:
    blob = " ".join(str(v).lower() for k, v in d.items() if isinstance(v, str))
    keys = {k.lower(): str(v).lower() for k, v in d.items() if isinstance(v, str)}
    for k in ("optiontype", "direction", "type", "producttype"):
        v = keys.get(k, "")
        if v in ("call", "long", "bull"):
            return "long"
        if v in ("put", "short", "bear"):
            return "short"
    if " long" in f" {blob}" or "call" in blob or "bull" in blob:
        return "long"
    if " short" in f" {blob}" or "put" in blob or "bear" in blob:
        return "short"
    return None


def parse_knockouts(raw) -> list[dict]:
    items = []
    for d in _find_items(raw):
        direction = _direction(d)
        issuer = d.get("issuerDisplayName") or d.get("issuer") or ""
        barrier = d.get("barrier") if isinstance(d.get("barrier"), (int, float)) else _num_field(d, "knockout", "barrier", "knock_out")
        kind = d.get("productCategoryName") or "Knock-out"
        name = d.get("name") or d.get("shortName") or " ".join(x for x in (
            issuer, kind, {"long": "Long", "short": "Short"}.get(direction or "", ""),
            f"KO {barrier:g}" if isinstance(barrier, (int, float)) else "") if x)
        items.append({"isin": d["isin"], "name": name, "issuer": issuer, "direction": direction,
                      "leverage": _num_field(d, "leverage", "hebel"), "barrier": barrier,
                      "strike": d.get("strike") if isinstance(d.get("strike"), (int, float)) else _num_field(d, "strike", "basispreis"),
                      "size": d.get("size") if isinstance(d.get("size"), (int, float)) else None,
                      "currency": d.get("currency") or "", "expiry": d.get("expiry"), "kind": kind})
    return items


def product_price(ko: dict, underlying: float, direction: str) -> float | None:
    """Ungefährer Preis eines Turbos ohne Laufzeit: (Kurs − Basispreis) × Bezugsverhältnis (Short umgekehrt)."""
    strike, size = ko.get("strike") or ko.get("barrier"), ko.get("size")
    if strike is None or not size:
        return None
    v = (underlying - strike) if direction == "long" else (strike - underlying)
    return max(0.0, v * size)


DIAG: dict = {}   # letzte Antworten/Fehler der Derivate-Suche (für die Diagnose)


def _variants(isin: str, direction: str | None):
    """Abfragevarianten (Format von TR nicht dokumentiert) – die erste mit Ergebnissen gewinnt."""
    for cat in KO_CATEGORIES:
        if direction:
            for o in ({"long": ("long", "call"), "short": ("short", "put")}[direction]):   # TR: "long"/"short"
                yield {"type": "derivatives", "underlying": isin, "productCategory": cat, "optionType": o}
        yield {"type": "derivatives", "underlying": isin, "productCategory": cat}   # alle (groß)


async def _search_derivatives(api, isin: str, direction: str | None = None) -> tuple[list[dict], object]:
    DIAG.clear()
    DIAG["tries"] = []
    for payload in _variants(isin, direction):
        entry = {"payload": payload}
        try:
            sid = await api.subscribe(payload)
            try:
                raw = await asyncio.wait_for(api._recv_subscription(sid), 15)
            finally:
                try:
                    await api.unsubscribe(sid)
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001 – TR-Fehler mit Begründung festhalten
            detail = getattr(e, "error", None) or getattr(e, "args", [""])
            entry["error"] = f"{type(e).__name__}: {str(detail)[:300]}"
            DIAG["tries"].append(entry)
            continue
        items = parse_knockouts(raw)
        if direction:
            items = [i for i in items if i["direction"] in (None, direction)]
        entry["items"] = len(items)
        entry["keys"] = list(raw.keys())[:15] if isinstance(raw, dict) else type(raw).__name__
        if isinstance(raw, dict):
            entry["resultCount"] = raw.get("resultCount")
            res = raw.get("results")
            entry["sample"] = json.dumps(res[:2] if isinstance(res, list) else res, default=str)[:1500]
        DIAG["tries"].append(entry)
        if items:
            return items, raw
    return [], None


def _big_frames() -> None:
    """Die vollständige Derivate-Liste ist größer als 1 MB – Standardgrenze der Websocket-Bibliothek anheben."""
    import functools
    import websockets
    if not getattr(websockets.connect, "_nt_big", False):
        big = functools.partial(websockets.connect, max_size=32 * 1024 * 1024)
        big._nt_big = True
        websockets.connect = big


def find_knockouts(isin: str, direction: str | None = None, cached_hours: float = 0.0) -> tuple[list[dict], object]:
    """Knock-out-Scheine auf einen Basiswert, die bei Trade Republic handelbar sind. ([] wenn nicht verbunden)."""
    key = (isin, direction or "")
    hit = _ko_cache.get(key)
    if cached_hours and hit and time.time() - hit[0] < cached_hours * 3600:
        return hit[1], None
    if not _load().get("connected"):
        return [], None
    with _api_lock:
        items, raw = _find_knockouts_locked(isin, direction)
    if items:
        _ko_cache[key] = (time.time(), items)
    return items, raw


def _find_knockouts_locked(isin: str, direction: str | None) -> tuple[list[dict], object]:
    _big_frames()
    api = _api()
    if not api.resume_websession():
        return [], None

    async def run():
        try:
            return await _search_derivatives(api, isin, direction)
        finally:
            await api.close()
    return asyncio.run(run())


def pick_for_zocker(items: list[dict], direction: str, price_native: float, cfg) -> dict | None:
    """Schein für den Zocker: richtige Richtung, ohne Laufzeit bevorzugt, Hebel (mit aktuellem Kurs neu gerechnet)
    zwischen product_min_leverage und product_max_leverage, möglichst nah am Ziel."""
    best = None
    for it in items:
        if it.get("direction") not in (None, direction):
            continue
        k = it.get("strike") or it.get("barrier")
        b = it.get("barrier") or k
        if not k or not b:
            continue
        gap = (price_native - k) if direction == "long" else (k - price_native)
        dist = (price_native - b) / price_native if direction == "long" else (b - price_native) / price_native
        if gap <= 0 or dist <= 0.01:
            continue  # schon ausgeknockt / zu nah
        lev = price_native / gap
        if not cfg.product_min_leverage <= lev <= cfg.product_max_leverage:
            continue
        cand = {**it, "leverage": round(lev, 2), "barrier_dist": round(dist, 4)}
        rank = (it.get("expiry") is not None, abs(lev - cfg.product_target_leverage))
        if best is None or rank < best[0]:
            best = (rank, cand)
    return best[1] if best else None


def pick_knockout(items: list[dict], direction: str, stop: float, target_leverage: float,
                  max_leverage: float | None = None, price: float | None = None) -> dict | None:
    """Schein in der richtigen Richtung, dessen Knock-out-Schwelle jenseits des Stop-Loss liegt (Stop greift zuerst),
    mit einem Hebel möglichst nah am Ziel."""
    ok = []
    for it in items:
        if it["direction"] and it["direction"] != direction:
            continue
        b = it.get("barrier") or it.get("strike")
        if b is None or it.get("leverage") is None:
            continue
        if (direction == "long" and b >= stop * 0.99) or (direction == "short" and b <= stop * 1.01):
            continue
        if price and (it.get("strike") or b):   # Hebel mit aktuellem Kurs; nicht über dem Maximum
            k = it.get("strike") or b
            gap = (price - k) if direction == "long" else (k - price)
            if gap <= 0:
                continue
            it = {**it, "leverage": round(price / gap, 2)}
        if max_leverage and it["leverage"] > max_leverage:
            continue
        ok.append(it)
    if not ok:
        return None
    # ohne Laufzeit bevorzugen (Open End), dann Hebel möglichst nah am Ziel
    return min(ok, key=lambda it: (it.get("expiry") is not None, abs(it["leverage"] - target_leverage)))
