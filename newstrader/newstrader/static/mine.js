// Portfolio-Ansichten: „Portfolio“ (Musterdepot des Portfoliomanagers) und „Mein Depot“ (deine echten Käufe).
// Nutzt die Hilfsfunktionen aus index.html (nf, money, pct, signed, cls, esc, cssVar, niceTicks, scoreCell, dt).
function createPortfolioView(opts) {
  "use strict";
  const P = opts.prefix, API = opts.api, MODE = opts.mode;
  const PF = { ov: null, perf: null, risk: null, tx: null, alerts: null, range: "Max", allocKey: "symbol", busy: false, loadedOnce: false,
               vRange: "1T", values: null, multi: false, multiMode: "eur", multiData: null, posRange: "1M", posData: {} };
  try { PF.multi = localStorage.getItem("nt-multi") === "1"; PF.multiMode = localStorage.getItem("nt-multi-mode") || "eur";
        PF.posRange = localStorage.getItem("nt-pos-range") || "1M"; } catch (e) {}
  const MONTHS = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
  const ALLOC = { symbol: "Positionen", sector: "Branche", country: "Land", currency: "Währung" };
  // Jede Aktion: gedrückter Knopf zeigt „läuft …“ und ist gesperrt, danach kurze Meldung (Erfolg oder Fehler).
  const DONE = {
    "api/pf/action:confirm": "✓ Abgehakt", "api/pf/action:confirm_all": "✓ Alle abgehakt",
    "api/pf/action:reject": "Als „nicht gemacht“ entfernt", "api/pf/action:adjust": "✓ Mit deinem Kurs gespeichert",
    "api/pf/strategy": "✓ Strategie gespeichert", "api/pf/deposit": "✓ Gebucht", "api/pf/booking": "✓ Buchung gespeichert",
    "api/pf/spec:on": "✓ Hebel-Vorschlag eingeschaltet", "api/pf/spec:off": "Hebel-Vorschlag ausgeschaltet",
    "api/pf/spec/prepare": "Vorschau läuft – den Fortschritt siehst du in der Spekulations-Karte",
    "api/pf/tr/sync": "Abgleich mit Trade Republic läuft …", "api/pf/tr/login": "Anmeldung gestartet – bitte in der TR-App bestätigen",
    "api/pf/tr/logout": "Trade Republic getrennt", "api/pf/tr/cancel": "Anmeldung abgebrochen",
    "api/pf/check": "Prüfung läuft …", "api/pf/reset": "Depot zurückgesetzt", "api/pf/setup": "Portfolio wird aufgebaut …",
  };
  let lastBtn = null, lastBtnAt = 0;
  document.addEventListener("click", (e) => { const b = e.target.closest("button, .switch"); if (b) { lastBtn = b; lastBtnAt = Date.now(); } }, true);
  const POST = async (path, body) => {
    const btnEl = Date.now() - lastBtnAt < 1500 && lastBtn?.tagName === "BUTTON" ? lastBtn : null;
    const old = btnEl?.innerHTML;
    if (btnEl) { btnEl.disabled = true; btnEl.classList.add("busy"); btnEl.innerHTML = `<span class="spin sm"></span> ${btnEl.textContent.trim().replace(/^[✓⟳]\s*/, "")} …`; }
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-NewsTrader": "1" }, body: JSON.stringify(body || {}) });
      const res = { status: r.status, data: await r.json().catch(() => ({ ok: false, error: "Antwort unlesbar" })) };
      const key = path + (body?.do ? ":" + body.do : "") + (path === "api/pf/spec" ? (body.enabled ? ":on" : ":off") : "");
      if (res.data.ok && DONE[key]) toast(DONE[key]);
      return res;
    } catch (e) {
      return { status: 0, data: { ok: false, error: "Keine Verbindung zum NewsTrader" } };
    } finally {
      if (btnEl && btnEl.isConnected) { btnEl.disabled = false; btnEl.classList.remove("busy"); btnEl.innerHTML = old; }
    }
  };
  const GET = (path) => fetch(path, { cache: "no-store" }).then((r) => r.json()).catch(() => null);
  const eur = (v, d = 0) => (v == null || !isFinite(v) ? "–" : `${nf(d).format(v)} €`);
  const signedEur = (v, d = 0) => (v == null || !isFinite(v) ? "–" : `${v > 0 ? "+" : ""}${nf(d).format(v)} €`);
  const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
  const root = () => document.getElementById(opts.root);

  // ------------------------------------------------------------------ Laden
  async function load(full) {
    if (PF.busy) return;
    PF.busy = true;
    try {
      if (MODE === "model") {
        const st = await GET("api/pf/state");
        if (st && !st.error) PF.state = st;
        if (!PF.state || !PF.state.setup) { render(true); return; }
      }
      const jobs = [GET(`${API}/overview`)];
      if (full || !PF.loadedOnce) jobs.push(GET(`${API}/performance`), GET(`${API}/risk`), GET(`${API}/transactions`),
        MODE === "real" ? GET("api/my/alerts") : Promise.resolve(null));
      const [ov, perf, risk, tx, alerts] = await Promise.all(jobs);
      if (ov && !ov.error) PF.ov = ov;
      const isFull = perf !== undefined;
      if (isFull) { PF.perf = perf; PF.risk = risk; PF.tx = tx; PF.alerts = alerts; }
      PF.loadedOnce = true;
      render(isFull);
    } finally { PF.busy = false; }
  }

  // Gerüst einmal aufbauen, dann Teile gezielt aktualisieren (Formulare bleiben beim Live-Update unberührt)
  let layout = null;
  const set = (id, html) => { const n = document.getElementById(P + id); if (n) n.innerHTML = html; };
  function render(full = true) {
    const el = root();
    if (!el) return;
    if (MODE === "model" && !(PF.state && PF.state.setup)) {
      if (layout !== "setup") { layout = "setup"; el.innerHTML = setupScreen(); bindSetup(); }
      else updateBuild();
      return;
    }
    const ov = PF.ov;
    if (!ov) { el.innerHTML = `<div class="card empty-big">Lade …</div>`; layout = null; return; }
    if (MODE === "model") return renderModel(full);
    if (MODE === "real") {
      const vc = document.getElementById("myValueCard");
      if (vc) vc.style.display = ov.has_txns ? "none" : "";   // manueller Depotwert nur ohne Transaktionen
    }
    const want = MODE === "model" ? "full" : ov.has_txns ? "full" : "empty";
    if (layout !== want) {
      layout = want;
      el.innerHTML = want === "empty"
        ? `<div class="card empty-big">
            <div style="font-size:15px;margin-bottom:6px"><b>Trag deine Käufe ein</b></div>
            Hier verfolgst du dein <b>echtes</b> Depot: Käufe, Verkäufe, Dividenden. Empfehlungen aus dem Tab „Portfolio“,
            die du umgesetzt hast, landen mit einem Klick hier. NewsTrader rechnet daraus Wert, Gewinn, Rendite gegen
            DAX und MSCI World, das Risiko – und schickt Alarme aufs iPhone.
          </div><div id="${P}TxBox"></div>`
        : `${MODE === "model" ? `<div id="${P}Value"></div>` : ""}<div id="${P}Kpi"></div>${MODE === "model" ? `<div id="${P}Actions"></div>` : ""}<div id="${P}Pos"></div>
           <section class="grid pf-two"><div id="${P}Perf"></div><div id="${P}AllocBox"></div></section>
           <div id="${P}Monthly"></div><div id="${P}Risk"></div><div id="${P}TxBox"></div><div id="${P}Alerts"></div>`;
      full = true;
    }
    if (want === "full") {
      set("Kpi", kpis());
      set("Pos", positions());
      set("AllocBox", allocCard());
      bindAlloc();
      bindCash();
      if (MODE === "model") { set("Actions", actionsCard()); bindActions(); }  // bindet auch die Knöpfe in der Positionstabelle
      if (MODE === "model") { if (full || !PF.values) loadValues(); else drawValue(); }
      if (full) {
        set("Perf", perfCard());
        set("Monthly", monthlyCard());
        set("Risk", riskCard());
        set("Alerts", MODE === "real" ? alertsCard() : settingsCard());
        if (MODE === "model") bindSettings();
        drawPerf();
        bindRange();
      }
    }
    if (full) { set("TxBox", txSection()); bindTx(); }
  }
  function bindRange() {
    root().querySelectorAll("[data-range]").forEach((b) => b.onclick = () => {
      PF.range = b.dataset.range;
      set("Perf", perfCard()); drawPerf(); bindRange();
    });
  }

  // ------------------------------------------------------------------ Musterdepot: Aufbau
  function setupScreen() {
    const st = PF.state?.styles || {};
    const desc = (k) => { const x = st[k]; return x ? `${x.n} Werte · max. ${nf(0).format(x.max_weight * 100)} % je Wert · Stop ${nf(0).format(x.stop * 100)} % / Trailing ${nf(0).format(x.trailing * 100)} % · ${nf(0).format(x.cash * 100)} % Cash · Krypto ${x.crypto ? "bis " + nf(0).format(x.crypto * 100) + " %" : "keine"}` : ""; };
    return `<div class="card" style="padding:22px 22px 18px">
        <div style="font-size:17px;font-weight:600;margin-bottom:6px">Portfoliomanager starten</div>
        <div class="name" style="font-size:13px;max-width:760px">Gib dein Startkapital ein. Der Bot schlägt daraus ein Portfolio aus den stärksten, von Claude geprüften Werten vor
          (DAX, große US-Werte, Krypto), setzt Stop-Loss-Marken und betreut es laufend: Stops jederzeit, neue Käufe und Verkäufe
          zu festen Zeiten (${(PF.state?.decision_times || []).join(", ")} Uhr). Jeder Schritt kommt als Empfehlung aufs iPhone – <b>freiwillig</b>.</div>
        <form id="${P}Setup" style="margin-top:16px" autocomplete="off">
          <div class="field" style="padding:0 0 12px"><input type="text" inputmode="decimal" name="capital" placeholder="z. B. 10000" style="font:inherit;font-size:20px;font-weight:600;width:200px;padding:7px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--ink)" required> <span>€ Startkapital</span></div>
          ${["ausgewogen", "defensiv", "offensiv"].map((k) => `<label style="display:flex;gap:10px;align-items:flex-start;padding:8px 0;cursor:pointer">
            <input type="radio" name="style" value="${k}" ${k === "ausgewogen" ? "checked" : ""} style="margin-top:3px">
            <span><b>${esc(st[k]?.label || k)}</b>${k === "ausgewogen" ? ' <span class="tag">empfohlen</span>' : ""}<br><span class="name">${desc(k)}</span></span></label>`).join("")}
          <div class="field" style="padding:12px 0 0"><button class="btn" type="submit">Portfolio bauen</button><span class="msg" id="${P}SetupMsg" style="padding:0"></span></div>
        </form>
        <div class="name" style="margin-top:10px">Keine Anlageberatung; das Verfahren ist ein Experiment. Der Bot handelt nicht selbst – er bucht seine Empfehlungen in diesem Depot, du korrigierst, was du anders machst.</div>
      </div>`;
  }
  function bindSetup() {
    const f = document.getElementById(P + "Setup");
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      const body = Object.fromEntries(new FormData(f));
      const r = await POST("api/pf/setup", body);
      if (!r.data.ok) { document.getElementById(P + "SetupMsg").textContent = r.data.error; return; }
      f.querySelector("button").disabled = true;
      poll();
    };
    updateBuild();
  }
  function updateBuild() {
    const b = PF.state?.build || {};
    const m = document.getElementById(P + "SetupMsg");
    if (!m) return;
    if (b.running) { m.textContent = b.step || "Baue …"; setTimeout(poll, 3000); }
    else if (b.error) m.textContent = "Fehler: " + b.error;
  }
  async function poll() {
    const st = await GET("api/pf/state");
    if (st) PF.state = st;
    if (st && st.setup && !st.build.running) { layout = null; load(true); return; }
    updateBuild();
  }

  // ------------------------------------------------------------------ Depot: Empfehlungen
  const KIND = { setup: "Aufbau", signal: "Signal", stop: "Stop-Loss", trim: "Teilverkauf", rapid: "Kurssturz", manual: "Eigene",
                 cash: "Cash für Hebel-Trade", rebalance: "Nachkauf" };
  const STATUS = { angenommen: "⏳ unbestätigt", "bestätigt": "✓ bestätigt", angepasst: "✓ bestätigt (angepasst)", abgelehnt: "✕ nicht gemacht", eigene: "✓ eigene Buchung" };
  const STATUS_CLS = { angenommen: "warn", "bestätigt": "ok", angepasst: "ok", abgelehnt: "", eigene: "ok" };
  const unconfirmed = (sym) => (PF.state?.actions || []).filter((a) => a.status === "angenommen" && (!sym || a.symbol === sym));
  const btn = (label, attrs, primary) => `<button class="btn${primary ? "" : " ghost"}" ${attrs} style="padding:4px 9px;font-size:12px">${label}</button>`;
  function nextDecision() {
    const times = PF.state?.decision_times || [];
    const now = new Date(), hm = now.toTimeString().slice(0, 5), wd = now.getDay();
    if (wd >= 1 && wd <= 5) { const t = times.find((x) => x > hm); if (t) return `heute ${t} Uhr`; }
    return `nächster Börsentag ${times[0] || ""} Uhr`;
  }
  function actionsCard() {
    const acts = (PF.state?.actions || []).slice(0, 40);
    const row = (a) => {
      const off = a.status === "abgelehnt";
      const open = a.status === "angenommen";
      const editable = open || a.status === "bestätigt" || a.status === "angepasst";
      return `<tr data-act="${esc(a.id)}" style="${off ? "opacity:.55" : ""}">
        <td class="name" style="white-space:nowrap">${dt(a.time)}</td>
        <td style="white-space:nowrap"><span class="tag ${a.side === "buy" ? "long" : "short"}">${a.side === "buy" ? "Kaufen" : a.side === "dividend" ? "Dividende" : "Verkaufen"}</span> <span class="name">${KIND[a.kind] || a.kind}</span></td>
        <td><span class="sym" style="${off ? "text-decoration:line-through" : ""}">${esc(a.name)}</span><div class="name">${esc(a.symbol)}</div></td>
        <td class="num">${a.side === "dividend" ? "–" : nf(a.qty % 1 ? 4 : 0).format(a.qty)}</td>
        <td class="num">${nf(2).format(a.price)} €<div class="name">${eur(a.value)}</div></td>
        <td><div class="reason">${esc(a.reason)}</div></td>
        <td style="white-space:nowrap"><div class="status ${STATUS_CLS[a.status] || ""}" style="margin-bottom:4px">${STATUS[a.status] || a.status}</div>
          ${open ? btn("✓ Bestätigen", 'data-do="confirm"', true) + " " : ""}${editable ? btn("ändern", 'data-do="adjust"') + " " + btn("nicht gemacht", 'data-do="reject"') : ""}</td></tr>
        <tr class="hidden" data-form="${esc(a.id)}"><td colspan="7"></td></tr>`;
    };
    const nOpen = unconfirmed().length;
    return `<section class="card" style="margin-bottom:14px"><h2>Alle Empfehlungen${nOpen ? ` · <span class="status warn">${nOpen} offen</span>` : ` · <span class="status ok">alles erledigt</span>`}</h2>
      <div class="hint">So funktioniert's: Hast du eine Empfehlung bei deinem Broker <b>genau so</b> umgesetzt → <b>✓ Bestätigen</b>. Anderer Kurs oder andere Stückzahl → <b>ändern</b>.
        Gar nicht gemacht → <b>nicht gemacht</b> (wird aus dem Depot entfernt). Unbestätigte Empfehlungen zählen vorerst so, als hättest du sie umgesetzt.
        Nächste Prüfung: ${nextDecision()} · Stop-Loss wird laufend überwacht.</div>
      ${nOpen > 1 ? `<div class="field" style="padding-top:8px">${btn(`✓ Alle ${nOpen} bestätigen`, 'data-confirm-all="1"', true)}<span class="name">wenn du alle offenen Empfehlungen genau so umgesetzt hast</span></div>` : ""}
      ${acts.length ? `<div class="tbl"><table><tr><th>Zeit</th><th>Aktion</th><th>Wert</th><th class="num">Stück</th><th class="num">Kurs</th><th>Grund</th><th></th></tr>${acts.map(row).join("")}</table></div>`
        : `<div class="empty">Noch keine Empfehlungen.</div>`}</section>`;
  }
  function bindActions() {
    root().querySelectorAll("[data-confirm-all], [data-confirm-sym]").forEach((b) => b.onclick = async () => {
      const sym = b.dataset.confirmSym || null;
      const n = unconfirmed(sym).length;
      if (!sym && !confirm(`Alle ${n} offenen Empfehlungen als genau so umgesetzt bestätigen?`)) return;
      const r = await POST("api/pf/action", { do: "confirm_all", symbol: sym });
      if (!r.data.ok) toast(r.data.error, "err"); else load(false);
    });
    root().querySelectorAll("[data-act] [data-do]").forEach((b) => b.onclick = async () => {
      const id = b.closest("[data-act]").dataset.act;
      const a = (PF.state.actions || []).find((x) => x.id === id);
      if (b.dataset.do === "confirm") {
        const r = await POST("api/pf/action", { id, do: "confirm" });
        if (!r.data.ok) toast(r.data.error, "err"); else load(false);
        return;
      }
      if (b.dataset.do === "reject") {
        if (!confirm(`${a.side === "buy" ? "Kauf" : "Verkauf"} von ${a.name} nicht gemacht? Die Buchung wird aus deinem Depot entfernt.`)) return;
        const r = await POST("api/pf/action", { id, do: "reject" });
        if (!r.data.ok) toast(r.data.error, "err"); else load(true);
        return;
      }
      const fr = root().querySelector(`[data-form="${id}"]`);
      fr.classList.remove("hidden");
      fr.firstElementChild.innerHTML = `<form class="txform" style="padding:4px 0" data-adjust="${esc(id)}">
          <label>Datum<input class="w-date" type="date" name="date" value="${a.time.slice(0, 10)}" max="${today()}"></label>
          <label>Stück<input class="w-num" name="qty" inputmode="decimal" value="${String(a.qty).replace(".", ",")}"></label>
          <label>Dein Kurs €<input class="w-num" name="price" inputmode="decimal" value="${a.price.toFixed(2).replace(".", ",")}"></label>
          <label>Gebühr €<input class="w-num" name="fee" inputmode="decimal" value="1"></label>
          <button class="btn" type="submit">Speichern</button>
          <button class="btn ghost" type="button" data-cancel="1">Abbrechen</button>
        </form><div class="formmsg" data-msg="${esc(id)}"></div>`;
      const f = fr.querySelector("form");
      const msgEl = fr.querySelector(`[data-msg="${id}"]`);
      const send = async (force) => {
        const r = await POST("api/pf/action", { id, do: "adjust", force, ...Object.fromEntries(new FormData(f)) });
        if (r.status === 409 && r.data.code === "price_check") {
          msgEl.innerHTML = `<div class="confirm">${esc(r.data.error)} <button class="btn" type="button">Trotzdem speichern</button></div>`;
          msgEl.querySelector("button").onclick = () => send(true);
          return;
        }
        if (!r.data.ok) { msgEl.className = "formmsg err"; msgEl.textContent = r.data.error; return; }
        load(true);
      };
      f.onsubmit = (ev) => { ev.preventDefault(); send(false); };
      f.querySelector("[data-cancel]").onclick = () => fr.classList.add("hidden");
    });
  }

  // ------------------------------------------------------------------ Musterdepot: Einstellungen
  function settingsCard() {
    const s = PF.state || {}, st = s.styles?.[s.style] || {};
    return `<section class="card" style="margin-bottom:14px"><h2>Geld &amp; Depot</h2>
      <div class="rules"><span>Startkapital ${eur(s.start_capital)}</span><span>seit ${s.created ? new Date(s.created).toLocaleDateString("de-DE") : "–"}</span>
        <span>Nachrichten: stündlich, sofort bei Kursstürzen</span></div>
      <div class="field"><button class="btn ghost" id="${P}Dep">+ Einzahlung</button><button class="btn ghost" id="${P}Wd">− Entnahme</button>
        <span class="name">Geld, das du ins Depot einzahlst oder herausnimmst (ändert das Cash, nicht die Rendite).</span></div>
      <div class="field"><button class="btn ghost" id="${P}Reset">Depot zurücksetzen …</button><span class="name">Der bisherige Stand wird archiviert, danach kannst du neu starten.</span></div></section>`;
  }
  function bindSettings() {
    [["Dep", "deposit", "Einzahlung (€):"], ["Wd", "withdraw", "Entnahme (€):"]].forEach(([id, kind, q]) => {
      const el = document.getElementById(P + id);
      if (el) el.onclick = async () => {
        const v = prompt(q, "");
        if (v == null || !v.trim()) return;
        const r = await POST("api/pf/deposit", { amount: v, kind });
        if (!r.data.ok) toast(r.data.error, "err"); else load(true);
      };
    });
    const b = document.getElementById(P + "Reset");
    if (b) b.onclick = async () => {
      const v = prompt("Depot wirklich zurücksetzen? Zur Bestätigung ZURÜCKSETZEN eingeben:");
      if (v == null) return;
      const r = await POST("api/pf/reset", { confirm: v.trim().toUpperCase() });
      if (!r.data.ok) { toast(r.data.error, "err"); return; }
      PF.state = null; PF.ov = null; layout = null; load(true);
    };
  }




  // ------------------------------------------------------------------ Strategie
  const STYLE_TXT = {
    defensiv: "Viele Werte, enge Stops, mehr Cash – ruhiger Verlauf.",
    ausgewogen: "Mittelweg aus Streuung und Chancen.",
    offensiv: "Wenige Werte, weite Stops, kaum Cash – schwankt stärker.",
  };
  const fmtParam = (k, v, meta) => {
    if (meta.pct) { const x = Math.round(v * 1000) / 10; return nf(x % 1 ? 1 : 0).format(x); }
    return k.startsWith("n_") ? String(v) : nf(2).format(v);
  };
  function strategyCard() {
    const g = PF.state?.strategy;
    if (!g) return "";
    const sel = PF.stratStyle || g.style;
    const vals = PF.stratStyle && PF.stratStyle !== g.style ? g.defaults[sel] : g.values;
    const field = (k) => {
      const m = g.params[k], custom = sel === g.style && k in g.custom;
      return `<label class="sfield">${esc(m.label)}${custom ? ' <span class="status warn">angepasst</span>' : ""}
        <span class="sinput"><input name="${k}" inputmode="decimal" value="${fmtParam(k, vals[k], m)}">${m.pct ? "<i>%</i>" : ""}</span>
        <small>Stil-Standard: ${fmtParam(k, g.defaults[sel][k], m)}${m.pct ? " %" : ""}</small></label>`;
    };
    return `<section class="card" style="margin-bottom:14px"><h2>Strategie</h2>
      <form id="${P}StratForm" autocomplete="off" style="padding:0 16px 16px">
        <div class="seg" style="margin:4px 0 6px">${Object.keys(g.defaults).map((k) => `<button type="button" data-style="${k}" class="${k === sel ? "on" : ""}">${esc(PF.state.styles[k]?.label || k)}</button>`).join("")}</div>
        <div class="hint" style="padding:0 0 10px">${STYLE_TXT[sel] || ""}${sel !== g.style ? " <b>Noch nicht gespeichert.</b>" : ""}</div>
        <div class="sgrid">${Object.keys(g.params).map(field).join("")}
          <label class="sfield" style="grid-column:1/-1">Prüfzeiten Mo–Fr<span class="sinput"><input name="times" value="${esc(g.decision_times.join(", "))}"><i>Uhr</i></span>
            <small>z. B. 09:35, 13:00, 16:00 · <a href="#" id="${P}Hourly">stündlich 09–21 Uhr</a></small></label></div>
        <div class="hint" style="padding:10px 0 0">Mindestsignal: ab welchem Bot-Signal (−1 bis +1) ein Kauf empfohlen wird – höher = wählerischer.
          Änderungen gelten sofort für die Stop-Marken; Umschichtungen kommen zum nächsten Prüftermin als Empfehlung.</div>
        <div class="field" style="padding:12px 0 0"><button class="btn" type="submit">Speichern</button>
          <button class="btn ghost" type="button" id="${P}StratStd">Stil-Standard einsetzen</button></div>
        <div class="formmsg${PF.stratSaved && Date.now() - PF.stratSaved < 8000 ? " ok" : ""}" id="${P}StratMsg">${PF.stratSaved && Date.now() - PF.stratSaved < 8000 ? "Gespeichert ✓" : ""}</div></form></section>`;
  }
  function bindStrategy() {
    const f = document.getElementById(P + "StratForm");
    if (!f) return;
    const redraw = () => { set("Strategy", strategyCard()); bindStrategy(); };
    f.querySelectorAll("[data-style]").forEach((b) => b.onclick = () => { PF.stratStyle = b.dataset.style; redraw(); });
    document.getElementById(P + "Hourly").onclick = (ev) => {
      ev.preventDefault();
      f.elements.times.value = Array.from({ length: 13 }, (_, i) => String(9 + i).padStart(2, "0") + ":00").join(", ");
    };
    document.getElementById(P + "StratStd").onclick = () => {
      const g = PF.state.strategy, sel = PF.stratStyle || g.style;
      Object.keys(g.params).forEach((k) => { f.elements[k].value = fmtParam(k, g.defaults[sel][k], g.params[k]); });
    };
    f.onsubmit = async (ev) => {
      ev.preventDefault();
      const g = PF.state.strategy, msgEl = document.getElementById(P + "StratMsg");
      const values = {};
      Object.keys(g.params).forEach((k) => { values[k] = f.elements[k].value; });
      const r = await POST("api/pf/strategy", { style: PF.stratStyle || g.style, values, decision_times: f.elements.times.value });
      if (!r.data.ok) { msgEl.className = "formmsg err"; msgEl.textContent = r.data.error; return; }
      PF.state.strategy = r.data.strategy; PF.state.style = r.data.strategy.style;
      PF.state.decision_times = r.data.strategy.decision_times; PF.stratStyle = null;
      PF.stratSaved = Date.now();
      redraw();
      load(true);
    };
  }


  // ------------------------------------------------------------------ Trade Republic
  function brokerKey() {
    const b = PF.state?.broker || {};
    return JSON.stringify([b.connected, b.expired, b.login?.status, b.login?.msg, b.running, b.last, b.error, b.compare?.time, b.changes?.time]);
  }
  function drawBroker(force) {
    if (MODE !== "model") return;
    const k = brokerKey();
    if (!force && k === PF.brokerKey) return;
    PF.brokerKey = k;
    set("Broker", brokerCard()); bindBroker();
    const ls = PF.state?.broker?.login?.status;
    if (["starting", "waiting"].includes(ls) || PF.state?.broker?.running) pollBroker();
  }
  let brokerPolling = false;
  async function pollBroker() {
    if (brokerPolling) return;
    brokerPolling = true;
    try {
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 2500));
        const st = await GET("api/pf/state");
        if (st && !st.error) PF.state = st;
        const b = PF.state?.broker || {};
        drawBroker();
        if (!["starting", "waiting"].includes(b.login?.status) && !b.running) {
          if (b.error) toast(b.error, "err");
          else if (b.login?.status === "error") toast(b.login.msg, "err");
          else toast(b.changes?.time && Date.now() - new Date(b.changes.time) < 120000 ? "✓ Abgleich fertig – Änderungen übernommen" : "✓ Abgleich fertig – alles stimmt überein");
          load(true); break;
        }
      }
    } finally { brokerPolling = false; }
  }
  function brokerCard() {
    const b = PF.state?.broker;
    if (!b) return "";
    const ls = b.login?.status || "idle";
    const head = `<section class="card" style="margin-bottom:14px"><h2>Trade Republic</h2><div style="padding:0 16px 16px">`;
    const foot = `</div></section>`;
    const msg = b.login?.msg ? `<div class="formmsg ${ls === "error" ? "err" : ""}" style="padding:8px 0 0">${esc(b.login.msg)}</div>` : "";
    if (ls === "starting" || ls === "waiting") {
      return head + `<div class="tr-wait"><span class="spin"></span><div><b>${esc(b.login.msg)}</b>
          ${ls === "waiting" ? '<div class="name">Öffne die Trade-Republic-App auf deinem iPhone und tippe auf „Bestätigen“. Das Fenster läuft ca. 2 Minuten.</div>' : ""}</div></div>
        <div class="field" style="padding:10px 0 0">${btn("Abbrechen", `id="${P}TrCancel"`)}</div>` + foot;
    }
    if (ls === "code") {
      return head + `<form id="${P}TrCode" class="txform" style="padding:0" autocomplete="off">
          <label>Code<input class="w-num" name="code" inputmode="numeric" autocomplete="one-time-code"></label>
          <button class="btn" type="submit">Bestätigen</button></form>${msg}
        <div class="field" style="padding:10px 0 0">${btn("Abbrechen", `id="${P}TrCancel"`)}</div>` + foot;
    }
    if (b.connected && !b.expired) {
      const r = b.changes?.report || {};
      const lines = [];
      if (r.matched?.length) lines.push(`✓ abgehakt: ${esc(r.matched.join(", "))}`);
      if (r.corrected?.length) lines.push(`✓ auf Cent korrigiert: ${esc(r.corrected.join(", "))}`);
      if (r.new?.length) lines.push(`＋ eigene Trades übernommen: ${esc(r.new.join(", "))}`);
      if (r.dividends?.length) lines.push(`＋ Dividende: ${esc(r.dividends.join(", "))}`);
      if (r.imported?.length) lines.push(`＋ aus dem Depot übernommen (zum TR-Einstand): ${esc(r.imported.join(", "))}`);
      if (r.deposits?.length) lines.push(`＋ Einzahlung nachgebucht: ${eur(r.deposits.reduce((a, b) => a + b, 0), 2)}`);
      if (r.open?.length) lines.push(`⏳ noch nicht in Trade Republic gefunden: ${esc(r.open.join(", "))}`);
      (r.problems || []).forEach((x) => lines.push(`<span class="down">⚠️ ${esc(x)}</span>`));
      const bookCash = PF.state?.cash;
      return head + `<div class="rules" style="padding:0 0 8px"><span><span class="status ok">verbunden</span> ${esc(b.phone || "")}</span>
          <span>Abgleich alle 30 Min. und bei „Jetzt prüfen“</span>
          <span>Zuletzt: ${b.last_ok ? dt(b.last_ok) : "noch nie"}${b.running ? " · läuft gerade …" : ""}</span>
          ${b.cash != null ? `<span>Cash bei TR ${eur(b.cash, 2)} · im Portfolio ${eur(bookCash, 2)}</span>` : ""}</div>
        ${b.error ? `<div class="formmsg err" style="padding:0 0 8px">${esc(b.error)}</div>` : ""}
        ${lines.length ? `<div class="name" style="padding:0 0 4px">Zuletzt übernommen (${dt(b.changes.time)}):</div><div class="tr-rep">${lines.map((x) => `<div>${x}</div>`).join("")}</div>` : ""}
        ${(b.report?.problems || []).length && b.changes?.report !== b.report ? `<div class="tr-rep" style="margin-top:6px">${b.report.problems.map((x) => `<div class="down">⚠️ ${esc(x)}</div>`).join("")}</div>` : ""}
        ${compareBlock(b.compare)}
        <div class="name" style="padding:8px 0 0;line-height:1.5">Trades zu Empfehlungen werden automatisch abgehakt – mit deinem echten Kurs, deiner Stückzahl und Gebühr.
          Trades ohne Empfehlung landen als „eigene“ Buchung im Portfolio, alle übrigen Positionen aus deinem TR-Depot werden mit dem Einstandskurs von TR übernommen.
          Fehlt für einen Kauf Cash im Portfolio, wird die Einzahlung automatisch nachgebucht.</div>
        <div class="field" style="padding:10px 0 0">${btn(b.running ? "Gleiche ab …" : "⟳ Jetzt abgleichen", `id="${P}TrSync" ${b.running ? "disabled" : ""}`, true)}${btn("Trennen", `id="${P}TrOut"`)}</div>` + foot;
    }
    return head + `${b.expired ? '<div class="formmsg err" style="padding:0 0 8px">Die Anmeldung ist abgelaufen – bitte neu verbinden. Bisher Abgeglichenes bleibt erhalten.</div>' : ""}
      <div class="name" style="padding:0 0 10px;line-height:1.5">Deine Käufe und Verkäufe werden automatisch übernommen und die passenden Empfehlungen abgehakt.
        <b>Nur lesend</b> – Orders sind nicht möglich. Die PIN wird nicht gespeichert, die Anmeldung bestätigst du in der Trade-Republic-App.
        Inoffizielle Schnittstelle: kann ausfallen, wenn Trade Republic etwas ändert.</div>
      <form id="${P}TrLogin" class="txform" style="padding:0" autocomplete="off">
        <label>Telefonnummer<input name="phone" inputmode="tel" placeholder="+49 171 1234567" autocomplete="off" style="width:170px"></label>
        <label>PIN<input class="w-num" name="pin" type="password" inputmode="numeric" maxlength="4" autocomplete="off"></label>
        <button class="btn" type="submit">Verbinden</button></form>${msg}` + foot;
  }
  function compareBlock(c) {
    if (!c) return "";
    const q = (x) => x == null ? "–" : nf(x % 1 ? 4 : 0).format(x);
    const bad = c.rows.filter((r) => !r.ok && !r.pending), wait = c.rows.filter((r) => r.pending);
    const head = bad.length
      ? `<b class="down">${bad.length} Abweichung${bad.length > 1 ? "en" : ""}</b> · ${c.rows.length - bad.length} von ${c.rows.length} Positionen stimmen`
      : `<span class="status ok">✓ ${c.rows.length - wait.length} von ${c.rows.length} Positionen stimmen genau${wait.length ? ", der Rest wartet auf eine offene Empfehlung" : ""}</span>`;
    const rows = bad.map((r) => `<div class="cmp-row"><div><b>${esc(r.name)}</b>
        <div class="name">Portfolio ${q(r.book)} Stk · Trade Republic ${r.tr == null ? "unbekannt (keine ISIN)" : q(r.tr) + " Stk"}</div></div>
        ${r.action_id ? btn("Nicht gekauft – entfernen", `data-cmp-reject="${esc(r.action_id)}" data-cmp-name="${esc(r.name)}"`) : ""}</div>`).join("");
    const waits = wait.map((r) => `<div class="cmp-row"><div><b>${esc(r.name)}</b>
        <div class="name">⏳ Empfehlung noch offen – Portfolio ${q(r.book)} Stk · Trade Republic ${q(r.tr)} Stk</div></div></div>`).join("");
    const extra = c.only_tr?.length ? `<div class="name" style="padding-top:6px">Noch nicht übernommen: ${c.only_tr.map((x) => `${esc(x.name || x.isin)} ${q(x.tr)} Stk`).join(", ")}</div>` : "";
    return `<div style="padding-top:10px"><div style="font-size:13px;padding-bottom:4px">Bestandsvergleich: ${head}</div>${rows}${waits}${extra}</div>`;
  }
  function bindBroker() {
    const $ = (id) => document.getElementById(P + id);
    const act = async (op, body = {}) => {
      const r = await POST("api/pf/tr/" + op, body);
      if (!r.data.ok) { toast(r.data.error, "err"); return; }
      PF.state.broker = r.data.broker; drawBroker(true);
    };
    if ($("TrLogin")) $("TrLogin").onsubmit = (ev) => {
      ev.preventDefault();
      const f = ev.target, body = { phone: f.phone.value, pin: f.pin.value };
      f.pin.value = "";
      act("login", body);
    };
    if ($("TrCode")) $("TrCode").onsubmit = (ev) => { ev.preventDefault(); act("code", { code: ev.target.code.value }); };
    if ($("TrCancel")) $("TrCancel").onclick = () => act("cancel");
    if ($("TrSync")) $("TrSync").onclick = () => act("sync");
    document.querySelectorAll("[data-cmp-reject]").forEach((x) => x.onclick = async () => {
      if (!confirm(`${x.dataset.cmpName} nicht gekauft? Der Kauf wird aus dem Portfolio entfernt (das Cash kommt zurück).`)) return;
      const r = await POST("api/pf/action", { id: x.dataset.cmpReject, do: "reject" });
      if (!r.data.ok) { toast(r.data.error, "err"); return; }
      await POST("api/pf/tr/sync", {});
      load(true);
    });
    if ($("TrOut")) $("TrOut").onclick = () => { if (confirm("Verbindung zu Trade Republic trennen? Bereits übernommene Buchungen bleiben.")) act("logout"); };
  }


  // ------------------------------------------------------------------ Spekulativer Hebel-Vorschlag
  const DIR = { long: "Long", short: "Short" };
  const fmt2 = (x) => x == null ? "–" : nf(2).format(x);
  function specCard() {
    const sp = PF.state?.spec;
    if (!sp?.enabled) return "";
    const act = sp.active || [];
    const todayProp = (sp.history || []).find((h) => h.day === new Date().toISOString().slice(0, 10));
    const plan = sp.plan_next;
    const stat = sp.stats?.n ? `${sp.stats.wins} von ${sp.stats.n} im Plus · Ø ${pct(sp.stats.avg_product_pct, 0)} auf den Schein` : "noch keine abgeschlossenen Vorschläge";
    const prop = (h) => {
      const now = h.product_pct_now;
      return `<div class="todo" style="background:var(--surface-2)">
        <div class="todo-top"><span class="tag ${h.direction === "long" ? "long" : "short"}">${DIR[h.direction]}</span>
          <span class="name">${esc(h.day)} · ${h.grade ? `Qualität ${esc(h.grade)} · ` : ""}Überzeugung ${pct(h.conviction, 0, false)} · ${esc(h.provider || "")}</span>
          ${h.result ? `<span class="status ${h.result === "Ziel" ? "ok" : "bad"}">${esc(h.result)}</span>` : ""}</div>
        <div class="todo-main"><b>${esc(h.name)}</b> ${h.ko ? `<span class="name">via ${esc(h.ko.name)} · ${esc(h.ko.isin)}</span>` : ""}</div>
        <div class="lvl3"><div><span>Einstieg</span><b>${fmt2(h.entry)}</b></div><div><span>Stop-Loss</span><b class="down">${fmt2(h.stop)}</b></div>
          <div><span>Ziel</span><b class="up">${fmt2(h.target)}</b></div></div>
        <div class="name" style="margin-top:-2px">Kurse des Basiswerts in ${esc(h.currency)} · Chance/Risiko ${h.rr ? nf(1).format(h.rr) : "–"}</div>
        ${h.leverage ? `<div class="kv"><span>Hebel · Einsatz höchstens</span><b>${nf(1).format(h.leverage)}x · ${h.stake ? eur(h.stake) : "–"}</b></div>` : ""}
        ${h.cash_short ? `<div class="name down">⚠️ Für den Einsatz fehlen ca. ${eur(h.cash_short)} Cash im Portfolio</div>` : ""}
        ${h.product_entry ? `<div class="kv"><span>Schein Einstieg · Stop · Ziel</span><b>${fmt2(h.product_entry)} · <span class="down">${fmt2(h.product_stop)}</span> · <span class="up">${fmt2(h.product_target)}</span> €</b></div>` : ""}
        ${now != null && !h.result ? `<div class="kv"><span>Stand (Schein, ca.)</span><b class="${cls(now)}">${pct(now, 0)}</b></div>` : ""}
        ${h.result && h.product_pct != null ? `<div class="kv"><span>Ergebnis (Schein, ca.)</span><b class="${cls(h.product_pct)}">${pct(h.product_pct, 0)}</b></div>` : ""}
        <div class="reason" style="max-width:none">${esc(h.rationale || h.note || "")}</div>
        ${!h.result || h.result === "offen" ? `<div class="field" style="padding:4px 0 0">
          <a class="btn" href="https://app.traderepublic.com/instrument/${encodeURIComponent((h.ko && h.ko.isin) || h.isin)}" target="_blank" rel="noopener">In Trade Republic öffnen ↗</a>
          ${h.ko ? `<button class="btn ghost" data-copy="${esc(h.ko.isin)}">ISIN kopieren</button>` : ""}</div>` : ""}</div>`;
    };
    const body = act.length ? act.map(prop).join("")
      : todayProp ? prop(todayProp)
      : `<div class="name">Heute kein offener Vorschlag.</div>`;
    const pr = sp.progress || {};
    const progress = sp.preparing ? `<div class="progress"><span class="spin sm"></span><div style="flex:1">
        <div>${esc(pr.text || "KI arbeitet …")}</div>
        ${pr.total ? `<div class="pbar"><i style="width:${Math.round((pr.done / pr.total) * 100)}%"></i></div>` : ""}
        ${(pr.results || []).length ? `<div class="name" style="margin-top:4px">${pr.results.map((r) => `${r.ok ? "✓" : "✗"} ${esc(r.name)}${r.ok ? " " + DIR[r.direction] : ""}`).join(" · ")}</div>` : ""}
      </div></div>` : "";
    const next = sp.preparing ? "wird gerade vorbereitet …"
      : plan ? (plan.manual ? `Vorschau: ${plan.idea ? `${DIR[plan.idea.direction]} ${esc(plan.idea.name)}` : "kein lohnendes Setup"} – endgültig um 22:30 Uhr`
        : plan.idea ? `Plan: ${DIR[plan.idea.direction]} ${esc(plan.idea.name)} (Push um 9 Uhr)` : "Plan: kein lohnendes Setup")
      : "Plan steht um 22:30 Uhr";
    return `<section class="card" style="margin-bottom:14px"><div class="todo-head"><h2>🎲 Spekulation</h2><span class="name">${stat}</span></div>
      <div style="padding:10px 16px 14px;display:flex;flex-direction:column;gap:10px">${body}
        ${progress}<div class="name">Nächster Handelstag: ${next}</div>${cashLine()}${watchList(sp.watch)}</div></section>`;
  }
  // Cash-Planung für den Hebel-Trade (wird bei jeder Prüfung und nach dem 22:30-Plan neu gerechnet)
  function cashLine() {
    const c = PF.state?.cash_plan;
    if (!c || !c.need) return "";
    const ok = !c.short;
    const tr = c.cash_tr != null ? ` · bei Trade Republic ${eur(c.cash_tr)}` : "";
    const fix = c.sells?.length ? ` · gedeckt durch Teilverkauf ${c.sells.map(esc).join(", ")} (≈ ${eur(c.freed)})`
      : !ok ? " · Lücke zu klein für einen Extra-Verkauf – Einsatz dann etwas kleiner" : "";
    return `<div class="kv" title="Stand ${c.time ? dt(c.time) : "–"} · ${esc(c.source || "")}"><span>💶 Cash-Plan: Einsatz ~${eur(c.need)}
        <span class="name">(${esc(c.source || "")})</span></span>
      <b class="${ok || c.sells?.length ? "up" : "down"}">${ok ? "✓ gedeckt" : c.sells?.length ? "✓ wird frei" : "⚠ fehlt " + eur(c.short)}</b></div>
      <div class="name" style="margin-top:-6px">Verfügbar ${eur(c.available)}${tr}${fix}</div>`;
  }
  function watchList(w) {
    if (!w || !w.items?.length) return "";
    const done = w.items.filter((e) => e.evaluated).length;
    const row = (e) => {
      const lead = e.symbol === w.leader;
      const st = !e.evaluated ? (e.why && !/nicht erreichbar/.test(e.why) ? `<span class="down">${esc(e.why)}</span>`
          : `<span class="name">⏳ ${e.why ? "KI kurz nicht erreichbar – neuer Versuch folgt" : "wartet auf die KI"}</span>`)
        : e.ok ? `<b>${DIR[e.direction]}</b> · Überzeugung ${pct(e.conviction, 0, false)} · Chance/Risiko ${nf(1).format(e.rr)}`
        : `<span class="name">✗ ${esc(e.direction && e.direction !== "none" ? DIR[e.direction] + ": " : "")}${esc(e.why)}</span>`;
      return `<div class="cmp-row${lead ? " lead" : ""}"><div style="min-width:0"><b>${lead ? "⭐ " : ""}${esc(e.name)}</b>
          <span class="name">Signal ${e.score >= 0 ? "+" : ""}${nf(2).format(e.score)}${e.vol_day ? " · bewegt sich ~" + nf(1).format(e.vol_day * 100) + " %/Tag" : ""}${e.evaluated ? " · bewertet " + dt(e.evaluated) : ""}</span>
          ${e.earnings ? `<span class="status warn">📅 Zahlen ${new Date(e.earnings).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })}</span>` : ""}
          <div style="font-size:12.5px">${st}</div>
          ${lead && e.rationale ? `<div class="reason" style="max-width:none;margin-top:4px">${esc(e.rationale)}</div>` : ""}</div></div>`;
    };
    return `<details class="watch" ${PF.watchOpen ? "open" : ""}><summary>Vorauswahl für ${esc(w.for_day)} · ${done}/${w.items.length} bewertet${w.leader ? " · Favorit ⭐" : ""}</summary>
      ${w.items.map(row).join("")}</details>`;
  }
  function specSettingsCard() {
    const sp = PF.state?.spec || {};
    const plan = sp.plan_next;
    return `<section class="card" style="margin-bottom:14px"><h2>Spekulativer Hebel-Vorschlag</h2>
      <div style="padding:0 16px 16px">
        <label class="switch"><input type="checkbox" id="${P}SpecOn" ${sp.enabled ? "checked" : ""}><span></span>
          <b>${sp.enabled ? "Eingeschaltet" : "Ausgeschaltet"}</b></label>
        <div class="name" style="padding:8px 0 0;line-height:1.5">Mo–Fr um 9 Uhr ein Vorschlag für einen Knock-out-Schein, der bei Trade Republic handelbar ist –
          mit Stop-Loss, Kursziel und Einsatz (am Stop höchstens 1 % deines Depots). Die KI (Gemini, sonst Claude) bewertet
          tagsüber nach und nach die Vorauswahl, um 22:30 Uhr steht der Plan, um 9 Uhr wird er noch einmal geprüft. Gekauft wird nichts – du entscheidest. Hebelprodukte können schnell wertlos werden.</div>
        ${sp.enabled && sp.preparing ? `<div class="progress" style="margin-top:10px"><span class="spin sm"></span>${esc(sp.progress?.text || "KI arbeitet …")}</div>` : ""}
        ${sp.enabled ? `<div class="field" style="padding:10px 0 0">${btn(sp.preparing ? "Wird vorbereitet …" : "Vorschau jetzt", `id="${P}SpecPrep" ${sp.preparing ? "disabled" : ""}`)}
          <span class="name">${plan ? `Plan für ${esc(plan.for_day)}: ${plan.idea ? `${DIR[plan.idea.direction]} ${esc(plan.idea.name)}, Chance/Risiko ${nf(1).format(plan.idea.rr)}` : "kein Setup"}` : ""}</span></div>
          ${plan?.rejected?.length ? `<div class="name" style="padding-top:6px">Verworfen: ${plan.rejected.map((r) => `${esc(r.name)} (${esc(r.why)})`).join(", ")}</div>` : ""}` : ""}
      </div></section>`;
  }
  function redrawSpec() {
    set("Spec", specCard());
    const open = document.activeElement?.closest?.(`#${P}SpecSet`);   // nicht ins Formular hineinzeichnen
    if (!open) set("SpecSet", specSettingsCard());
    bindSpec();
  }
  let specPolling = false;
  async function pollSpec() {
    if (specPolling) return;
    specPolling = true;
    try {
      for (let i = 0; i < 200; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        const st = await GET("api/pf/state");
        if (!st || st.error) continue;
        PF.state.spec = st.spec;
        redrawSpec();
        if (!st.spec?.preparing) {
          const p = st.spec?.plan_next;
          toast(p ? (p.idea ? `Vorschau fertig: ${DIR[p.idea.direction]} ${p.idea.name} (Chance/Risiko ${nf(1).format(p.idea.rr)})` : "Vorschau fertig: kein lohnendes Setup") : "Vorschau beendet", "ok", 6000);
          break;
        }
      }
    } finally { specPolling = false; }
  }
  function bindSpec() {
    document.querySelectorAll("details.watch").forEach((d) => d.ontoggle = () => { PF.watchOpen = d.open; });
    document.querySelectorAll("[data-copy]").forEach((b) => b.onclick = async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); toast(`ISIN ${b.dataset.copy} kopiert – in der TR-App in die Suche einfügen`); }
      catch (e) { toast(`ISIN: ${b.dataset.copy}`, "info", 8000); }
    });
    const on = document.getElementById(P + "SpecOn");
    if (on) on.onchange = async () => {
      const r = await POST("api/pf/spec", { enabled: on.checked });
      if (!r.data.ok) { toast(r.data.error, "err"); on.checked = !on.checked; return; }
      PF.state.spec = r.data.spec; set("SpecSet", specSettingsCard()); set("Spec", specCard()); bindSpec();
    };
    const prep = document.getElementById(P + "SpecPrep");
    if (prep) prep.onclick = async () => {
      const r = await POST("api/pf/spec/prepare", {});
      if (!r.data.ok) { toast(r.data.error, "err"); return; }
      PF.state.spec = { ...r.data.spec, preparing: true }; redrawSpec();
      pollSpec();
    };
    if (PF.state?.spec?.preparing) pollSpec();
  }


  // ------------------------------------------------------------------ Heute: Marktlage + KI-Durchsicht
  function todayCard() {
    const reg = PF.state?.regime, rev = PF.state?.review;
    if (!reg && !rev) return "";
    const today = new Date().toISOString().slice(0, 10);
    const fresh = rev && rev.day === today && !rev.error;
    const flagged = fresh ? (rev.positions || []).filter((p) => p.view !== "halten") : [];
    const icon = { positiv: "🟢", neutral: "🟡", negativ: "🔴" }[reg?.state] || "⚪";
    return `<section class="card today-card" style="margin-bottom:14px"><h2>Heute</h2><div style="padding:0 16px 14px;display:flex;flex-direction:column;gap:8px">
      ${reg ? `<div class="kv"><span>Marktlage</span><b>${icon} ${esc(reg.text)}</b></div>
        <div class="kv"><span>Zielanteil investiert</span><b>${pct(reg.invest, 0, false)}</b></div>` : ""}
      ${fresh ? `<div style="font-size:13.5px;line-height:1.5">☀️ ${esc(rev.summary)} <span class="name">(${esc(rev.provider || "KI")})</span></div>
        ${flagged.map((p) => `<div class="kv"><span>${esc(p.symbol)}</span><b class="${p.view === "verkaufen" ? "down" : ""}">${esc(p.view)}</b></div>
          <div class="name" style="margin-top:-4px">${esc(p.reason)}</div>`).join("")}
        ${(rev.risks || []).length ? `<div class="name">Risiken: ${rev.risks.map(esc).join(" · ")}</div>` : ""}`
        : `<div class="name">KI-Durchsicht des Depots kommt Mo–Fr um 8:30 Uhr${rev?.error ? ` (zuletzt fehlgeschlagen: ${esc(rev.error)})` : ""}.</div>`}
    </div></section>`;
  }


  // ------------------------------------------------------------------ Lernen aus den Empfehlungen
  function learnCard() {
    const L = PF.state?.pf_learning;
    if (!L) return "";
    const q = (o) => o.n ? `${pct(o.hit, 0, false)} richtig · Ø ${pct(o.excess, 1)} ggü. MSCI World (${o.n})` : "noch keine";
    const kindTxt = { signal: "Signal", rebalance: "Nachkauf", trim: "Kürzen", stop: "Stop", setup: "Aufbau" };
    return `<section class="card" style="margin-bottom:14px"><h2>Lernt aus seinen Empfehlungen</h2>
      <div style="padding:0 16px 16px;display:flex;flex-direction:column;gap:8px">
        <div class="kv"><span>Käufe</span><b>${q(L.buys)}</b></div>
        <div class="kv"><span>Verkäufe</span><b>${q(L.sells)}</b></div>
        ${L.pending ? `<div class="name">${L.pending} Empfehlung(en) warten noch auf die Auswertung (nach 5 und 20 Handelstagen).</div>` : ""}
        <div class="tr-rep">${(L.lessons.length ? L.lessons : ["Noch keine Anpassungen – bisherige Empfehlungen liegen im Rahmen."]).map((l) => `<div>${esc(l)}</div>`).join("")}</div>
        ${(L.recent || []).map((r) => `<div class="kv"><span>${r.side === "buy" ? "Kauf" : (kindTxt[r.kind] || "Verkauf")} ${esc(r.name)} · ${dt(r.time)}</span>
          <b class="${r.good ? "up" : "down"}">${r.good ? "✓" : "✗"} ${pct(r.excess, 1)}</b></div>`).join("")}
        <div class="name">Gemessen wird gegen den MSCI World: Ein Kauf war gut, wenn die Aktie danach besser lief als der Markt;
          ein Verkauf, wenn sie danach schlechter lief. Angepasst wird erst ab 8 Bewertungen und nur in festen Grenzen.</div>
      </div></section>`;
  }

  // ================================================================== Portfolio: Aufbau der Seite
  const SUBS = [["overview", "Übersicht"], ["analyse", "Analyse"], ["verlauf", "Verlauf"]];
  try { PF.sub = localStorage.getItem("nt-sub") || "overview"; } catch (e) { PF.sub = "overview"; }
  if (!SUBS.some(([k]) => k === PF.sub)) PF.sub = "overview";   // „Einstellungen“ ist jetzt ein eigener Menüpunkt
  function renderModel(full) {
    const el = root();
    if (layout !== "model") {
      layout = "model";
      el.innerHTML = `<nav class="subnav" id="${P}Sub">${SUBS.map(([k, v]) => `<button data-sub="${k}">${v}</button>`).join("")}</nav>
        <div id="${P}S-overview"><div id="${P}Value"></div><div id="${P}Todo"></div><div id="${P}Spec"></div>
          <div id="${P}Today"></div><div id="${P}Pos"></div></div>
        <div id="${P}S-analyse" class="hidden"><div id="${P}Kpi"></div>
          <section class="grid pf-two"><div id="${P}Perf"></div><div id="${P}AllocBox"></div></section>
          <div id="${P}Learn"></div><div id="${P}Monthly"></div><div id="${P}Risk"></div></div>
        <div id="${P}S-verlauf" class="hidden"><div id="${P}Actions"></div><div id="${P}TxBox"></div></div>`;
      // Einstellungs-Karten (Trade Republic, Strategie, Spekulation, Geld) stehen im Menüpunkt „Einstellungen“ (index.html)
      root().querySelectorAll("[data-sub]").forEach((b) => b.onclick = () => showSub(b.dataset.sub));
      full = true;
    }
    // offenes „Anders gemacht“-Formular nicht durch die Aktualisierung wegwerfen
    const keep = PF.todoOpen && document.querySelector(`#${P}Todo [data-todo-id="${PF.todoOpen}"] .todo-form:not(.hidden)`)
      && unconfirmed().some((a) => a.id === PF.todoOpen);
    if (!keep) { PF.todoOpen = null; set("Todo", todoCard()); bindTodo(); }
    set("Spec", specCard()); bindSpec();
    if (full) { set("SpecSet", specSettingsCard()); bindSpec(); }
    set("Pos", positionList()); bindPositionList();
    set("Today", todayCard());
    set("Learn", learnCard());
    set("Kpi", kpis());
    set("AllocBox", allocCard()); bindAlloc();
    set("Actions", actionsCard()); bindActions();
    // Live-Ansichten (letzte Stunde, Multigraph heute) bei jeder Aktualisierung neu laden
    if (full || !PF.values || PF.vRange === "1h" || (PF.multi && PF.vRange === "1T")) loadValues(); else drawValue();
    if (full) {
      set("Perf", perfCard()); bindRange();
      set("Monthly", monthlyCard());
      set("Risk", riskCard());
      set("Alerts", settingsCard()); bindSettings();
      set("Strategy", strategyCard()); bindStrategy();

      set("TxBox", txSection()); bindTx();
    }
    drawBroker();
    showSub(PF.sub, true);
  }
  function showSub(k, quiet) {
    PF.sub = k;
    try { localStorage.setItem("nt-sub", k); } catch (e) {}
    SUBS.forEach(([x]) => document.getElementById(P + "S-" + x)?.classList.toggle("hidden", x !== k));
    root().querySelectorAll("[data-sub]").forEach((b) => b.classList.toggle("on", b.dataset.sub === k));
    if (k === "analyse") drawPerf();  // Grafik braucht die sichtbare Breite
    if (k === "overview" && PF.values) drawValue();
    if (!quiet) window.scrollTo({ top: 0 });
  }

  // ------------------------------------------------------------------ Zu erledigen (nur offene Empfehlungen)
  function todoCard() {
    const open = unconfirmed();
    if (!open.length) return "";
    const card = (a) => {
      const buy = a.side === "buy";
      const qty = nf(a.qty % 1 ? 4 : 0).format(a.qty);
      return `<div class="todo" data-todo-id="${esc(a.id)}">
        <div class="todo-top"><span class="tag ${buy ? "long" : "short"}">${buy ? "Kaufen" : "Verkaufen"}</span>
          <span class="name">${KIND[a.kind] || ""} · ${dt(a.time)}</span></div>
        <div class="todo-main"><b>${qty} × ${esc(a.name)}</b> <span class="name">${esc(a.symbol)}</span></div>
        <div class="todo-sub">zu ca. <b>${nf(2).format(a.price)}&nbsp;€</b> = ${eur(a.value)}${buy ? ` · Stop-Loss bei&nbsp;${nf(2).format(a.price * (1 - (PF.state?.strategy?.values?.stop || 0.1)))}&nbsp;€` : ""}</div>
        <div class="reason" style="max-width:none">${esc(a.reason)}</div>
        <div class="todo-btns">${btn("✓ Erledigt", 'data-t="done"', true)}${btn("Anders gemacht", 'data-t="other"')}${btn("Nicht gemacht", 'data-t="no"')}</div>
        ${a.isin ? `<a class="btn ghost tr-link" href="https://app.traderepublic.com/instrument/${encodeURIComponent(a.isin)}" target="_blank" rel="noopener">In Trade Republic öffnen ↗</a>` : ""}
        <div class="todo-form hidden"></div></div>`;
    };
    return `<section class="card todo-card"><div class="todo-head"><h2>Zu erledigen · ${open.length}</h2>
        ${open.length > 1 ? btn(`✓ Alle ${open.length} erledigt`, 'data-confirm-all="1"', true) : ""}</div>
      <div class="hint">Setz diese Empfehlungen bei deinem Broker um und hak sie hier ab. Anderer Kurs oder andere Stückzahl → „Anders gemacht“.</div>
      <div class="todo-list">${open.map(card).join("")}</div></section>`;
  }
  function bindTodo() {
    root().querySelectorAll(".todo-card [data-confirm-all]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Alle ${unconfirmed().length} Empfehlungen als genau so umgesetzt abhaken?`)) return;
      const r = await POST("api/pf/action", { do: "confirm_all" });
      if (!r.data.ok) toast(r.data.error, "err"); else load(false);
    });
    root().querySelectorAll(".todo [data-t]").forEach((b) => b.onclick = async () => {
      const box = b.closest(".todo"), id = box.dataset.todoId;
      const a = (PF.state.actions || []).find((x) => x.id === id);
      if (b.dataset.t === "done") {
        const r = await POST("api/pf/action", { id, do: "confirm" });
        if (!r.data.ok) toast(r.data.error, "err"); else load(false);
        return;
      }
      if (b.dataset.t === "no") {
        if (!confirm(`${a.side === "buy" ? "Kauf" : "Verkauf"} von ${a.name} nicht gemacht? Er wird aus deinem Depot entfernt.`)) return;
        const r = await POST("api/pf/action", { id, do: "reject" });
        if (!r.data.ok) toast(r.data.error, "err"); else load(true);
        return;
      }
      const f = box.querySelector(".todo-form");
      if (!f.classList.contains("hidden")) { f.classList.add("hidden"); PF.todoOpen = null; return; }
      PF.todoOpen = id;
      f.classList.remove("hidden");
      f.innerHTML = adjustForm(a);
      bindAdjust(f, id);
    });
  }
  function adjustForm(a) {
    return `<form class="txform" style="padding:8px 0 0" autocomplete="off">
        <label>Datum<input class="w-date" type="date" name="date" value="${a.time.slice(0, 10)}" max="${today()}"></label>
        <label>Stück<input class="w-num" name="qty" inputmode="decimal" value="${String(a.qty).replace(".", ",")}"></label>
        <label>Dein Kurs €<input class="w-num" name="price" inputmode="decimal" value="${a.price.toFixed(2).replace(".", ",")}"></label>
        <label>Gebühr €<input class="w-num" name="fee" inputmode="decimal" value="1"></label>
        <button class="btn" type="submit">Speichern</button></form><div class="formmsg"></div>`;
  }
  function bindAdjust(box, id) {
    const f = box.querySelector("form"), msgEl = box.querySelector(".formmsg");
    const send = async (force) => {
      const r = await POST("api/pf/action", { id, do: "adjust", force, ...Object.fromEntries(new FormData(f)) });
      if (r.status === 409 && r.data.code === "price_check") {
        msgEl.innerHTML = `<div class="confirm">${esc(r.data.error)} <button class="btn" type="button">Trotzdem speichern</button></div>`;
        msgEl.querySelector("button").onclick = () => send(true);
        return;
      }
      if (!r.data.ok) { msgEl.className = "formmsg err"; msgEl.textContent = r.data.error; return; }
      load(true);
    };
    f.onsubmit = (ev) => { ev.preventDefault(); send(false); };
  }

  // ------------------------------------------------------------------ Positionen als Liste (wie im Broker)
  const openPos = new Set();
  function positionList() {
    const ps = PF.ov.positions, k = PF.ov.kpi;
    const share = (v) => (k.total ? v / k.total : 0);
    const rules = { buy: STATE?.rules?.buy ?? 0.25, sell: STATE?.rules?.sell ?? -0.15, shorts: false };
    const item = (p) => {
      const open = openPos.has(p.symbol), pending = unconfirmed(p.symbol).length;
      const below = p.price <= p.stop_level;
      return `<div class="pos${open ? " open" : ""}" data-pos="${esc(p.symbol)}">
        <div class="pos-row">
          <div class="pos-l"><div class="pos-name">${esc(p.name)}${pending ? ' <span class="status warn">offen</span>' : ""}${below ? ' <span class="status bad">unter Stop</span>' : ""}</div>
            <div class="name">${nf(p.qty < 1 ? 4 : p.qty % 1 ? 3 : 0).format(p.qty)} Stk · ${pct(share(p.value), 1, false)} · heute <span class="${cls(p.day_change)}">${pct(p.day_change, 1)}</span></div></div>
          <div class="pos-r"><div class="pos-val">${eur(p.value, 2)}</div>
            <div class="${cls(p.pnl)}" style="font-size:12.5px">${signedEur(p.pnl, 2)} · ${pct(p.pnl_pct, 1)}</div></div>
        </div>
        ${open ? `<div class="pos-detail">
          <div class="kv"><span>Einstand</span><b>${nf(2).format(p.avg_price)} €</b></div>
          <div class="kv"><span>Kurs</span><b><span class="dotsrc${p.stale ? " stale" : ""}"></span>${nf(2).format(p.price)} €</b></div>
          <div class="kv"><span>Stop-Marke</span><b class="${below ? "down" : ""}">${nf(2).format(p.stop_level)} €</b></div>
          <div class="kv"><span>Branche · Land</span><b>${esc(p.sector || "–")} · ${esc(p.country || "–")}</b></div>
          <div class="kv"><span>Bot-Signal</span>${p.signal == null ? "<b>–</b>" : scoreCell(p.signal, true, true, rules)}</div>
          ${p.signal_summary ? `<div class="reason" style="max-width:none;margin-top:6px">${esc(p.signal_summary)}</div>` : ""}
          <div class="pos-chart"><div class="seg">${POS_RANGES.map((r) => `<button data-pr="${r}" class="${r === PF.posRange ? "on" : ""}">${r}</button>`).join("")}</div>
            <div class="pc-mini" id="${pcId(p.symbol)}"></div></div>
        </div>` : ""}</div>`;
    };
    const cashRow = k.cash != null ? `<div class="pos cash"><div class="pos-row"><div class="pos-l"><div class="pos-name">Cash</div>
        <div class="name">nicht investiert · ${pct(share(k.cash), 1, false)}</div></div><div class="pos-r"><div class="pos-val">${eur(k.cash, 2)}</div></div></div></div>` : "";
    return `<section class="card" style="margin-bottom:14px"><div class="todo-head"><h2>Positionen · ${ps.length}</h2>
        <span class="name">Antippen für Details</span></div>
      <div class="pos-list">${ps.map(item).join("")}${cashRow}</div></section>`;
  }
  function bindPositionList() {
    root().querySelectorAll(".pos[data-pos] .pos-row").forEach((r) => r.onclick = () => {
      const sym = r.parentElement.dataset.pos;
      openPos.has(sym) ? openPos.delete(sym) : openPos.add(sym);
      set("Pos", positionList()); bindPositionList();
    });
    root().querySelectorAll(".pos-chart [data-pr]").forEach((b) => b.onclick = (ev) => {
      ev.stopPropagation();
      PF.posRange = b.dataset.pr;
      try { localStorage.setItem("nt-pos-range", PF.posRange); } catch (e) {}
      root().querySelectorAll(".pos-chart [data-pr]").forEach((x) => x.classList.toggle("on", x.dataset.pr === PF.posRange));
      openPos.forEach((sym) => { drawPosChart(sym); loadPosChart(sym); });
    });
    openPos.forEach((sym) => { drawPosChart(sym); loadPosChart(sym); });
  }

  // ------------------------------------------------------------------ Wertverlauf (oben)
  const VRANGES = ["1h", "1T", "1W", "1M", "3M", "YTD", "1J", "Max"];
  async function loadValues() {
    const want = PF.vRange;
    const [v, mg] = await Promise.all([GET(`api/pf/values?range=${encodeURIComponent(want)}`),
      PF.multi ? GET(`api/pf/multi?range=${encodeURIComponent(want)}`) : Promise.resolve(null)]);
    if (want !== PF.vRange) return;
    PF.values = v && !v.error ? v : { points: [], change: {} };
    if (PF.multi) PF.multiData = mg && !mg.error ? mg : { t: [], series: {}, cash: [], names: {} };
    drawValue();
  }
  function checkStatus() {
    const c = PF.state?.check || {};
    if (c.running) return `<span class="name">${esc(c.step || "Prüfe …")}</span>`;
    if (c.result) return `<span class="name">${esc(c.result)}</span>`;
    return `<span class="name">Nächste Prüfung: ${nextDecision()}</span>`;
  }
  async function pollCheck() {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 4000));
      const st = await GET("api/pf/state");
      if (st && !st.error) PF.state = st;
      drawValue();
      if (!PF.state?.check?.running) { toast(PF.state?.check?.result || "Prüfung fertig", "ok", 6000); load(true); return; }
    }
  }
  function drawValue() {
    const el = document.getElementById(P + "Value");
    if (!el || !PF.ov) return;
    const k = PF.ov.kpi;
    // aktuellen Live-Wert als letzten Punkt anhängen
    let pts = (PF.values?.points || []).map((x) => ({ t: x.t, v: x.v }));
    const nowIso = new Date().toISOString();
    if (PF.values?.intraday) pts.push({ t: nowIso, v: k.total });
    else if (pts.length) { if (pts[pts.length - 1].t === today()) pts[pts.length - 1] = { t: today(), v: k.total }; else pts.push({ t: today(), v: k.total }); }
    // Veränderung = Server-Wert (Ein-/Auszahlungen schon herausgerechnet) + Bewegung seit dem letzten gespeicherten Punkt
    const srv = PF.values?.points || [];
    let ch = {};
    if (pts.length >= 2 && srv.length) {
      const abs = (srv.length >= 2 ? (PF.values.change?.abs ?? 0) : 0) + (k.total - srv[srv.length - 1].v);
      ch = { abs, pct: pts[0].v ? abs / pts[0].v : null };
    }
    const up = (ch.abs ?? 0) >= 0;
    const label = { "1h": "letzte Stunde", "1T": "heute", "1W": "1 Woche", "1M": "1 Monat", "3M": "3 Monate", YTD: "seit Jahresbeginn", "1J": "1 Jahr", Max: "seit Start" }[PF.vRange];
    el.innerHTML = `<section class="card" style="margin-bottom:14px;padding:16px 16px 10px">
        <div class="label name" style="font-size:12.5px">Portfoliowert</div>
        <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
          <div style="font-size:32px;font-weight:600;letter-spacing:-0.01em;font-variant-numeric:tabular-nums">${eur(k.total, 2)}</div>
          <div class="${ch.abs == null ? "" : up ? "up" : "down"}" style="font-size:15px;font-weight:600;font-variant-numeric:tabular-nums">
            ${ch.abs == null ? "" : `${up ? "▲" : "▼"} ${signedEur(ch.abs, 2)} (${pct(ch.pct, 2)})`} <span class="name" style="font-weight:400">${label}</span></div>
        </div>
        ${k.cash != null && k.total > 0 ? `<div class="splitbar" title="Investiert ${eur(k.securities, 2)} · Cash ${eur(k.cash, 2)}">
            <span class="inv" style="width:${(k.securities / k.total * 100).toFixed(2)}%"></span><span class="csh" style="width:${(Math.max(0, k.cash) / k.total * 100).toFixed(2)}%"></span></div>
          <div class="splitlbl"><span><i class="inv"></i>Investiert <b>${eur(k.securities)}</b> · ${pct(k.securities / k.total, 0, false)}</span>
            <span><i class="csh"></i>Cash <b>${eur(k.cash)}</b> · ${pct(k.cash / k.total, 0, false)}</span></div>` : ""}
        <div class="chart-box" id="${P}VBox" style="padding:6px 0 0"><div class="tooltip" id="${P}VTip"></div></div>
        <div class="value-foot"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <div class="seg">${VRANGES.map((r) => `<button data-vr="${r}" class="${r === PF.vRange ? "on" : ""}">${r}</button>`).join("")}</div>
            <button class="btn ghost sm toggle${PF.multi ? " on" : ""}" id="${P}Multi" aria-pressed="${PF.multi}" title="Verlauf jeder Position im Verhältnis zum Gesamtwert">Multigraph</button>
            ${PF.multi ? `<div class="seg" id="${P}MMode"><button data-mm="eur" class="${PF.multiMode === "eur" ? "on" : ""}">€</button><button data-mm="pct" class="${PF.multiMode === "pct" ? "on" : ""}">%</button></div>` : ""}</div>
          <div class="check">${checkStatus()}${btn(PF.state?.check?.running ? "Prüfe …" : "⟳ Jetzt prüfen", `id="${P}Check" ${PF.state?.check?.running ? "disabled" : ""}`, true)}</div></div>
      </section>`;
    el.querySelectorAll("[data-vr]").forEach((b) => b.onclick = () => { PF.vRange = b.dataset.vr; PF.values = null; PF.multiData = null; drawValue(); loadValues(); });
    document.getElementById(P + "Multi").onclick = () => {
      PF.multi = !PF.multi; PF.multiData = null;
      try { localStorage.setItem("nt-multi", PF.multi ? "1" : "0"); } catch (e) {}
      drawValue(); if (PF.multi) loadValues();
    };
    el.querySelectorAll("[data-mm]").forEach((b) => b.onclick = () => {
      PF.multiMode = b.dataset.mm; try { localStorage.setItem("nt-multi-mode", PF.multiMode); } catch (e) {} drawValue(); });
    const cb = document.getElementById(P + "Check");
    if (cb) cb.onclick = async () => {
      const r = await POST("api/pf/check", {});
      if (!r.data.ok) { toast(r.data.error, "err"); return; }
      PF.state.check = r.data.check; drawValue(); pollCheck();
    };
    const box = document.getElementById(P + "VBox");
    if (PF.multi) { drawMulti(box); return; }
    if (!PF.values) { box.insertAdjacentHTML("beforeend", `<div class="empty" style="padding:50px 0">Lade …</div>`); return; }
    if (pts.length < 2) {
      box.insertAdjacentHTML("beforeend", `<div class="empty" style="padding:50px 0">${PF.values.intraday ? "Der Verlauf wird jede Minute aufgezeichnet – gleich geht's los." : "Der Verlauf erscheint ab dem zweiten Tag."}</div>`);
      return;
    }
    const W = Math.max(280, box.clientWidth), H = box.clientWidth < 500 ? 170 : 220, m = { l: 4, r: 64, t: 10, b: 22 };
    const T = pts.map((x) => new Date(x.t.length > 10 ? x.t : x.t + "T17:30:00").getTime());
    const t0 = T[0], t1 = T[T.length - 1];
    let lo = Math.min(...pts.map((x) => x.v)), hi = Math.max(...pts.map((x) => x.v));
    const pad = Math.max((hi - lo) * 0.12, hi * 0.002); lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi, 4).filter((v) => v >= lo && v <= hi);
    const x = (t) => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
    const y = (v) => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(T[i]).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    const area = `${line} L${x(t1).toFixed(1)},${H - m.b} L${x(t0).toFixed(1)},${H - m.b} Z`;
    const col = cssVar("--series-1");
    const fmtX = (t) => { const d = new Date(t);
      if (PF.vRange === "1T" || PF.vRange === "1h") return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
      if (PF.vRange === "1W") return d.toLocaleDateString("de-DE", { weekday: "short" });
      if (PF.vRange === "1J" || PF.vRange === "Max" && t1 - t0 > 200 * 864e5) return d.toLocaleDateString("de-DE", { month: "short", year: "2-digit" });
      return d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" }); };
    const nX = Math.min(5, pts.length);
    const xs = Array.from({ length: nX }, (_, i) => t0 + (t1 - t0) * i / Math.max(1, nX - 1));
    const base = pts[0].v;
    box.insertAdjacentHTML("beforeend", `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Wertverlauf des Portfolios">
      <defs><linearGradient id="${P}grad" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity="0.22"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
      ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${cssVar("--grid")}"/><text x="${W - m.r + 8}" y="${y(v) + 4}">${nf(0).format(v)} €</text>`).join("")}
      ${base >= lo && base <= hi ? `<line x1="${m.l}" x2="${W - m.r}" y1="${y(base)}" y2="${y(base)}" stroke="${cssVar("--muted")}" stroke-dasharray="2 4"/>` : ""}
      <path d="${area}" fill="url(#${P}grad)"/>
      <path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${x(t1)}" cy="${y(pts[pts.length - 1].v)}" r="4" fill="${col}" stroke="${cssVar("--surface")}" stroke-width="2"/>
      ${xs.map((t, i) => `<text x="${x(t)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === nX - 1 ? "end" : "middle"}">${fmtX(t)}</text>`).join("")}
      <line id="${P}VX" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--muted")}" stroke-dasharray="3 3" visibility="hidden"/>
      <circle id="${P}VD" r="4.5" fill="${col}" stroke="${cssVar("--surface")}" stroke-width="2" visibility="hidden"/>
      <rect id="${P}VHit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`);
    const hit = document.getElementById(P + "VHit"), tip = document.getElementById(P + "VTip");
    const vx = document.getElementById(P + "VX"), vd = document.getElementById(P + "VD");
    const show = (clientX) => {
      const r = hit.getBoundingClientRect(), tx = t0 + (clientX - r.left) / r.width * (t1 - t0);
      let i = 0; for (let j = 0; j < T.length; j++) if (Math.abs(T[j] - tx) < Math.abs(T[i] - tx)) i = j;
      vx.setAttribute("x1", x(T[i])); vx.setAttribute("x2", x(T[i])); vx.setAttribute("visibility", "visible");
      vd.setAttribute("cx", x(T[i])); vd.setAttribute("cy", y(pts[i].v)); vd.setAttribute("visibility", "visible");
      const d = new Date(T[i]), diff = pts[i].v - base;
      tip.innerHTML = `<div class="t">${PF.values.intraday ? d.toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })}</div>
        <div class="row"><span>Wert</span><b>${eur(pts[i].v, 2)}</b></div><div class="row"><span>seit Beginn</span><b class="${cls(diff)}">${signedEur(diff, 2)}</b></div>`;
      tip.style.display = "block";
      const bx = box.getBoundingClientRect();
      let left = x(T[i]) + 12; if (left + tip.offsetWidth > bx.width) left = x(T[i]) - tip.offsetWidth - 12;
      tip.style.left = left + "px"; tip.style.top = "6px";
    };
    hit.addEventListener("mousemove", (ev) => show(ev.clientX));
    hit.addEventListener("touchmove", (ev) => { show(ev.touches[0].clientX); ev.preventDefault(); }, { passive: false });
    const hide = () => { tip.style.display = "none"; vx.setAttribute("visibility", "hidden"); vd.setAttribute("visibility", "hidden"); };
    hit.addEventListener("mouseleave", hide); hit.addEventListener("touchend", hide);
  }

  // ------------------------------------------------------------------ Multigraph: Positionen übereinander = Portfoliowert
  const CAT = () => Array.from({ length: 8 }, (_, i) => cssVar(`--cat-${i + 1}`));
  const tOf = (t) => new Date(t.length > 10 ? t : t + "T17:30:00").getTime();
  function fmtAxis(t, t0, t1) {
    const d = new Date(t);
    if (["1h", "1T"].includes(PF.vRange)) return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    if (PF.vRange === "1W") return d.toLocaleDateString("de-DE", { weekday: "short" });
    if (PF.vRange === "1J" || (PF.vRange === "Max" && t1 - t0 > 200 * 864e5)) return d.toLocaleDateString("de-DE", { month: "short", year: "2-digit" });
    return d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" });
  }
  function drawMulti(box) {
    const d = PF.multiData;
    if (!d) { box.insertAdjacentHTML("beforeend", `<div class="empty" style="padding:50px 0">Lade …</div>`); return; }
    if ((d.t || []).length < 2) {
      box.insertAdjacentHTML("beforeend", `<div class="empty" style="padding:50px 0">${d.intraday
        ? "Der Verlauf je Position wird seit dem Update jede Minute aufgezeichnet – bald zu sehen. Längere Zeiträume (1M, 3M …) gehen sofort."
        : "Noch zu wenige Tage für einen Verlauf."}</div>`);
      return;
    }
    const n = d.t.length, last = (a) => a[n - 1] || 0;
    const syms = Object.keys(d.series).sort((a, b) => last(d.series[b]) - last(d.series[a]));
    const pal = CAT(), top = syms.slice(0, 7), rest = syms.slice(7);
    const layers = top.map((s, i) => ({ key: s, label: d.names[s] || s, color: pal[i], vals: d.series[s] }));
    if (rest.length) layers.push({ key: "_rest", label: `Weitere ${rest.length}`, color: cssVar("--axis"),
      vals: d.t.map((_, j) => rest.reduce((a, s) => a + (d.series[s][j] || 0), 0)) });
    layers.push({ key: "_cash", label: "Cash", color: cssVar("--grid"), vals: d.cash });
    const tot = d.t.map((_, j) => layers.reduce((a, L) => a + (L.vals[j] || 0), 0));
    const pctMode = PF.multiMode === "pct";
    const val = (L, j) => pctMode ? (tot[j] ? (L.vals[j] || 0) / tot[j] : 0) : (L.vals[j] || 0);
    const T = d.t.map(tOf), t0 = T[0], t1 = T[n - 1];
    const W = Math.max(280, box.clientWidth), H = box.clientWidth < 500 ? 200 : 250, m = { l: 4, r: 64, t: 10, b: 22 };
    const hi = pctMode ? 1 : Math.max(...tot) * 1.05;
    const ticks = pctMode ? [0, 0.25, 0.5, 0.75, 1] : niceTicks(0, hi, 4).filter((v) => v <= hi);
    const x = (t) => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
    const y = (v) => m.t + (hi - v) / hi * (H - m.t - m.b);
    // von unten nach oben stapeln: größte Position unten, Cash oben
    const base = new Array(n).fill(0), paths = [];
    for (const L of layers) {
      const top_ = base.map((b, j) => b + val(L, j));
      const up = T.map((t, j) => `${j ? "L" : "M"}${x(t).toFixed(1)},${y(top_[j]).toFixed(1)}`).join("");
      const down = T.map((t, j) => `L${x(T[n - 1 - j]).toFixed(1)},${y(base[n - 1 - j]).toFixed(1)}`).join("");
      paths.push(`<path d="${up}${down}Z" fill="${L.color}" fill-opacity="${L.key === "_cash" ? 0.9 : 0.85}" stroke="${cssVar("--surface")}" stroke-width="1"/>`);
      top_.forEach((v, j) => { base[j] = v; });
    }
    const nX = Math.min(5, n), xs = Array.from({ length: nX }, (_, i) => t0 + (t1 - t0) * i / Math.max(1, nX - 1));
    box.insertAdjacentHTML("afterbegin", `<div class="mg-legend">${layers.map((L) => `<span><i style="background:${L.color}"></i>${esc(L.label)}</span>`).join("")}</div>`);
    box.insertAdjacentHTML("beforeend", `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Wert jeder Position übereinander, oben der Portfoliowert">
      ${paths.join("")}
      ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${cssVar("--surface")}" stroke-opacity="0.35"/><text x="${W - m.r + 8}" y="${y(v) + 4}">${pctMode ? nf(0).format(v * 100) + " %" : nf(0).format(v) + " €"}</text>`).join("")}
      ${xs.map((t, i) => `<text x="${x(t)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === nX - 1 ? "end" : "middle"}">${fmtAxis(t, t0, t1)}</text>`).join("")}
      <line id="${P}MX" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--ink")}" stroke-width="1" visibility="hidden"/>
      <rect id="${P}MHit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`);
    const hit = document.getElementById(P + "MHit"), tip = document.getElementById(P + "VTip"), vx = document.getElementById(P + "MX");
    const colorOf = Object.fromEntries(top.map((s, i) => [s, pal[i]]));
    const show = (clientX) => {
      const r = hit.getBoundingClientRect(), tx = t0 + (clientX - r.left) / r.width * (t1 - t0);
      let i = 0; for (let j = 0; j < n; j++) if (Math.abs(T[j] - tx) < Math.abs(T[i] - tx)) i = j;
      vx.setAttribute("x1", x(T[i])); vx.setAttribute("x2", x(T[i])); vx.setAttribute("visibility", "visible");
      const rows = syms.map((s) => [s, d.series[s][i] || 0]).filter((r_) => r_[1] > 0).sort((a, b) => b[1] - a[1]);
      const dd = new Date(T[i]);
      tip.innerHTML = `<div class="t">${d.intraday ? dd.toLocaleString("de-DE", { weekday: "short", hour: "2-digit", minute: "2-digit" }) : dd.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })} · ${eur(tot[i], 2)}</div>
        ${rows.map(([s, v]) => `<div class="row mg"><i class="dot" style="background:${colorOf[s] || cssVar("--axis")}"></i><span>${esc(d.names[s] || s)}</span><b>${eur(v)}</b><span class="name">${pct(tot[i] ? v / tot[i] : 0, 1, false)}</span></div>`).join("")}
        <div class="row mg"><i class="dot" style="background:${cssVar("--grid")}"></i><span>Cash</span><b>${eur(d.cash[i] || 0)}</b><span class="name">${pct(tot[i] ? (d.cash[i] || 0) / tot[i] : 0, 1, false)}</span></div>`;
      tip.style.display = "block";
      const bx = box.getBoundingClientRect();
      let left = x(T[i]) + 12; if (left + tip.offsetWidth > bx.width) left = x(T[i]) - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, left) + "px"; tip.style.top = "6px";
    };
    hit.addEventListener("mousemove", (ev) => show(ev.clientX));
    hit.addEventListener("touchmove", (ev) => { show(ev.touches[0].clientX); ev.preventDefault(); }, { passive: false });
    const hide = () => { tip.style.display = "none"; vx.setAttribute("visibility", "hidden"); };
    hit.addEventListener("mouseleave", hide); hit.addEventListener("touchend", hide);
  }

  // ------------------------------------------------------------------ Kurs-Chart einer Position (aufgeklappt)
  const POS_RANGES = ["1h", "1T", "1W", "1M", "3M", "1J", "Max"];
  const pcId = (sym) => P + "PC-" + sym.replace(/[^A-Za-z0-9]/g, "_");
  async function loadPosChart(sym, force) {
    const key = `${sym}|${PF.posRange}`, hit_ = PF.posData[key];
    const maxAge = ["1h", "1T"].includes(PF.posRange) ? 30e3 : 5 * 60e3;
    if (hit_ && !force && Date.now() - hit_.at < maxAge) { drawPosChart(sym); return; }
    const rng = PF.posRange;
    const d = await GET(`api/pf/chart?symbol=${encodeURIComponent(sym)}&range=${encodeURIComponent(rng)}`);
    PF.posData[`${sym}|${rng}`] = { at: Date.now(), data: d && !d.error ? d : { prices: [], trades: [] } };
    if (rng === PF.posRange) drawPosChart(sym);
  }
  function drawPosChart(sym) {
    const box = document.getElementById(pcId(sym));
    if (!box) return;
    const d = PF.posData[`${sym}|${PF.posRange}`]?.data;
    if (!d) { box.innerHTML = `<div class="empty" style="padding:50px 0">Lade Kursverlauf …</div>`; return; }
    const pts = (d.prices || []).filter((p) => p[1] != null);
    if (pts.length < 2) { box.innerHTML = `<div class="empty" style="padding:50px 0">Für diesen Zeitraum gibt es keine Kurse${["1h", "1T"].includes(PF.posRange) ? " (Börse heute noch zu?)" : ""}.</div>`; return; }
    const T = pts.map((p) => tOf(p[0])), t0 = T[0], t1 = T[T.length - 1];
    const W = Math.max(260, box.clientWidth), H = box.clientWidth > 700 ? 230 : 160, m = { l: 4, r: 58, t: 10, b: 20 };
    const intra = ["1h", "1T"].includes(PF.posRange);
    const marks = intra ? [] : (d.trades || []).map((t) => ({ ...t, ts: tOf(t.date) })).filter((t) => t.ts >= t0 - 864e5 && t.ts <= t1 + 864e5);
    const vals = pts.map((p) => p[1]).concat(d.avg_price ? [d.avg_price] : [], marks.map((t) => t.price));
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.1, hi * 0.003); lo -= pad; hi += pad;
    const x = (t) => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
    const y = (v) => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
    const first = pts[0][1], lastP = pts[pts.length - 1][1], chg = lastP / first - 1;
    const col = cssVar("--series-1");
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(T[i]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
    const ticks = niceTicks(lo, hi, 3).filter((v) => v >= lo && v <= hi);
    const nX = 3, xs = Array.from({ length: nX }, (_, i) => t0 + (t1 - t0) * i / (nX - 1));
    const fx = (t) => { const dd = new Date(t);
      return intra ? dd.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })
        : PF.posRange === "1W" ? dd.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit" })
        : dd.toLocaleDateString("de-DE", ["1J", "Max"].includes(PF.posRange) ? { month: "short", year: "2-digit" } : { day: "2-digit", month: "2-digit" }); };
    box.innerHTML = `<div class="name" style="padding:2px 0 4px">${PF.posRange === "1h" ? "Letzte Stunde" : PF.posRange === "1T" ? "Heute" : "Im Zeitraum"}:
        <b class="${cls(chg)}">${pct(chg, 2)}</b>${d.avg_price ? ` · Einstand ${nf(2).format(d.avg_price)} € (gestrichelt)` : ""}${marks.length ? " · ▲ Kauf ▼ Verkauf" : ""}</div>
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Kursverlauf ${esc(d.name || sym)}">
      ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${cssVar("--grid")}"/><text x="${W - m.r + 6}" y="${y(v) + 4}">${nf(v < 10 ? 2 : 0).format(v)} €</text>`).join("")}
      ${d.avg_price && d.avg_price >= lo && d.avg_price <= hi ? `<line x1="${m.l}" x2="${W - m.r}" y1="${y(d.avg_price)}" y2="${y(d.avg_price)}" stroke="${cssVar("--muted")}" stroke-dasharray="4 4"/>` : ""}
      <path d="${line}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      ${marks.map((t) => { const cx = Math.min(Math.max(x(t.ts), m.l), W - m.r), cy = y(t.price);
        return t.side === "buy" ? `<path d="M${cx},${cy - 6} l5,9 h-10 Z" fill="${cssVar("--good-text")}" stroke="${cssVar("--surface")}" stroke-width="1.5"><title>Kauf ${nf(4).format(t.qty).replace(/,?0+$/, "")} Stk zu ${nf(2).format(t.price)} € am ${new Date(t.date).toLocaleDateString("de-DE")}</title></path>`
          : `<path d="M${cx},${cy + 6} l5,-9 h-10 Z" fill="${cssVar("--bad-text")}" stroke="${cssVar("--surface")}" stroke-width="1.5"><title>Verkauf ${nf(4).format(t.qty).replace(/,?0+$/, "")} Stk zu ${nf(2).format(t.price)} € am ${new Date(t.date).toLocaleDateString("de-DE")}</title></path>`; }).join("")}
      <circle cx="${x(t1)}" cy="${y(lastP)}" r="3.5" fill="${col}" stroke="${cssVar("--surface")}" stroke-width="2"/>
      ${xs.map((t, i) => `<text x="${x(t)}" y="${H - 4}" text-anchor="${i === 0 ? "start" : i === nX - 1 ? "end" : "middle"}">${fx(t)}</text>`).join("")}
      <line class="pcx" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--muted")}" stroke-dasharray="3 3" visibility="hidden"/>
      <circle class="pcd" r="4" fill="${col}" stroke="${cssVar("--surface")}" stroke-width="2" visibility="hidden"/>
      <rect class="pch" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>
      <div class="tooltip"></div>`;
    const hitR = box.querySelector(".pch"), tip = box.querySelector(".tooltip"), vx = box.querySelector(".pcx"), vd = box.querySelector(".pcd");
    const show = (clientX) => {
      const r = hitR.getBoundingClientRect(), tx = t0 + (clientX - r.left) / r.width * (t1 - t0);
      let i = 0; for (let j = 0; j < T.length; j++) if (Math.abs(T[j] - tx) < Math.abs(T[i] - tx)) i = j;
      vx.setAttribute("x1", x(T[i])); vx.setAttribute("x2", x(T[i])); vx.setAttribute("visibility", "visible");
      vd.setAttribute("cx", x(T[i])); vd.setAttribute("cy", y(pts[i][1])); vd.setAttribute("visibility", "visible");
      const dd = new Date(T[i]), p_ = pts[i][1];
      tip.innerHTML = `<div class="t">${pts[i][0].length > 10 ? dd.toLocaleString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : dd.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })}</div>
        <div class="row"><span>Kurs</span><b>${nf(2).format(p_)} €</b></div>
        ${d.avg_price ? `<div class="row"><span>zum Einstand</span><b class="${cls(p_ - d.avg_price)}">${pct(p_ / d.avg_price - 1, 1)}</b></div>` : ""}`;
      tip.style.display = "block";
      let left = x(T[i]) + 12; if (left + tip.offsetWidth > W) left = x(T[i]) - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, left) + "px"; tip.style.top = "24px";
    };
    hitR.addEventListener("mousemove", (ev) => show(ev.clientX));
    hitR.addEventListener("touchmove", (ev) => { show(ev.touches[0].clientX); ev.preventDefault(); }, { passive: false });
    const hide = () => { tip.style.display = "none"; vx.setAttribute("visibility", "hidden"); vd.setAttribute("visibility", "hidden"); };
    hitR.addEventListener("mouseleave", hide); hitR.addEventListener("touchend", hide);
  }

  // ------------------------------------------------------------------ Kennzahlen
  function kpis() {
    const k = PF.ov.kpi, s = PF.perf?.summary;
    const bench = s?.bench || {};
    const benchTxt = Object.entries(bench).map(([n, v]) => `${n} ${pct(v, 1)}`).join(" · ");
    const hero = MODE === "model"  // der Wert selbst steht groß in der Grafik oben
      ? { label: "Seit Start", value: `<span class="${cls(k.since_start)}">${signedEur(k.total - k.start_capital)}</span>`,
          delta: `<span class="${cls(k.since_start)}">${pct(k.since_start, 2)}</span> auf ${eur(k.start_capital)} eingesetzt · Cash ${eur(k.cash)}` }
      : { hero: true, label: "Depotwert", value: eur(k.total, 2),
          delta: `Wertpapiere ${eur(k.securities)}${k.cash != null ? ` · Cash ${eur(k.cash)}` : ""} <button class="del" id="${P}CashBtn" title="Cash eintragen">✎ Cash</button>` };
    const tiles = [hero,
      { label: "Heute", value: `<span class="${cls(k.day_pnl)}">${signedEur(k.day_pnl)}</span>`, delta: `<span class="${cls(k.day_change)}">${pct(k.day_change)}</span>` },
      { label: "Kursgewinn offen", value: `<span class="${cls(k.unrealized)}">${signedEur(k.unrealized)}</span>`, delta: `<span class="${cls(k.unrealized)}">${pct(k.unrealized_pct, 1)}</span> auf ${eur(k.cost)} Einstand` },
      { label: "Gesamtergebnis", value: `<span class="${cls(k.total_pnl)}">${signedEur(k.total_pnl)}</span>`, delta: `realisiert ${signedEur(k.realized)} · Gebühren ${eur(k.fees)}` },
      { label: "Rendite (zeitgewichtet)", value: s ? `<span class="${cls(s.twr)}">${pct(s.twr, 1)}</span>` : "–", delta: s ? `seit ${new Date(s.start).toLocaleDateString("de-DE")}${benchTxt ? "<br>" + benchTxt : ""}` : "" },
      { label: "Größter Rückgang", value: s ? `<span class="${cls(s.max_drawdown)}">${pct(s.max_drawdown, 1)}</span>` : "–", delta: "vom Höchststand, seit Start" },
    ];
    return `<section class="grid pf-kpis">${tiles.map((t) => `<div class="card kpi${t.hero ? " hero" : ""}">
      <div class="label">${t.label}</div><div class="value">${t.value}</div><div class="delta">${t.delta}</div></div>`).join("")}</section>`;
  }

  // ------------------------------------------------------------------ Positionen
  function positions() {
    const ps = PF.ov.positions, k = PF.ov.kpi;
    const withCash = k.cash != null;
    const base = withCash ? k.total : k.securities;          // Anteile am ganzen Depot (inkl. Cash)
    const share = (v) => (base ? v / base : 0);
    const maxW = Math.max(...ps.map((p) => share(p.value)), withCash ? share(k.cash) : 0, 0.0001);
    const rows = ps.map((p) => {
      const rules = { buy: STATE?.rules?.buy ?? 0.25, sell: STATE?.rules?.sell ?? -0.15, shorts: false };
      const sig = p.signal == null ? "–" : scoreCell(p.signal, true, true, rules);
      const belowStop = p.price <= p.stop_level;
      return `<tr title="${esc(p.signal_summary || "")}">
        <td><span class="sym">${esc(p.name)}</span><div class="name">${esc(p.symbol)}${p.sector ? " · " + esc(p.sector) : ""}</div></td>
        <td class="num">${nf(p.qty < 1 ? 4 : p.qty % 1 ? 3 : 0).format(p.qty)}</td>
        <td class="num">${nf(2).format(p.avg_price)} €</td>
        <td class="num"><span class="dotsrc${p.stale ? " stale" : ""}" title="${esc(p.price_source)}"></span>${nf(2).format(p.price)} €</td>
        <td class="num"><b>${eur(p.value)}</b></td>
        <td class="num" style="white-space:nowrap"><span class="wbar" style="width:${Math.round(share(p.value) / maxW * 46)}px"></span>${pct(share(p.value), 1, false)}</td>
        <td class="num ${cls(p.day_change)}">${pct(p.day_change, 1)}</td>
        <td class="num ${cls(p.pnl)}">${signedEur(p.pnl)}<div class="name ${cls(p.pnl)}">${pct(p.pnl_pct, 1)}</div></td>
        <td class="num">${belowStop ? '<span class="status bad">' : ""}${nf(2).format(p.stop_level)} €${belowStop ? "</span>" : ""}</td>
        <td>${sig}</td>${MODE === "model" ? `<td style="white-space:nowrap">${unconfirmed(p.symbol).length
          ? btn("✓ Bestätigen", `data-confirm-sym="${esc(p.symbol)}"`, true) : '<span class="status ok">bestätigt</span>'}</td>` : ""}</tr>`;
    }).join("");
    const closed = PF.ov.closed.length ? `<details style="padding:4px 16px 12px"><summary class="name" style="cursor:pointer">Verkaufte Positionen (${PF.ov.closed.length})</summary>
      <table>${PF.ov.closed.map((c) => `<tr><td>${esc(c.name)} <span class="name">${esc(c.symbol)}</span></td><td class="num ${cls(c.realized)}">${signedEur(c.realized, 2)}</td><td class="num name">Dividenden ${eur(c.dividends, 2)}</td><td class="name">zuletzt ${new Date(c.last_txn).toLocaleDateString("de-DE")}</td></tr>`).join("")}</table></details>` : "";
    return `<section class="card" style="margin-bottom:14px"><h2>Positionen</h2>
      <div class="hint">Kurse live (grüner Punkt = Echtzeit). ${MODE === "model"
        ? "Stop-Marke laut Anlagestil (fester Stop unter Einstand oder Trailing unter dem Hoch seit Kauf) – wird sie erreicht, kommt sofort die Verkaufsempfehlung."
        : `Stop-Marke: ${nf(0).format(PF.alerts?.settings?.stop * 100 || 10)} % unter Einstand oder ${nf(0).format(PF.alerts?.settings?.trailing * 100 || 15)} % unter dem Hoch seit Kauf – darunter gibt es einen Alarm.`} Bot-Signal: Einschätzung aus Technik + Nachrichten.</div>
      <div class="tbl"><table>
        <tr><th>Wert</th><th class="num">Stück</th><th class="num">Einstand</th><th class="num">Kurs</th><th class="num">Wert</th><th class="num">Anteil</th><th class="num">Heute</th><th class="num">Gewinn/Verlust</th><th class="num">Stop-Marke</th><th>Bot-Signal</th>${MODE === "model" ? "<th>Deine Buchung</th>" : ""}</tr>
        ${rows}
        <tr class="sumrow"><td><b>Investiert</b><div class="name">${ps.length} Positionen</div></td><td></td><td class="num name">${eur(k.cost)}</td><td></td>
          <td class="num"><b>${eur(k.securities)}</b></td><td class="num">${pct(share(k.securities), 1, false)}</td>
          <td class="num ${cls(k.day_change)}">${pct(k.day_change, 1)}</td><td class="num ${cls(k.unrealized)}">${signedEur(k.unrealized)}</td><td colspan="${MODE === "model" ? 3 : 2}"></td></tr>
        ${withCash ? `<tr class="cashrow"><td><b>Cash</b><div class="name">nicht investiert</div></td><td></td><td></td><td></td>
          <td class="num"><b>${eur(k.cash)}</b></td><td class="num" style="white-space:nowrap"><span class="wbar cash" style="width:${Math.round(share(k.cash) / maxW * 46)}px"></span>${pct(share(k.cash), 1, false)}</td>
          <td colspan="${MODE === "model" ? 5 : 4}"></td></tr>
        <tr class="sumrow"><td><b>Gesamt</b></td><td></td><td></td><td></td><td class="num"><b>${eur(k.total)}</b></td><td class="num">100 %</td><td colspan="${MODE === "model" ? 5 : 4}"></td></tr>` : ""}
        </table></div>${closed}</section>`;
  }

  // ------------------------------------------------------------------ Wertentwicklung
  function perfCard() {
    const has = PF.perf?.series?.length > 1;
    return `<div class="card"><h2>Wertentwicklung</h2>
      <div class="hint">Zeitgewichtete Rendite: Käufe und Verkäufe verzerren sie nicht – direkt vergleichbar mit den Indizes.</div>
      <div class="sig-filter"><div class="seg">${["1M", "3M", "YTD", "1J", "Max"].map((r) => `<button data-range="${r}" class="${r === PF.range ? "on" : ""}">${r}</button>`).join("")}</div></div>
      <div class="legend" id="${P}Legend"></div>
      <div class="chart-box" id="${P}Chart">${has ? "" : `<div class="empty">Die Kurve erscheint ab dem zweiten Handelstag nach deinem ersten Kauf.</div>`}<div class="tooltip" id="${P}Tip"></div></div></div>`;
  }

  function rangeStart(dates) {
    const last = new Date(dates[dates.length - 1]);
    const d = new Date(last);
    if (PF.range === "1M") d.setMonth(d.getMonth() - 1);
    else if (PF.range === "3M") d.setMonth(d.getMonth() - 3);
    else if (PF.range === "1J") d.setFullYear(d.getFullYear() - 1);
    else if (PF.range === "YTD") return `${last.getFullYear()}-01-01`;
    else return dates[0];
    return d.toISOString().slice(0, 10);
  }

  function drawPerf() {
    const box = document.getElementById(P + "Chart");
    const all = PF.perf?.series || [];
    if (!box || all.length < 2) return;
    const startDate = rangeStart(all.map((r) => r.date));
    let first = all.findIndex((r) => r.date >= startDate);
    if (first < 0 || all.length - first < 2) first = Math.max(0, all.length - 2);
    // Bezugspunkt = Schlusswert des Vortags; bei „seit Start“ das eingesetzte Kapital (Index 1)
    const prev = first > 0 ? all[first - 1] : null;
    const rows = prev ? all.slice(first - 1) : all.slice(first);
    const names = ["index", ...Object.keys(rows[0]).filter((k) => !["date", "value", "index"].includes(k))];
    const label = { index: "Mein Depot" };
    const colors = [cssVar("--series-1"), cssVar("--series-2"), cssVar("--series-3") || "#1baf7a", cssVar("--series-5") || "#e87ba4"];
    const base = {};
    names.forEach((n) => {
      if (prev && prev[n] != null) base[n] = prev[n];
      else if (n === "index") base[n] = 1;
      else { const f = rows.find((r) => r[n] != null); base[n] = f ? f[n] : null; }
    });
    const ser = names.map((n, i) => ({ n, label: label[n] || n, color: colors[i] || colors[0],
      pts: rows.map((r, j) => ({ j, d: r.date, v: r[n] != null && base[n] ? r[n] / base[n] - 1 : null, value: r.value })) }));
    document.getElementById(P + "Legend").innerHTML = ser.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("");
    const W = Math.max(320, box.clientWidth - 24), H = 270, m = { l: 48, r: 108, t: 12, b: 26 };
    const vals = ser.flatMap((s) => s.pts.map((p) => p.v)).filter((v) => v != null).concat([0]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = Math.max((hi - lo) * 0.1, 0.005); lo -= pad; hi += pad;
    const ticks = niceTicks(lo, hi);
    lo = Math.min(lo, ticks[0]); hi = Math.max(hi, ticks[ticks.length - 1]);
    const N = rows.length - 1;
    const x = (j) => m.l + j / N * (W - m.l - m.r);
    const y = (v) => m.t + (hi - v) / (hi - lo) * (H - m.t - m.b);
    const path = (s) => { let d = "", pen = false; s.pts.forEach((p) => { if (p.v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(p.j).toFixed(1)},${y(p.v).toFixed(1)}`; pen = true; }); return d; };
    // Endbeschriftungen ohne Überlappung
    const ends = ser.map((s) => { const p = [...s.pts].reverse().find((q) => q.v != null); return p ? { s, p, ly: y(p.v) } : null; }).filter(Boolean).sort((a, b) => a.ly - b.ly);
    for (let i = 1; i < ends.length; i++) if (ends[i].ly - ends[i - 1].ly < 14) ends[i].ly = ends[i - 1].ly + 14;
    const nT = Math.min(5, N + 1);
    const xl = Array.from({ length: nT }, (_, i) => Math.round(i * N / Math.max(1, nT - 1)));
    const fmtD = (d) => new Date(d).toLocaleDateString("de-DE", N > 200 ? { month: "2-digit", year: "2-digit" } : { day: "2-digit", month: "2-digit" });
    box.querySelectorAll("svg").forEach((n) => n.remove());
    box.insertAdjacentHTML("beforeend", `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="Wertentwicklung">
      ${ticks.map((v) => `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="${Math.abs(v) < 1e-9 ? cssVar("--axis") : cssVar("--grid")}"/><text x="${m.l - 8}" y="${y(v) + 4}" text-anchor="end">${nf(Math.abs(hi - lo) < 0.06 ? 1 : 0).format(Math.abs(v) < 1e-9 ? 0 : v * 100)} %</text>`).join("")}
      ${xl.map((j, k) => `<text x="${x(j)}" y="${H - 6}" text-anchor="${k === 0 ? "start" : k === nT - 1 ? "end" : "middle"}">${fmtD(rows[j].date)}</text>`).join("")}
      ${ser.slice().reverse().map((s) => `<path d="${path(s)}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`).join("")}
      ${ends.map((e) => `<circle cx="${x(e.p.j)}" cy="${y(e.p.v)}" r="4" fill="${e.s.color}" stroke="${cssVar("--surface")}" stroke-width="2"/><text x="${W - m.r + 10}" y="${e.ly + 4}" style="fill:${e.s.n === "index" ? cssVar("--ink") : cssVar("--ink-2")};font-weight:${e.s.n === "index" ? 600 : 400}">${esc(e.s.label === "Mein Depot" ? "Depot" : e.s.label)} ${pct(e.p.v, 1)}</text>`).join("")}
      <line id="${P}X" y1="${m.t}" y2="${H - m.b}" stroke="${cssVar("--muted")}" stroke-dasharray="3 3" visibility="hidden"/>
      <rect id="${P}Hit" x="${m.l}" y="${m.t}" width="${W - m.l - m.r}" height="${H - m.t - m.b}" fill="transparent"/></svg>`);
    const tip = document.getElementById(P + "Tip"), hit = box.querySelector("#" + P + "Hit"), xh = box.querySelector("#" + P + "X");
    hit.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); });
    hit.addEventListener("mousemove", (ev) => {
      const r = hit.getBoundingClientRect();
      const j = Math.max(0, Math.min(N, Math.round((ev.clientX - r.left) / r.width * N)));
      xh.setAttribute("x1", x(j)); xh.setAttribute("x2", x(j)); xh.setAttribute("visibility", "visible");
      tip.innerHTML = `<div class="t">${new Date(rows[j].date).toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" })}</div>`
        + ser.map((s) => `<div class="row"><span><span class="sw" style="background:${s.color}"></span>${esc(s.label)}</span><b>${pct(s.pts[j].v, 2)}</b></div>`).join("")
        + `<div class="row"><span>Depotwert</span><b>${eur(rows[j].value)}</b></div>`;
      tip.style.display = "block";
      const bx = box.getBoundingClientRect();
      let left = ev.clientX - bx.left + 14;
      if (left + tip.offsetWidth > bx.width - 4) left = ev.clientX - bx.left - tip.offsetWidth - 14;
      tip.style.left = left + "px";
      tip.style.top = Math.max(4, ev.clientY - bx.top - tip.offsetHeight - 10) + "px";
    });
  }

  // ------------------------------------------------------------------ Verteilung
  function allocCard() {
    const k = PF.ov.kpi;
    const withCash = k.cash != null && k.total > 0;
    let list = (PF.ov.allocation[PF.allocKey] || []).map((a) => ({ ...a }));
    if (withCash) {
      list.forEach((a) => { a.weight = a.value / k.total; });
      list.push({ group: "Cash", value: k.cash, weight: k.cash / k.total, cash: true });
    }
    const names = PF.ov.names || {};
    const max = Math.max(...list.map((a) => a.weight), 0.0001);
    const top = list.find((a) => !a.cash);
    const warn = PF.allocKey === "symbol" && top && top.weight > 0.25;
    return `<div class="card"><h2>Verteilung</h2><div class="hint">${withCash ? "Anteil am ganzen Depot (inkl. Cash)" : "Anteil am Wertpapierwert"}${warn ? ` · <span class="status warn">${esc(names[top.group] || top.group)} über 25 %</span>` : ""}</div>
      <div class="sig-filter"><div class="seg" id="${P}Alloc">${Object.entries(ALLOC).map(([k, v]) => `<button data-a="${k}" class="${k === PF.allocKey ? "on" : ""}">${v}</button>`).join("")}</div></div>
      <div class="alloc">${list.map((a) => `<div class="alloc-row${a.cash ? " cash" : ""}" title="${eur(a.value)}"><span class="lbl">${a.cash ? "<b>Cash</b>" : esc(PF.allocKey === "symbol" ? (names[a.group] || a.group) : a.group)}</span>
        <span class="track"><span class="fill" style="width:${(a.weight / max * 100).toFixed(1)}%"></span></span><span class="num">${pct(a.weight, 1, false)}</span></div>`).join("")
        || `<div class="empty">Keine Daten.</div>`}
        ${PF.allocKey !== "symbol" && list.some((a) => a.group === "Unbekannt") ? `<div class="name" style="margin-top:6px">„Unbekannt“: Firmendaten werden gerade nachgeladen – in einer Minute neu laden.</div>` : ""}</div></div>`;
  }
  function bindAlloc() {
    document.querySelectorAll("#" + P + "Alloc button").forEach((b) => b.onclick = () => { PF.allocKey = b.dataset.a; render(); });
  }

  // ------------------------------------------------------------------ Monatsrenditen
  function heat(v) {
    if (v == null) return "transparent";
    const a = Math.min(1, Math.abs(v) / 0.1) * 0.85 + 0.08;
    return `color-mix(in srgb, ${v >= 0 ? cssVar("--pos") : cssVar("--neg")} ${Math.round(a * 100)}%, ${cssVar("--surface")})`;
  }
  function monthlyCard() {
    const m = PF.perf?.monthly || [];
    if (!m.length) return "";
    const years = {};
    m.forEach((r) => { const [y, mo] = r.month.split("-"); (years[y] = years[y] || {})[+mo - 1] = r.ret; });
    const bm = PF.perf.bench_monthly || {};
    const benchYear = (name, y) => { const arr = (bm[name] || []).filter((r) => r.month.startsWith(y)); return arr.length ? arr.reduce((a, r) => a * (1 + r.ret), 1) - 1 : null; };
    const rows = Object.keys(years).sort().reverse().map((y) => {
      const vals = Array.from({ length: 12 }, (_, i) => years[y][i]);
      const yr = vals.filter((v) => v != null).reduce((a, v) => a * (1 + v), 1) - 1;
      const ink = (v) => v != null && Math.abs(v) > 0.06 ? "#fff" : "inherit";
      return `<tr><th>${y}</th>${vals.map((v) => `<td style="background:${heat(v)};color:${ink(v)}" title="${v == null ? "" : pct(v, 2)}">${v == null ? "" : nf(1).format(v * 100)}</td>`).join("")}
        <td style="background:${heat(yr)};color:${ink(yr)}"><b>${nf(1).format(yr * 100)}</b></td>
        ${Object.keys(bm).map((n) => { const b = benchYear(n, y); return `<td class="name">${b == null ? "–" : nf(1).format(b * 100)}</td>`; }).join("")}</tr>`;
    }).join("");
    return `<section class="card" style="margin-bottom:14px"><h2>Monatsrenditen (%)</h2>
      <div class="hint">Zeitgewichtet. Blau = Gewinn, Rot = Verlust, je kräftiger desto größer. Rechts zum Vergleich das Jahr der Indizes.</div>
      <div class="tbl" style="padding:8px 12px 12px"><table class="mgrid"><tr><th></th>${MONTHS.map((x) => `<th>${x}</th>`).join("")}<th>Jahr</th>${Object.keys(bm).map((n) => `<th>${esc(n)}</th>`).join("")}</tr>${rows}</table></div></section>`;
  }

  // ------------------------------------------------------------------ Risiko
  function riskCard() {
    const r = PF.risk;
    if (!r || r.volatility == null) {
      return `<section class="card" style="margin-bottom:14px"><h2>Risiko</h2><div class="empty">Für die Risiko-Analyse braucht es mindestens einen Monat Kursdaten deiner Werte.</div></section>`;
    }
    const sec = PF.ov.kpi.securities;
    const st = (cond, ok, warn) => cond === 0 ? `<span class="status ok">${ok}</span>` : cond === 1 ? `<span class="status warn">${warn}</span>` : `<span class="status bad">${warn}</span>`;
    const lvl = (v, a, b) => (v <= a ? 0 : v <= b ? 1 : 2);
    const tiles = [
      { l: "Schwankung (Volatilität p. a.)", v: pct(r.volatility, 1, false), h: r.bench_volatility != null ? `${esc(r.bench_name)}: ${pct(r.bench_volatility, 1, false)}` : "" },
      { l: "Größter Rückgang (1 Jahr)", v: pct(r.max_drawdown, 1), h: "mit heutiger Zusammensetzung" },
      { l: "Verlustrisiko 1 Tag (VaR 95 %)", v: eur(r.var95_1d * sec), h: `${pct(r.var95_1d, 1, false)} – an 19 von 20 Tagen weniger Verlust` },
      { l: `Beta zum ${esc(r.bench_name || "Index")}`, v: r.beta != null ? nf(2).format(r.beta) : "–", h: r.beta == null ? "" : r.beta > 1.1 ? "schwankt stärker als der Markt" : r.beta < 0.9 ? "schwankt weniger als der Markt" : "schwankt etwa wie der Markt" },
      { l: "Effektive Anzahl Positionen", v: nf(1).format(r.eff_positions), h: st(r.eff_positions >= 8 ? 0 : r.eff_positions >= 4 ? 1 : 2, "gut gestreut", r.eff_positions >= 4 ? "mäßig gestreut" : "wenig gestreut") },
      { l: "Größte Position", v: pct(r.largest_weight, 1, false), h: st(lvl(r.largest_weight, 0.2, 0.35), "kein Klumpenrisiko", "Klumpenrisiko") },
      { l: "Top 3 zusammen", v: pct(r.top3_weight, 1, false), h: st(lvl(r.top3_weight, 0.5, 0.7), "ausgewogen", "konzentriert") },
      { l: "Rendite 1 Jahr (rückgerechnet)", v: `<span class="${cls(r.return_1y)}">${pct(r.return_1y, 1)}</span>`, h: "heutige Gewichte × Kurse des letzten Jahres" },
    ];
    return `<section class="card" style="margin-bottom:14px"><h2>Risiko</h2>
      <div class="hint">Rückgerechnet mit deiner heutigen Zusammensetzung und den Tageskursen des letzten Jahres (in €).${r.missing?.length ? " Ohne Kursdaten: " + r.missing.map(esc).join(", ") : ""}</div>
      <div class="risk-tiles">${tiles.map((t) => `<div class="rtile"><div class="label">${t.l}</div><div class="value">${t.v}</div><div class="hint2">${t.h}</div></div>`).join("")}</div>
      ${corrTable(r.corr)}</section>`;
  }
  function corrTable(c) {
    if (!c || c.symbols.length < 2) return "";
    const names = PF.ov.names || {};
    const short = (s) => esc((names[s] || s).slice(0, 12));
    const col = (v) => `color-mix(in srgb, ${v >= 0 ? cssVar("--series-2") : cssVar("--series-1")} ${Math.round(Math.abs(v) * 85)}%, ${cssVar("--surface")})`;
    return `<div class="hint" style="padding-top:6px">Korrelation der Tagesrenditen: nahe +1 (kräftig orange) = bewegen sich gleich, bringen wenig Streuung; nahe 0 oder negativ (blau) = gute Streuung.</div>
      <div class="corr"><table><tr><th></th>${c.symbols.map((s) => `<th>${short(s)}</th>`).join("")}</tr>
      ${c.symbols.map((a, i) => `<tr><th style="text-align:right">${short(a)}</th>${c.matrix[i].map((v, j) => `<td style="background:${i === j ? cssVar("--surface-2") : col(v)};color:${Math.abs(v) > 0.6 && i !== j ? "#fff" : "inherit"}" title="${short(a)} / ${short(c.symbols[j])}: ${nf(2).format(v)}">${i === j ? "" : nf(2).format(v)}</td>`).join("")}</tr>`).join("")}</table></div>`;
  }

  // ------------------------------------------------------------------ Transaktionen
  function txSection() {
    if (MODE === "model") {
      const tx = PF.tx?.transactions || [];
      return ownForm() + (tx.length ? `<details class="card" style="margin-bottom:14px"><summary>Alle Buchungen (${tx.length})</summary><div class="tbl"><table>
        <tr><th>Datum</th><th>Art</th><th>Wert</th><th class="num">Stück</th><th class="num">Kurs</th><th class="num">Gebühr</th><th>Grund</th></tr>
        ${tx.map((t) => `<tr><td>${new Date(t.date).toLocaleDateString("de-DE")}</td><td>${t.type === "buy" ? "Kauf" : "Verkauf"}</td><td class="sym">${esc(t.symbol)}</td>
          <td class="num">${nf(t.qty % 1 ? 4 : 0).format(t.qty)}</td><td class="num">${nf(2).format(t.price)} €</td><td class="num">${nf(2).format(t.fee)} €</td><td class="reason">${esc(t.note)}</td></tr>`).join("")}
        </table></div></details>` : "");
    }
    const tx = PF.tx?.transactions || [];
    const syms = PF.tx?.symbols || [];
    const art = { buy: "Kauf", sell: "Verkauf", dividend: "Dividende" };
    const rows = tx.map((t) => `<tr>
      <td>${new Date(t.date).toLocaleDateString("de-DE")}</td><td>${art[t.type]}</td>
      <td><span class="sym">${esc(t.symbol)}</span></td>
      <td class="num">${t.type === "dividend" ? "–" : nf(t.qty % 1 ? 4 : 0).format(t.qty)}</td>
      <td class="num">${nf(2).format(t.price)} €${t.type === "dividend" ? " brutto" : ""}</td>
      <td class="num">${nf(2).format(t.fee)} €</td>
      <td class="num">${t.type === "dividend" ? eur(t.price - t.fee, 2) : eur(t.qty * t.price + (t.type === "buy" ? t.fee : -t.fee), 2)}</td>
      <td class="name">${esc(t.note)}</td>
      <td><button class="del" data-del="${esc(t.id)}" title="Löschen">✕</button></td></tr>`).join("");
    return `<section class="card" id="${P}Tx" style="margin-bottom:14px"><h2>Transaktionen</h2>
      <div class="hint">Kurs und Gebühr in Euro, so wie auf deiner Abrechnung. Ein Verkauf ist nur bis zur gehaltenen Stückzahl möglich. Dividende: Bruttobetrag bei „Kurs/Betrag“, einbehaltene Steuer bei „Gebühr/Steuer“.</div>
      <form class="txform" id="${P}Form" autocomplete="off">
        <label>Art<select name="type"><option value="buy">Kauf</option><option value="sell">Verkauf</option><option value="dividend">Dividende</option></select></label>
        <label>Datum<input class="w-date" type="date" name="date" value="${today()}" max="${today()}" required></label>
        <label>Wert (Symbol)<input class="w-sym" name="symbol" list="${P}Syms" placeholder="z. B. SAP.DE" required></label>
        <label id="${P}QtyL">Stück<input class="w-num" name="qty" inputmode="decimal" placeholder="10"></label>
        <label><span id="${P}PriceLbl">Kurs €</span><input class="w-num" name="price" inputmode="decimal" placeholder="183,50" required></label>
        <label><span id="${P}FeeLbl">Gebühr €</span><input class="w-num" name="fee" inputmode="decimal" value="1"></label>
        <label>ISIN (optional)<input class="w-isin" name="isin" placeholder="für Echtzeitkurse"></label>
        <button class="btn" type="submit">Speichern</button>
        <datalist id="${P}Syms">${syms.map((s) => `<option value="${esc(s.symbol)}">${esc(s.name)}</option>`).join("")}</datalist>
      </form>
      <div id="${P}Confirm"></div><div class="formmsg" id="${P}Msg"></div>
      ${tx.length ? `<div class="tbl"><table><tr><th>Datum</th><th>Art</th><th>Wert</th><th class="num">Stück</th><th class="num">Kurs/Betrag</th><th class="num">Gebühr/Steuer</th><th class="num">Summe</th><th>Notiz</th><th></th></tr>${rows}</table></div>
      <div class="field"><a class="btn ghost" href="api/my/transactions.csv" download style="text-decoration:none">Als CSV exportieren</a><span class="name">Backup deiner Transaktionen, lässt sich in Excel/Numbers öffnen.</span></div>` : ""}</section>`;
  }

  function ownForm() {
    const syms = PF.tx?.symbols || [];
    return `<details class="card" style="margin-bottom:14px" ${draft ? "open" : ""}><summary>Eigene Buchung eintragen</summary>
      <div class="hint">Für Käufe, Verkäufe oder Dividenden, die du selbst gemacht hast (nicht aus einer Empfehlung). Kurs und Gebühr in €.</div>
      <form class="txform" id="${P}Form" autocomplete="off">
        <label>Art<select name="type"><option value="buy">Kauf</option><option value="sell">Verkauf</option><option value="dividend">Dividende</option></select></label>
        <label>Datum<input class="w-date" type="date" name="date" value="${today()}" max="${today()}" required></label>
        <label>Wert (Symbol)<input class="w-sym" name="symbol" list="${P}Syms" placeholder="z. B. SAP.DE" required></label>
        <label id="${P}QtyL">Stück<input class="w-num" name="qty" inputmode="decimal" placeholder="10"></label>
        <label><span id="${P}PriceLbl">Kurs €</span><input class="w-num" name="price" inputmode="decimal" placeholder="183,50" required></label>
        <label><span id="${P}FeeLbl">Gebühr €</span><input class="w-num" name="fee" inputmode="decimal" value="1"></label>
        <label>ISIN (optional)<input class="w-isin" name="isin" placeholder="für Echtzeitkurse"></label>
        <button class="btn" type="submit">Buchen</button>
        <datalist id="${P}Syms">${syms.map((x) => `<option value="${esc(x.symbol)}">${esc(x.name)}</option>`).join("")}</datalist>
      </form><div id="${P}Confirm"></div><div class="formmsg" id="${P}Msg"></div></details>`;
  }

  let draft = null;
  function bindTx() {
    const f = document.getElementById(P + "Form");
    if (!f) return;
    if (draft) { Object.entries(draft).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; }); }
    const sync = () => {
      const div = f.elements.type.value === "dividend";
      document.getElementById(P + "QtyL").style.display = div ? "none" : "";
      document.getElementById(P + "PriceLbl").textContent = div ? "Betrag brutto €" : "Kurs €";
      document.getElementById(P + "FeeLbl").textContent = div ? "Steuer €" : "Gebühr €";
    };
    f.elements.type.onchange = sync; sync();
    f.elements.symbol.onchange = () => {
      const s = (PF.tx?.symbols || []).find((x) => x.symbol.toUpperCase() === f.elements.symbol.value.trim().toUpperCase());
      if (s && s.isin && !f.elements.isin.value) f.elements.isin.value = s.isin;
    };
    f.oninput = () => { draft = Object.fromEntries(new FormData(f)); };
    f.onsubmit = (ev) => { ev.preventDefault(); submit(false); };
    root().querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      const t = (PF.tx?.transactions || []).find((x) => x.id === b.dataset.del);
      if (!t || !confirm(`Transaktion vom ${new Date(t.date).toLocaleDateString("de-DE")} (${t.symbol}) wirklich löschen?`)) return;
      const r = await POST("api/my/transactions/delete", { id: t.id });
      msg(r.data.ok ? "Gelöscht." : r.data.error, !r.data.ok);
      if (r.data.ok) load(true);
    });
  }

  function bindCash() {
    const cb = document.getElementById(P + "CashBtn");
    if (cb) cb.onclick = async () => {
      const cur = PF.ov?.kpi?.cash;
      const v = prompt("Cash auf deinem Verrechnungskonto (€):", cur != null ? String(cur).replace(".", ",") : "");
      if (v == null) return;
      const r = await POST("api/my/cash", { cash: v });
      if (!r.data.ok) toast(r.data.error, "err"); else load(false);
    };
  }

  function msg(text, err) {
    const m = document.getElementById(P + "Msg");
    if (m) { m.textContent = text || ""; m.className = "formmsg " + (err ? "err" : "okm"); }
  }

  async function submit(force) {
    const f = document.getElementById(P + "Form");
    const body = Object.fromEntries(new FormData(f));
    if (body.type === "dividend") body.qty = 0;
    body.force = force;
    document.getElementById(P + "Confirm").innerHTML = "";
    msg("Prüfe und speichere …", false);
    const r = await POST(MODE === "model" ? "api/pf/booking" : "api/my/transactions", body);
    if (r.status === 409 && r.data.code === "price_check") {
      msg("", false);
      document.getElementById(P + "Confirm").innerHTML = `<div class="confirm">${esc(r.data.error)}
        <div style="margin-top:8px;display:flex;gap:8px"><button class="btn" id="${P}Force">Trotzdem speichern</button><button class="btn ghost" id="${P}Fix">Korrigieren</button></div></div>`;
      document.getElementById(P + "Force").onclick = () => submit(true);
      document.getElementById(P + "Fix").onclick = () => { document.getElementById(P + "Confirm").innerHTML = ""; f.elements.price.focus(); };
      return;
    }
    if (!r.data.ok) { msg(r.data.error || "Fehler beim Speichern", true); return; }
    draft = { type: body.type, date: body.date };
    msg("Gespeichert. Depot wird neu berechnet …", false);
    await load(true);
    msg("Gespeichert.", false);
  }

  // ------------------------------------------------------------------ Alarme
  function alertsCard() {
    const a = PF.alerts;
    if (!a) return "";
    const s = a.settings;
    return `<section class="card" style="margin-bottom:14px"><h2>Alarme</h2>
      <div class="rules">${s.enabled ? "<span>✓ aktiv</span>" : "<span>aus</span>"}<span>Stop-Loss ${nf(0).format(s.stop * 100)} % unter Einstand</span>
        <span>Trailing ${nf(0).format(s.trailing * 100)} % unter Hoch seit Kauf</span><span>Tagesbewegung ±${nf(0).format(s.move * 100)} %</span>
        <span>Abendbericht ${s.report ? s.report + " Uhr (Mo–Fr)" : "aus"}</span><span>an: ${esc(s.notify)}</span><span class="name">Änderbar in config.json (my_alert_…)</span></div>
      ${a.log.length ? `<div class="tbl"><table>${a.log.map((l) => `<tr><td style="white-space:nowrap" class="name">${dt(l.time)}</td><td><b>${esc(l.title)}</b><div class="reason">${esc(l.message)}</div></td></tr>`).join("")}</table></div>`
        : `<div class="empty">Noch keine Alarme ausgelöst.</div>`}</section>`;
  }

  // ------------------------------------------------------------------ Start
  function visible() {
    return !document.hidden && [].concat(opts.tabEl).some((id) => { const t = document.getElementById(id); return t && !t.classList.contains("hidden"); });
  }
  document.addEventListener("DOMContentLoaded", () => {
    [].concat(opts.tab).forEach((t) => document.querySelectorAll(`#tabs button[data-tab="${t}"]`).forEach((b) => b.addEventListener("click", () => load(true))));
    if (visible() || [].concat(opts.hash).includes(location.hash)) load(true);
    setInterval(() => { if (visible()) load(false); }, 30000);               // Live-Kurse + Empfehlungen
    setInterval(() => { if (visible()) load(true); }, 10 * 60 * 1000);      // Verlauf/Risiko
    let rt;
    addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (visible()) { drawPerf(); if (MODE === "model") { drawValue(); openPos.forEach((sym) => drawPosChart(sym)); } } }, 150); });
  });
  return load;
}

window.realView = createPortfolioView({ prefix: "rv", root: "pf", api: "api/my", mode: "real", tab: "mine", tabEl: "tabMine", hash: "#mein-depot" });
window.modelView = createPortfolioView({ prefix: "mv", root: "pm", api: "api/pf", mode: "model", tab: ["pm", "set"], tabEl: ["tabPm", "tabSet"],
  hash: ["#portfolio", "#einstellungen"] });
