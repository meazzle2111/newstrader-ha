"""Tab „Mein Depot": eigener Depotwert + zweimal täglich Kauf-/Verkaufssignale (nur Long, ohne Hebel).

Die Signale kommen aus demselben Verfahren wie der Spielgeld-Bot (Technik + Nachrichten) und werden auf den
eingetragenen Depotwert umgerechnet. Es sind automatisch erzeugte Hinweise eines Experiments – keine Anlageberatung.
Der Bot handelt hier nichts; was du damit machst, entscheidest du selbst.
"""
from __future__ import annotations

import json
import logging
import math
import subprocess
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")
DEPOT_FILE = STATE_DIR / "my_depot.json"
REPORTS_FILE = STATE_DIR / "my_signals.json"


def _read(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def load_depot() -> dict:
    return _read(DEPOT_FILE, {"value": None, "holdings": [], "updated": None})


def effective_depot(cfg: Config) -> tuple[float | None, set[str]]:
    """Depotwert + Bestand: aus deinen Transaktionen (live bewertet) + Cash, sonst die manuelle Angabe."""
    from . import portfolio as pf
    depot = load_depot()
    txns = pf.load_txns()
    if not txns:
        return depot.get("value"), set(depot.get("holdings") or [])
    from .portfolio_service import overview
    ov = overview(cfg, depot.get("cash"))
    return ov["kpi"]["total"] or depot.get("value"), {p["symbol"] for p in ov["positions"]}


def save_cash(value) -> dict:
    data = load_depot()
    from .portfolio import parse_number
    try:
        cash = round(parse_number(value), 2)
    except (TypeError, ValueError):
        raise ValueError("Cash muss eine Zahl sein")
    if not -1e8 < cash < 1e9:
        raise ValueError("Cash außerhalb des gültigen Bereichs")
    data["cash"] = cash
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write(DEPOT_FILE, data)
    return data


def save_depot(cfg: Config, value, holdings) -> dict:
    try:
        value = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError("Depotwert muss eine Zahl sein")
    if not 0 < value < 1e9:
        raise ValueError("Depotwert außerhalb des gültigen Bereichs")
    holdings = [h for h in (holdings or []) if h in cfg.watchlist]
    data = {**load_depot(), "value": value, "holdings": holdings,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}  # Cash usw. behalten
    _write(DEPOT_FILE, data)
    return data


def load_reports() -> list[dict]:
    return _read(REPORTS_FILE, [])


def _slot_times(cfg: Config, day: datetime) -> list[datetime]:
    out = []
    for t in cfg.my_signal_times:
        h, m = (int(x) for x in t.split(":"))
        out.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
    return sorted(out)


def next_slot(cfg: Config) -> str | None:
    now = datetime.now(BERLIN)
    for day_offset in range(0, 8):
        day = now + timedelta(days=day_offset)
        if day.weekday() >= 5:
            continue
        for s in _slot_times(cfg, day):
            if s > now:
                return s.isoformat(timespec="minutes")
    return None


def generate_report(cfg: Config, slot: str = "manuell") -> dict | None:
    """Signale aus dem letzten Bot-Durchlauf auf den eigenen Depotwert umrechnen."""
    cycle = _read(STATE_DIR / "last_cycle.json", {})
    symbols = cycle.get("symbols", {})
    if not symbols:
        return None
    value, holdings = effective_depot(cfg)
    claude_on = cycle.get("analyst") == "claude"

    def checked(v):  # bei aktivem Claude nur Werte, deren Nachrichten Claude gelesen hat
        return not claude_on or v.get("analyst") == "claude" or not v.get("headlines")

    def reason(v):
        return f"{v['tech_detail']} | {v['news_summary']}"

    buys = []
    for sym, v in symbols.items():
        score = v["combined"]
        if score <= cfg.buy_threshold or sym in holdings or not checked(v):
            continue
        strength = 0.5 + 0.5 * max(0.0, min(1.0, (score - cfg.buy_threshold) / (0.6 - cfg.buy_threshold)))
        amount = value * cfg.max_position_pct * strength if value else None
        amount = math.floor(amount / 10) * 10 if amount else None
        buys.append({
            "symbol": sym, "name": cfg.names.get(sym, sym), "price": v["price"], "score": score,
            "amount": amount, "shares": (amount / v["price"]) if amount else None,
            "stop_loss": v["price"] * (1 - cfg.stop_loss_pct), "tradable": v["tradable"],
            "source": v.get("source", "Yahoo"), "reason": reason(v),
        })
    buys.sort(key=lambda b: -b["score"])
    buys = buys[:cfg.my_max_buys]

    sells, holds = [], []
    for sym in holdings:
        v = symbols.get(sym)
        if not v:
            continue
        item = {"symbol": sym, "name": cfg.names.get(sym, sym), "price": v["price"], "score": v["combined"],
                "reason": reason(v)}
        (sells if v["combined"] < cfg.sell_threshold else holds).append(item)
    sells.sort(key=lambda x: x["score"])
    holds.sort(key=lambda x: -x["score"])

    # Ohne Bestandsangabe: die schwächsten Werte als allgemeine Warnung („falls im Depot“)
    warnings = []
    if not holdings:
        warnings = sorted(
            ({"symbol": s, "name": cfg.names.get(s, s), "price": v["price"], "score": v["combined"], "reason": reason(v)}
             for s, v in symbols.items() if v["combined"] < cfg.sell_threshold and checked(v)),
            key=lambda x: x["score"])[:cfg.my_max_buys]

    report = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slot": slot, "cycle_time": cycle.get("time"), "analyst": cycle.get("analyst"),
        "depot_value": value, "holdings": sorted(holdings),
        "buys": buys, "sells": sells, "holds": holds, "warnings": warnings,
        "rules": {"buy": cfg.buy_threshold, "sell": cfg.sell_threshold, "stop_loss": cfg.stop_loss_pct,
                  "max_position": cfg.max_position_pct},
    }
    reports = load_reports()
    reports.insert(0, report)
    _write(REPORTS_FILE, reports[:60])
    _notify(cfg, report)
    log.info("Signale für dein Depot (%s): %d Kauf, %d Verkauf", slot, len(buys), len(sells))
    return report


def _notify(cfg: Config, report: dict) -> None:
    """macOS-Mitteilung, damit du die Signale nicht verpasst."""
    if not cfg.my_notify:
        return
    msg = f"{len(report['buys'])} Kauf-, {len(report['sells'])} Verkaufssignal(e)"
    if report["buys"]:
        msg += " · " + ", ".join(b["name"] for b in report["buys"][:3])
    notify(cfg, "NewsTrader – Mein Depot", msg)


def tr_url(isin: str | None) -> str | None:
    return f"https://app.traderepublic.com/instrument/{isin}" if isin else None


def notify(cfg: Config, title: str, msg: str, url: str | None = None) -> None:
    """Im Add-on: Home-Assistant-Benachrichtigung (Handy-App bzw. Glocke in HA). Auf dem Mac: macOS-Mitteilung."""
    import os
    link = url
    token = os.environ.get("SUPERVISOR_TOKEN")
    if token:
        import requests
        svc = cfg.ha_notify_service.removeprefix("notify.")
        url = (f"http://supervisor/core/api/services/notify/{svc}" if svc
               else "http://supervisor/core/api/services/persistent_notification/create")
        try:
            body = {"title": title, "message": msg}
            if link and svc:   # Tippen auf die Mitteilung öffnet den Link (iOS: url, Android: clickAction)
                body["data"] = {"url": link, "clickAction": link}
            requests.post(url, json=body, timeout=10,
                          headers={"Authorization": f"Bearer {token}"}).raise_for_status()
        except Exception as e:
            log.warning("Home-Assistant-Benachrichtigung fehlgeschlagen: %s", e)
        return
    try:
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
                       timeout=5, capture_output=True)
    except Exception:
        pass


def maybe_scheduled_report(cfg: Config) -> None:
    """Vom Bot nach jedem Durchlauf aufgerufen: fälligen Termin (Mo–Fr) genau einmal ausführen."""
    now = datetime.now(BERLIN)
    if now.weekday() >= 5:
        return
    done = {r["slot"] for r in load_reports()}
    for slot in _slot_times(cfg, now):
        key = slot.isoformat(timespec="minutes")
        # nur nachholen, wenn der Termin heute ist und höchstens 2 Stunden zurückliegt
        if slot <= now and (now - slot).total_seconds() < 7200 and key not in done:
            generate_report(cfg, key)


# --------------------------------------------------------------------------- Startportfolio
BUILD_FILE = STATE_DIR / "my_build.json"
_build_lock = __import__("threading").Lock()


def build_status() -> dict:
    return _read(BUILD_FILE, {"running": False})


def start_portfolio_build(cfg: Config) -> bool:
    """Startet den Aufbau im Hintergrund (dauert ca. 1 Minute, weil Claude die Kandidaten einzeln liest)."""
    if not _build_lock.acquire(blocking=False):
        return False
    import threading

    def run():
        try:
            build_portfolio(cfg)
        except Exception as e:
            log.exception("Startportfolio fehlgeschlagen")
            _write(BUILD_FILE, {"running": False, "error": str(e)})
        finally:
            _build_lock.release()

    _write(BUILD_FILE, {"running": True, "step": "Kandidaten auswählen …",
                        "started": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    threading.Thread(target=run, name="portfolio", daemon=True).start()
    return True


def build_portfolio(cfg: Config) -> dict:
    """Startportfolio: die stärksten Long-Signale, Claude liest vorher jeweils die Nachrichten, Gewichte nach Signalstärke."""
    from concurrent.futures import ThreadPoolExecutor

    from .analyst import NewsAnalyst
    from .news import get_headlines

    value, _ = effective_depot(cfg)
    if not value:
        raise ValueError("Bitte zuerst den Depotwert eintragen")
    cycle = _read(STATE_DIR / "last_cycle.json", {})
    symbols = cycle.get("symbols", {})
    if not symbols:
        raise ValueError("Noch kein Bot-Durchlauf vorhanden")

    def combined(tech, news, n):
        if n == 0:
            return tech
        w = cfg.weight_technical + cfg.weight_news
        return (cfg.weight_technical * tech + cfg.weight_news * news) / w

    # 1) Kandidaten nach Vorfilter (Technik + Wörterbuch/letzte Claude-Bewertung)
    pool = sorted(((s, v) for s, v in symbols.items() if v["combined"] > cfg.portfolio_min_score * 0.5),
                  key=lambda x: -x[1]["combined"])[: cfg.portfolio_size + 4]

    # 2) Claude liest die Nachrichten der Kandidaten (sofern aktiv)
    analyst = NewsAnalyst(cfg)
    checked = {}
    _write(BUILD_FILE, {"running": True, "step": f"Claude liest Nachrichten zu {len(pool)} Kandidaten …"})

    def deep(item):
        sym, v = item
        hl = get_headlines(sym, cfg.news_max_age_hours, cfg.names.get(sym), cfg.german_news)
        ctx = f"Kurs {v.get('native_price', v['price']):.2f} {v.get('currency', 'EUR')}, {v['tech_detail']}"
        news = analyst.analyze(sym, hl, ctx, use_claude=analyst.mode == "claude")
        return sym, news, len(hl)

    with ThreadPoolExecutor(max(1, cfg.claude_parallel)) as ex:
        for sym, news, n in ex.map(deep, pool):
            v = symbols[sym]
            checked[sym] = {"score": combined(v["tech"], news.score, news.n_headlines), "summary": news.summary,
                            "analyst": news.analyst, "tech_detail": v["tech_detail"], "price": v["price"]}

    # 3) Auswahl + Gewichtung
    picks = sorted((x for x in checked.items() if x[1]["score"] > cfg.portfolio_min_score),
                   key=lambda x: -x[1]["score"])[: cfg.portfolio_size]
    invest = value * (1 - cfg.portfolio_cash_pct)
    weights = {}
    if picks:
        total = sum(x[1]["score"] for x in picks)
        weights = {s: min(cfg.max_position_pct, d["score"] / total * (1 - cfg.portfolio_cash_pct)) for s, d in picks}
        crypto = [s for s in weights if s.endswith(("-EUR", "-USD"))]
        csum = sum(weights[s] for s in crypto)
        if csum > cfg.portfolio_max_crypto_pct:
            for s in crypto:
                weights[s] *= cfg.portfolio_max_crypto_pct / csum
    positions = []
    for sym, w in sorted(weights.items(), key=lambda x: -x[1]):
        d = checked[sym]
        amount = math.floor(value * w / 10) * 10
        if amount < 50:
            continue
        positions.append({
            "symbol": sym, "name": cfg.names.get(sym, sym), "weight": amount / value, "amount": amount,
            "price": d["price"], "shares": amount / d["price"], "score": d["score"],
            "stop_loss": d["price"] * (1 - cfg.stop_loss_pct), "analyst": d["analyst"],
            "reason": f"{d['tech_detail']} | {d['summary']}",
        })
    invested = sum(p["amount"] for p in positions)
    rejected = [{"symbol": s, "name": cfg.names.get(s, s), "score": d["score"], "reason": d["summary"]}
                for s, d in checked.items() if s not in weights]

    report = {
        "kind": "portfolio",
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "slot": "startportfolio", "cycle_time": cycle.get("time"), "analyst": analyst.mode,
        "depot_value": value, "holdings": [], "positions": positions, "cash": value - invested,
        "rejected": sorted(rejected, key=lambda x: -x["score"]),
        "buys": [], "sells": [], "holds": [], "warnings": [],
        "rules": {"buy": cfg.buy_threshold, "sell": cfg.sell_threshold, "stop_loss": cfg.stop_loss_pct,
                  "max_position": cfg.max_position_pct, "min_score": cfg.portfolio_min_score},
    }
    reports = load_reports()
    reports.insert(0, report)
    _write(REPORTS_FILE, reports[:60])
    _write(BUILD_FILE, {"running": False, "done": report["created"]})
    log.info("Startportfolio: %d Werte, %.0f € investiert, %.0f € Cash", len(positions), invested, value - invested)
    return report


def adopt_portfolio(cfg: Config) -> dict:
    """„Habe ich so gekauft": Werte des letzten Startportfolios als Bestand übernehmen."""
    rep = next((r for r in load_reports() if r.get("kind") == "portfolio"), None)
    if not rep:
        raise ValueError("Kein Startportfolio vorhanden")
    depot = load_depot()
    holdings = sorted(set(depot.get("holdings") or []) | {p["symbol"] for p in rep["positions"]})
    return save_depot(cfg, depot.get("value"), holdings)
