"""Google Gemini (kostenloser API-Schlüssel aus Google AI Studio) – bevorzugte KI, Claude ist die Reserve.

Schlüssel: Add-on-Option „gemini_api_key“ (Umgebungsvariable GEMINI_API_KEY). Es werden nur Schlagzeilen,
Kursdaten und Wertpapiernamen gesendet – nichts aus deinem Depot oder Konto.
Hinweis: Im kostenlosen Tarif darf Google die Anfragen zur Verbesserung seiner Modelle verwenden.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import requests

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
USAGE_FILE = STATE_DIR / "gemini_usage.json"


class GeminiError(RuntimeError):
    pass


class Gemini:
    def __init__(self, cfg: Config, key: str | None = None):
        self.cfg = cfg
        self.key = key if key is not None else os.environ.get("GEMINI_API_KEY", "")
        self.models = list(cfg.gemini_models)
        self.paused_until = 0.0
        self.pause_reason = ""
        self._lock = threading.Lock()       # nacheinander + Mindestabstand (kostenlose Stufe: wenige Anfragen/Min.)
        self._model_pause: dict[str, float] = {}   # je Modell: gesperrt bis (Kontingent erschöpft)
        self._logged_429: set[str] = set()
        self._last = 0.0
        try:
            self.usage: dict = json.loads(USAGE_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self.usage = {}

    def discover(self) -> list[str]:
        """Welche Flash-Modelle gibt es gerade? (Namen ändern sich; jedes hat ein eigenes Gratis-Kontingent.)"""
        if not self.enabled:
            return self.models
        try:
            r = requests.get(URL.rsplit("/", 1)[0], params={"pageSize": 200}, headers={"x-goog-api-key": self.key}, timeout=20)
            r.raise_for_status()
            found = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                     if "generateContent" in m.get("supportedGenerationMethods", [])
                     and "flash" in m["name"] and not any(x in m["name"] for x in ("image", "tts", "audio", "live", "thinking"))]
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini-Modellliste nicht abrufbar: %s", e)
            return self.models
        # bevorzugte Reihenfolge behalten, dann die übrigen gefundenen (stabile vor Vorschau-Modellen)
        pref = [m for m in self.models if m in found or m.endswith("-latest")]
        rest = sorted((m for m in found if m not in pref), key=lambda m: ("preview" in m or "exp" in m, m))
        self.models = pref + rest
        log.info("Gemini-Modelle: %s", ", ".join(self.models))
        return self.models

    @staticmethod
    def _next_reset() -> float:
        """Tageskontingente setzt Google um Mitternacht Pazifikzeit zurück (bei uns 9 Uhr)."""
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Los_Angeles"))
        return (now.replace(hour=0, minute=1, second=0, microsecond=0) + timedelta(days=1)).timestamp()

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def today_calls(self) -> int:
        return self.usage.get(time.strftime("%Y-%m-%d"), 0)

    def available(self) -> bool:
        return self.enabled and time.time() >= self.paused_until and self.today_calls() < self.cfg.gemini_max_calls_per_day

    def status(self) -> str:
        if not self.enabled:
            return "kein Schlüssel"
        if time.time() < self.paused_until:
            return f"pausiert: {self.pause_reason}"
        if self.today_calls() >= self.cfg.gemini_max_calls_per_day:
            return "Tageslimit erreicht"
        return "bereit"

    def _pause(self, seconds: float, reason: str) -> None:
        self.paused_until = time.time() + seconds
        self.pause_reason = reason
        log.warning("Gemini pausiert für %d Min.: %s – Claude springt ein", seconds / 60, reason)

    def _book(self) -> None:
        day = time.strftime("%Y-%m-%d")
        self.usage[day] = self.usage.get(day, 0) + 1
        for old in sorted(self.usage)[:-60]:
            del self.usage[old]
        try:
            USAGE_FILE.write_text(json.dumps(self.usage))
        except OSError:
            pass

    def ask_json(self, system: str, prompt: str, schema: dict, temperature: float = 0.2) -> dict:
        """Antwort als JSON-Objekt. Wirft GeminiError, wenn es nicht geht (dann übernimmt Claude)."""
        if not self.available():
            raise GeminiError(self.status())
        instr = (system + "\n\nAntworte ausschließlich mit einem JSON-Objekt nach diesem JSON-Schema:\n"
                 + json.dumps(schema, ensure_ascii=False))
        body = {"systemInstruction": {"parts": [{"text": instr}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
        with self._lock:
            wait = self.cfg.gemini_min_seconds - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            last_err = None
            timeouts = 0
            for model in list(self.models):
                if self._model_pause.get(model, 0) > time.time():
                    continue
                self._last = time.time()
                try:
                    r = requests.post(URL.format(model=model), json=body, timeout=self.cfg.gemini_timeout,
                                      headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
                except requests.Timeout:
                    timeouts += 1   # Modell denkt zu lange – nächstes probieren (höchstens zweimal pro Frage)
                    last_err = f"{model} antwortet nicht rechtzeitig"
                    if timeouts >= 2:
                        break
                    continue
                except requests.RequestException as e:
                    raise GeminiError(f"nicht erreichbar ({type(e).__name__})")
                if r.status_code == 404:          # Modellname gibt es (nicht mehr) – nächsten probieren
                    last_err = f"Modell {model} unbekannt"
                    self.models.remove(model)
                    self.models.append(model)
                    continue
                if r.status_code == 429:   # Kontingent dieses Modells erschöpft – nächstes Modell probieren
                    if model not in self._logged_429:
                        self._logged_429.add(model)
                        log.warning("Gemini %s: Kontingent (HTTP 429): %s", model, " ".join(r.text.split())[:400])
                    per_day = "PerDay" in r.text or "per_day" in r.text.lower() or "limit: 0" in r.text
                    self._model_pause[model] = self._next_reset() if per_day else time.time() + 90
                    last_err = f"{model}: Kontingent erschöpft"
                    continue
                if r.status_code in (400, 401, 403) and ("API_KEY" in r.text or "API key" in r.text or r.status_code != 400):
                    self._pause(6 * 3600, "API-Schlüssel ungültig")
                    raise GeminiError("Schlüssel")
                if r.status_code >= 500:   # überlastet/vorübergehend – nächstes Modell probieren
                    last_err = f"{model} überlastet (HTTP {r.status_code})"
                    continue
                if r.status_code >= 400:
                    raise GeminiError(f"HTTP {r.status_code}: {r.text[:150]}")
                self._book()
                try:
                    parts = r.json()["candidates"][0]["content"]["parts"]
                    text = "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError, ValueError):
                    raise GeminiError(f"unerwartete Antwort: {r.text[:150]}")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    m = re.search(r"\{.*\}", text, re.S)
                    if not m:
                        raise GeminiError(f"kein JSON: {text[:120]}")
                    return json.loads(m.group(0))
            if last_err and "überlastet" in last_err:
                self._pause(300, "alle Modelle gerade überlastet")
            elif all(self._model_pause.get(m, 0) > time.time() for m in self.models):
                wait = min(self._model_pause[m] for m in self.models) - time.time()
                self._pause(max(60, wait), "Kontingent aller Modelle erschöpft")
            raise GeminiError(last_err or "kein Modell verfügbar")
