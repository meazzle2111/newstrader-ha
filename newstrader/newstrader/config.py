"""Einstellungen. Werte in config.json (Projektordner) überschreiben die Defaults."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import os

# Im Home-Assistant-Add-on liegen Einstellungen und Zustand unter /data (NEWSTRADER_HOME), sonst im Projektordner.
ROOT = Path(os.environ.get("NEWSTRADER_HOME") or Path(__file__).resolve().parent.parent)
STATE_DIR = ROOT / "state"
CONFIG_FILE = ROOT / "config.json"


@dataclass
class Config:
    # Depotwährung. Kurse in anderen Währungen (z. B. US-Aktien in USD) werden live umgerechnet,
    # so wie bei einem deutschen Broker, der US-Aktien in Euro abrechnet.
    base_currency: str = "EUR"
    # Yahoo-Symbole: ".DE" = Xetra (Deutsche Börse), ohne Endung = US-Börse, "-EUR" = Krypto in Euro
    watchlist: list[str] = field(default_factory=lambda: [
        "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "IFX.DE", "RHM.DE", "MBG.DE", "BAS.DE",
        "AAPL", "NVDA", "MSFT", "BTC-EUR",
    ])
    # Firmennamen für die Suche nach deutschen Nachrichten (Google News)
    names: dict[str, str] = field(default_factory=lambda: {
        "SAP.DE": "SAP", "SIE.DE": "Siemens", "ALV.DE": "Allianz", "DTE.DE": "Deutsche Telekom",
        "IFX.DE": "Infineon", "RHM.DE": "Rheinmetall", "MBG.DE": "Mercedes-Benz", "BAS.DE": "BASF",
        "AAPL": "Apple", "NVDA": "Nvidia", "MSFT": "Microsoft", "BTC-EUR": "Bitcoin",
    })
    german_news: bool = True            # zusätzlich deutschsprachige Schlagzeilen holen
    # Echtzeitkurse: ISIN -> Tradegate (Aktien in Euro, Mo–Fr 8–22 Uhr), Kraken-Paar -> Krypto rund um die Uhr.
    # Werte ohne Eintrag nutzen Yahoo (Xetra ~15 Min. verzögert).
    isins: dict[str, str] = field(default_factory=lambda: {
        "SAP.DE": "DE0007164600", "SIE.DE": "DE0007236101", "ALV.DE": "DE0008404005", "DTE.DE": "DE0005557508",
        "IFX.DE": "DE0006231004", "RHM.DE": "DE0007030009", "MBG.DE": "DE0007100000", "BAS.DE": "DE000BASF111",
        "AAPL": "US0378331005", "NVDA": "US67066G1040", "MSFT": "US5949181045",
    })
    kraken_pairs: dict[str, str] = field(default_factory=lambda: {"BTC-EUR": "XBTEUR", "ETH-EUR": "ETHEUR"})
    risk_check_seconds: float = 20.0    # so oft werden Stop-Loss/Margin offener Positionen mit Echtzeitkursen geprüft
    start_capital: float = 10_000.0
    zocker_profile: str = "action"      # Zocker (Spielgeld): "maximal" (ruhig) oder "action" (aktiv, Gewinnsicherung)
    lev_always_max: bool = False        # Hebel immer voll ausreizen statt nach Signalstärke

    # Schleife
    loop_minutes: float = 10.0          # wie oft Markt + News geprüft werden
    news_max_age_hours: float = 48.0    # ältere Schlagzeilen werden ignoriert
    claude_min_minutes: float = 60.0    # pro Wert höchstens stündlich Claude fragen (außer bei Kursstürzen)
    claude_top_n: int = 5               # pro Durchlauf höchstens so viele Kandidaten von Claude bewerten lassen
    claude_parallel: int = 3            # gleichzeitige Claude-Aufrufe
    candidate_fraction: float = 0.6     # Vorfilter: Kandidat, wenn Signal ≥ 60 % der Kauf-/Short-Schwelle
    entry_requires_claude: bool = True  # neue Positionen nur nach Claude-Bewertung (wenn Claude aktiv ist)
    spec_target_leverage: float = 12.0  # Spekulation: angestrebter Hebel des Knock-out-Scheins …
    spec_max_leverage: float = 15.0     # … höchstens
    spec_risk_pct: float = 0.01         # Spekulation: Verlust am Stop höchstens 1 % deines Depots (Einsatzvorschlag)
    gemini_models: list[str] = field(default_factory=lambda: ["gemini-flash-latest", "gemini-2.5-flash",
                                                              "gemini-flash-lite-latest", "gemini-2.5-flash-lite"])
    gemini_max_calls_per_day: int = 240  # kostenlose Stufe: Tageskontingent schonen (Rest übernimmt Claude)
    gemini_min_seconds: float = 6.5      # Mindestabstand zwischen Gemini-Anfragen (kostenlos: ~10 pro Minute)
    gemini_timeout: float = 120.0        # neue Modelle „denken“ vor der Antwort – ausführliche Analysen dauern
    claude_new_headlines: int = 2       # Claude erst wieder fragen, wenn mind. so viele neue Meldungen da sind …
    claude_max_age_hours: float = 6.0   # … oder die letzte Bewertung älter ist
    lev_needs_claude: bool = True       # Hebel nur, wenn Claude die Nachrichtenlage geprüft hat
    entry_requires_news: bool = True    # Einstieg erst, wenn die Schlagzeilen geladen sind
    rotate_gap: float = 0.0             # Zocker: schwächste Position gegen ein um so viel stärkeres Signal tauschen (0 = aus)
    take_profit_pct: float = 0.0        # Zocker: Gewinn mitnehmen ab so viel Kursbewegung (0 = aus)
    max_hold_days: float = 0.0          # Zocker: nach so vielen Tagen schließen, wenn nichts passiert (0 = aus)
    lock_after_pct: float = 0.0         # Gewinnsicherung: ab so viel Kursbewegung im Plus …
    lock_trailing_pct: float = 0.0      # … wird der Trailing-Stop so eng nachgezogen (0 = aus)
    max_margin_factor: float = 0.0      # Positionen mit mehr als Faktor × Zielgröße verkleinern/schließen (0 = aus)
    real_products: bool = False         # Zocker: echte TR-Knock-out-Scheine statt abstraktem Hebel
    product_target_leverage: float = 12.0
    product_min_leverage: float = 6.0
    product_max_leverage: float = 15.0
    product_stop_share: float = 0.6     # Stop bei höchstens 60 % des Abstands zur Knock-out-Schwelle
    product_searches_per_cycle: int = 4
    news_refresh_minutes: float = 60.0  # Schlagzeilen pro Wert höchstens so oft neu laden (stündlich) …
    rapid_move_pct: float = 0.02        # „rasante Kursänderung": ≥ 2 % …
    rapid_window_minutes: float = 15.0  # … innerhalb von 15 Minuten -> Schlagzeilen + Claude sofort neu
    news_per_cycle: int = 20            # … und pro Durchlauf höchstens so viele Werte (Rest rotiert)
    news_top_n: int = 50                # Live-Kurse + Nachrichten nur für die N deutlichsten Signale (+ alles im Depot)
    fetch_workers: int = 4              # parallele Kurs-/Nachrichtenabrufe

    # Nachrichten-Analyse: "auto" = Claude, wenn ein API-Key gesetzt ist, sonst lokales Wörterbuch
    # "claude-cli" = über die Claude-Code-Kommandozeile mit deinem Claude-Abo (kein API-Guthaben nötig)
    analyst: str = "auto"               # "auto" | "claude" | "claude-cli" | "lexicon"
    claude_cli_path: str = "~/.local/bin/claude"
    claude_cli_model: str = ""          # leer = Standardmodell deines Abos, sonst z. B. "sonnet" oder "opus"
    claude_cli_max_calls_per_day: int = 120  # schont dein Abo-Kontingent (Chat + Claude Code teilen es)
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "low"          # low | medium | high – Klassifikation braucht wenig
    claude_daily_budget_usd: float = 3.0  # Tageslimit für API-Kosten; danach Wörterbuch bis Mitternacht

    # Gewichtung der Signale (Summe muss nicht 1 sein)
    weight_technical: float = 0.5
    weight_news: float = 0.5

    # Handelsmodus: "cash" = normales Depot (nur Long, kein Hebel)
    #               "leverage" = Margin-Konto wie bei CFDs: Long und Short, Hebel je nach Signalstärke
    trading_mode: str = "cash"
    allow_short: bool = True            # nur im Hebelmodus
    max_leverage_stocks: float = 20.0   # Zocker: bewusst über der EU-Privatkundengrenze (5:1) – nur Spielgeld
    max_leverage_crypto: float = 10.0   # Zocker: Krypto bis 10x (EU-Grenze wäre 2:1)
    max_gross_exposure: float = 8.0     # Summe aller Positionsvolumen ≤ 8 × Depotwert
    margin_closeout_pct: float = 0.50   # Zwangsschließung, wenn 50 % der Margin verloren sind (EU-Regel)
    financing_pct_pa: float = 0.06      # Zinsen p. a. auf den geliehenen Teil (Volumen − Margin)
    short_borrow_pct_pa: float = 0.01   # Leihgebühr p. a. aufs Short-Volumen
    short_threshold: float = -0.30      # Gesamtsignal darunter -> Short eröffnen
    cover_threshold: float = 0.10       # Gesamtsignal darüber -> Short eindecken

    # Tab „Mein Depot": Signale für dein echtes Depot (nur Long, ohne Hebel), Mo–Fr zu diesen Uhrzeiten (Berlin)
    my_signal_times: list[str] = field(default_factory=list)  # alt; Empfehlungen kommen jetzt vom Portfoliomanager
    my_max_buys: int = 5                # höchstens so viele Kaufsignale pro Termin
    my_notify: bool = True              # Mitteilung bei neuen Signalen (Mac bzw. Home Assistant)
    ha_notify_service: str = ""         # im Add-on: z. B. "mobile_app_iphone" -> Push aufs Handy über die HA-App
    my_alerts_enabled: bool = True      # Push-Alarme für dein echtes Depot
    my_alert_stop_pct: float = 0.10     # Alarm, wenn ein Wert 10 % unter deinen Einstand fällt …
    my_alert_trailing_pct: float = 0.15 # … oder 15 % unter sein Hoch seit dem Kauf
    my_alert_move_pct: float = 0.05     # Alarm bei ±5 % an einem Tag
    my_daily_report_time: str = "22:15" # Abendbericht Mo–Fr (leer = aus)
    my_benchmarks: dict[str, str] = field(default_factory=lambda: {"DAX": "^GDAXI", "MSCI World": "EUNL.DE"})
    # Portfoliomanager (Musterdepot): Käufe/Verkäufe nur zu diesen Zeiten (Mo–Fr), Stop-Loss jederzeit
    pm_decision_times: list[str] = field(default_factory=lambda: ["09:35", "13:00", "16:00"])
    config_version: int = 3

    portfolio_size: int = 10            # Startportfolio: so viele Werte (weniger, wenn zu wenige gute Signale)
    portfolio_min_score: float = 0.15   # nur Werte mit Gesamtsignal darüber kommen ins Startportfolio
    portfolio_cash_pct: float = 0.10    # so viel bleibt im Startportfolio als Cash
    portfolio_max_crypto_pct: float = 0.10  # Krypto zusammen höchstens 10 %

    # Handelsregeln
    buy_threshold: float = 0.25         # Gesamtsignal über diesem Wert -> kaufen
    sell_threshold: float = -0.15       # Gesamtsignal unter diesem Wert -> verkaufen
    max_position_pct: float = 0.15      # max. Anteil vom Depotwert pro Position
    min_cash_reserve_pct: float = 0.10  # so viel Cash bleibt immer liegen
    stop_loss_pct: float = 0.06         # fester Stop-Loss unter Einstand
    trailing_stop_pct: float = 0.10     # Trailing-Stop unter Hoch seit Kauf
    min_hold_minutes: float = 60.0      # nach Kauf nicht sofort wieder per Signal verkaufen
    max_quote_age_minutes: float = 30.0 # ältere Kurse = Markt zu -> nicht handeln

    # Kosten der Simulation
    fee_fixed: float = 1.0              # in Depotwährung pro Order
    fee_pct: float = 0.001              # 0,1 % vom Ordervolumen
    slippage_pct: float = 0.0005        # 0,05 % schlechterer Ausführungskurs

    def zocker(self) -> "Config":
        """Einstellungen nur für den Zocker (das echte Portfolio nutzt weiter die normalen Werte).
        Das in der Oberfläche gewählte Profil (state/zocker_profile.txt) hat Vorrang vor config.json."""
        from dataclasses import replace
        name = self.zocker_profile
        try:
            chosen = (STATE_DIR / "zocker_profile.txt").read_text().strip()
            if chosen in ZOCKER_PROFILES:
                name = chosen
        except OSError:
            pass
        return replace(self, zocker_profile=name, **ZOCKER_PROFILES.get(name, {}))

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text())
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
                else:
                    print(f"Warnung: unbekannte Einstellung '{key}' in config.json ignoriert")
        return cfg

    def write_default(self) -> None:
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2) + "\n")


# Zocker-Profile. „maximal“: so viel Gewinn wie möglich – volles Risiko, Spielgeld.
ZOCKER_PROFILES: dict[str, dict] = {
    "standard": {},
    "maximal": {
        # Backtest Jan–Nov 2026 (228 Werte, nur Technik): +84 %, max. Rückgang −40 %, 62 Trades.
        # 20x Hebel: −99 % (zu viele Margin-Calls). Umschichten auf stärkere Signale: deutlich schlechter (zu viel Hin und Her).
        # Achtung: Nachbarwerte der Einstellungen schwanken stark – das Ergebnis ist zu einem guten Teil Zufall.
        "lev_always_max": True,         # immer den vollen Hebel …
        "max_leverage_stocks": 5.0,     # … und der liegt bei 5x
        "max_leverage_crypto": 5.0,
        "max_gross_exposure": 12.5,     # Gesamtvolumen bis 12,5x Depotwert
        "max_position_pct": 0.20,       # bis 20 % des Depots als Margin je Position (mehr Positionen gleichzeitig)
        "min_cash_reserve_pct": 0.0,    # keine Cash-Reserve
        "buy_threshold": 0.12,          # früh einsteigen …
        "short_threshold": -0.12,
        "sell_threshold": -0.05,        # … und raus, sobald das Signal kippt
        "cover_threshold": 0.05,
        "stop_loss_pct": 0.10,          # 10 % gegen die Position
        "trailing_stop_pct": 0.20,      # Gewinne laufen lassen: erst 20 % Rücklauf vom besten Kurs sichern
        "min_hold_minutes": 10.0,
        # „mehr zocken“: früher rein, mehr Positionen gleichzeitig, umschichten, nicht auf Claude warten
        "entry_requires_claude": False,
        "entry_requires_news": False,
        "lev_needs_claude": False,
        "rotate_gap": 0.0,              # Umschichten aus: kostete im Backtest viel Rendite
    },
}
# „action“: Gewinne regelmäßig sichern – ab +4 % Kursplus Trailing-Stop eng auf 3 %. Im Backtest 227 Trades, +11 %
# (feste Gewinnmitnahme/Haltedauer: −52 %; „ruhig“ mit 20 % Trailing: +84 %, aber nur 62 Trades).
ZOCKER_PROFILES["action"] = {**ZOCKER_PROFILES["maximal"], "lock_after_pct": 0.04, "lock_trailing_pct": 0.03,
                             "max_position_pct": 0.10,      # kleine Positionen: fast immer Geld für neue Ideen
                             "max_margin_factor": 1.5,      # Altlasten > 15 % Einsatz werden geschlossen
                             "real_products": True}         # echte TR-Knock-outs (Hebel ~12, max. 15)
ZOCKER_PROFILES["maximal"]["real_products"] = True
