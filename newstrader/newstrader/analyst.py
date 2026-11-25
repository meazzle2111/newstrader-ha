"""Nachrichten-Interpretation: Claude (wenn API-Key vorhanden) oder lokales Wörterbuch."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from dataclasses import dataclass, asdict

from .config import Config, STATE_DIR
from .news import Headline

log = logging.getLogger(__name__)

CACHE_FILE = STATE_DIR / "news_cache.json"
USAGE_FILE = STATE_DIR / "claude_usage.json"

# USD pro Mio. Tokens (Eingabe, Ausgabe) – für die Kostenschätzung und das Tageslimit
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


@dataclass
class NewsSignal:
    score: float        # -1 .. +1, bereits mit Konfidenz gewichtet
    sentiment: float
    confidence: float
    summary: str
    analyst: str        # "claude" (= von einer KI geprüft) | "lexicon" | "none"
    n_headlines: int
    provider: str = ""  # welche KI: "gemini" | "claude"

    def describe(self) -> str:
        return f"news={self.score:+.2f} [{self.analyst}, {self.n_headlines} Meldungen] {self.summary}"


NEUTRAL = NewsSignal(0.0, 0.0, 0.0, "keine aktuellen Meldungen", "none", 0)


# --------------------------------------------------------------------------- lokal
POSITIVE = {
    "beat": 2, "beats": 2, "surge": 2, "surges": 2, "soar": 2, "soars": 2, "record": 1.5,
    "upgrade": 2, "upgraded": 2, "outperform": 1.5, "rally": 1.5, "rallies": 1.5, "jump": 1.5,
    "jumps": 1.5, "gain": 1, "gains": 1, "rise": 1, "rises": 1, "strong": 1, "growth": 1,
    "profit": 1, "bullish": 2, "buy": 1, "raises": 1.5, "raised": 1, "approval": 1.5,
    "approved": 1.5, "partnership": 1, "deal": 0.5, "breakthrough": 1.5, "tops": 1.5,
    "higher": 0.5, "boost": 1, "boosts": 1, "expands": 0.5, "wins": 1, "optimistic": 1,
}
NEGATIVE = {
    "miss": 2, "misses": 2, "plunge": 2, "plunges": 2, "crash": 2.5, "tumble": 2, "tumbles": 2,
    "downgrade": 2, "downgraded": 2, "underperform": 1.5, "sell-off": 2, "selloff": 2,
    "drop": 1.5, "drops": 1.5, "fall": 1, "falls": 1, "slump": 2, "weak": 1, "loss": 1,
    "losses": 1, "bearish": 2, "lawsuit": 1.5, "probe": 1.5, "investigation": 1.5,
    "recall": 1.5, "fraud": 2.5, "cuts": 1, "layoffs": 1, "warning": 1.5, "warns": 1.5,
    "curbs": 1.5, "ban": 1.5, "tariff": 1, "tariffs": 1, "lower": 0.5, "fears": 1,
    "concerns": 1, "delay": 1, "delays": 1, "halt": 1.5, "fine": 1, "fined": 1.5, "sinks": 2,
}
# Deutsche Begriffe (Google-News-Schlagzeilen)
POSITIVE.update({
    "steigt": 1, "steigen": 1, "springt": 1.5, "klettert": 1, "rekord": 1.5, "rekordhoch": 2,
    "gewinnt": 1, "hochgestuft": 2, "kaufempfehlung": 2, "übertrifft": 2, "starke": 1,
    "stark": 1, "wachstum": 1, "erholung": 1, "erholt": 1, "großauftrag": 2, "auftrag": 1,
    "angehoben": 1.5, "anhebung": 1, "optimistisch": 1, "zuversichtlich": 1, "durchbruch": 1.5,
    "kursziel-erhöhung": 1.5, "kaufen": 1, "aufwärts": 1, "höhenflug": 1.5, "boom": 1.5,
})
NEGATIVE.update({
    "fällt": 1, "fallen": 1, "sinkt": 1.5, "verliert": 1.5, "verlust": 1, "verluste": 1,
    "abverkauf": 2, "einbruch": 2, "bricht": 2, "absturz": 2.5, "herabgestuft": 2, "verkaufen": 1,
    "verfehlt": 2, "schwach": 1, "schwache": 1, "warnung": 1.5, "warnt": 1.5, "gewinnwarnung": 2.5,
    "senkt": 1, "gesenkt": 1, "klage": 1.5, "ermittlungen": 1.5, "stellenabbau": 1, "zölle": 1,
    "sorgen": 1, "angst": 1, "gedrückt": 1, "rückgang": 1, "kursrutsch": 2, "talfahrt": 2,
    "abwärts": 1, "krise": 1.5, "pleite": 2.5, "insolvenz": 2.5,
})
_WORD = re.compile(r"[a-zäöüß\-]+")


def lexicon_signal(headlines: list[Headline]) -> NewsSignal:
    if not headlines:
        return NEUTRAL
    total, weight_sum, hits = 0.0, 0.0, 0
    for h in headlines:
        words = _WORD.findall(f"{h.title} {h.title} {h.summary}".lower())  # Titel doppelt gewichtet
        pos = sum(POSITIVE.get(w, 0) for w in words)
        neg = sum(NEGATIVE.get(w, 0) for w in words)
        if pos == neg == 0:
            continue
        hits += 1
        recency = math.exp(-h.age_hours() / 24)  # Halbwertszeit ~17 h
        total += recency * (pos - neg) / (pos + neg)
        weight_sum += recency
    if weight_sum == 0:
        return NewsSignal(0.0, 0.0, 0.1, "Meldungen ohne klare Tendenz", "lexicon", len(headlines))
    sentiment = total / weight_sum
    confidence = min(0.6, 0.15 + 0.05 * hits)  # Wörterbuch ist grob -> Konfidenz gedeckelt
    return NewsSignal(sentiment * confidence, sentiment, confidence,
                      f"{hits} von {len(headlines)} Meldungen mit Tendenz", "lexicon", len(headlines))


# --------------------------------------------------------------------------- Claude
SYSTEM_PROMPT = """Du bist Nachrichten-Analyst in einem Paper-Trading-Experiment (Spielgeld, keine echten Orders).
Du bekommst aktuelle Schlagzeilen (deutsch und englisch) zu genau einem Wertpapier sowie ein paar Kursdaten.
Schätze ein, wie sich die Nachrichtenlage voraussichtlich auf den Kurs in den nächsten 1-5 Handelstagen auswirkt.

Regeln:
- sentiment: -1 (sehr negativ) bis +1 (sehr positiv). 0 = neutral oder gemischt.
- confidence: 0 bis 1. Niedrig, wenn Meldungen nur am Rande mit dem Wertpapier zu tun haben,
  alt, Meinungsartikel/Clickbait ("Should you buy...?") oder bereits im Kurs eingepreist sind.
- Mehrere Portale berichten oft über dasselbe Ereignis – zähle das nicht mehrfach.
- Viele deutsche Börsenportale schreiben reißerisch ("Jetzt kaufen?", "Kurs-Explosion?"); gewichte solche
  Titel schwach und achte auf harte Fakten (Zahlen, Prognosen, Aufträge, Analysten-Einstufungen).
- Berücksichtige, dass bekannte Nachrichten oft schon eingepreist sind; ein starker Kursanstieg
  bei guten Nachrichten lässt weniger Spielraum.
- summary: ein kurzer deutscher Satz mit dem wichtigsten Treiber.
Antworte nur im vorgegebenen JSON-Format."""

SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "number"},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "key_drivers": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sentiment", "confidence", "summary", "key_drivers"],
    "additionalProperties": False,
}


def _has_claude_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


class NewsAnalyst:
    """Merkt sich Ergebnisse pro Ticker und fragt Claude nur bei neuen Schlagzeilen erneut."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = None
        self.cache: dict = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
        self.backend = None  # "api" | "cli"
        self.cli = None
        want_api = cfg.analyst == "claude" or (cfg.analyst == "auto" and _has_claude_credentials())
        if cfg.analyst == "claude-cli":
            cli = Path(os.path.expanduser(cfg.claude_cli_path))
            self.cli = str(cli) if cli.exists() else shutil.which("claude")
            if self.cli:
                self.backend = "cli"
                self.client = True  # Marker: Claude aktiv
                log.info("Nachrichten-Analyse: Claude über Kommandozeile (Abo), Modell %s, max. %d Aufrufe/Tag",
                         cfg.claude_cli_model or "Standard", cfg.claude_cli_max_calls_per_day)
            else:
                log.error("Claude-Kommandozeile nicht gefunden (%s) – nutze Wörterbuch", cfg.claude_cli_path)
        elif want_api:
            import anthropic
            self.client = anthropic.Anthropic()
            self.backend = "api"
            log.info("Nachrichten-Analyse: Claude-API (%s, effort=%s)", cfg.claude_model, cfg.claude_effort)
        from .gemini import Gemini
        self.gemini = Gemini(cfg)
        if self.gemini.enabled:
            threading.Thread(target=self.gemini.discover, daemon=True, name="gemini-models").start()
        if self.gemini.enabled:
            log.info("Nachrichten-Analyse: Gemini bevorzugt (max. %d/Tag), Claude als Reserve%s",
                     cfg.gemini_max_calls_per_day, "" if self.backend else " – nicht eingerichtet")
        elif not self.backend:
            log.info("Nachrichten-Analyse: lokales Wörterbuch (für Claude ANTHROPIC_API_KEY setzen)")
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.paused_until = 0.0     # nach Abrechnungs-/Key-Fehlern kurz pausieren
        self.pause_reason = ""
        self.usage: dict = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        self._lock = threading.Lock()  # Claude-Aufrufe laufen parallel

    @property
    def mode(self) -> str:
        return "claude" if (self.client or self.gemini.enabled) else "lexicon"

    def claude_ok(self) -> bool:
        return bool(self.client) and time.time() >= self.paused_until and self.budget_left()

    def providers(self) -> str:
        """Kurztext: welche KI gerade arbeitet."""
        parts = []
        if self.gemini.enabled:
            parts.append(f"Gemini {self.gemini.today_calls()}/{self.cfg.gemini_max_calls_per_day}"
                         + ("" if self.gemini.available() else f" ({self.gemini.status()})"))
        if self.client:
            parts.append(f"Claude{' Reserve' if self.gemini.enabled else ''} {self.today_calls()}/{self.cfg.claude_cli_max_calls_per_day}"
                         + ("" if self.claude_ok() else " (pausiert)" if time.time() < self.paused_until else " (Limit)"))
        return " · ".join(parts) or "Wörterbuch"

    # ------------------------------------------------------------------ Kosten
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def today_usd(self) -> float:
        return self.usage.get(self._today(), {}).get("usd", 0.0)

    def today_calls(self) -> int:
        return self.usage.get(self._today(), {}).get("calls", 0)

    def budget_left(self) -> bool:
        if self.backend == "cli":
            return self.today_calls() < self.cfg.claude_cli_max_calls_per_day
        return self.today_usd() < self.cfg.claude_daily_budget_usd

    def status(self) -> str:
        """Kurztext für die Oberfläche: warum gerade (nicht) eine KI genutzt wird."""
        if self.gemini.available():
            return "claude"
        if not self.client:
            return "lexicon" if not self.gemini.enabled else f"pausiert: Gemini {self.gemini.status()}"
        if time.time() < self.paused_until:
            return f"pausiert: {self.pause_reason}"
        if not self.budget_left():
            return "Tageslimit erreicht – bis Mitternacht Wörterbuch"
        return "claude"

    def model_label(self) -> str:
        if self.backend == "cli":
            return f"{self.cfg.claude_cli_model or 'Standardmodell'} (Abo)"
        return self.cfg.claude_model

    def _book(self, tokens_in: int, tokens_out: int, usd: float | None = None) -> None:
        with self._lock:
            self._book_locked(tokens_in, tokens_out, usd)

    def _book_locked(self, tokens_in: int, tokens_out: int, usd: float | None) -> None:
        if usd is None:
            p_in, p_out = PRICES.get(self.cfg.claude_model, PRICES["claude-opus-5-5"])
            usd = tokens_in * p_in / 1e6 + tokens_out * p_out / 1e6
        day = self.usage.setdefault(self._today(), {"calls": 0, "usd": 0.0})
        day["calls"] += 1
        day["usd"] = round(day["usd"] + usd, 6)
        for old in sorted(self.usage)[:-60]:  # nur die letzten 60 Tage aufheben
            del self.usage[old]
        USAGE_FILE.write_text(json.dumps(self.usage, indent=1))

    def analyze(self, symbol: str, headlines: list[Headline], context: str, use_claude: bool = True,
                force: bool = False) -> NewsSignal:
        """`use_claude=False`: Vorfilter – nur Wörterbuch, außer es gibt schon eine Claude-Bewertung genau dieser Meldungen."""
        if not headlines:
            return NEUTRAL
        key = ",".join(sorted(h.id for h in headlines))
        cached = self.cache.get(symbol)
        if cached:
            cached["signal"].setdefault("provider", "claude" if cached["signal"].get("analyst") == "claude" else "")
        if not use_claude:
            if cached and cached["key"] == key:
                return NewsSignal(**cached["signal"])
            return lexicon_signal(headlines)
        gem_ok, cl_ok = self.gemini.available(), self.claude_ok()
        if not gem_ok and not cl_ok:
            # keine KI verfügbar: letzte KI-Bewertung weiterverwenden, solange die Meldungen gleich sind
            return NewsSignal(**cached["signal"]) if cached and cached["key"] == key else lexicon_signal(headlines)

        if cached:
            age = time.time() - cached["ts"]
            new = set(key.split(",")) - set(cached["key"].split(","))
            too_soon = age < self.cfg.claude_min_minutes * 60
            # nur neu fragen, wenn genug wirklich neue Meldungen da sind oder die Bewertung alt ist
            little_new = len(new) < self.cfg.claude_new_headlines and age < self.cfg.claude_max_age_hours * 3600
            if (not new or too_soon or little_new) and not force:  # force: rasante Kursbewegung
                return NewsSignal(**cached["signal"])

        signal = None
        if gem_ok:  # bevorzugt: Gemini (kostenlos), sonst Claude
            from .gemini import GeminiError
            try:
                data = self.gemini.ask_json(SYSTEM_PROMPT, self._prompt(symbol, headlines, context), SCHEMA)
                signal = self._to_signal(data, len(headlines))
                signal.provider = "gemini"
            except (GeminiError, KeyError, TypeError, ValueError) as e:
                log.info("Gemini für %s nicht verfügbar (%s) – %s", symbol, e, "Claude springt ein" if cl_ok else "Wörterbuch")
        if signal is None and not cl_ok:
            return NewsSignal(**cached["signal"]) if cached and cached["key"] == key else lexicon_signal(headlines)
        if signal is not None:
            with self._lock:
                self.cache[symbol] = {"key": key, "ts": time.time(), "signal": asdict(signal)}
                CACHE_FILE.write_text(json.dumps(self.cache, indent=1))
            return signal

        import anthropic
        try:
            if self.backend == "cli":
                signal = self._ask_cli(symbol, headlines, context)
            else:
                signal = self._ask_claude(symbol, headlines, context)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            self._pause(3600, "API-Key ungültig oder ohne Berechtigung", e)
            return lexicon_signal(headlines)
        except anthropic.BadRequestError as e:
            if "credit" in str(e).lower() or "billing" in str(e).lower():
                self._pause(3600, "kein API-Guthaben", e)
            else:
                log.warning("Claude-Anfrage für %s abgelehnt (%s) – nutze Wörterbuch", symbol, e)
            return lexicon_signal(headlines)
        except Exception as e:
            log.warning("Claude-Analyse für %s fehlgeschlagen (%s) – nutze Wörterbuch", symbol, e)
            return lexicon_signal(headlines)
        with self._lock:
            self.cache[symbol] = {"key": key, "ts": time.time(), "signal": asdict(signal)}
            CACHE_FILE.write_text(json.dumps(self.cache, indent=1))
        return signal

    @staticmethod
    def _prompt(symbol: str, headlines: list[Headline], context: str) -> str:
        lines = [f"- [{h.age_hours():.0f} h alt, {h.source}] {h.title}" + (f" — {h.summary}" if h.summary else "")
                 for h in headlines]
        return f"Wertpapier: {symbol}\nKurslage: {context}\n\nSchlagzeilen (neueste zuerst):\n" + "\n".join(lines)

    @staticmethod
    def _to_signal(data: dict, n: int) -> NewsSignal:
        sentiment = max(-1.0, min(1.0, float(data["sentiment"])))
        confidence = max(0.0, min(1.0, float(data["confidence"])))
        return NewsSignal(sentiment * confidence, sentiment, confidence, str(data["summary"]).strip(), "claude", n, "claude")

    def _ask_cli(self, symbol: str, headlines: list[Headline], context: str) -> NewsSignal:
        """Claude Code im Skriptmodus (-p): ohne Werkzeuge, ohne gespeicherte Sitzung, mit JSON-Schema."""
        cmd = [self.cli, "-p", self._prompt(symbol, headlines, context),
               "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
               "--system-prompt", SYSTEM_PROMPT, "--tools", "", "--no-session-persistence"]
        if self.cfg.claude_cli_model:
            cmd += ["--model", self.cfg.claude_cli_model]
        # Ohne API-Key in der Umgebung, sonst würde die Kommandozeile das (leere) API-Konto statt des Abos nutzen.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env, cwd=STATE_DIR)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            raise RuntimeError(f"Kommandozeile lieferte kein JSON (Exit {proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip()[:200]}")
        if out.get("is_error") or proc.returncode != 0:
            msg = str(out.get("result") or out.get("error") or proc.stderr)[:200]
            low = msg.lower()
            if any(w in low for w in ("limit", "rate", "quota", "usage")):
                self._pause(3600, "Abo-Limit erreicht", RuntimeError(msg))
            elif any(w in low for w in ("login", "auth", "log in", "credential")):
                self._pause(3600, "nicht angemeldet (claude auth login)", RuntimeError(msg))
            raise RuntimeError(msg)

        data = out.get("structured_output")
        if not isinstance(data, dict):
            text = str(out.get("result", ""))
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise RuntimeError(f"keine JSON-Antwort: {text[:120]}")
            data = json.loads(match.group(0))
        usage = out.get("usage") or {}
        self.calls += 1
        self.tokens_in += int(usage.get("input_tokens", 0))
        self.tokens_out += int(usage.get("output_tokens", 0))
        self._book(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                   usd=float(out.get("total_cost_usd") or 0.0))
        return self._to_signal(data, len(headlines))

    def ask_json(self, system: str, prompt: str, schema: dict) -> tuple[dict, str]:
        """Allgemeine KI-Frage mit JSON-Antwort: erst Gemini, dann Claude (Kommandozeile). Gibt (Antwort, Anbieter)."""
        from .gemini import GeminiError
        if self.gemini.available():
            try:
                return self.gemini.ask_json(system, prompt, schema), "gemini"
            except GeminiError as e:
                log.info("Gemini nicht verfügbar (%s) – versuche Claude", e)
        if self.backend == "cli" and self.claude_ok():
            cmd = [self.cli, "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
                   "--system-prompt", system, "--tools", "", "--no-session-persistence"]
            if self.cfg.claude_cli_model:
                cmd += ["--model", self.cfg.claude_cli_model]
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240, env=env, cwd=STATE_DIR)
            out = json.loads(proc.stdout or "{}")
            if out.get("is_error") or proc.returncode != 0:
                msg = str(out.get("result") or proc.stderr)[:200]
                if any(w in msg.lower() for w in ("limit", "rate", "quota", "usage")):
                    self._pause(3600, "Abo-Limit erreicht", RuntimeError(msg))
                raise RuntimeError(msg)
            usage = out.get("usage") or {}
            self._book(int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0)),
                       usd=float(out.get("total_cost_usd") or 0.0))
            data = out.get("structured_output")
            if not isinstance(data, dict):
                m = re.search(r"\{.*\}", str(out.get("result", "")), re.S)
                if not m:
                    raise RuntimeError("keine JSON-Antwort von Claude")
                data = json.loads(m.group(0))
            return data, "claude"
        raise RuntimeError("keine KI verfügbar (Gemini und Claude am Limit oder nicht eingerichtet)")

    def _ask_claude(self, symbol: str, headlines: list[Headline], context: str) -> NewsSignal:
        user = self._prompt(symbol, headlines, context)

        response = self.client.beta.messages.create(
            model=self.cfg.claude_model,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.claude_effort,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.calls += 1
        self.tokens_in += response.usage.input_tokens
        self.tokens_out += response.usage.output_tokens
        self._book(response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason != "end_turn":
            raise RuntimeError(f"stop_reason={response.stop_reason}")

        data = json.loads(next(b.text for b in response.content if b.type == "text"))
        return self._to_signal(data, len(headlines))

    def _pause(self, seconds: float, reason: str, err: Exception) -> None:
        if time.time() >= self.paused_until:
            log.error("Claude pausiert für %d Min.: %s (%s) – solange Wörterbuch", seconds // 60, reason, err)
        self.paused_until = time.time() + seconds
        self.pause_reason = reason

    def cost_estimate_usd(self) -> float:
        """Geschätzte API-Kosten seit Programmstart (bei der Kommandozeile: über das Abo abgedeckt)."""
        if self.backend == "cli":
            return 0.0
        p_in, p_out = PRICES.get(self.cfg.claude_model, PRICES["claude-opus-5-5"])
        return self.tokens_in * p_in / 1e6 + self.tokens_out * p_out / 1e6
