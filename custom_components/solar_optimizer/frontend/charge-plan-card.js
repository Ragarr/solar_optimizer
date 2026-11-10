/* Charge plan card: draw the periods of the car (fixed power, sun, away) by dragging on a grid of
 * 7 day rows, then choose the mode and its parameters. Shows the estimated battery curve.
 * No dependencies. Reads sensor.solar_optimizer_plan_coche and calls the services
 * solar_optimizer.set_charge_period / remove_charge_period.
 *
 *   type: custom:charge-plan-card
 *   entity: sensor.solar_optimizer_plan_coche     # default
 *   switch: switch.solar_optimizer_plan_coche     # default
 *   settings:                                     # optional: entities of the cog panel
 *     - { entity: input_number.plan_coche_confianza, name: Confianza en la previsión, icon: mdi:weather-partly-cloudy }
 *
 * The geometry (rows, snapping, limits) is exported as pure functions for the tests.
 */

export const SNAP = 900; // seconds
const MODES = {
  fixed: { name: 'Fija', glyph: '⚡', icon: 'mdi:flash', color: '#ab47bc' },
  sun: { name: 'Sol', glyph: '☀', icon: 'mdi:white-balance-sunny', color: '#66bb6a' },
  away: { name: 'Fuera', glyph: '🚗', icon: 'mdi:car-arrow-right', color: '#9a9ca1' },
};
const DAYS = ['L', 'M', 'X', 'J', 'V', 'S', 'D'];
const DOW = ['dom', 'lun', 'mar', 'mié', 'jue', 'vie', 'sáb'];
const ROWS = 7, LBL = 46, AXIS = 22, ROW = 56, HANDLE = 14;

const pad = (n) => String(n).padStart(2, '0');
export const hhmm = (min) => `${pad(Math.floor(min / 60) % 24)}:${pad(min % 60)}`;
const num = (x, d = 1) => Number(x).toFixed(d).replace('.', ',');

// ---------------------------------------------------------------------------- geometry (pure)

const ymd = (t) => { const d = new Date(t * 1000); return [d.getFullYear(), d.getMonth(), d.getDate()]; };
export const dayStart = (t) => { const [y, m, d] = ymd(t); return new Date(y, m, d).getTime() / 1000; };
export const minuteOf = (t) => { const d = new Date(t * 1000); return d.getHours() * 60 + d.getMinutes(); };
export const weekdayOf = (t) => (new Date(t * 1000).getDay() + 6) % 7; // 0 = Monday
/** Epoch of `minute` of the day `row` after day0 (local time, DST-safe) */
export const timeAt = (day0, row, minute) => { const [y, m, d] = ymd(day0); return new Date(y, m, d + row, 0, Math.floor(minute)).getTime() / 1000; };
export const rowOf = (day0, t) => Math.round((dayStart(t) - day0) / 86400);
export const snapMin = (m, unit = SNAP / 60) => Math.round(m / unit) * unit;

/** Pieces of [t0, t1) per visible row: {row, m0, m1} in minutes of the day */
export function segments(day0, t0, t1, rows = ROWS) {
  const out = [];
  for (let r = 0; r < rows; r++) {
    const a = timeAt(day0, r, 0), b = timeAt(day0, r + 1, 0);
    if (t1 <= a || t0 >= b) continue;
    out.push({ row: r, m0: t0 <= a ? 0 : minuteOf(t0), m1: t1 >= b ? 1440 : minuteOf(t1) });
  }
  return out;
}

/** The (snapped) time under a point of the grid: x01 in [0, 1] along the day, row of the day */
export function pointToTime(day0, row, x01) {
  return timeAt(day0, Math.max(0, Math.min(ROWS - 1, row)), snapMin(Math.min(1, Math.max(0, x01)) * 1440));
}

/** While drawing from `anchor`, the cursor stops at the nearest busy interval ({t0, t1}) */
export function limitCursor(anchor, cur, busy) {
  if (cur >= anchor) {
    let cap = cur;
    for (const o of busy) if (o.t0 >= anchor && o.t0 < cap) cap = o.t0;
    return cap;
  }
  let floor = cur;
  for (const o of busy) if (o.t1 <= anchor && o.t1 > floor) floor = o.t1;
  return floor;
}

/** Moving [t0, t1) by `delta` without crossing a busy interval */
export function limitShift(t0, t1, delta, busy) {
  let lo = -Infinity, hi = Infinity;
  for (const o of busy) {
    if (o.t0 >= t1) hi = Math.min(hi, o.t0 - t1);
    else if (o.t1 <= t0) lo = Math.max(lo, o.t1 - t0);
  }
  return Math.min(hi, Math.max(lo, delta));
}

/** New value of one edge ('l' | 'r') of [t0, t1) dragged to `cur`, with a minimum length */
export function limitEdge(edge, t0, t1, cur, busy, minLen = SNAP) {
  if (edge === 'r') {
    let v = cur;
    for (const o of busy) if (o.t0 >= t1 && o.t0 < v) v = o.t0;
    return Math.max(v, t0 + minLen);
  }
  let v = cur;
  for (const o of busy) if (o.t1 <= t0 && o.t1 > v) v = o.t1;
  return Math.min(v, t1 - minLen);
}

/** Weekdays moved along with the start of a weekly period dragged from oldT0 to newT0 */
export function shiftWeekdays(weekdays, day0, oldT0, newT0) {
  const shift = rowOf(day0, newT0) - rowOf(day0, oldT0);
  return [...new Set(weekdays.map((d) => (((d + shift) % 7) + 7) % 7))].sort();
}

/** Data of the service set_charge_period for a period {id, mode, kw, pct, enabled, weekdays, t0, t1} */
export function toService(s) {
  const d = { mode: s.mode, enabled: s.enabled !== false };
  if (s.id) d.id = s.id;
  if (s.mode === 'fixed') d.kw = s.kw;
  if (s.mode === 'away') d.pct = s.pct;
  if (s.weekdays.length) {
    Object.assign(d, { weekdays: [...s.weekdays], start_time: hhmm(minuteOf(s.t0)), end_time: hhmm(minuteOf(s.t1)) });
  } else {
    Object.assign(d, { start: s.t0, end: s.t1, weekdays: [] });
  }
  return d;
}

export function fmtDuration(seconds) {
  const h = seconds / 3600;
  if (h >= 24) { const d = Math.floor(h / 24), r = h - d * 24; return `${d} d${r ? ' ' + fmtDuration(r * 3600) : ''}`; }
  const m = Math.round((h - Math.floor(h)) * 60);
  return `${Math.floor(h)} h${m ? ` ${m} min` : ''}`.replace(/^0 h /, '');
}

export function fmtRange(t0, t1) {
  const a = new Date(t0 * 1000), b = new Date(t1 * 1000);
  const f = (d) => `${DOW[d.getDay()]} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const end = dayStart(t0) === dayStart(t1) ? `${pad(b.getHours())}:${pad(b.getMinutes())}` : f(b);
  return `${f(a)} → ${end} · ${fmtDuration(t1 - t0)}`;
}

// ---------------------------------------------------------------------------- card

const Base = typeof HTMLElement !== 'undefined' ? HTMLElement : class {};

class ChargePlanCard extends Base {
  constructor() {
    super();
    this._sel = null; // period being edited {id, persisted, mode, kw, pct, enabled, weekdays, t0, t1, occ0}
    this._drag = null;
    this._fine = false;
    this._w = 0;
    this._day0 = null;
    this._timer = null;
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `<style>${ChargePlanCard.css}</style>
      <ha-card>
        <div id="head"></div><div id="help" hidden></div><div id="settings" hidden></div>
        <div id="wrap"><svg id="svg"><g id="layer"></g></svg></div>
        <div id="panel"></div>
      </ha-card>`;
    const $ = (id) => this.shadowRoot.getElementById(id);
    this._svg = $('svg'); this._layer = $('layer'); this._panelEl = $('panel'); this._headEl = $('head');
    this._svg.addEventListener('pointerdown', (e) => this._down(e));
    this._svg.addEventListener('pointermove', (e) => this._move(e));
    this._svg.addEventListener('pointerup', (e) => this._up(e));
    this._svg.addEventListener('pointercancel', () => this._cancel());
    this.shadowRoot.addEventListener('click', (e) => this._click(e));
    this.shadowRoot.addEventListener('input', (e) => this._input(e));
  }

  setConfig(config) {
    this._config = { entity: 'sensor.solar_optimizer_plan_coche', switch: 'switch.solar_optimizer_plan_coche', settings: [], ...config };
  }

  getCardSize() { return 9; }

  connectedCallback() {
    if (typeof ResizeObserver === 'undefined') { this._measure(); return; }
    this._ro = new ResizeObserver(() => this._measure());
    this._ro.observe(this.shadowRoot.getElementById('wrap'));
  }

  disconnectedCallback() { if (this._ro) this._ro.disconnect(); }

  set hass(hass) {
    this._hass = hass;
    if (!this._panelInit) { this._panelInit = true; this._drawPanel(); }
    this._drawHead();
    if (this._showSettings) this._drawSettings();
    if (!this._drag) { this._draw(); this._updateResult(); }
  }

  // ------------------------------------------------------------------ data

  get _attrs() { const s = this._hass && this._hass.states[this._config.entity]; return (s && s.attributes) || {}; }
  get _now() { return this._attrs.ahora || Math.floor(Date.now() / 1000); }
  _periods() { return this._attrs.periodos || []; }
  _levels() { return (this._attrs.niveles_kw || [1.4, 2.2, 3.5, 7]).filter((x) => x > 0); }

  _measure() {
    const w = Math.max(300, Math.round(this.shadowRoot.getElementById('wrap').clientWidth || 600));
    if (w === this._w) return;
    this._w = w;
    this._svg.setAttribute('viewBox', `0 0 ${w} ${AXIS + ROWS * ROW}`);
    this._svg.style.height = `${AXIS + ROWS * ROW}px`;
    if (this._hass) this._draw();
  }

  /** Everything drawn on the grid: server occurrences (minus the one being edited) and the edit */
  _blocks() {
    const a = this._attrs, sel = this._sel, out = [];
    const byId = Object.fromEntries(this._periods().map((p) => [p.id, p]));
    for (const [id, t0, t1, mode, p0, p1, sun, grid, cost] of a.intervalos || []) {
      const p = byId[id];
      if (!p) continue;
      const weekly = p.weekdays.length > 0;
      if (sel && sel.id === id && (!weekly || (sel.occ0 && sel.occ0.t0 === t0 && sel.occ0.t1 === t1))) continue;
      out.push({ id, t0, t1, mode, kw: p.kw, pct: p.pct, weekly, p0, p1, cost, enabled: p.enabled, sibling: !!(sel && sel.id === id) });
    }
    out.sort((x, y) => Number(y.weekly) - Number(x.weekly)); // weekly under one-off
    if (sel) {
      const r = this._resultOf(sel);
      out.push({ id: sel.id, t0: sel.t0, t1: sel.t1, mode: sel.mode, kw: sel.kw, pct: sel.pct, weekly: sel.weekdays.length > 0, p0: r && r[4], p1: r && r[5], cost: r && r[8], enabled: sel.enabled, selected: true });
    }
    return out;
  }

  _resultOf(sel) {
    return (this._attrs.intervalos || []).find((r) => r[0] === sel.id && r[2] > sel.t0 && r[1] < sel.t1) || null;
  }

  _busy(withSel) {
    const id = withSel ? null : this._sel && this._sel.id;
    const byId = Object.fromEntries(this._periods().map((p) => [p.id, p]));
    return (this._attrs.intervalos || []).filter((r) => r[0] !== id && byId[r[0]] && !byId[r[0]].weekdays.length).map((r) => ({ t0: r[1], t1: r[2] }));
  }

  // ------------------------------------------------------------------ drawing

  _geo() {
    const W = this._w || 600, plotW = W - LBL;
    return { W, plotW, X: (m) => LBL + (m / 1440) * plotW, Y: (r) => AXIS + r * ROW };
  }

  _draw() {
    if (!this._hass || !this._w) return;
    const a = this._attrs, now = this._now;
    if (!this._drag) this._day0 = dayStart(now);
    const day0 = this._day0, { W, plotW, X, Y } = this._geo();
    let g = '';
    for (let r = 0; r < ROWS; r++) {
      if (r % 2 === 0) g += `<rect x="${LBL}" y="${Y(r)}" width="${plotW}" height="${ROW}" fill="rgba(154,156,161,.06)"/>`;
      const d = new Date(timeAt(day0, r, 0) * 1000);
      g += `<text x="6" y="${Y(r) + ROW / 2 - 2}" class="dow${r === 0 ? ' today' : ''}">${r === 0 ? 'hoy' : DOW[d.getDay()]}</text><text x="6" y="${Y(r) + ROW / 2 + 13}" class="dnum">${d.getDate()}</text>`;
    }
    const sunMax = Math.max(1, ...(a.horas || []).map((h) => h[1]));
    for (const [t, sun, promo] of a.horas || []) {
      const r = rowOf(day0, t);
      if (r < 0 || r >= ROWS) continue;
      const x = X(minuteOf(t)), w = plotW / 24;
      if (promo) g += `<rect x="${x}" y="${Y(r)}" width="${w}" height="${ROW}" fill="rgba(66,165,245,.11)"/>`;
      if (sun > 0.05) { const h = (sun / sunMax) * ROW * 0.5; g += `<rect x="${x}" y="${Y(r) + ROW - h}" width="${w}" height="${h}" fill="rgba(255,202,40,.17)"/>`; }
    }
    g += `<rect x="${LBL}" y="${Y(0)}" width="${X(minuteOf(now)) - LBL}" height="${ROW}" fill="rgba(0,0,0,.22)"/>`;
    for (const h of [0, 6, 12, 18, 24]) {
      const x = X(h * 60);
      g += `<line x1="${x}" x2="${x}" y1="${AXIS - 4}" y2="${AXIS + ROWS * ROW}" class="${h % 24 === 0 ? 'edge' : 'hour'}"/>`;
      if (h < 24) g += `<text x="${x + 3}" y="${AXIS - 8}" class="tick">${h}h</text>`;
    }
    for (let r = 1; r < ROWS; r++) g += `<line x1="${LBL}" x2="${W}" y1="${Y(r)}" y2="${Y(r)}" class="rowline"/>`;
    // battery curve inside each row
    const pts = a.curva || [];
    for (let r = 0; r < ROWS; r++) {
      const lo = timeAt(day0, r, 0), hi = timeAt(day0, r + 1, 0);
      const row = pts.filter(([t]) => t >= lo - 1800 && t <= hi + 1800).map(([t, p]) => `${X(Math.max(0, Math.min(1440, (t - lo) / 60))).toFixed(1)},${(Y(r) + ROW - 5 - (p / 100) * (ROW - 12)).toFixed(1)}`);
      if (row.length > 1) g += `<polyline points="${row.join(' ')}" class="curve"/>`;
    }
    for (const b of this._blocks()) g += this._blockSvg(b, day0, X, Y);
    if (this._drag && this._drag.ghost) g += this._ghostSvg(this._drag.ghost, day0, X, Y);
    const nr = rowOf(day0, now);
    if (nr >= 0 && nr < ROWS) g += `<line x1="${X(minuteOf(now))}" x2="${X(minuteOf(now))}" y1="${Y(nr)}" y2="${Y(nr) + ROW}" class="now"/>`;
    this._layer.innerHTML = g;
  }

  _blockSvg(b, day0, X, Y) {
    const m = MODES[b.mode], color = m ? m.color : '#90caf9';
    const segs = segments(day0, b.t0, b.t1);
    if (!segs.length) return '';
    let g = '';
    const best = segs.reduce((p, s) => (s.m1 - s.m0 > p.m1 - p.m0 ? s : p), segs[0]);
    for (const s of segs) {
      const x0 = X(s.m0), x1 = X(s.m1), y = Y(s.row) + 3, h = ROW - 6, w = x1 - x0;
      const fill = b.weekly ? 0.3 : 0.55;
      g += `<rect x="${x0}" y="${y}" width="${Math.max(2, w)}" height="${h}" rx="9" fill="${color}" fill-opacity="${b.enabled === false ? 0.15 : fill}" stroke="${b.selected ? '#fff' : color}" stroke-width="${b.selected ? 2.5 : 1.2}" ${b.weekly ? 'stroke-dasharray="5 3"' : ''} class="blk"/>`;
      if (s === best && w > 34) {
        const detail = !m ? '¿Qué hacer?' : b.mode === 'fixed' ? `${num(b.kw)} kW` : b.mode === 'away' ? `−${Math.round(b.pct)} %` : 'Sol';
        g += `<text x="${x0 + 9}" y="${y + 20}" class="bt">${m ? m.glyph + ' ' : ''}${detail}${b.weekly ? ' ↻' : ''}</text>`;
        if (b.p1 != null && w > 92 && b.mode !== 'away') g += `<text x="${x0 + 9}" y="${y + 38}" class="bs">${Math.round(b.p0)} → ${Math.round(b.p1)} %</text>`;
        else if (b.p1 != null && w > 70) g += `<text x="${x0 + 9}" y="${y + 38}" class="bs">→ ${Math.round(b.p1)} %</text>`;
      }
    }
    if (b.selected) {
      const f = segs[0], l = segs[segs.length - 1];
      for (const [x, row] of [[X(f.m0), f.row], [X(l.m1), l.row]]) g += `<rect x="${x - 5}" y="${Y(row) + 12}" width="10" height="${ROW - 24}" rx="5" fill="#fff" class="handle"/>`;
    }
    return g;
  }

  _ghostSvg(gh, day0, X, Y) {
    const segs = segments(day0, gh.t0, gh.t1);
    let g = '';
    for (const s of segs) g += `<rect x="${X(s.m0)}" y="${Y(s.row) + 3}" width="${Math.max(2, X(s.m1) - X(s.m0))}" height="${ROW - 6}" rx="9" fill="rgba(255,255,255,.18)" stroke="#fff" stroke-dasharray="4 3"/>`;
    if (segs.length) {
      const l = segs[segs.length - 1], label = fmtRange(gh.t0, gh.t1), w = label.length * 6.6 + 14;
      const x = Math.min(this._geo().W - w - 2, Math.max(LBL, X(l.m1) - w / 2)), y = Math.max(1, Y(l.row) - 22);
      g += `<rect x="${x}" y="${y}" width="${w}" height="20" rx="10" fill="#000" fill-opacity=".8"/><text x="${x + 7}" y="${y + 14}" class="bub">${label}</text>`;
    }
    return g;
  }

  _drawHead() {
    const a = this._attrs, st = this._hass.states[this._config.entity];
    const on = (this._hass.states[this._config.switch] || {}).state === 'on';
    const soc = a.soc_estimado, fin = a.soc_final;
    const warn = (a.avisos || []).map((w) => `<div class="warn">⚠ ${w}</div>`).join('');
    const html = `<div class="head"><div class="pct">${soc == null ? '—' : Math.round(soc)} % <span class="arrow">→</span> ${fin == null ? '—' : Math.round(fin)} %</div>
      <div class="state">${st ? st.state : 'sin datos'}</div>
      <button class="pill ${on ? 'on' : ''}" data-act="toggle">${on ? '▶ Ejecuta' : '◌ Vista previa'}</button>
      <button class="ic" data-act="settings" title="Ajustes">⚙</button><button class="ic" data-act="help" title="Ayuda">?</button></div>${warn}`;
    if (html !== this._headHtml) { this._headHtml = html; this._headEl.innerHTML = html; }
  }

  _drawSettings() {
    const el = this.shadowRoot.getElementById('settings');
    el.hidden = !this._showSettings;
    if (!this._showSettings) return;
    el.innerHTML = (this._config.settings || []).map((s) => {
      const st = this._hass.states[s.entity];
      return `<div class="srow" data-entity="${s.entity}"><span>${s.name || (st && st.attributes.friendly_name) || s.entity}</span><b>${st ? st.state + (st.attributes.unit_of_measurement ? ' ' + st.attributes.unit_of_measurement : '') : '—'}</b></div>`;
    }).join('') || '<div class="hint">Sin ajustes configurados</div>';
  }

  _drawHelp() {
    const el = this.shadowRoot.getElementById('help');
    el.hidden = !this._showHelp;
    if (this._showHelp) el.innerHTML = `<div class="helpbox"><b>Dibuja</b> arrastrando sobre un día (puedes seguir hacia el día de abajo); <b>toca</b> una franja para editarla; arrastra su <b>cuerpo</b> para moverla o sus <b>bordes</b> para cambiar la duración.<br>
      <span class="k" style="background:#ab47bc"></span>Fija: potencia constante · <span class="k" style="background:#66bb6a"></span>Sol: el coche se lleva todo el excedente · <span class="k" style="background:#9a9ca1"></span>Fuera: no carga y gasta el % indicado. Rayada con ↻: se repite cada semana.<br>
      Fondo azul: horas promocionadas · amarillo: sol previsto · línea blanca: batería estimada. En vista previa solo se estima; al ejecutar, el plan lleva el cargador.</div>`;
  }

  // ------------------------------------------------------------------ panel

  _drawPanel() {
    const s = this._sel, el = this._panelEl;
    if (!s) {
      el.innerHTML = `<div class="hint">Arrastra sobre un día para dibujar una franja · toca una para editarla</div>
        <div class="bar"><button class="add" data-act="new">＋ Nueva franja</button></div>`;
      return;
    }
    const levels = this._levels(), weekly = s.weekdays.length > 0, long = s.t1 - s.t0 > 86400;
    const adj = (who) => [[-3600, '−1 h'], [-900, '−15'], [900, '+15'], [3600, '+1 h']].map(([d, l]) => `<button class="chip sm" data-adj="${who}:${d}">${l}</button>`).join('');
    el.innerHTML = `<div class="sheet">
      <div class="sum"><span id="range">${fmtRange(s.t0, s.t1)}${weekly ? ' ↻' : ''}</span><button class="chip sm ${this._fine ? 'sel' : ''}" data-act="fine">⏱</button></div>
      ${this._fine ? `<div class="adj"><span>Inicio</span>${adj('s')}</div><div class="adj"><span>Fin</span>${adj('e')}</div>` : ''}
      <div class="modes">${Object.entries(MODES).map(([k, m]) => `<button class="mode ${s.mode === k ? 'sel' : ''}" style="--c:${m.color}" data-mode="${k}"><span>${m.glyph}</span>${m.name}</button>`).join('')}</div>
      ${s.mode === 'fixed' ? `<div class="chips">${levels.map((x) => `<button class="chip ${Math.abs(s.kw - x) < 0.05 ? 'sel' : ''}" data-kw="${x}">${num(x)} kW</button>`).join('')}</div>` : ''}
      ${s.mode === 'away' ? `<div class="pctrow"><b id="pctv">−${Math.round(s.pct)} %</b><input type="range" id="pct" min="0" max="100" step="1" value="${s.pct}"></div>
        <div class="chips">${[5, 10, 20, 30, 50].map((x) => `<button class="chip" data-pct="${x}">−${x} %</button>`).join('')}</div>` : ''}
      ${s.mode === 'sun' ? '<div class="hint">Todo el excedente va al coche; los aires, solo lo que le sobre.</div>' : ''}
      ${!s.mode ? '<div class="hint">Elige qué hará el coche en esta franja</div>' : ''}
      <div class="rep"><span>Repetir</span>${DAYS.map((l, i) => `<button class="chip day ${s.weekdays.includes(i) ? 'sel' : ''} ${!weekly && weekdayOf(s.t0) === i ? 'own' : ''}" data-wd="${i}" ${long ? 'disabled' : ''}>${l}</button>`).join('')}</div>
      <div class="res" id="res"></div>
      <div class="bar">${s.persisted ? '<button class="del" data-act="del">🗑 Borrar</button>' : ''}
        ${s.persisted ? `<button class="chip ${s.enabled ? 'sel' : ''}" data-act="enable">${s.enabled ? '✓ Activa' : 'Desactivada'}</button>` : ''}
        <span style="flex:1"></span><button class="done" data-act="close">Listo</button></div></div>`;
    this._updateResult();
  }

  _updateResult() {
    const el = this.shadowRoot.getElementById('res'), s = this._sel;
    if (!el || !s) return;
    const r = this._resultOf(s);
    el.textContent = !s.mode ? '' : r ? `${Math.round(r[4])} → ${Math.round(r[5])} %  ·  sol ${num(r[6])} kWh · red ${num(r[7])} kWh · ${num(r[8], 2)} €` : 'Calculando…';
  }

  _updateRange() { const el = this.shadowRoot.getElementById('range'); if (el && this._sel) el.textContent = `${fmtRange(this._sel.t0, this._sel.t1)}${this._sel.weekdays.length ? ' ↻' : ''}`; }

  // ------------------------------------------------------------------ selection and saving

  _newDraft(t0, t1) {
    const levels = this._levels();
    const kw = levels.reduce((p, x) => (Math.abs(x - 3.5) < Math.abs(p - 3.5) ? x : p), levels[0] || 3.5);
    this._sel = { id: Math.random().toString(16).slice(2, 10), persisted: false, mode: null, kw, pct: 20, enabled: true, weekdays: [], t0, t1, occ0: { t0, t1 } };
    this._fine = false;
    this._drawPanel();
  }

  _select(b) {
    const p = this._periods().find((q) => q.id === b.id);
    if (!p) return;
    this._flush();
    this._sel = { id: p.id, persisted: true, mode: p.mode, kw: p.kw, pct: p.pct, enabled: p.enabled, weekdays: [...p.weekdays], t0: b.t0, t1: b.t1, occ0: { t0: b.t0, t1: b.t1 } };
    this._drawPanel();
  }

  _deselect() { this._flush(); this._sel = null; this._drawPanel(); this._draw(); }

  _commit(now) {
    clearTimeout(this._timer);
    const go = () => {
      this._timer = null;
      const s = this._sel;
      if (!s || !s.mode) return;
      this._hass.callService('solar_optimizer', 'set_charge_period', toService(s));
      s.persisted = true; s.occ0 = { t0: s.t0, t1: s.t1 };
      const del = this.shadowRoot.querySelector('[data-act="del"]');
      if (!del) this._drawPanel();
    };
    this._pendingGo = go;
    if (now) go(); else this._timer = setTimeout(go, 400);
  }

  _flush() { if (this._timer) { clearTimeout(this._timer); this._timer = null; if (this._pendingGo) this._pendingGo(); } }

  // ------------------------------------------------------------------ gestures

  _xy(e) { const r = this._svg.getBoundingClientRect(); const k = (this._w || r.width) / r.width; return { x: (e.clientX - r.left) * k, y: (e.clientY - r.top) * k }; }

  _timeAt(p) {
    const { plotW } = this._geo();
    return pointToTime(this._day0, Math.floor((p.y - AXIS) / ROW), (p.x - LBL) / plotW);
  }

  _hit(p) {
    const { plotW, X } = this._geo(), day0 = this._day0;
    const row = Math.floor((p.y - AXIS) / ROW);
    if (row < 0 || row >= ROWS || p.x < LBL) return { kind: 'none' };
    const t = timeAt(day0, row, ((p.x - LBL) / plotW) * 1440);
    const s = this._sel;
    if (s) {
      const segs = segments(day0, s.t0, s.t1), f = segs[0], l = segs[segs.length - 1];
      const z = Math.min(HANDLE, Math.max(6, (X(l.m1) - X(f.m0)) / 3));
      if (f && f.row === row && Math.abs(p.x - X(f.m0)) <= z) return { kind: 'edgeL' };
      if (l && l.row === row && Math.abs(p.x - X(l.m1)) <= z) return { kind: 'edgeR' };
    }
    const blocks = this._blocks().reverse(); // the edited one first, then one-off over weekly
    const b = blocks.find((q) => t >= q.t0 && t < q.t1);
    return b ? { kind: 'block', block: b } : { kind: 'none' };
  }

  _down(e) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (!this._w) return;
    const p = this._xy(e), hit = this._hit(p);
    this._svg.setPointerCapture(e.pointerId);
    this._day0 = this._day0 || dayStart(this._now);
    const d = { start: p, moved: false, kind: 'create', t: this._timeAt(p), ghost: null };
    if (hit.kind === 'block') {
      if (!(this._sel && hit.block.selected)) this._select(hit.block);
      if (!this._sel) return;
      Object.assign(d, { kind: 'move', t0: this._sel.t0, t1: this._sel.t1 });
    } else if (hit.kind === 'edgeL' || hit.kind === 'edgeR') {
      Object.assign(d, { kind: hit.kind, t0: this._sel.t0, t1: this._sel.t1 });
    } else if (hit.kind === 'none' && p.x >= LBL && p.y >= AXIS) {
      d.anchor = d.t;
    } else { return; }
    d.busy = d.kind === 'create' ? this._busy(true) : this._sel.weekdays.length ? [] : this._busy();
    this._drag = d;
    this._draw();
  }

  _move(e) {
    const d = this._drag;
    if (!d) { this._hover(e); return; }
    const p = this._xy(e);
    if (!d.moved && Math.abs(p.x - d.start.x) + Math.abs(p.y - d.start.y) < 5) return;
    d.moved = true;
    const t = this._timeAt(p), s = this._sel;
    if (d.kind === 'create') {
      const cur = limitCursor(d.anchor, t, d.busy);
      d.ghost = cur >= d.anchor ? { t0: d.anchor, t1: cur } : { t0: cur, t1: d.anchor };
    } else if (d.kind === 'move') {
      const delta = limitShift(d.t0, d.t1, t - d.t, d.busy);
      s.t0 = d.t0 + delta; s.t1 = d.t1 + delta;
    } else if (d.kind === 'edgeR') {
      s.t1 = limitEdge('r', d.t0, d.t1, t, d.busy);
    } else if (d.kind === 'edgeL') {
      s.t0 = limitEdge('l', d.t0, d.t1, t, d.busy);
    }
    if (!this._raf) this._raf = requestAnimationFrame(() => { this._raf = null; this._draw(); this._updateRange(); });
  }

  _hover(e) {
    if (e.pointerType !== 'mouse' || !this._w) return;
    const k = this._hit(this._xy(e)).kind;
    this._svg.style.cursor = k === 'block' ? 'grab' : k === 'none' ? 'crosshair' : 'ew-resize';
  }

  _up() {
    const d = this._drag;
    if (!d) return;
    this._drag = null;
    const s = this._sel;
    if (d.kind === 'create') {
      if (!d.moved) {
        if (s) { this._deselect(); return; }
        const end = limitCursor(d.anchor, d.anchor + 7200, d.busy);
        if (end - d.anchor >= SNAP) this._newDraft(d.anchor, end);
      } else if (d.ghost && d.ghost.t1 - d.ghost.t0 >= SNAP) {
        this._flush();
        this._newDraft(d.ghost.t0, d.ghost.t1);
      }
    } else if (d.moved && s) {
      if (s.weekdays.length) s.weekdays = shiftWeekdays(s.weekdays, this._day0, s.occ0.t0, s.t0);
      this._drawPanel();
      this._commit(true);
    }
    this._draw();
  }

  _cancel() {
    const d = this._drag;
    this._drag = null;
    if (d && this._sel && d.t0 != null) { this._sel.t0 = d.t0; this._sel.t1 = d.t1; }
    this._draw();
  }

  // ------------------------------------------------------------------ buttons

  _click(e) {
    const el = e.target.closest ? e.target.closest('[data-act],[data-mode],[data-kw],[data-pct],[data-wd],[data-adj],[data-entity]') : null;
    if (!el) return;
    const s = this._sel, ds = el.dataset;
    if (ds.entity) { this.dispatchEvent(new CustomEvent('hass-more-info', { detail: { entityId: ds.entity }, bubbles: true, composed: true })); return; }
    if (ds.act === 'toggle') { this._hass.callService('switch', 'toggle', { entity_id: this._config.switch }); return; }
    if (ds.act === 'settings') { this._showSettings = !this._showSettings; this._drawSettings(); return; }
    if (ds.act === 'help') { this._showHelp = !this._showHelp; this._drawHelp(); return; }
    if (ds.act === 'new') {
      const t = Math.ceil(this._now / 1800) * 1800;
      this._flush(); this._day0 = dayStart(this._now);
      this._newDraft(t, t + 7200); this._draw(); return;
    }
    if (!s) return;
    if (ds.mode) { s.mode = ds.mode; this._drawPanel(); this._commit(true); }
    else if (ds.kw) { s.kw = Number(ds.kw); this._drawPanel(); this._commit(false); }
    else if (ds.pct) { s.pct = Number(ds.pct); this._drawPanel(); this._commit(false); }
    else if (ds.wd !== undefined) {
      const i = Number(ds.wd);
      // one-off -> weekly: its own day plus the tapped one; no day left -> one-off again
      const w = s.weekdays.length ? new Set(s.weekdays) : new Set([weekdayOf(s.t0)]);
      if (s.weekdays.length && w.has(i)) w.delete(i); else w.add(i);
      s.weekdays = [...w].sort(); s.occ0 = { t0: s.t0, t1: s.t1 };
      this._drawPanel(); this._commit(true);
    } else if (ds.adj) {
      const [who, delta] = ds.adj.split(':'), dt = Number(delta), busy = s.weekdays.length ? [] : this._busy();
      if (who === 's') s.t0 = limitEdge('l', s.t0, s.t1, s.t0 + dt, busy); else s.t1 = limitEdge('r', s.t0, s.t1, s.t1 + dt, busy);
      this._updateRange(); this._draw(); this._commit(false);
    } else if (ds.act === 'fine') { this._fine = !this._fine; this._drawPanel(); }
    else if (ds.act === 'enable') { s.enabled = !s.enabled; this._drawPanel(); this._commit(true); }
    else if (ds.act === 'del') { clearTimeout(this._timer); this._timer = null; this._hass.callService('solar_optimizer', 'remove_charge_period', { id: s.id }); this._sel = null; this._drawPanel(); this._draw(); }
    else if (ds.act === 'close') this._deselect();
  }

  _input(e) {
    if (e.target.id !== 'pct' || !this._sel) return;
    this._sel.pct = Number(e.target.value);
    this.shadowRoot.getElementById('pctv').textContent = `−${Math.round(this._sel.pct)} %`;
    this._commit(false);
  }
}

ChargePlanCard.css = `
  :host { display: block; }
  ha-card { padding: 10px 12px 14px; }
  button { font: inherit; color: var(--primary-text-color); cursor: pointer; border: 0; background: none; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .pct { font-size: 24px; font-weight: 600; flex: 1; min-width: 120px; } .arrow { color: var(--secondary-text-color); }
  .state { color: var(--secondary-text-color); font-size: 13px; }
  .pill { padding: 6px 12px; border-radius: 16px; background: rgba(154,156,161,.2); font-size: 13px; }
  .pill.on { background: rgba(102,187,106,.25); color: #66bb6a; }
  .ic { width: 32px; height: 32px; border-radius: 50%; background: rgba(154,156,161,.15); font-size: 16px; }
  .warn { color: #ffa726; font-size: 13px; margin-top: 4px; }
  #help, #settings { margin: 8px 0; } .helpbox { font-size: 13px; line-height: 1.6; background: rgba(154,156,161,.1); padding: 10px 12px; border-radius: 10px; }
  .k { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin: 0 4px 0 0; vertical-align: middle; }
  .srow { display: flex; justify-content: space-between; padding: 9px 4px; border-top: 1px solid rgba(154,156,161,.2); cursor: pointer; font-size: 14px; }
  #wrap { margin-top: 8px; } svg { display: block; width: 100%; touch-action: pan-y; user-select: none; -webkit-user-select: none; }
  text { fill: var(--secondary-text-color); font-size: 11px; } .dow { font-size: 12px; font-weight: 500; } .dow.today { fill: #ffa726; } .dnum { font-size: 11px; opacity: .8; } .tick { font-size: 10px; opacity: .8; }
  .edge { stroke: rgba(154,156,161,.5); } .hour { stroke: rgba(154,156,161,.2); } .rowline { stroke: rgba(154,156,161,.2); }
  .curve { fill: none; stroke: var(--primary-text-color); stroke-width: 2; stroke-linejoin: round; opacity: .9; pointer-events: none; }
  .now { stroke: #ffa726; stroke-width: 2; } .bt { fill: #fff; font-size: 13px; font-weight: 600; pointer-events: none; } .bs { fill: #fff; font-size: 11px; opacity: .9; pointer-events: none; }
  .bub { fill: #fff; font-size: 12px; } .blk { cursor: grab; }
  .hint { color: var(--secondary-text-color); font-size: 13px; padding: 8px 2px; }
  .sheet { margin-top: 10px; padding: 12px; border-radius: 14px; background: rgba(154,156,161,.1); display: flex; flex-direction: column; gap: 10px; }
  .sum { display: flex; align-items: center; justify-content: space-between; font-weight: 600; font-size: 15px; }
  .modes { display: flex; gap: 8px; } .mode { flex: 1; padding: 10px 0; border-radius: 12px; background: rgba(154,156,161,.15); font-size: 14px; display: flex; flex-direction: column; align-items: center; gap: 2px; } .mode span { font-size: 20px; }
  .mode.sel { background: var(--c); color: #fff; }
  .chips, .adj, .rep { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; } .adj span, .rep span { width: 52px; font-size: 13px; color: var(--secondary-text-color); }
  .chip { padding: 6px 12px; border-radius: 14px; background: rgba(154,156,161,.2); font-size: 13px; } .chip.sm { padding: 3px 9px; font-size: 12px; }
  .chip.sel { background: var(--primary-color); color: var(--text-primary-color, #fff); } .chip.day { min-width: 34px; padding: 7px 0; } .chip.own { outline: 1.5px dashed var(--primary-color); } .chip:disabled { opacity: .35; }
  .pctrow { display: flex; align-items: center; gap: 12px; } .pctrow b { min-width: 58px; font-size: 18px; } .pctrow input { flex: 1; }
  .res { font-size: 13px; color: var(--secondary-text-color); min-height: 16px; }
  .bar { display: flex; align-items: center; gap: 8px; margin-top: 4px; } .del { color: #ef5350; padding: 6px 4px; }
  .done { background: var(--primary-color); color: var(--text-primary-color, #fff); padding: 8px 22px; border-radius: 18px; } .add { color: var(--primary-color); padding: 6px 2px; }
`;

if (typeof customElements !== 'undefined' && !customElements.get('charge-plan-card')) {
  customElements.define('charge-plan-card', ChargePlanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: 'charge-plan-card', name: 'Plan de carga del coche', description: 'Franjas de carga dibujadas arrastrando y batería estimada' });
}
