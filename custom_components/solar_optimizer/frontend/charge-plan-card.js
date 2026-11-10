/* Charge plan card: periods of the car (fixed power, sun, away) on a timeline with the estimated
 * battery. No dependencies. Reads sensor.solar_optimizer_plan_coche and calls the services
 * solar_optimizer.set_charge_period / remove_charge_period.
 *
 *   type: custom:charge-plan-card
 *   entity: sensor.solar_optimizer_plan_coche     # default
 *   switch: switch.solar_optimizer_plan_coche     # default
 */
const MODES = {
  fixed: { name: 'Fija', icon: 'mdi:flash', color: '#ab47bc' },
  sun: { name: 'Sol', icon: 'mdi:white-balance-sunny', color: '#66bb6a' },
  away: { name: 'Fuera', icon: 'mdi:car-arrow-right', color: '#9a9ca1' },
};
const DAYS = ['L', 'M', 'X', 'J', 'V', 'S', 'D'];
const DAY_NAMES = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];
const SPANS = [[1, '1 d'], [3, '3 d'], [7, '7 d']];
const W = 1000, H = 230, TOP = 14, BOT = 190;

const pad = (n) => String(n).padStart(2, '0');
const hhmm = (min) => `${pad(Math.floor(min / 60))}:${pad(min % 60)}`;
const clock = (t) => { const d = new Date(t * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const num = (x, d = 1) => Number(x).toFixed(d).replace('.', ',');
const localInput = (t) => { const d = new Date(t * 1000); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; };

class ChargePlanCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._span = 3;
    this._key = '';
    this._dlg = null;
    this.shadowRoot.innerHTML = `<style>${ChargePlanCard.css}</style><ha-card><div id="main"></div></ha-card><div id="dlg"></div>`;
    this.shadowRoot.addEventListener('click', (e) => this._click(e));
  }

  setConfig(config) {
    this._config = { entity: 'sensor.solar_optimizer_plan_coche', switch: 'switch.solar_optimizer_plan_coche', ...config };
  }

  getCardSize() { return 6; }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config.entity];
    const sw = hass.states[this._config.switch];
    const key = JSON.stringify([st && st.state, st && st.attributes, sw && sw.state, Math.floor(Date.now() / 60000)]);
    if (key !== this._key) { this._key = key; this._render(); }
  }

  get _attrs() { const s = this._hass.states[this._config.entity]; return (s && s.attributes) || {}; }

  _render() {
    const st = this._hass.states[this._config.entity];
    const main = this.shadowRoot.getElementById('main');
    if (!st) { main.innerHTML = `<div class="empty">No existe ${this._config.entity}</div>`; return; }
    const a = st.attributes;
    const on = (this._hass.states[this._config.switch] || {}).state === 'on';
    const now = a.ahora || Math.floor(Date.now() / 1000);
    const span = this._span * 86400;
    const t0 = now - 1800, t1 = t0 + span;
    const X = (t) => ((t - t0) / span) * W;
    const Y = (p) => BOT - (p / 100) * (BOT - TOP);
    let svg = '';
    // days and hours
    const d0 = new Date(t0 * 1000); d0.setHours(0, 0, 0, 0);
    for (let d = new Date(d0); d.getTime() / 1000 < t1; d.setDate(d.getDate() + 1)) {
      const x = X(d.getTime() / 1000);
      if (x >= 0) svg += `<line x1="${x}" x2="${x}" y1="${TOP}" y2="${BOT}" class="day"/>`;
      const label = `${DAY_NAMES[d.getDay()]} ${d.getDate()}`;
      svg += `<text x="${Math.max(x, 0) + 4}" y="${H - 8}" class="lbl">${label}</text>`;
      if (this._span <= 3) for (const h of [6, 12, 18]) {
        const xh = X(d.getTime() / 1000 + h * 3600);
        if (xh > 0 && xh < W) svg += `<line x1="${xh}" x2="${xh}" y1="${TOP}" y2="${BOT}" class="hour"/><text x="${xh + 3}" y="${BOT + 13}" class="tick">${h}h</text>`;
      }
    }
    for (const p of [0, 50, 100]) svg += `<line x1="0" x2="${W}" y1="${Y(p)}" y2="${Y(p)}" class="grid"/><text x="2" y="${Y(p) - 3}" class="tick">${p}%</text>`;
    // bands
    const bands = (a.intervalos || []);
    for (const [id, s, e, mode] of bands) {
      if (e < t0 || s > t1) continue;
      const x0 = Math.max(0, X(s)), x1 = Math.min(W, X(e));
      const c = (MODES[mode] || MODES.sun).color;
      svg += `<rect x="${x0}" y="${TOP}" width="${Math.max(2, x1 - x0)}" height="${BOT - TOP}" fill="${c}" opacity=".28" class="band" data-id="${id}"/>`;
    }
    // curve
    const pts = (a.curva || []).filter(([t]) => t >= t0 - 3600 && t <= t1 + 3600).map(([t, p]) => `${X(t).toFixed(1)},${Y(p).toFixed(1)}`);
    if (pts.length > 1) svg += `<polyline points="${pts.join(' ')}" class="curve"/>`;
    svg += `<line x1="${X(now)}" x2="${X(now)}" y1="${TOP - 6}" y2="${BOT}" class="now"/>`;
    svg += `<rect x="0" y="${TOP}" width="${W}" height="${BOT - TOP}" fill="transparent" id="hit"/>`;

    const soc = a.soc_estimado, fin = a.soc_final;
    const warn = (a.avisos || []).map((w) => `<div class="warn"><ha-icon icon="mdi:alert-outline"></ha-icon>${w}</div>`).join('');
    const rows = this._rows(a, now);
    main.innerHTML = `
      <div class="head">
        <div class="pct"><ha-icon icon="mdi:car-battery"></ha-icon>${soc == null ? '—' : Math.round(soc)} %
          <ha-icon icon="mdi:arrow-right" class="arrow"></ha-icon>${fin == null ? '—' : Math.round(fin)} %</div>
        <div class="state">${st.state}</div>
        <button class="pill ${on ? 'on' : ''}" data-act="toggle" title="Ejecutar el plan"><ha-icon icon="${on ? 'mdi:play-circle' : 'mdi:eye-outline'}"></ha-icon>${on ? 'Ejecuta' : 'Vista previa'}</button>
      </div>
      <div class="spans">${SPANS.map(([d, l]) => `<button class="chip ${d === this._span ? 'sel' : ''}" data-span="${d}">${l}</button>`).join('')}
        <span class="sum">${a.coste_total_estimado != null ? `${num(a.coste_total_estimado, 2)} € · sol ${num(a.sol_kwh || 0, 0)} kWh · red ${num(a.red_kwh || 0, 0)} kWh` : ''}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${svg}</svg>
      ${warn}
      <div class="list">${rows}</div>
      <button class="add" data-act="add"><ha-icon icon="mdi:plus"></ha-icon>Añadir franja</button>`;
    const svgEl = main.querySelector('svg');
    svgEl.addEventListener('click', (e) => {
      const r = svgEl.getBoundingClientRect();
      const t = t0 + ((e.clientX - r.left) / r.width) * span;
      const hit = (a.intervalos || []).find(([, s, en]) => t >= s && t < en);
      if (hit) { const p = (a.periodos || []).find((q) => q.id === hit[0]); if (p) return this._open(p); }
      const s = Math.round(t / 1800) * 1800;
      this._open({ mode: 'fixed', start: s, end: s + 2 * 3600, kw: 3.5, pct: 0, enabled: true, weekdays: [] });
    });
  }

  _rows(a, now) {
    const res = {};
    for (const r of a.intervalos || []) if (!res[r[0]] || r[1] < res[r[0]][1]) res[r[0]] = r;
    const ps = [...(a.periodos || [])].sort((p, q) => (p.weekdays.length ? 1 : 0) - (q.weekdays.length ? 1 : 0) || (p.start || 0) - (q.start || 0));
    if (!ps.length) return '<div class="empty">Sin franjas: el coche lo lleva el optimizador. Toca la gráfica para añadir una.</div>';
    return ps.map((p) => {
      const m = MODES[p.mode];
      const when = p.weekdays.length
        ? `${p.weekdays.map((d) => DAYS[d]).join(' ')} · ${hhmm(p.start_min)}–${hhmm(p.end_min)}`
        : `${DAY_NAMES[new Date(p.start * 1000).getDay()]} ${new Date(p.start * 1000).getDate()} · ${clock(p.start)}–${clock(p.end)}`;
      const what = p.mode === 'fixed' ? `${num(p.kw)} kW` : p.mode === 'away' ? `−${Math.round(p.pct)} %` : '';
      const r = res[p.id];
      const out = r ? `${Math.round(r[4])} → ${Math.round(r[5])} %` : '';
      return `<div class="row ${p.enabled ? '' : 'off'}" data-edit="${p.id}"><span class="dot" style="background:${m.color}"><ha-icon icon="${m.icon}"></ha-icon></span>
        <span class="txt"><b>${m.name}</b> ${what}<small>${when}</small></span><span class="res">${out}</span><ha-icon icon="mdi:chevron-right"></ha-icon></div>`;
    }).join('');
  }

  _click(e) {
    const t = e.composedPath()[0];
    const el = t.closest ? t.closest('[data-act],[data-span],[data-edit],[data-d]') : null;
    if (!el) return;
    if (el.dataset.span) { this._span = Number(el.dataset.span); this._key = ''; this._render(); }
    else if (el.dataset.edit) this._open((this._attrs.periodos || []).find((p) => p.id === el.dataset.edit));
    else if (el.dataset.act === 'add') {
      const s = Math.ceil(Date.now() / 1800000) * 1800;
      this._open({ mode: 'fixed', start: s, end: s + 2 * 3600, kw: 3.5, pct: 0, enabled: true, weekdays: [] });
    } else if (el.dataset.act === 'toggle') this._hass.callService('switch', 'toggle', { entity_id: this._config.switch });
  }

  // ------------------------------------------------------------------ editor

  _open(p) {
    if (!p) return;
    this._dlg = { ...p, weekdays: [...(p.weekdays || [])], weekly: (p.weekdays || []).length > 0, isNew: !p.id };
    if (this._dlg.weekly === false && !this._dlg.start) { const s = Math.ceil(Date.now() / 1800000) * 1800; this._dlg.start = s; this._dlg.end = s + 7200; }
    if (!this._dlg.start_min && !this._dlg.end_min) { this._dlg.start_min = 8 * 60; this._dlg.end_min = 18 * 60; }
    this._drawDlg();
  }

  _drawDlg() {
    const d = this._dlg, box = this.shadowRoot.getElementById('dlg');
    if (!d) { box.innerHTML = ''; return; }
    const levels = (this._attrs.niveles_kw || [1.4, 2.2, 3.5, 7]).filter((x) => x > 0);
    box.innerHTML = `<div class="veil"><div class="dlg">
      <div class="modes">${Object.entries(MODES).map(([k, m]) => `<button class="mode ${d.mode === k ? 'sel' : ''}" style="--c:${m.color}" data-m="${k}"><ha-icon icon="${m.icon}"></ha-icon>${m.name}</button>`).join('')}</div>
      ${d.mode === 'fixed' ? `<div class="chips">${levels.map((x) => `<button class="chip ${Math.abs(d.kw - x) < 0.05 ? 'sel' : ''}" data-kw="${x}">${num(x)} kW</button>`).join('')}</div>` : ''}
      ${d.mode === 'away' ? `<label class="fld">Gasto de batería: <b id="pctv">−${Math.round(d.pct)} %</b><input type="range" id="pct" min="0" max="100" step="1" value="${d.pct}"></label>` : ''}
      ${d.mode === 'sun' ? '<div class="hint">El coche se lleva todo el excedente; los aires, solo lo que le sobre.</div>' : ''}
      <div class="chips"><button class="chip ${d.weekly ? '' : 'sel'}" data-r="once">Fecha</button><button class="chip ${d.weekly ? 'sel' : ''}" data-r="weekly">Semanal</button></div>
      ${d.weekly
        ? `<div class="chips">${DAYS.map((l, i) => `<button class="chip day ${d.weekdays.includes(i) ? 'sel' : ''}" data-d="${i}">${l}</button>`).join('')}</div>
           <div class="times"><input type="time" id="t0" value="${hhmm(d.start_min)}"><ha-icon icon="mdi:arrow-right"></ha-icon><input type="time" id="t1" value="${hhmm(d.end_min)}"></div>`
        : `<div class="times col"><input type="datetime-local" id="s0" value="${localInput(d.start)}"><input type="datetime-local" id="s1" value="${localInput(d.end)}"></div>`}
      <div class="err" id="err"></div>
      <div class="btns">${d.isNew ? '' : '<button class="del" data-x="del"><ha-icon icon="mdi:delete-outline"></ha-icon></button>'}
        ${d.isNew ? '' : `<button class="pill ${d.enabled ? 'on' : ''}" data-x="en"><ha-icon icon="${d.enabled ? 'mdi:check-circle' : 'mdi:circle-outline'}"></ha-icon>Activa</button>`}
        <span style="flex:1"></span><button class="cancel" data-x="cancel">Cancelar</button><button class="save" data-x="save">Guardar</button></div></div></div>`;
    const q = (id) => box.querySelector(id);
    box.onclick = (e) => {
      const el = e.target.closest('[data-m],[data-kw],[data-r],[data-d],[data-x]');
      if (!el) { if (e.target.classList.contains('veil')) { this._dlg = null; this._drawDlg(); } return; }
      this._readFields();
      const s = el.dataset;
      if (s.m) d.mode = s.m;
      else if (s.kw) d.kw = Number(s.kw);
      else if (s.r) d.weekly = s.r === 'weekly';
      else if (s.d !== undefined) { const i = Number(s.d); d.weekdays = d.weekdays.includes(i) ? d.weekdays.filter((x) => x !== i) : [...d.weekdays, i].sort(); }
      else if (s.x === 'cancel') { this._dlg = null; }
      else if (s.x === 'en') d.enabled = !d.enabled;
      else if (s.x === 'del') { this._hass.callService('solar_optimizer', 'remove_charge_period', { id: d.id }); this._dlg = null; this._key = ''; }
      else if (s.x === 'save') { if (!this._save()) return; }
      this._drawDlg();
    };
    const range = q('#pct');
    if (range) range.oninput = () => { d.pct = Number(range.value); q('#pctv').textContent = `−${Math.round(d.pct)} %`; };
  }

  _readFields() {
    const d = this._dlg, box = this.shadowRoot.getElementById('dlg');
    const v = (id) => { const el = box.querySelector(id); return el ? el.value : null; };
    if (v('#t0')) { const [a, b] = v('#t0').split(':'); d.start_min = Number(a) * 60 + Number(b); }
    if (v('#t1')) { const [a, b] = v('#t1').split(':'); d.end_min = Number(a) * 60 + Number(b); }
    if (v('#s0')) d.start = new Date(v('#s0')).getTime() / 1000;
    if (v('#s1')) d.end = new Date(v('#s1')).getTime() / 1000;
  }

  _save() {
    const d = this._dlg, err = this.shadowRoot.getElementById('err');
    const data = { mode: d.mode, enabled: d.enabled };
    if (d.id) data.id = d.id;
    if (d.mode === 'fixed') data.kw = d.kw;
    if (d.mode === 'away') data.pct = d.pct;
    if (d.weekly) {
      if (!d.weekdays.length) { err.textContent = 'Elige al menos un día'; return false; }
      Object.assign(data, { weekdays: d.weekdays, start_time: hhmm(d.start_min), end_time: hhmm(d.end_min) });
    } else {
      if (!(d.end > d.start)) { err.textContent = 'El fin debe ser posterior al inicio'; return false; }
      if (d.end < Date.now() / 1000) { err.textContent = 'La franja ya ha pasado'; return false; }
      Object.assign(data, { start: d.start, end: d.end, weekdays: [] });
    }
    this._hass.callService('solar_optimizer', 'set_charge_period', data);
    this._dlg = null; this._key = '';
    return true;
  }
}

ChargePlanCard.css = `
  :host { display: block; }
  ha-card { padding: 12px 14px 14px; }
  .head { display: flex; align-items: center; gap: 10px; }
  .pct { font-size: 26px; font-weight: 600; display: flex; align-items: center; gap: 6px; flex: 1; }
  .pct ha-icon { --mdc-icon-size: 24px; color: #ab47bc; } .pct .arrow { color: var(--secondary-text-color); --mdc-icon-size: 20px; }
  .state { color: var(--secondary-text-color); font-size: 14px; }
  button { font: inherit; color: var(--primary-text-color); cursor: pointer; border: 0; background: none; }
  .pill { display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px; border-radius: 16px; background: rgba(154,156,161,.2); font-size: 13px; }
  .pill.on { background: rgba(102,187,106,.25); color: #66bb6a; } .pill ha-icon { --mdc-icon-size: 18px; }
  .spans { display: flex; align-items: center; gap: 6px; margin: 10px 0 4px; } .sum { margin-left: auto; font-size: 12px; color: var(--secondary-text-color); }
  .chip { padding: 5px 12px; border-radius: 14px; background: rgba(154,156,161,.18); font-size: 13px; }
  .chip.sel { background: var(--primary-color); color: var(--text-primary-color, #fff); } .chip.day { min-width: 34px; padding: 6px 0; }
  svg { width: 100%; height: 190px; display: block; touch-action: manipulation; }
  .day { stroke: rgba(154,156,161,.5); } .hour { stroke: rgba(154,156,161,.18); } .grid { stroke: rgba(154,156,161,.2); stroke-dasharray: 3 4; }
  .curve { fill: none; stroke: var(--primary-text-color); stroke-width: 2.5; vector-effect: non-scaling-stroke; }
  .now { stroke: #ffa726; stroke-width: 2; vector-effect: non-scaling-stroke; }
  .band { cursor: pointer; } text { fill: var(--secondary-text-color); font-size: 12px; } .tick { font-size: 10px; opacity: .8; } .lbl { font-size: 12px; }
  .warn { display: flex; align-items: center; gap: 6px; color: #ffa726; font-size: 13px; margin: 4px 0; } .warn ha-icon { --mdc-icon-size: 18px; }
  .list { margin-top: 8px; } .empty { color: var(--secondary-text-color); font-size: 13px; padding: 10px 2px; }
  .row { display: flex; align-items: center; gap: 10px; padding: 8px 2px; cursor: pointer; border-top: 1px solid rgba(154,156,161,.18); } .row.off { opacity: .5; }
  .dot { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center; color: #fff; flex: none; } .dot ha-icon { --mdc-icon-size: 20px; }
  .txt { flex: 1; display: flex; flex-direction: column; } .txt small { color: var(--secondary-text-color); font-size: 12px; } .res { font-weight: 500; font-size: 14px; }
  .add { margin-top: 6px; display: flex; align-items: center; gap: 4px; color: var(--primary-color); padding: 6px 0; }
  .veil { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 9; display: grid; place-items: center; padding: 16px; }
  .dlg { background: var(--card-background-color, #1c1c1c); border-radius: 16px; padding: 16px; width: min(420px, 100%); display: flex; flex-direction: column; gap: 12px; }
  .modes { display: flex; gap: 8px; } .mode { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 10px 0; border-radius: 12px; background: rgba(154,156,161,.15); }
  .mode.sel { background: var(--c); color: #fff; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; } .fld { display: flex; flex-direction: column; gap: 6px; font-size: 14px; } .hint { font-size: 13px; color: var(--secondary-text-color); }
  .times { display: flex; align-items: center; gap: 8px; } .times.col { flex-direction: column; align-items: stretch; }
  input[type=time], input[type=datetime-local] { font: inherit; padding: 8px; border-radius: 8px; border: 1px solid rgba(154,156,161,.4); background: transparent; color: var(--primary-text-color); color-scheme: dark light; }
  .err { color: #ef5350; font-size: 13px; min-height: 0; } .btns { display: flex; align-items: center; gap: 8px; }
  .save { background: var(--primary-color); color: var(--text-primary-color, #fff); padding: 8px 18px; border-radius: 18px; } .cancel { padding: 8px 12px; } .del { color: #ef5350; }
`;

customElements.define('charge-plan-card', ChargePlanCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: 'charge-plan-card', name: 'Plan de carga del coche', description: 'Franjas de carga y batería estimada' });
