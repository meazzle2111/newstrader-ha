"""Aufruf:  python -m newstrader [gui|run|once|status|backtest|reset|init-config]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys

from .config import Config, STATE_DIR, CONFIG_FILE, ROOT


def load_dotenv() -> None:
    """Liest KEY=WERT-Zeilen aus NewsTrader/.env (z. B. ANTHROPIC_API_KEY), ohne Umgebung zu überschreiben."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def setup_logging(verbose: bool) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%d.%m. %H:%M:%S")
    root = logging.getLogger()
    for h in list(root.handlers):  # nicht doppelt ausgeben, falls eine Bibliothek schon einen Handler gesetzt hat
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    from logging.handlers import RotatingFileHandler
    from . import logbuf
    logbuf.prefill(STATE_DIR / "bot.log")   # Log-Seite nach einem Neustart nicht leer
    file = RotatingFileHandler(STATE_DIR / "bot.log", maxBytes=5_000_000, backupCount=2, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s", "%d.%m. %H:%M:%S"))
    root.addHandler(console)
    root.addHandler(file)
    root.addHandler(logbuf.RingHandler())
    for noisy in ("yfinance", "httpx", "httpx2", "anthropic", "urllib3", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # „possibly delisted“ bei jedem Abruf – fehlende Kurse fangen wir selbst ab


def status(cfg: Config) -> None:
    pf = STATE_DIR / "portfolio.json"
    last = STATE_DIR / "last_cycle.json"
    if not pf.exists():
        print("Noch kein Depot – erst 'run' oder 'once' starten.")
        return
    state = json.loads(pf.read_text())
    cycle = json.loads(last.read_text()) if last.exists() else {"symbols": {}, "time": "-"}
    prices = {s: v["price"] for s, v in cycle["symbols"].items()}
    from .broker import PaperBroker
    for p in state["positions"].values():
        p.setdefault("side", "long"); p.setdefault("leverage", 1.0); p.setdefault("margin", p["qty"] * p["avg_price"])
    eq = state["cash"] + sum(PaperBroker.position_value(p, prices.get(s, p["avg_price"]))
                             for s, p in state["positions"].items())
    start = state["start_capital"]
    c = {"EUR": "€", "USD": "$"}.get(state.get("currency", "USD"), state.get("currency"))
    rets = [prices[s] / p0 - 1 for s, p0 in state["benchmark_start"].items() if s in prices]
    print(f"Depot seit {state['created'][:16]}, letzter Durchlauf {cycle['time'][:16]} (UTC)")
    print(f"  Depotwert:   {eq:,.2f} {c}  ({eq / start - 1:+.2%})")
    if rets:
        print(f"  Buy & Hold:  {sum(rets) / len(rets):+.2%} (gleichgewichtet, gleiche Startzeit)")
    print(f"  Cash:        {state['cash']:,.2f} {c}   realisiert: {state['realized_pnl']:+,.2f} {c}   Gebühren: {state['fees_paid']:,.2f} {c}")
    for s, p in state["positions"].items():
        px = prices.get(s, p["avg_price"])
        pnl = PaperBroker.pnl(p, px)
        print(f"  📦 {s:<8} {p['side']:<5} {p['leverage']:g}x  {p['qty']:.4f} Stk  Einstand {p['avg_price']:.2f} {c}  "
              f"jetzt {px:.2f} {c}  G/V {pnl:+.2f} {c} ({pnl / p['margin']:+.1%} auf die Margin)")


def migrate_config() -> None:
    """Gespeicherte Einstellungen auf neue Standardwerte heben (nur Werte, die sich mit einer Version geändert haben)."""
    if not CONFIG_FILE.exists():
        return
    data = json.loads(CONFIG_FILE.read_text())
    if data.get("config_version", 1) < 2:
        data.update({"claude_min_minutes": 60.0, "news_refresh_minutes": 60.0, "claude_cli_max_calls_per_day": 200,
                     "my_signal_times": [], "config_version": 2})
        CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("Einstellungen aktualisiert: Nachrichten stündlich")
    if data.get("config_version", 1) < 3:  # Claude-Abo schonen: weniger Aufrufe
        data.update({"claude_cli_max_calls_per_day": 120, "claude_top_n": 5, "config_version": 3})
        CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print("Einstellungen aktualisiert: weniger Claude-Aufrufe")


# Ändert sich diese Kennung, startet das Zockerkonto (Spielgeld) beim nächsten Start einmalig neu.
# Das echte Portfolio, Trade Republic und die Einstellungen bleiben unberührt.
ZOCKER_RESET_ID = "2026-11-25-momentum-nachrichten-gestreut"
ZOCKER_FILES = ("portfolio.json", "trades.csv", "trades_alt.csv", "levels.json", "zocker_learning.json", "momentum.json")


def zocker_fresh_start() -> bool:
    marker = STATE_DIR / "zocker_reset.txt"
    try:
        if marker.read_text().strip() == ZOCKER_RESET_ID:
            return False
    except FileNotFoundError:
        pass
    from datetime import datetime as _dt
    dst = STATE_DIR / "archiv" / f"zocker-{_dt.now().strftime('%Y%m%d-%H%M%S')}"
    moved = []
    for name in ZOCKER_FILES:
        src = STATE_DIR / name
        if src.exists():
            dst.mkdir(parents=True, exist_ok=True)
            src.rename(dst / name)
            moved.append(name)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker.write_text(ZOCKER_RESET_ID)
    print(f"Zockerkonto neu gestartet ({ZOCKER_RESET_ID}); alter Stand archiviert: {', '.join(moved) or '–'}")
    return True


def run_addon() -> None:
    """Start als Home-Assistant-Add-on: Optionen aus /data/options.json, Oberfläche über Ingress (Port 8099)."""
    import shutil
    from pathlib import Path

    data = Path(os.environ.get("NEWSTRADER_HOME", "/data"))
    (data / "state" / "bot.pid").unlink(missing_ok=True)  # frischer Container: alte Sperre gilt nicht mehr
    opts = {}
    try:
        opts = json.loads((data / "options.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if opts.get("claude_token"):
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = opts["claude_token"]
    if opts.get("gemini_api_key"):
        os.environ["GEMINI_API_KEY"] = opts["gemini_api_key"]
    # Frischer Start auf Wunsch: /share/newstrader/RESET anlegen -> alter Stand wird archiviert (nicht gelöscht)
    reset_flag = Path("/share/newstrader/RESET")
    if reset_flag.exists():
        from datetime import datetime as _dt
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in ("state", "config.json"):
            src = data / item
            if src.exists():
                src.rename(data / f"archiv-{stamp}-{item}")
        imported_old = Path("/share/newstrader/state")
        if imported_old.exists():
            imported_old.rename(Path(f"/share/newstrader/state-archiv-{stamp}"))
        reset_flag.unlink()  # genau einmal
        print(f"Frischer Start: alter Stand archiviert ({stamp})")
    # Erster Start: Einstellungen aus dem Paket übernehmen, Zustand vom Mac (über /share hochgeladen) übernehmen
    # Umzug vom alten (lokalen) Add-on: dessen Export unter /share übernehmen – genau einmal, nur in ein leeres Add-on
    move = Path(os.environ.get("NEWSTRADER_SHARE", "/share")) / "newstrader" / "umzug"
    empty = not (STATE_DIR / "model.json").exists() and not (STATE_DIR / "portfolio.json").exists()
    if move.exists() and empty:
        from datetime import datetime as _dt
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copytree(move / "state", STATE_DIR, dirs_exist_ok=True)
        (STATE_DIR / "bot.pid").unlink(missing_ok=True)
        if (move / "config.json").exists():
            shutil.copy(move / "config.json", CONFIG_FILE)
        move.rename(move.with_name(f"umzug-uebernommen-{_dt.now().strftime('%Y%m%d-%H%M%S')}"))
        print("Umzug: Zustand und Einstellungen vom alten Add-on übernommen")
    if not CONFIG_FILE.exists():
        shutil.copy(Path(__file__).parent / "defaults.json", CONFIG_FILE)
    imported = Path("/share/newstrader/state")
    if imported.exists() and not (STATE_DIR / "portfolio.json").exists():
        shutil.copytree(imported, STATE_DIR, dirs_exist_ok=True)
        (STATE_DIR / "bot.pid").unlink(missing_ok=True)
        print("Zustand vom Mac übernommen:", imported)
    migrate_config()
    zocker_fresh_start()
    cfg = Config.load()
    if opts.get("notify_service"):
        cfg.ha_notify_service = opts["notify_service"]
    if opts.get("trading_mode") in ("cash", "leverage"):
        cfg.trading_mode = opts["trading_mode"]
    if not opts.get("claude_token") and cfg.analyst == "claude-cli":
        print("Kein Claude-Token eingetragen – " + ("nur Gemini." if opts.get("gemini_api_key") else "Nachrichten werden mit dem Wörterbuch bewertet."))
        cfg.analyst = "lexicon"
    setup_logging(False)
    from .dashboard import serve
    serve(cfg, 8099, with_bot=True, open_browser=False, host="0.0.0.0", fixed_port=True)


def main() -> None:
    ap = argparse.ArgumentParser(prog="newstrader", description="Paper-Trading-Bot (Spielgeld)")
    ap.add_argument("command", nargs="?", default="run",
                    choices=["gui", "addon", "run", "once", "status", "backtest", "reset", "init-config"])
    ap.add_argument("--period", default="2y", help="Zeitraum für backtest, z. B. 1y, 2y, 5y")
    ap.add_argument("--mode", choices=["cash", "leverage"], help="backtest: Handelsmodus überschreiben")
    ap.add_argument("--port", type=int, default=8765, help="Port der Oberfläche (gui)")
    ap.add_argument("--no-bot", action="store_true", help="gui: nur anzeigen, Bot nicht mitstarten")
    ap.add_argument("--no-browser", action="store_true", help="gui: Browser nicht automatisch öffnen")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    migrate_config()
    cfg = Config.load()
    if args.command == "init-config":
        cfg.write_default()
        print(f"Konfiguration geschrieben: {CONFIG_FILE}")
        return
    if args.command == "status":
        return status(cfg)
    if args.command == "reset":
        from .bot import running_pid
        if running_pid():
            raise SystemExit("Der Bot läuft noch – erst beenden, dann zurücksetzen.")
        if STATE_DIR.exists():
            shutil.rmtree(STATE_DIR)
        print("Depot zurückgesetzt.")
        return

    setup_logging(args.verbose)
    from . import bot, backtest  # erst hier importieren (yfinance-Import dauert)
    from .analyst import NewsAnalyst
    from .broker import PaperBroker

    if args.mode:
        cfg.trading_mode = args.mode
    if args.command == "addon":
        return run_addon()
    if args.command == "gui":
        from .dashboard import serve
        serve(cfg, args.port, with_bot=not args.no_bot, open_browser=not args.no_browser)
    elif args.command == "backtest":
        backtest.run_backtest(cfg, args.period)
    elif args.command == "once":
        bot.run_once(cfg, PaperBroker(cfg), NewsAnalyst(cfg))
    else:
        try:
            bot.run_forever(cfg)
        except KeyboardInterrupt:
            print("\nBeendet. Depot ist gespeichert – 'status' zeigt den Stand.")


if __name__ == "__main__":
    main()
