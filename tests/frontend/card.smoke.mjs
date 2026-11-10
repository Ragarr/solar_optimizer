// Gesture smoke test of the card in jsdom (not part of CI): needs `npm i jsdom` in JSDOM_DIR.
//   TZ=Europe/Madrid JSDOM_DIR=/tmp/jsd node tests/frontend/card.smoke.mjs
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const { JSDOM } = createRequire(`${process.env.JSDOM_DIR}/x.js`)('jsdom');
const dom = new JSDOM('<body></body>', { pretendToBeVisual: true });
const w = dom.window;
Object.assign(globalThis, { window: w, document: w.document, HTMLElement: w.HTMLElement, customElements: w.customElements, CustomEvent: w.CustomEvent });
globalThis.requestAnimationFrame = (f) => { f(); return 1; };
w.Element.prototype.setPointerCapture = () => {};
w.Element.prototype.getBoundingClientRect = function () { return this.id === 'svg' ? { left: 0, top: 0, width: 600, height: 414 } : { left: 0, top: 0, width: 600, height: 0 }; };
await import('../../custom_components/solar_optimizer/frontend/charge-plan-card.js');

const T = (s) => new Date(s).getTime() / 1000;
const now = T('2026-11-11T15:20:00');
const calls = [];
const attrs = { ahora: now, periodos: [], intervalos: [], curva: [], horas: [], niveles_kw: [1.4, 2.2, 3.5, 7], soc_estimado: 50, soc_final: 60, avisos: [] };
const hass = () => ({ states: { 'sensor.solar_optimizer_plan_coche': { state: 'Vista previa', attributes: attrs }, 'switch.solar_optimizer_plan_coche': { state: 'off' } }, callService: (d, s, data) => calls.push([d, s, data]) });
const card = document.createElement('charge-plan-card');
card.setConfig({});
card.hass = hass();
document.body.append(card);
card._measure();

const LBL = 46, AXIS = 22, ROW = 56, X = (h) => LBL + (h / 24) * (600 - LBL), Y = (r) => AXIS + r * ROW + ROW / 2;
const fire = (type, x, y) => card._svg.dispatchEvent(new w.MouseEvent(type, { clientX: x, clientY: y, bubbles: true }));
const drag = (a, b) => { fire('pointerdown', ...a); fire('pointermove', ...b); fire('pointerup', ...b); };
const click = (sel) => card.shadowRoot.querySelector(sel).dispatchEvent(new w.MouseEvent('click', { bubbles: true, composed: true }));

// 1. draw 08:00-10:00 on Friday (row 4 of the week Mon 9 - Sun 15): a draft, nothing saved yet
drag([X(8) + 1, Y(4)], [X(10) + 1, Y(4)]);
assert.equal(card._sel.t0, T('2026-11-13T08:00:00'));
assert.equal(card._sel.t1, T('2026-11-13T10:00:00'));
assert.equal(calls.length, 0);
assert.ok(card.shadowRoot.querySelector('[data-mode="fixed"]'), 'mode buttons shown');
// 2. choose the mode: saved at once
click('[data-mode="fixed"]');
assert.equal(calls.length, 1);
assert.equal(calls[0][1], 'set_charge_period');
assert.equal(calls[0][2].mode, 'fixed');
assert.equal(calls[0][2].kw, 3.5);
assert.equal(calls[0][2].end - calls[0][2].start, 7200);
const id = calls[0][2].id;
// 3. the server publishes it; stretch its right edge by one hour
const p = { id, mode: 'fixed', kw: 3.5, pct: 0, enabled: true, weekdays: [] };
attrs.periodos = [p];
attrs.intervalos = [[id, calls[0][2].start, calls[0][2].end, 'fixed', 50, 60, 0, 7, 0.9]];
card.hass = hass();
drag([X(10) - 2, Y(4)], [X(11) + 1, Y(4)]);
await new Promise((r) => setTimeout(r, 500));
const last = calls.at(-1)[2];
assert.equal(last.id, id);
assert.equal(last.end - last.start, 3 * 3600, 'stretched to 3 h');
// 4. repeat on Fridays and Saturdays -> weekly
click('[data-wd="5"]');
const wk = calls.at(-1)[2];
assert.deepEqual(wk.weekdays, [4, 5]);
assert.equal(wk.start_time, '08:00');
assert.equal(wk.end_time, '11:00');
// 5. delete
click('[data-act="del"]');
assert.deepEqual(calls.at(-1), ['solar_optimizer', 'remove_charge_period', { id }]);
assert.equal(card._sel, null);
// 6. a tap on empty space makes a 2 h draft; a second tap deselects it
fire('pointerdown', X(14), Y(3)); fire('pointerup', X(14), Y(3));
assert.equal(card._sel.t1 - card._sel.t0, 7200);
fire('pointerdown', X(2), Y(5)); fire('pointerup', X(2), Y(5));
assert.equal(card._sel, null);
// 7. weeks: next week starts on Monday 16 and drawing there lands on those dates
click('[data-act="wk:1"]');
assert.equal(card._week, 1);
drag([X(9) + 1, Y(1)], [X(12) + 1, Y(1)]); // Tuesday 17
assert.equal(card._sel.t0, T('2026-11-17T09:00:00'));
assert.equal(card._sel.t1, T('2026-11-17T12:00:00'));
click('[data-act="wk:0"]');
assert.equal(card._week, 0);
assert.equal(card._sel, null, 'changing week closes the editor');
click('[data-act="wk:-1"]');
assert.equal(card._week, 0, 'no weeks before the current one');
click('[data-act="wk:4"]'); click('[data-act="wk:4"]'); click('[data-act="wk:4"]');
assert.ok(card._week <= card._maxWeek() && card._week >= 7, `week ${card._week} within the horizon`);
console.log('card smoke test: ok');
