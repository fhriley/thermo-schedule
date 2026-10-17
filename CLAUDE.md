# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-process Python service that drives one or more Radio Thermostat–style Wi-Fi thermostats over their local HTTP API (`GET /query/info`, `POST /control`) according to a YAML schedule, with time-of-use "peak" handling that uses live solar production/consumption from TimescaleDB. It runs as a container (Python 3.10, `TZ=America/Phoenix`) on the `pods` podman network, managed by an Ansible-generated quadlet (`/etc/containers/systemd/container-thermo-schedule.container`, image `docker.io/fhriley/thermo-schedule`), and reads its config from `/config/schedule.yaml`.

## Commands

```bash
pip install -r requirements.txt                          # deps (the checked-in .venv is stale and lacks them)
python -m unittest test_main                             # all tests
python -m unittest test_main.Test.test__get_active_schedule   # single test
SCHEDULE=./schedule.yaml LOGLEVEL=DEBUG python main.py   # run locally (talks to real thermostat + TimescaleDB; host `timescaledb` only resolves on the `pods` network)
podman build -t docker.io/fhriley/thermo-schedule .      # build the image the quadlet runs
systemctl restart container-thermo-schedule              # restart the deployed container
```

Environment variables: `SCHEDULE` (config path, default `/config/schedule.yaml`), `LOGLEVEL` (default `INFO`). The deployed config lives at `/zfs/docker/appdata/thermo-schedule/schedule.yaml` (mounted as `/config`), not the repo copy.

## Architecture (all in `main.py`)

- **`load_schedule`** parses the YAML into a per-thermostat list of seasonal schedules sorted by `start` (`MM/DD` → `(month, day)`). Each season has `heat`/`cool` sections with all 7 weekdays (required), an optional `holiday` day, and a `peak` setpoint; time keys are integer `HHMM` (e.g. `530` = 05:30). The season-level `peak` list holds `{start, end}` HHMM windows. YAML anchors/merge keys (`<<: *x`) are used heavily to share day definitions.
- **`_get_active_schedule`** picks the season by bisecting on `(month, day)` (wrapping to the last season before the first start date via index `-1`), then the day's setpoint by bisecting on time. If the current time is before the day's first entry, it falls back to the *previous* day's last entry (and the previous season if today is a season start). Returns a dict whose `id` tuple identifies the schedule slot — the task only POSTs to the thermostat when this `id` changes, so the `id` must capture everything that should trigger an update.
- **Holidays/peak**: `_is_holiday` only counts weekday holidays whose name (from the `holidays` package, observed dates) starts with an entry in `settings.holidays`. `_is_peak` is weekdays-only, inside a peak window, and not a holiday.
- **`thermo_task`** runs every `interval` seconds per thermostat via APScheduler (`OrTrigger` of an immediate `DateTrigger` + cron; interval is clamped to ≤60 and adjusted to divide 60). It does nothing if the thermostat's own schedule is enabled or mode is off/auto (only heat=1 / cool=2 are handled). During peak it overrides the setpoint with the peak temp when solar is insufficient: if the compressor is running, compare `production - consumption` to `consumption_thresh`; if idle, compare `production` to `solar_prod_thresh`. After forcing peak, it holds peak for at least `min_comp_time_s` (tracked via `Data.last_off`) to avoid short-cycling the compressor.
- **`_set_temp`** POSTs the new state, waits, then re-reads `/query/info` to verify the setpoint (retries 3×).
- **`get_solar_prod_cons`** connects to TimescaleDB (`settings.timescaledb`: host/port/dbname/user/password) per call and averages `production_w`/`consumption_w` from the `meters` hypertable (written by telegraf every ~10s) over the last 10 minutes; values are divided by 1000 (W → kW). Failures are logged and treated as 0 production.

## Tests

`test_main.py` uses `test.yaml`, which has deliberately distinctive setpoint values (50, 86, 104, …) so tests can assert which schedule slot was chosen. It covers schedule selection, season/day rollover, holidays, and peak detection — not `thermo_task` or the network code.
