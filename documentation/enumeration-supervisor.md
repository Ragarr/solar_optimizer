# Enumeration optimiser and supervisor

`algorithm: enumeration` (default for new configurations) replaces the simulated annealing by an
exact economic optimisation, and adds a supervisor that protects the devices. Existing
configurations keep `annealing` until it is changed.

The core is pure Python (no Home Assistant import), shared by the integration, the tests and the
offline simulator:

| Module | Role |
|---|---|
| `smoothing.py` | Time-weighted moving average |
| `optimizer_core.py` | Enumeration per group + water-filling between groups |
| `supervisor_core.py` | Guards, load shedding, invariants, verification |
| `controller_core.py` | One control cycle (measures → plan → commands) |
| `supervisor_runtime.py` | Home Assistant side: inputs, commands, entities, notifications |

## Model

- Consumers are split in **groups** ordered by priority (group 1 first). Each one has admissible
  **levels** (W; on/off devices: 0 and `power_max`; modulable devices: the options of a `select`
  power entity, e.g. `6A`…`32A` × the divide factor, or `power_min`…`power_max` by `power_step`),
  a **value** of 1 kWh delivered (€/kWh, one number entity per tariff period) and a **share**
  `alpha_g` of the surplus per group (number entities, normalised to sum 1).
- Surplus: `E = P − R` with `R = X − Σ y_i` (house consumption without the controllable devices,
  `y_i` measured if a measure entity is configured, nominal level otherwise). With the net
  consumption `N = X − P`: `E = Σ y_i − N`. Excluded devices, devices in manual mode and faulty
  devices are not controllable: their consumption stays in `R`.
- `Ê` is the 2 min time-weighted mean of `E` (samples right after a command are discarded);
  `E+ = max(0, Ê)`. Import margin `H` = contracted power × (1 − margin) − import of the house
  without the controllable devices (worst of `Ê` and the instantaneous `E`).
- Group sub-problem (budget `b`, margin `h`), exhaustive enumeration of the admissible levels:

  ```
  S = Σ x_i;  u = min(S, b);  m = max(0, S − b);  r = b − u
  J = Σ v_i x_i − p_exp u − p_imp m        subject to m ≤ min(h, cap_g)
  ```

  Ties keep the current plan, then prefer the largest `S`.
- Water-filling: each group gets `alpha_g E+`, then the surplus left is offered again to the groups
  in priority order until no plan changes (at most 10 passes).

## Supervisor

It never rewrites the plan, it only delays (confirmations) or holds it (blocks). Each cycle it
gives the optimiser the admissible levels:

```
can_start = OFF and t − t_off ≥ T_off and starts in 60 min < N_max and t − last start (any) ≥ separation
can_stop  = ON and (t − t_on ≥ T_on or load shedding)
L_ef: ON and not can_stop → L without 0;  OFF and not can_start → {0};  else L
```

Priority of the rules: 1. contracted power, 2. minimal off time (no exception), 3. minimal on time
and starts per hour, 4. confirmations, 5. the plan.

| Parameter (device) | Option | Default |
|---|---|---|
| Minimal on time `T_on` | `duration_min` | — |
| Minimal off time `T_off` | `duration_stop_min` | `duration_min` |
| Interval between level changes | `duration_power_min` | `duration_min` |
| Start / stop confirmation | `confirm_on_min` / `confirm_off_min` | 1 / 5 min |
| Starts per hour | `max_starts_per_hour` | 3 |
| Load shedding reaction | `shed_reaction_sec` | 10 s |
| State read back after a command | `verify_after_sec` | 45 s (3 retries: 30, 60, 120 s) |
| Echo window | `echo_window_sec` | 60 s |
| Not absorbing (W, optional) | `not_absorbing_w` | — |

| Parameter (central) | Option | Default |
|---|---|---|
| Period | `refresh_period_sec` | 30 s |
| Smoothing window | `smoothing_window_sec` | 120 s |
| Contracted power, margin | `contracted_power_entity_id`, `contracted_power_margin_percent` | —, 10 % |
| Off-peak period | `offpeak_entity_id` | — |
| Separation between two starts | `start_separation_sec` | 60 s |
| Warm-up after a restart | `warmup_sec` | 120 s |
| Measures discarded after a command | `discard_after_command_sec` | 25 s |
| Notifications | `notify_service` | — |
| Groups, import caps | `group_count`, `group_import_caps` (`2:0.5`) | 2, none |
| Users of another controller | `foreign_user_ids` | — |

**Load shedding** (fast path on every new measure of the net consumption): when the import exceeds
the limit, the modulable device goes to its minimal level, then it stops, then the other devices
stop from the lowest to the highest value, waiting the reaction time between steps. The minimal
off time still applies; manual and excluded devices are never touched; a notification is sent if
nothing is left.

**Invariants**, checked before every command (a command that would break one is not sent, it is
counted in `sensor.solar_optimizer_violations` and notified):

- I1 between a stop and the next start: at least `T_off`;
- I2 between a start and the next stop: at least `T_on` (except load shedding);
- I3 starts in any 60 min window ≤ `max_starts_per_hour`;
- I4 level changes separated by the level interval (except shedding decreases);
- I5 the import goes back under the limit within the reaction time (checked over time);
- I6 two starts of any devices separated by `start_separation_sec`.

**Robustness**: sensors unavailable or frozen (unchanged 5 min with sun) → hold; for more than
10 min → safe state (modulable device stopped, respecting `T_on`). Commands are read back and
retried 3 times, then the device is marked faulty (left out until "back to automatic" or the
daily reset). After a restart the state of each device (timers, starts) is restored from its
switch attributes; a device whose real state changed while Home Assistant was down counts as just
switched; nothing is commanded during the warm-up. A device that does not absorb is left out for
30 min. Within the echo window, a reported state going back to the previous value is a flicker
(cloud devices) and is ignored.

**Manual changes** (`manual_detection`): each command has its own `Context`. A change of the
device entity or of its power entity is an echo (own or derived context, or within the echo
window), from another controller (`foreign_user_ids`), made by the device itself
(`manual_ignore_template`), or manual (user, or a value different from the last commanded). A
manual change puts the device in manual mode until the "back to automatic" button / notification
action / `end_manual` service, the daily reset, `manual_max_hours` or the release template. Ending
it sends no command.

**Dry run** (`switch.solar_optimizer_dry_run`, on by default): the plan and the commands are
computed and published (`sensor.solar_optimizer_plan`, attribute `commands`), nothing is sent.

## Entities

| Entity | Content |
|---|---|
| `switch.solar_optimizer_dry_run` | Dry run |
| `sensor.solar_optimizer_plan` | Devices on in the plan; attributes: levels, `Ê`, `H`, mode, commands, per device status |
| `sensor.solar_optimizer_surplus` | `Ê` (W) |
| `sensor.solar_optimizer_planned_import` / `_export` | W |
| `sensor.solar_optimizer_violations` | Counter (should stay at 0) |
| `number.solar_optimizer_alpha_group_<g>` | Share of the surplus of group g |
| `sensor.solar_optimizer_status_<device>` | auto / manual / faulty / excluded (+ when it can start or stop) |
| `number.solar_optimizer_value_peak_<device>` / `_offpeak_` | €/kWh |
| `button.solar_optimizer_end_manual_<device>` | Back to automatic |
| `switch.enable_solar_optimizer_<device>` | Included in the plan (never switches the device) |

## Simulator

```
python -m simulator --seeds 3
```

Synthetic days (square clouds from 30 s to 10 min, random walk, clear sky, sensor outage and frozen
sensor, manual changes, full car, tariff change at 12:00 and 22:00) with a car charger and cloud
air conditioners (latencies, flickers). An independent checker verifies the invariants on the real
transitions; the report gives the switches per device and day and the extra cost against the ideal
plan (instantaneous surplus, no guards, instant devices). `tests/test_simulator.py` requires zero
violations.
