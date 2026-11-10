// node --test tests/frontend  (TZ=Europe/Madrid)
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  dayStart, timeAt, rowOf, minuteOf, segments, pointToTime, limitCursor, limitShift, limitEdge,
  shiftWeekdays, toService, fmtDuration, fmtRange, weekdayOf, weekStart, fmtWeek, SNAP,
} from '../../custom_components/solar_optimizer/frontend/charge-plan-card.js';

const T = (s) => new Date(s).getTime() / 1000; // local time
const day0 = dayStart(T('2026-11-11T15:20:00')); // Wed 11 Nov

test('rows and minutes', () => {
  assert.equal(rowOf(day0, T('2026-11-13T08:00:00')), 2);
  assert.equal(minuteOf(T('2026-11-13T08:30:00')), 510);
  assert.equal(timeAt(day0, 2, 510), T('2026-11-13T08:30:00'));
  assert.equal(weekdayOf(T('2026-11-11T10:00:00')), 2); // Wednesday
});

test('segments split a night over two rows and clip to the visible days', () => {
  const s = segments(day0, T('2026-11-11T22:00:00'), T('2026-11-12T06:00:00'));
  assert.deepEqual(s, [{ row: 0, m0: 1320, m1: 1440 }, { row: 1, m0: 0, m1: 360 }]);
  const last = segments(day0, T('2026-11-17T22:00:00'), T('2026-11-18T06:00:00'));
  assert.deepEqual(last, [{ row: 6, m0: 1320, m1: 1440 }]);
  assert.deepEqual(segments(day0, T('2026-11-09T10:00:00'), T('2026-11-10T10:00:00')), []);
});

test('segments across the DST change keep local clock minutes', () => {
  const d0 = dayStart(T('2026-10-24T12:00:00')); // clocks go back on the 25th
  const s = segments(d0, T('2026-10-24T22:00:00'), T('2026-10-25T06:00:00'));
  assert.deepEqual(s, [{ row: 0, m0: 1320, m1: 1440 }, { row: 1, m0: 0, m1: 360 }]);
  assert.equal(fmtDuration(T('2026-10-25T06:00:00') - T('2026-10-24T22:00:00')), '9 h');
});

test('pointToTime snaps to 15 minutes and clamps', () => {
  assert.equal(pointToTime(day0, 1, 0.5), T('2026-11-12T12:00:00'));
  assert.equal(pointToTime(day0, 1, 10 / 1440), T('2026-11-12T00:15:00') - 0); // 10 min -> 15
  assert.equal(pointToTime(day0, 1, 2), T('2026-11-13T00:00:00'));
  assert.equal(pointToTime(day0, 9, 0), T('2026-11-17T00:00:00')); // row clamped to 6
});

test('drawing stops at neighbouring periods', () => {
  const busy = [{ t0: 1000, t1: 2000 }, { t0: 5000, t1: 6000 }];
  assert.equal(limitCursor(2500, 9000, busy), 5000);
  assert.equal(limitCursor(2500, 100, busy), 2000);
  assert.equal(limitCursor(2500, 3000, busy), 3000);
  assert.equal(limitCursor(6500, 100, busy), 6000);
});

test('moving and stretching respect neighbours and the minimum length', () => {
  const busy = [{ t0: 1000, t1: 2000 }, { t0: 5000, t1: 6000 }];
  assert.equal(limitShift(2500, 3500, 5000, busy), 1500); // stops against 5000
  assert.equal(limitShift(2500, 3500, -5000, busy), -500); // stops against 2000
  assert.equal(limitEdge('r', 2500, 3500, 9000, busy), 5000);
  assert.equal(limitEdge('r', 2500, 3500, 2500, busy), 2500 + SNAP);
  assert.equal(limitEdge('l', 2500, 3500, 0, busy), 2000);
  assert.equal(limitEdge('l', 2500, 3500, 4000, busy), 3500 - SNAP);
});

test('moving a weekly period to another day shifts its weekdays', () => {
  const wd = [0, 1, 2, 3, 4]; // Mon-Fri
  assert.deepEqual(shiftWeekdays(wd, day0, T('2026-11-11T22:00:00'), T('2026-11-12T22:00:00')), [1, 2, 3, 4, 5]);
  assert.deepEqual(shiftWeekdays([6], day0, T('2026-11-11T22:00:00'), T('2026-11-12T22:00:00')), [0]);
  assert.deepEqual(shiftWeekdays(wd, day0, T('2026-11-11T22:00:00'), T('2026-11-11T20:00:00')), wd);
});

test('service data for one-off and weekly periods', () => {
  const base = { id: 'a1', mode: 'fixed', kw: 3.5, pct: 0, enabled: true, t0: T('2026-11-11T22:00:00'), t1: T('2026-11-12T06:00:00') };
  assert.deepEqual(toService({ ...base, weekdays: [] }), { mode: 'fixed', enabled: true, id: 'a1', kw: 3.5, start: base.t0, end: base.t1, weekdays: [] });
  assert.deepEqual(toService({ ...base, mode: 'away', pct: 15, weekdays: [2, 3] }),
    { mode: 'away', enabled: true, id: 'a1', pct: 15, weekdays: [2, 3], start_time: '22:00', end_time: '06:00' });
  assert.deepEqual(toService({ ...base, mode: 'sun', weekdays: [] }).kw, undefined);
});

test('labels', () => {
  assert.equal(fmtDuration(1800), '30 min');
  assert.equal(fmtDuration(8 * 3600), '8 h');
  assert.equal(fmtDuration(26 * 3600), '1 d 2 h');
  assert.equal(fmtRange(T('2026-11-11T22:00:00'), T('2026-11-12T06:00:00')), 'mié 22:00 → jue 06:00 · 8 h');
  assert.equal(fmtRange(T('2026-11-11T10:00:00'), T('2026-11-11T12:30:00')), 'mié 10:00 → 12:30 · 2 h 30 min');
});

test('calendar weeks start on Monday', () => {
  const wed = T('2026-11-11T15:20:00');
  assert.equal(weekStart(wed), T('2026-11-09T00:00:00'));
  assert.equal(weekStart(wed, 2), T('2026-11-23T00:00:00'));
  assert.equal(weekStart(T('2026-11-15T23:00:00')), T('2026-11-09T00:00:00')); // Sunday
  assert.equal(weekStart(T('2026-11-16T00:00:00')), T('2026-11-16T00:00:00')); // Monday
  assert.equal(fmtWeek(T('2026-11-09T00:00:00')), '9–15 nov');
  assert.equal(fmtWeek(T('2026-12-28T00:00:00')), '28 dic – 3 ene');
});
