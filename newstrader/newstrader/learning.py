"""Zocker lernt aus seinen Trades.

Nach jeder geschlossenen Position wird ausgewertet, was gut oder schlecht lief – und die Zocker-Einstellungen
werden nachgeführt (nur für den Zocker, das echte Portfolio bleibt unberührt):

- Technik oder Nachrichten: welches Signal lag öfter richtig? -> Gewichtung verschieben (20–80 %).
- Trefferquote: verliert er oft, wird er wählerischer (höhere Einstiegsschwelle), gewinnt er oft, mutiger.
- Long oder Short: läuft eine Seite dauerhaft schlecht, braucht sie ein stärkeres Signal.
- Margin-Calls: häufen sie sich, sinkt der Hebel (bis auf 35 % des Maximums); bleiben sie aus, steigt er wieder.
- Einzelwerte: zwei Verluste in Folge mit einem Wert -> 3 Tage Pause; gute/schlechte Werte werden höher/tiefer gereiht.
Alle Anpassungen sind begrenzt, damit ein paar Zufallstreffer den Bot nicht kippen lassen.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .config import Config, STATE_DIR

log = logging.getLogger(__name__)
LEARN_FILE = STATE_DIR / "zocker_learning.json"
ALPHA = 0.12            # wie schnell neue Erfahrungen alte überschreiben (gleitender Mittelwert)
MIN_TRADES = 5          # vorher wird nichts verstellt
BLOCK_DAYS = 3


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sign(x: float | None) -> int:
    return 0 if not x else (1 if x > 0 else -1)


class Learner:
    def __init__(self, path=LEARN_FILE, persist: bool = True):
        self.path, self.persist = path, persist
        self._lock = threading.Lock()
        self.s = {"n": 0, "winrate": 0.5, "liq": 0.0, "acc_tech": 0.5, "acc_news": 0.5,
                  "side": {"long": {"n": 0, "ret": 0.0}, "short": {"n": 0, "ret": 0.0}},
                  "symbols": {}, "log": []}
        if persist:
            try:
                self.s.update(json.loads(path.read_text()))
            except (FileNotFoundError, json.JSONDecodeError):
                pass

    def _save(self) -> None:
        if not self.persist:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    # ------------------------------------------------------------------ Erfahrung sammeln
    def record(self, symbol: str, pos: dict, realized: float, reason: str, liquidation: bool, now: datetime) -> None:
        """Vom Broker nach jedem Schließen aufgerufen."""
        with self._lock:
            s = self.s
            margin = pos.get("margin") or 1.0
            ret = realized / margin                                  # Ergebnis bezogen auf den Einsatz
            win = realized > 0
            d = -1 if pos.get("side") == "short" else 1
            e = pos.get("entry") or {}
            s["n"] += 1
            s["winrate"] += ALPHA * ((1.0 if win else 0.0) - s["winrate"])
            s["liq"] += ALPHA * ((1.0 if liquidation else 0.0) - s["liq"])
            # Welches Signal hatte recht? „Recht“ = Signal zeigte in die Richtung, die Gewinn brachte.
            moved = d if win else -d                                 # Richtung, in die der Kurs tatsächlich lief
            for key, comp in (("acc_tech", e.get("tech")), ("acc_news", e.get("news"))):
                if _sign(comp) and (key != "acc_news" or e.get("n_news", 1)):
                    s[key] += ALPHA * ((1.0 if _sign(comp) == moved else 0.0) - s[key])
            side = s["side"].setdefault(pos.get("side", "long"), {"n": 0, "ret": 0.0})
            side["n"] += 1
            side["ret"] += ALPHA * (_clamp(ret, -1.0, 2.0) - side["ret"])
            sym = s["symbols"].setdefault(symbol, {"n": 0, "wins": 0, "last": [], "streak": 0, "blocked_until": None})
            sym["n"] += 1
            sym["wins"] += 1 if win else 0
            sym["last"] = (sym["last"] + [round(ret, 4)])[-5:]
            sym["streak"] = 0 if win else sym["streak"] + 1
            if sym["streak"] >= 2:
                sym["blocked_until"] = (now + timedelta(days=BLOCK_DAYS)).isoformat(timespec="seconds")
                sym["streak"] = 0
            s["log"] = (s["log"] + [{"time": now.isoformat(timespec="seconds"), "symbol": symbol, "side": pos.get("side"),
                                     "lev": pos.get("leverage"), "ret": round(ret, 4), "pnl": round(realized, 2),
                                     "liq": liquidation, "entry": e, "reason": reason[:80]}])[-200:]
            self._save()

    # ------------------------------------------------------------------ Erfahrung anwenden
    def tune(self, cfg: Config) -> Config:
        """Zocker-Einstellungen nach dem bisher Gelernten."""
        s = self.s
        if s["n"] < MIN_TRADES:
            return cfg
        wt = _clamp(s["acc_tech"] / max(1e-9, s["acc_tech"] + s["acc_news"]), 0.2, 0.8)
        total = cfg.weight_technical + cfg.weight_news
        shift = _clamp(0.25 * (0.5 - s["winrate"]), -0.06, 0.10)      # verliert oft -> wählerischer
        long_extra = 0.05 if s["side"]["long"]["n"] >= MIN_TRADES and s["side"]["long"]["ret"] < -0.15 else 0.0
        short_extra = 0.05 if s["side"]["short"]["n"] >= MIN_TRADES and s["side"]["short"]["ret"] < -0.15 else 0.0
        lev = _clamp(1 - 2.0 * max(0.0, s["liq"] - 0.15), 0.35, 1.0)  # viele Margin-Calls -> weniger Hebel
        return replace(cfg, weight_technical=wt * total, weight_news=(1 - wt) * total,
                       buy_threshold=_clamp(cfg.buy_threshold + shift + long_extra, 0.1, 0.6),
                       short_threshold=_clamp(cfg.short_threshold - shift - short_extra, -0.6, -0.1),
                       max_leverage_stocks=max(1.0, round(cfg.max_leverage_stocks * lev * 2) / 2),
                       max_leverage_crypto=max(1.0, round(cfg.max_leverage_crypto * lev * 2) / 2))

    def allows(self, symbol: str, now: datetime) -> bool:
        b = self.s["symbols"].get(symbol, {}).get("blocked_until")
        return not b or datetime.fromisoformat(b) <= now

    def factor(self, symbol: str) -> float:
        """Rangfolge: Werte, mit denen er gut verdient hat, etwas vorziehen – schlechte nach hinten."""
        last = self.s["symbols"].get(symbol, {}).get("last") or []
        if not last:
            return 1.0
        return 1.0 + _clamp(0.5 * sum(last) / len(last), -0.3, 0.2)

    # ------------------------------------------------------------------ Anzeige
    def summary(self, cfg: Config) -> dict:
        s, t = self.s, self.tune(cfg)
        now = datetime.now(timezone.utc)
        syms = [(k, v) for k, v in s["symbols"].items() if v["n"]]
        best = sorted(syms, key=lambda kv: -sum(kv[1]["last"]) / len(kv[1]["last"]))[:3]
        worst = sorted(syms, key=lambda kv: sum(kv[1]["last"]) / len(kv[1]["last"]))[:3]
        lessons = []
        if s["n"] < MIN_TRADES:
            lessons.append(f"Sammelt noch Erfahrung – ab {MIN_TRADES} abgeschlossenen Trades passt er sich an ({s['n']} bisher).")
        else:
            tot = cfg.weight_technical + cfg.weight_news
            lessons.append(f"Gewichtung jetzt {t.weight_technical / tot:.0%} Technik / {t.weight_news / tot:.0%} Nachrichten "
                           f"(Technik lag zu {s['acc_tech']:.0%} richtig, Nachrichten zu {s['acc_news']:.0%}).")
            if t.buy_threshold > cfg.buy_threshold + 0.005:
                lessons.append(f"Wählerischer geworden: Long erst ab Signal {t.buy_threshold:+.2f} (Trefferquote {s['winrate']:.0%}).")
            elif t.buy_threshold < cfg.buy_threshold - 0.005:
                lessons.append(f"Mutiger geworden: Long schon ab Signal {t.buy_threshold:+.2f} (Trefferquote {s['winrate']:.0%}).")
            if t.max_leverage_stocks < cfg.max_leverage_stocks:
                lessons.append(f"Zu viele Margin-Calls ({s['liq']:.0%}) – Hebel gedrosselt auf höchstens {t.max_leverage_stocks:g}x.")
            for side, label in (("long", "Long"), ("short", "Short")):
                v = s["side"].get(side, {})
                if v.get("n", 0) >= MIN_TRADES:
                    lessons.append(f"{label}: im Schnitt {v['ret']:+.0%} auf den Einsatz.")
        blocked = [k for k, v in s["symbols"].items() if v.get("blocked_until") and not self.allows(k, now)]
        if blocked:
            lessons.append(f"Pause nach zwei Verlusten in Folge: {', '.join(blocked)}.")
        return {"n": s["n"], "winrate": s["winrate"], "liq": s["liq"], "acc_tech": s["acc_tech"], "acc_news": s["acc_news"],
                "buy_threshold": t.buy_threshold, "short_threshold": t.short_threshold,
                "max_leverage": t.max_leverage_stocks, "blocked": blocked, "lessons": lessons,
                "best": [k for k, _ in best], "worst": [k for k, _ in worst], "recent": s["log"][-10:][::-1]}


_learner: Learner | None = None


def learner() -> Learner:
    global _learner
    if _learner is None:
        _learner = Learner()
    return _learner


def zocker_cfg(cfg: Config) -> Config:
    """Einstellungen für den Zocker: Profil (z. B. „maximal“) + das bisher Gelernte."""
    return learner().tune(cfg.zocker())
