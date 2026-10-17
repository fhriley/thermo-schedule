import bisect
import datetime
import logging
import os
import signal
import threading
from time import sleep
from typing import Optional, Tuple
from urllib.parse import urljoin

import holidays
import psycopg
import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

SOLAR_PROD_QUERY = """SELECT avg(production_w), avg(consumption_w)
      FROM meters
      WHERE time > now() - interval '10 minutes'"""


class Temperature:
    def __init__(self, hhmm, temp):
        self._hhmm = datetime.time(hhmm // 100, hhmm % 100)
        self._temp = temp

    @property
    def time(self):
        return self._hhmm

    @property
    def temperature(self):
        return self._temp


_days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def load_schedule(path: str) -> Tuple[list, dict]:
    with open(path, 'r') as inf:
        schedule = yaml.safe_load(inf)

    ret = []
    for thermostat in schedule['thermostats']:
        arr = []
        for name, sched in thermostat['schedules'].items():
            start = datetime.datetime.strptime(sched['start'], "%m/%d").date()
            sched['start'] = (start.month, start.day)
            sched['peak'] = sched.get('peak') or []
            for typ in ('heat', 'cool'):
                typ_sched = sched.get(typ)
                if typ_sched:
                    new = {}
                    holiday = None
                    peak = None
                    for day, times in typ_sched.items():
                        if day == 'peak':
                            peak = times
                            continue
                        day = day.lower()
                        temps = [Temperature(hhmm, times[hhmm]) for hhmm in sorted(times.keys())]
                        if day == 'holiday':
                            holiday = temps
                        else:
                            new[_days.index(day)] = temps
                    sched[typ]['days'] = [new[key] for key in sorted(new.keys())]
                    if len(sched[typ]['days']) != 7:
                        raise Exception("schedule does not contain all days of the week")
                    sched[typ]['holiday'] = holiday
                    sched[typ]['peak'] = peak
                    if peak is None and sched['peak']:
                        logging.getLogger('thermo').warning(
                            f'{name} {typ} schedule has no peak temp, so its peak periods will be ignored')
            arr.append(sched)
        ret.append({'url': thermostat.get('url'), 'schedule': sorted(arr, key=lambda xx: xx['start'])})
    return ret, schedule.get('settings', {})


def _is_holiday(us_holidays: holidays.HolidayBase, settings: dict, dt: datetime.datetime):
    weekday = dt.weekday()
    if weekday in (5, 6):
        return False
    holiday = us_holidays.get(dt.date())
    if not holiday:
        return False
    holiday = holiday.lower()
    for check in settings.get('holidays', []):
        if holiday.startswith(check.lower()):
            return True
    return False


def _is_peak(us_holidays: holidays.HolidayBase, settings: dict, peak: list, dt: datetime.datetime):
    weekday = dt.weekday()
    if weekday in (5, 6):
        return False
    comp = dt.time().hour * 100 + dt.time().minute
    for row in peak:
        if row['start'] <= comp < row['end']:
            return not _is_holiday(us_holidays, settings, dt)
    return False


def _get_active_schedule(us_holidays: holidays.HolidayBase, settings: dict, schedule: [dict],
                         dt: datetime.datetime, mode: str) -> Optional[dict]:
    if not schedule:
        return None

    mmdd = (dt.month, dt.day)
    sch_idx = bisect.bisect_right(schedule, mmdd, key=lambda xx: xx['start'])
    sch_idx -= 1

    peak = schedule[sch_idx]['peak']
    sched = schedule[sch_idx].get(mode)
    if not sched:
        return None

    weekday = dt.weekday()
    time = dt.time()

    is_holiday = _is_holiday(us_holidays, settings, dt)
    # Holidays fall back to the weekday schedule when no holiday schedule is defined
    day = sched['holiday'] if is_holiday and sched['holiday'] else sched['days'][weekday]

    # Handle the case where we are before the first time of a new weekday and/or schedule
    if time < day[0].time:
        weekday -= 1
        if mmdd == schedule[sch_idx]['start']:
            sch_idx -= 1
            peak = schedule[sch_idx]['peak']
            sched = schedule[sch_idx].get(mode)
            if not sched:
                return None
        is_holiday = _is_holiday(us_holidays, settings, dt - datetime.timedelta(days=1))
        day = sched['holiday'] if is_holiday and sched['holiday'] else sched['days'][weekday]
        return {
            'id': (sch_idx, mode, weekday, day[-1].time, day[-1].temperature),
            'temp': day[-1].temperature,
            'is_holiday': is_holiday,
            'is_peak': _is_peak(us_holidays, settings, peak, dt),
            'peak_temp': sched['peak'],
        }

    ii = bisect.bisect_right(day, time, key=lambda xx: xx.time) - 1
    return {
        'id': (sch_idx, mode, weekday, day[ii].time, day[ii].temperature),
        'temp': day[ii].temperature,
        'is_holiday': is_holiday,
        'is_peak': _is_peak(us_holidays, settings, peak, dt),
        'peak_temp': sched['peak'],
    }


class ThermostatRejected(Exception):
    pass


def _raise_if_rejected(resp: requests.Response):
    # Venstar reports a rejected request as HTTP 200 with {"error": true, "reason": "..."}
    try:
        body = resp.json()
    except ValueError:
        return
    if isinstance(body, dict) and body.get('error'):
        raise ThermostatRejected(f'thermostat rejected update: {body.get("reason", "no reason given")}')


def _equiv_temps(left, right):
    return round(left * 10) == round(right * 10)


def _set_temp(log: logging.Logger, uri: str, data: dict, timeout: float):
    tries = 3
    while tries > 0:
        try:
            resp = requests.post(urljoin(uri, '/control'), data=data, timeout=timeout)
            resp.raise_for_status()
            _raise_if_rejected(resp)
        except ThermostatRejected:
            raise
        except Exception as exc:
            if tries == 1:
                raise Exception(f'failed to set new state: {exc}')
            log.error('failure in _set_temp, will retry: %s', exc)
            sleep(3)
            tries -= 1
            continue

        sleep(3)

        try:
            resp = requests.get(urljoin(uri, '/query/info'), timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            if tries == 1:
                raise Exception(f'failed to set new state: {exc}')
            log.error('failure in _set_temp, will retry: %s', exc)
            tries -= 1
            continue

        resp = resp.json()
        mode = data['mode']
        if mode == 1:
            key = 'heattemp'
        elif mode == 2:
            key = 'cooltemp'
        else:
            raise Exception(f'invalid mode: {mode}')
        if _equiv_temps(resp[key], data[key]):
            return

        tries -= 1
    raise Exception('failed to set new state')


def _seconds_setting(settings: dict, key: str, default: float) -> float:
    value = settings.get(key)
    return default if value is None else float(value)


class Data:
    def __init__(self, log, thermo, settings):
        self.log: logging.Logger = log
        self.schedule: list = thermo['schedule']
        self.settings: dict = settings
        self.uri: str = thermo['url']
        self.timeout = float(settings.get('timeout', 3))
        self.us_holidays = holidays.UnitedStates(observed=True)
        self.state: tuple = ()
        self.solar_prod_thresh: dict = settings.get('solar_prod_thresh') or {}
        self.consumption_thresh: dict = settings.get('consumption_thresh') or {}
        # When peak was last forced, which turns the compressor off
        self.last_off: datetime.datetime = datetime.datetime(1900, 1, 1)
        self.min_comp_time_s = _seconds_setting(settings, 'min_comp_time_s', 300.0)
        # When the compressor was first seen running, or None if it is off
        self.on_since: Optional[datetime.datetime] = None
        self.min_on_time_s = _seconds_setting(settings, 'min_on_time_s', 300.0)
        # Whether the last setpoint sent was a peak override
        self.at_peak = False
        self.solar_failing = False
        # run_job uses these so runs never overlap and a tick during a run isn't lost
        self.lock = threading.Lock()
        self.rerun = threading.Event()


def get_solar_prod_cons(data: Data) -> Optional[tuple[float, float]]:
    """Returns (production, consumption) in kW, or None if they are unavailable."""
    try:
        db = data.settings['timescaledb']
        with psycopg.connect(host=db['host'], port=db.get('port', 5432), dbname=db['dbname'], user=db['user'],
                             password=db['password'], connect_timeout=int(data.timeout),
                             options=f'-c statement_timeout={int(data.timeout * 1000)}') as conn:
            row = conn.execute(SOLAR_PROD_QUERY).fetchone()
        if row[0] is None or row[1] is None:
            raise Exception('no recent meter data')
    except Exception:
        if data.solar_failing:
            data.log.debug('still failing to get solar production', exc_info=True)
        else:
            data.log.exception('failed to get solar production; treating solar as insufficient during peak')
            data.solar_failing = True
        return None

    if data.solar_failing:
        data.log.info('solar production is available again')
        data.solar_failing = False
    return row[0] / 1000.0, row[1] / 1000.0


def _peak_reason(data: Data, mode_str: str, is_on: bool, solar: Optional[tuple[float, float]]) -> Optional[str]:
    """Why solar can't cover the HVAC right now, or None if it can."""
    if solar is None:
        return 'solar production is unknown'
    solar_prod, consumption = solar
    if is_on:
        consumption_thresh = data.consumption_thresh.get(mode_str, 0.0)
        diff = solar_prod - consumption
        if diff < consumption_thresh:
            return f'prod - cons ({diff:.3f}) is below threshold ({consumption_thresh:.1f})'
    else:
        solar_prod_thresh = data.solar_prod_thresh.get(mode_str, 100.0)
        if solar_prod < solar_prod_thresh:
            return f'solar production ({solar_prod:.3f}) is below threshold ({solar_prod_thresh:.1f})'
    return None


def thermo_task(data: Data):
    if data.log.isEnabledFor(logging.DEBUG):
        data.log.debug('starting thermo task')

    # noinspection PyBroadException
    try:
        resp = requests.get(urljoin(data.uri, '/query/info'), timeout=data.timeout)
        resp.raise_for_status()

        resp = resp.json()
        data.log.debug('%s', resp)

        now = datetime.datetime.now()

        # state 1 is heating, 2 is cooling
        is_on = resp['state'] in (1, 2)
        if not is_on:
            data.on_since = None
        elif data.on_since is None:
            data.on_since = now

        mode = resp['mode']
        # Don't do anything if the schedule is on or the mode is off or auto
        if resp['schedule'] or mode in (0, 3):
            return

        if mode == 1:
            mode_str = 'heat'
        elif mode == 2:
            mode_str = 'cool'
        else:
            raise Exception(f'invalid mode: {mode}')

        new_sched = _get_active_schedule(data.us_holidays, data.settings, data.schedule, now, mode_str)
        if new_sched is None:
            data.log.debug('no schedule set')
            return

        solar = get_solar_prod_cons(data)
        solar_log = ('solar=unknown' if solar is None
                     else f'solar_prod={solar[0]:.3f} consumption={solar[1]:.3f}')

        is_holiday = new_sched['is_holiday']
        is_peak = new_sched['is_peak']
        peak_temp = new_sched['peak_temp']
        set_to_peak = False
        at_peak = False

        if is_peak and peak_temp is None:
            data.log.debug(f'no {mode_str} peak temp set, ignoring peak period')
        elif is_peak:
            off_s = int((now - data.last_off).total_seconds())
            if off_s < data.min_comp_time_s:
                data.log.info(
                    f'compressor off for {off_s} < {data.min_comp_time_s}, staying at peak temp: {peak_temp}')
                new_sched['temp'] = peak_temp
                at_peak = True
            else:
                reason = _peak_reason(data, mode_str, is_on, solar)
                on_s = int((now - data.on_since).total_seconds()) if is_on else None
                # Don't cut a compressor off right after it starts, unless it started at the peak temp
                if reason and is_on and not data.at_peak and on_s < data.min_on_time_s:
                    data.log.info(f'{reason}, but compressor on for {on_s} < {data.min_on_time_s}, '
                                  f'staying at schedule temp: {new_sched["temp"]}')
                elif reason:
                    data.log.info(f'{reason}, changing to peak temp: {peak_temp}')
                    new_sched['temp'] = peak_temp
                    set_to_peak = True
                    at_peak = True

        new_sched["id"] = new_sched["id"] + (new_sched["temp"],)

        if mode_str == 'heat':
            heattemp = new_sched['temp']
            cooltemp = resp['cooltemp']
        else:
            heattemp = resp['heattemp']
            cooltemp = new_sched['temp']

        data.log.debug(f'mode={mode_str} heattemp={heattemp} '
                       f'cooltemp={cooltemp} state={data.state} new_state={new_sched["id"]} '
                       f'is_holiday={is_holiday} '
                       f'is_peak={is_peak} '
                       f'{solar_log}')

        if data.state != new_sched['id']:
            api_data = dict(mode=mode, heattemp=heattemp, cooltemp=cooltemp)
            data.log.info(
                f'updating thermostat: mode={mode_str} '
                f'heattemp={heattemp} '
                f'cooltemp={cooltemp} '
                f'is_holiday={is_holiday} '
                f'is_peak={is_peak} '
                f'{solar_log}')
            _set_temp(data.log, data.uri, api_data, data.timeout)
            data.state = new_sched['id']
            data.at_peak = at_peak
            if set_to_peak:
                data.last_off = now
        else:
            data.log.debug('already in the desired state')
    except ThermostatRejected as exc:
        data.log.error('%s', exc)
    except Exception:
        data.log.exception('unknown failure')


def run_job(data: Data):
    """Run thermo_task without overlapping; a tick that arrives mid-run causes one more run right after."""
    data.rerun.set()
    while data.rerun.is_set():
        if not data.lock.acquire(blocking=False):
            return  # the running job will see rerun and go again
        try:
            data.rerun.clear()
            thermo_task(data)
        finally:
            data.lock.release()


def main():
    log = logging.getLogger('thermo')
    level_name = os.environ.get('LOGLEVEL', 'INFO').upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        log.warning(f'invalid LOGLEVEL {level_name!r}, using INFO')
        level = logging.INFO
    log.setLevel(level)
    thermostats, settings = load_schedule(os.environ.get('SCHEDULE', '/config/schedule.yaml'))
    interval_secs_requested = int(settings.get('interval', 60))
    interval_secs = 60 // int(round(60 / min(interval_secs_requested, 60)))

    if interval_secs_requested != interval_secs:
        log.warning(f'interval was adjusted to {interval_secs}')

    if interval_secs < 60:
        cron = CronTrigger(second=f'*/{interval_secs}')
    else:
        cron = CronTrigger(minute='*')

    scheduler = BlockingScheduler({
        'apscheduler.job_defaults.coalesce': 'true',
        # run_job prevents overlapping runs, so let overlapping ticks reach it instead of being skipped
        'apscheduler.job_defaults.max_instances': '3',
    })

    for thermo in thermostats:
        data = Data(log, thermo, settings)
        scheduler.add_job(run_job, cron, args=[data], next_run_time=datetime.datetime.now())

    # Python running as PID 1 in the container ignores SIGTERM by default, so podman would wait and then SIGKILL
    def handle_sigterm(signum, frame):
        log.info('received SIGTERM, shutting down')
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, handle_sigterm)

    scheduler.start()


if __name__ == "__main__":
    logging.basicConfig(format='%(levelname)s:%(asctime)s:%(message)s', level=logging.WARNING)
    main()
