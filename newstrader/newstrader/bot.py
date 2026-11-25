"""Hauptschleife: Daten holen, interpretieren, handeln, protokollieren."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .analyst import NewsAnalyst, NewsSignal
from .broker import PaperBroker
from .config import Config, STATE_DIR
from .market import get_daily_closes, get_quote, technical_signal
from .news import Headline, get_headlines
from .realtime import get_live_quote
from .strategy import Snapshot, check_risk, run_strategy

log = logging.getLogger("newstrader")
LAST_CYCLE_FILE = STATE_DIR / "last_cycle.json"
LOCK_FILE = STATE_DIR / "bot.pid"
WAKE = threading.Event()  # „Jetzt prüfen“: nächsten Durchlauf sofort starten


_headline_cache: dict[str, tuple[float, list[Headline]]] = {}
HEADLINE_CACHE_FILE = STATE_DIR / "cache" / "headlines.json"
_price_hist: dict[str, deque] = {}       # Kursverlauf der letzten Minuten je Wert (für „rasante“ Bewegungen)
_forced_at: dict[str, float] = {}        # wann zuletzt wegen rasanter Bewegung neu bewertet


def _load_headline_cache() -> None:
    """Schlagzeilen überstehen einen Neustart – sonst bräuchte der Bot ~40 Min., bis alle Werte wieder Nachrichten haben."""
    if _headline_cache or not HEADLINE_CACHE_FILE.exists():
        return
    try:
        data = json.loads(HEADLINE_CACHE_FILE.read_text())
        for sym, (ts, items) in data.items():
            _headline_cache[sym] = (ts, [Headline(sym, h["title"], h.get("summary", ""), h["source"],
                                                   datetime.fromisoformat(h["published"])) for h in items])
        log.info("Schlagzeilen für %d Werte aus dem Zwischenspeicher geladen", len(_headline_cache))
    except Exception as e:
        log.warning("Schlagzeilen-Zwischenspeicher unlesbar: %s", e)


def _save_headline_cache() -> None:
    data = {sym: [ts, [{"title": h.title, "summary": h.summary, "source": h.source,
                        "published": h.published.isoformat()} for h in hl]]
            for sym, (ts, hl) in _headline_cache.items()}
    HEADLINE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEADLINE_CACHE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    tmp.replace(HEADLINE_CACHE_FILE)


def note_price(sym: str, price: float) -> None:
    dq = _price_hist.setdefault(sym, deque(maxlen=400))
    dq.append((time.time(), price))


def rapid_moves(cfg: Config, symbols) -> dict[str, float]:
    """Werte mit ≥ rapid_move_pct Bewegung im Zeitfenster (größte Abweichung zum aktuellen Kurs), ohne Mehrfachauslösung."""
    out = {}
    now = time.time()
    window = cfg.rapid_window_minutes * 60
    for sym in symbols:
        dq = _price_hist.get(sym)
        if not dq or len(dq) < 2:
            continue
        last = dq[-1][1]
        olds = [p for t, p in dq if now - t <= window]
        move = max((last / p - 1 for p in olds), key=abs, default=0.0)
        if abs(move) >= cfg.rapid_move_pct and now - _forced_at.get(sym, 0) > window:
            out[sym] = move
    return out


def _stage1(cfg: Config, sym: str):
    """Kurs + Technik für einen Wert (läuft parallel)."""
    try:
        quote = get_quote(sym, cfg.base_currency, cfg)
        closes = get_daily_closes(sym)
    except Exception as e:
        log.warning("%s: Kursdaten nicht verfügbar (%s)", sym, e)
        return sym, None, None
    if quote is None or closes is None:
        return sym, None, None
    return sym, quote, technical_signal(closes, quote.native_price)  # Technik in Handelswährung


def _refresh_news(cfg: Config, symbols: list[str], must: set[str], force: set[str] | None = None) -> None:
    """Schlagzeilen nachladen (je Wert höchstens alle news_refresh_minutes): offene Positionen immer, sonst die ältesten
    zuerst, max. news_per_cycle pro Durchlauf. `force`: sofort, egal wie alt (rasante Kursbewegung)."""
    now = time.time()
    force = force or set()
    due = [s for s in symbols if s in force or now - _headline_cache.get(s, (0, []))[0] > cfg.news_refresh_minutes * 60]
    due.sort(key=lambda s: (s not in must, _headline_cache.get(s, (0, []))[0]))
    todo = [s for s in due if s in must or s in force] + [s for s in due if s not in must and s not in force][:cfg.news_per_cycle]
    if not todo:
        return

    def load(sym):
        try:
            return sym, get_headlines(sym, cfg.news_max_age_hours, cfg.names.get(sym), cfg.german_news)
        except Exception as e:
            log.debug("Schlagzeilen für %s fehlgeschlagen: %s", sym, e)
            return sym, None

    with ThreadPoolExecutor(cfg.fetch_workers) as ex:
        for sym, hl in ex.map(load, todo):
            if hl is not None:
                _headline_cache[sym] = (time.time(), hl)
    _save_headline_cache()


def collect(cfg: Config, analyst: NewsAnalyst, held: set[str] | None = None
            ) -> tuple[dict[str, Snapshot], dict[str, dict]]:
    """Stufe 1 für alle Werte (Kurs, Technik, Wörterbuch), Stufe 2 (Claude) nur für Kandidaten und offene Positionen."""
    held = held or set()
    _load_headline_cache()
    # Stufe 0: Technik für das ganze Universum aus (zwischengespeicherten) Tageskursen – ohne Live-Abfragen
    def tech_only(sym):
        try:
            closes = get_daily_closes(sym)
            return sym, closes, technical_signal(closes) if closes is not None else None
        except Exception:
            return sym, None, None
    # Alles, was in einem Depot liegt, wird mit beobachtet – auch wenn es nicht im Universum ist (z. B. eigene Käufe)
    universe = list(cfg.watchlist) + sorted(s for s in held if s not in cfg.watchlist)
    with ThreadPoolExecutor(cfg.fetch_workers) as ex:
        base = list(ex.map(tech_only, universe))
    valid = [b for b in base if b[2] is not None]
    n_long = round(cfg.news_top_n * 0.7)            # 70 % für die stärksten positiven (Portfolio kauft nur)
    best = sorted(valid, key=lambda b: -b[2].score)[:n_long]
    worst = sorted(valid, key=lambda b: b[2].score)[: cfg.news_top_n - n_long]   # Rest für Shorts im Zockerportfolio
    focus = set(held) | {b[0] for b in best + worst}
    zc = cfg.zocker()
    mom_cands: list[str] = []
    if zc.zocker_strategy == "momentum":   # Zocker: Rangliste nach Kursstärke; die Besten brauchen Live-Kurse + Nachrichten
        from . import momentum
        try:
            order = momentum.update(zc, {b[0]: b[1] for b in base if b[1] is not None})
            focus |= set(order[:zc.mom_keep_rank + 2])
            mom_cands = [s for s in order[:zc.mom_keep_rank] if s not in held][:zc.mom_positions + 2]
        except Exception:  # noqa: BLE001
            log.exception("Momentum-Rangliste fehlgeschlagen")
    # Stufe 1: Live-Kurs + Nachrichten nur für den Fokus (deutlichste Signale + alles im Depot)
    with ThreadPoolExecutor(cfg.fetch_workers) as ex:
        stage1 = list(ex.map(lambda s: _stage1(cfg, s), [s for s in universe if s in focus]))
    for sym, q, t in stage1:
        if q is not None:
            note_price(sym, q.price)
    rapid = rapid_moves(cfg, [s for s, q, t in stage1 if q and t])
    for sym, move in rapid.items():
        _forced_at[sym] = time.time()
        log.info("⚡ Rasante Kursbewegung %s: %+.1f %% in ≤ %.0f Min. – Nachrichten werden sofort neu bewertet",
                 sym, move * 100, cfg.rapid_window_minutes)
    _refresh_news(cfg, [s for s, q, t in stage1 if q and t], held, set(rapid))

    snaps: dict[str, Snapshot] = {}
    extra: dict[str, dict] = {}
    contexts: dict[str, str] = {}
    for sym, quote, tech in stage1:
        if quote is None or tech is None:
            continue
        tradable = quote.tradable if quote.tradable is not None else quote.age_minutes() <= cfg.max_quote_age_minutes
        headlines = _headline_cache.get(sym, (0, []))[1]
        if sym in _headline_cache:
            news = analyst.analyze(sym, headlines, "", use_claude=False)      # Vorfilter
        else:
            news = NewsSignal(0.0, 0.0, 0.0, "Nachrichten werden noch geladen", "none", 0)
        snaps[sym] = Snapshot(sym, quote.price, tradable, tech, news, news_loaded=sym in _headline_cache)
        contexts[sym] = f"Kurs {quote.native_price:.2f} {quote.currency}, {tech.describe()}"
        extra[sym] = {
            "native_price": quote.native_price, "currency": quote.currency, "source": quote.source,
            "quote_time": quote.time.isoformat(timespec="seconds"),
            "headlines": [_headline_json(h) for h in headlines[:12]],
            "news_loaded": sym in _headline_cache,
            "rapid": rapid.get(sym),
        }

    # Werte außerhalb des Fokus: nur anzeigen (letzter Schlusskurs, kein Handel)
    from .market import fx_rate, get_currency
    for sym, closes, tech in base:
        if sym in snaps or tech is None or closes is None:
            continue
        try:
            cur = get_currency(sym)
            price = float(closes.iloc[-1]) * (fx_rate(cur, cfg.base_currency) if cur != cfg.base_currency else 1.0)
        except Exception:
            continue
        snaps[sym] = Snapshot(sym, price, False, tech,
                              NewsSignal(0.0, 0.0, 0.0, "nicht im Fokus – andere Werte haben gerade deutlichere Signale", "none", 0),
                              news_loaded=False)
        extra[sym] = {"native_price": float(closes.iloc[-1]), "currency": get_currency(sym), "source": "Schlusskurs",
                      "quote_time": "", "headlines": [], "news_loaded": False, "rapid": None, "focus": False}

    # Stufe 2: Claude für offene Positionen + Werte nahe an einer Schwelle (stärkste zuerst)
    if analyst.mode == "claude":
        lo, hi = cfg.short_threshold * cfg.candidate_fraction, cfg.buy_threshold * cfg.candidate_fraction
        near = [s for s, v in snaps.items() if s not in held and v.news_loaded and (v.combined(cfg) >= hi or
                (cfg.trading_mode == "leverage" and cfg.allow_short and v.combined(cfg) <= lo))]
        near.sort(key=lambda s: abs(snaps[s].combined(cfg)), reverse=True)
        open_now = lambda s: snaps[s].tradable  # noqa: E731 – Börse zu: keine Claude-Aufrufe (spart dein Abo)
        near = [s for s in near if open_now(s)]
        chosen = [s for s in held if s in snaps and open_now(s)] + near[:cfg.claude_top_n]
        chosen += [s for s in rapid if s in snaps and s not in chosen]   # rasante Bewegung: immer zu Claude
        # Zocker-Kandidaten (Momentum): vor dem Kauf prüft die KI die Nachrichtenlage (Filter gegen klar schlechte News)
        chosen += [s for s in mom_cands if s in snaps and s not in chosen and open_now(s) and snaps[s].news_loaded]

        def deep(sym):
            hl = _headline_cache.get(sym, (0, []))[1]
            # Werte ohne Live-Kurs (z. B. vom Handel ausgesetzt) haben keinen Kontext aus Stufe 1
            base_ctx = contexts.get(sym) or f"letzter Schlusskurs {snaps[sym].price:.2f} EUR, kein aktueller Kurs"
            ctx = base_ctx + (f", ACHTUNG: Kurs {rapid[sym]:+.1%} in den letzten {cfg.rapid_window_minutes:.0f} Minuten"
                              if sym in rapid else "")
            try:
                return sym, analyst.analyze(sym, hl, ctx, use_claude=True, force=sym in rapid)
            except Exception:  # noqa: BLE001 – ein einzelner Wert darf den Durchlauf nicht stoppen
                log.exception("Analyse von %s fehlgeschlagen", sym)
                return sym, None

        g0, c0 = analyst.gemini.today_calls(), analyst.today_calls()
        with ThreadPoolExecutor(max(1, cfg.claude_parallel)) as ex:
            for sym, news in ex.map(deep, chosen):
                if news is not None:
                    snaps[sym].news = news
        new_calls = (analyst.gemini.today_calls() - g0, analyst.today_calls() - c0)
        for s in chosen:
            extra.setdefault(s, {})["claude_checked"] = True
        n_held = len([s for s in chosen if s in held])
        log.info("Vorfilter: %d Werte bewertet, %d zur KI (%d Positionen, %d Kandidaten) – neue Aufrufe: Gemini %d, Claude %d, "
                 "Rest aus dem Zwischenspeicher", len(snaps), len(chosen), n_held, len(chosen) - n_held, *new_calls)
    return snaps, extra


def _headline_json(h: Headline) -> dict:
    return {"title": h.title, "source": h.source, "published": h.published.isoformat(timespec="seconds")}


def print_cycle(cfg: Config, broker: PaperBroker, snaps: dict[str, Snapshot], analyst: NewsAnalyst) -> None:
    prices = {s: v.price for s, v in snaps.items()}
    eq = broker.equity(prices)
    start = broker.state["start_capital"]
    bench = broker.benchmark_return(prices)
    c = broker.cur
    print()
    print(f"── {datetime.now():%d.%m.%Y %H:%M} ─ Depot {eq:,.2f} {c} ({eq / start - 1:+.2%})"
          + (f" ─ Buy&Hold {bench:+.2%}" if bench is not None else "")
          + f" ─ Cash {broker.cash:,.0f} {c} ─ Modus: {cfg.trading_mode} ─ Analyst: {analyst.mode}")
    print(f"{'Symbol':<9}{'Kurs ' + c:>11}  {'Markt':<6}{'Tech':>6}{'News':>6}{'Gesamt':>8}  Bestand / Nachrichtenlage")
    shown = 0
    for sym, s in snaps.items():
        if s.news.summary.startswith("nicht im Fokus") and sym not in broker.positions:
            continue  # Protokoll kurz halten: nur Fokus + Bestand
        shown += 1
        pos = broker.positions.get(sym)
        held = (f"📦 {pos['side']} {pos['leverage']:g}x {broker.pnl(pos, s.price):+,.0f} {c}  " if pos else "")
        print(f"{sym:<9}{s.price:>11.2f}  {'offen' if s.tradable else 'zu':<6}{s.tech.score:>+6.2f}"
              f"{s.news.score:>+6.2f}{s.combined(cfg):>+8.2f}  {held}{s.news.summary[:70]}")
    if analyst.calls:
        if analyst.backend == "cli":
            print(f"Claude-Aufrufe heute: {analyst.today_calls()} von {cfg.claude_cli_max_calls_per_day} "
                  f"(über dein Abo, {analyst.status()})")
        else:
            print(f"Claude-Aufrufe: {analyst.calls}, API-Kosten heute ≈ {analyst.today_usd():.2f} $ "
                  f"von {cfg.claude_daily_budget_usd:.2f} $ Tageslimit ({analyst.status()})")


def save_cycle(cfg: Config, snaps: dict[str, Snapshot], extra: dict[str, dict], analyst: NewsAnalyst) -> None:
    data = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "analyst": analyst.mode,
        "analyst_status": analyst.status(),
        "ai_providers": analyst.providers(),
        "claude_model": analyst.model_label(),
        "claude_backend": analyst.backend,
        "claude_today_calls": analyst.today_calls(),
        "claude_max_calls": cfg.claude_cli_max_calls_per_day,
        "claude_calls": analyst.calls,
        "claude_cost_usd": round(analyst.cost_estimate_usd(), 4),
        "claude_today_usd": round(analyst.today_usd(), 4),
        "claude_budget_usd": cfg.claude_daily_budget_usd,
        "symbols": {s: {"price": v.price, "tradable": v.tradable, "tech": v.tech.score,
                        "news": v.news.score, "combined": v.combined(cfg),
                        "news_summary": v.news.summary, "news_sentiment": v.news.sentiment,
                        "news_confidence": v.news.confidence, "analyst": v.news.analyst,
                        "rsi": v.tech.rsi, "trend": v.tech.trend, "momentum": v.tech.momentum,
                        "tech_detail": v.tech.describe(), **extra.get(s, {})}
                    for s, v in snaps.items()},
    }
    tmp = LAST_CYCLE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(LAST_CYCLE_FILE)  # atomar, damit die Oberfläche nie eine halbe Datei liest


HEARTBEAT = [time.time()]          # letztes Lebenszeichen der Bot-Schleife (für /api/health und den Wächter)
HANG_SECONDS = 20 * 60             # so lange ohne Lebenszeichen = hängt


def beat() -> None:
    HEARTBEAT[0] = time.time()


def healthy() -> bool:
    return time.time() - HEARTBEAT[0] < HANG_SECONDS


def _watchdog(stop: threading.Event) -> None:
    """Hängt die Bot-Schleife, die hängende Stelle ins Log schreiben und den Prozess beenden.
    Home Assistant startet das Add-on dann neu (watchdog in config.yaml)."""
    import faulthandler
    import os
    import sys
    while not stop.wait(60):
        if healthy():
            continue
        log.error("Bot hängt seit %d Min. – hängende Stelle folgt, danach Neustart", (time.time() - HEARTBEAT[0]) / 60)
        for h in logging.getLogger().handlers:
            h.flush()
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        try:
            with open(STATE_DIR / "bot.log", "a", encoding="utf-8") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
        except OSError:
            pass
        os._exit(3)


_cycle_prices: dict[str, float] = {}   # Kurse des letzten Durchlaufs (für den minütlichen Depotwert)
_last_equity_ts = [0.0]


LEGACY_CLOSE_ID = "2026-11-25-echte-scheine"   # einmalig: alte Positionen ohne echten Schein schließen


def close_legacy_positions(broker: PaperBroker, snaps: dict) -> int:
    """Einmalig nach dem Umstieg auf echte Knock-out-Scheine: alle Positionen ohne Schein zum aktuellen Kurs schließen."""
    marker = STATE_DIR / "zocker_legacy_closed.txt"
    try:
        if marker.read_text().strip() == LEGACY_CLOSE_ID:
            return 0
    except FileNotFoundError:
        pass
    n = 0
    for sym, pos in list(broker.positions.items()):
        snap = snaps.get(sym)
        if pos.get("product") or not snap:
            continue
        n += broker.close(sym, snap.price, f"Umstellung auf echte Knock-out-Scheine – alte {pos.get('leverage', 1):g}x-Position "
                                           "geschlossen, Gewinn/Verlust realisiert")
    if all(pos.get("product") for pos in broker.positions.values()):   # erst als erledigt merken, wenn alle zu sind
        marker.write_text(LEGACY_CLOSE_ID)
    if n:
        log.info("Umstellung: %d alte Zocker-Positionen geschlossen", n)
    return n


def run_once(cfg: Config, broker: PaperBroker, analyst: NewsAnalyst) -> None:
    from . import portfolio as pf
    model_held = {s for s, h in pf.compute_holdings(pf.load_txns("model")).items() if h.qty > 0}
    # Claude liest zuerst die Nachrichten zu allem, was in einem der Depots liegt
    snaps, extra = collect(cfg, analyst, set(broker.positions) | model_held)
    if not snaps:
        log.warning("Keine Daten erhalten – Netzwerk/Yahoo prüfen")
        return
    for s in snaps.values():
        broker.note_benchmark(s.symbol, s.price)
    _cycle_prices.update({s: v.price for s, v in snaps.items()})
    from . import learning
    lrn = learning.learner()
    broker.cfg = learning.zocker_cfg(cfg)     # Zocker-Profil + Gelerntes (nur für den Zocker)
    broker.on_close = lrn.record
    if broker.cfg.zocker_strategy == "momentum":
        from . import momentum
        momentum.start_checks(broker.cfg, analyst, set(broker.positions))   # Kauf-Gutachten im Hintergrund
        momentum.run(broker, broker.cfg, snaps)
    else:
        close_legacy_positions(broker, snaps)
        run_strategy(broker, broker.cfg, snaps, claude_active=analyst.status() == "claude", learner=lrn)
    broker.record_equity({s: v.price for s, v in snaps.items()})
    broker.record_levels()
    broker.save()
    save_cycle(cfg, snaps, extra, analyst)
    print_cycle(cfg, broker, snaps, analyst)


def _live_prices(cfg: Config, syms: list[str]) -> dict[str, float]:
    """Echtzeitkurse (nur handelbare) für die Stop-Prüfung des Musterdepots."""
    from .portfolio_service import live_price
    out = {}
    for sym in syms:
        price, _, tradable = live_price(cfg, sym)
        if price and tradable:
            out[sym] = price
    return out


def risk_tick(cfg: Config, broker: PaperBroker) -> bool:
    """Zwischen den Durchläufen: Echtzeitkurse der offenen Positionen holen, Hoch/Tief nachführen, Stops prüfen.

    Gibt True zurück, wenn sich eine Position rasant bewegt -> sofort einen vollen Durchlauf (Nachrichten neu bewerten).
    """
    prices = {}
    for sym in list(broker.positions):
        q = get_live_quote(cfg, sym)
        if q is not None and q.tradable:
            prices[sym] = q.price
            note_price(sym, q.price)
    if not prices:
        return False
    broker.update_extremes(prices)
    closed = check_risk(broker, broker.cfg, prices)
    if time.time() - _last_equity_ts[0] >= 60:  # Depotwert jede Minute für die Grafik
        _last_equity_ts[0] = time.time()
        broker.record_equity({**_cycle_prices, **prices})
        broker.save()
    elif closed == 0:
        broker.save()  # neue Hochs/Tiefs (Trailing-Stop) sichern; close() speichert selbst
    return bool(rapid_moves(cfg, [s for s in prices if s in broker.positions]))


# --------------------------------------------------------------------------- Sperre
def running_pid() -> int | None:
    """PID eines laufenden Bots, sonst None (veraltete Sperrdateien werden ignoriert)."""
    try:
        pid = int(LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None
    except PermissionError:
        return pid


def other_bot_pid() -> int | None:
    """PID eines *anderen* laufenden Bots. Der eigene Prozess zählt nicht – wichtig im Container, wo nach einem
    Neustart wieder PID 1 vergeben wird und eine alte Sperrdatei sonst wie ein fremder Bot aussähe."""
    pid = running_pid()
    return pid if pid and pid != os.getpid() else None


def _acquire_lock() -> None:
    other = other_bot_pid()
    if other:
        raise SystemExit(f"Der Bot läuft bereits (Prozess {other}). Zwei Bots würden sich das Depot zerschießen.")
    STATE_DIR.mkdir(exist_ok=True)
    LOCK_FILE.write_text(str(os.getpid()))


def _release_lock() -> None:
    if running_pid() == os.getpid():
        LOCK_FILE.unlink(missing_ok=True)


def _register_booked_symbols(cfg: Config) -> None:
    """ISIN und Namen von Werten aus deinen Buchungen bekannt machen (Echtzeitkurs + Nachrichtensuche)."""
    from . import portfolio as pf
    from .portfolio_service import meta_for
    txns = pf.load_txns("model") + pf.load_txns("real")
    for t in txns:
        if t.isin:
            cfg.isins.setdefault(t.symbol, t.isin)
    missing = sorted({t.symbol for t in txns if t.symbol not in cfg.names})
    if missing:
        for sym, m in meta_for(cfg, missing).items():
            if m.get("name") and m["name"] != sym:
                cfg.names[sym] = m["name"]


def run_forever(cfg: Config, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    _acquire_lock()
    try:
        _register_booked_symbols(cfg)
    except Exception:
        log.exception("Werte aus Buchungen konnten nicht registriert werden")
    try:
        from . import learning
        broker = PaperBroker(cfg)
        broker.cfg = learning.zocker_cfg(cfg)
        broker.on_close = learning.learner().record
        analyst = NewsAnalyst(cfg)
        from . import spekulation
        spekulation.ANALYST = analyst
        from . import manager as _mgr
        _mgr.ANALYST = analyst            # KI-Prüfung der Depot-Vorschläge (Gemini vor, Claude entscheidet)
        from . import manager as _m
        times = _m.decision_times(cfg, _m.load_state())
        log.info("Bot läuft – Kurse & Technik alle %.0f Min., Nachrichten/Claude stündlich, Portfolio-Prüfung Mo–Fr %s",
                 cfg.loop_minutes, f"stündlich {times[0]}–{times[-1]} Uhr" if len(times) >= 10 else ", ".join(times) + " Uhr")
        beat()
        threading.Thread(target=_watchdog, args=(stop,), daemon=True, name="watchdog").start()
        while not stop.is_set():
            started = time.time()
            beat()
            try:
                run_once(cfg, broker, analyst)
            except Exception:
                log.exception("Fehler im Durchlauf – nächster Versuch beim nächsten Intervall")
            try:
                from .mydepot import maybe_scheduled_report
                maybe_scheduled_report(cfg)
            except Exception:
                log.exception("Fehler bei den Signalen für dein Depot")
            try:  # Musterdepot: rasante Bewegung bei einer Position + schlechtes Signal -> sofort verkaufen
                from . import manager
                from .mydepot import notify
                manager.on_rapid(cfg, notify, cfg.names)
            except Exception:
                log.exception("Fehler beim Portfoliomanager")
            try:  # „Jetzt prüfen“ aus der Oberfläche
                from . import manager
                from .mydepot import notify
                manager.run_requested_check(cfg, notify, cfg.names, lambda syms: _live_prices(cfg, syms))
            except Exception:
                log.exception("Fehler bei „Jetzt prüfen“")
            # Bis zum nächsten vollen Durchlauf: offene Positionen laufend mit Echtzeitkursen absichern
            next_cycle = started + cfg.loop_minutes * 60
            last_watch = 0.0
            while not stop.is_set() and time.time() < next_cycle:
                beat()
                if WAKE.is_set():          # „Jetzt prüfen“ -> Durchlauf vorziehen
                    WAKE.clear()
                    log.info("Prüfung angefordert – Durchlauf wird vorgezogen")
                    break
                if time.time() - last_watch >= 60:   # dein echtes Depot: Alarme + Abendbericht
                    last_watch = time.time()
                    try:
                        from . import portfolio_service as ps
                        from .mydepot import notify
                        from . import manager, reports, traderepublic
                        ps.watch(cfg, notify)                     # Mein Depot: Alarme
                        manager.tick(cfg, notify, cfg.names,      # Musterdepot: Stops + feste Termine
                                     lambda syms: _live_prices(cfg, syms))
                        manager.record_value(cfg)                 # Wertverlauf alle 5 Min. (für 1T/1W)
                        reports.maybe_send(cfg, notify)           # Abend-/Wochenbericht
                        traderepublic.maybe_sync(cfg, notify)     # Trade Republic: alle 30 Min. abgleichen
                        from . import spekulation
                        spekulation.tick(cfg, notify, analyst)    # Spekulativer Hebel-Vorschlag (wenn eingeschaltet)
                        from . import review
                        review.tick(cfg, analyst, notify)         # Mo–Fr 8:30: KI-Durchsicht des ganzen Depots
                        from . import pf_learning
                        pf_learning.tick(cfg)                     # abends: Empfehlungen auswerten (Lernen)
                    except Exception:
                        log.exception("Fehler bei der Depot-Überwachung")
                WAKE.wait(max(1.0, min(cfg.risk_check_seconds, next_cycle - time.time())))
                if stop.is_set():
                    break
                if broker.positions and not stop.is_set():
                    try:
                        if risk_tick(cfg, broker) and time.time() - started > 120:
                            log.info("⚡ Rasante Bewegung in einer offenen Position – Durchlauf wird vorgezogen")
                            break
                    except Exception:
                        log.exception("Fehler bei der Stop-Überwachung")
    finally:
        _release_lock()
