import os
import logging
import asyncio
import bisect
from typing import Optional, Tuple
import datetime
from urllib.parse import urljoin

import yaml
import aiohttp
import holidays


def _to_celsius(fahrenheit: float) -> float:
    cc = (fahrenheit - 32) * 5 / 9
    # round to nearest 0.5
    return round(cc * 2) / 2


def _to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


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
            for typ in ('heat', 'cool'):
                typ_sched = sched.get(typ)
                if typ_sched:
                    new = {}
                    holiday = None
                    for day, times in typ_sched.items():
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
            arr.append(sched)
        ret.append({'url': thermostat.get('url'), 'schedule': sorted(arr, key=lambda xx: xx['start'])})
    return ret, schedule.get('settings', {})


async def check_error(response: aiohttp.ClientResponse) -> dict:
    if response.status != 200:
        raise Exception(f'POST to {response.url} failed: {response.status}')
    body = await response.json()
    if body.get('error'):
        raise Exception(f"POST to {response.url} failed: {body['reason']}")
    return body


async def http_get(session: aiohttp.ClientSession, uri: str, endpoint: str, timeout: float = 5) -> dict:
    full_uri = urljoin(uri, endpoint)
    async with session.get(full_uri, timeout=timeout) as response:
        return await check_error(response)


async def http_post(session: aiohttp.ClientSession, uri: str, endpoint: str, data: dict, timeout: float = 5) -> dict:
    full_uri = urljoin(uri, endpoint)
    async with session.post(full_uri, data=data,
                            headers={'Content-Type': 'application/x-www-form-urlencoded'},
                            timeout=timeout) as response:
        return await check_error(response)


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


def _get_active_schedule(us_holidays: holidays.HolidayBase, settings: dict, schedule: [dict],
                         dt: datetime.datetime, mode: str) -> Optional[dict]:
    if not schedule:
        return

    mmdd = (dt.month, dt.day)
    sch_idx = bisect.bisect_right(schedule, mmdd, key=lambda xx: xx['start'])
    sch_idx -= 1
    if sch_idx == 0 and mmdd < schedule[0]['start']:
        sch_idx -= 1

    sched = schedule[sch_idx].get(mode)
    if not sched:
        return

    weekday = dt.weekday()
    time = dt.time()

    if _is_holiday(us_holidays, settings, dt):
        day = sched['holiday']
    else:
        day = sched['days'][weekday]

    # Handle the case were we are before the first time of a new weekday and/or schedule
    if time < day[0].time:
        weekday -= 1
        if mmdd == schedule[sch_idx]['start']:
            sch_idx -= 1
            sched = schedule[sch_idx].get(mode)
            if not sched:
                return
        if _is_holiday(us_holidays, settings, dt - datetime.timedelta(days=1)):
            day = sched['holiday']
        else:
            day = sched['days'][weekday]
        return {
            'id': (sch_idx, mode, weekday, day[-1].time, day[-1].temperature),
            'temp': _to_celsius(day[-1].temperature),
        }

    ii = bisect.bisect_right(day, time, key=lambda xx: xx.time) - 1
    return {
        'id': (sch_idx, mode, weekday, day[ii].time, day[ii].temperature),
        'temp': _to_celsius(day[ii].temperature),
    }


def _equiv_temps(left, right):
    return round(left * 10) == round(right * 10)


async def _set_temp(session: aiohttp.ClientSession, uri: str, data: dict):
    retries = 6
    while retries > 0:
        await http_post(session, uri, '/control', data)
        await asyncio.sleep(10)
        resp = await http_get(session, uri, '/query/info')
        mode = data['mode']
        if mode == 1:
            key = 'heattemp'
        elif mode == 2:
            key = 'cooltemp'
        else:
            raise Exception(f'invalid mode: {mode}')
        if _equiv_temps(resp[key], data[key]):
            return
        retries -= 1
    raise Exception('failed to set new state')

async def thermo_task(log: logging.Logger, schedule: dict, settings: dict, uri: str):
    us_holidays = holidays.UnitedStates(observed=True)
    interval = settings.get('interval', 10)
    state = None

    async with aiohttp.ClientSession() as session:
        while True:
            log.debug('starting iteration')

            while True:
                # noinspection PyBroadException
                try:
                    resp = await http_get(session, uri, '/query/info')
                    log.debug('%s', resp)

                    mode = resp['mode']
                    # Don't do anything if the schedule is on or the mode is off or auto
                    if resp['schedule'] or mode in (0, 3):
                        break

                    if mode == 1:
                        mode_str = 'heat'
                    elif mode == 2:
                        mode_str = 'cool'
                    else:
                        raise Exception(f'invalid mode: {mode}')

                    now = datetime.datetime.now()
                    new_sched = _get_active_schedule(us_holidays, settings, schedule, now, mode_str)
                    if new_sched is None:
                        log.debug('no schedule set')
                        break

                    if mode_str == 'heat':
                        check = 'heattemp'
                        heattemp = new_sched['temp']
                        cooltemp = resp['cooltemp']
                    elif mode_str == 'cool':
                        check = 'cooltemp'
                        heattemp = resp['heattemp']
                        cooltemp = new_sched['temp']

                    log.debug(f'mode={mode_str} check={check} heattemp={heattemp}({_to_fahrenheit(heattemp)}) '
                              f'cooltemp={cooltemp}({_to_fahrenheit(cooltemp)})')

                    if state != new_sched['id']:
                        # 0 == idle
                        fan = 0 if resp['state'] == 0 else resp['fan']
                        data = dict(mode=mode, heattemp=heattemp, cooltemp=cooltemp)
                        await _set_temp(session, uri, data)
                        log.info(
                            f'updated thermostat: mode={mode_str} '
                            f'heattemp={heattemp}({_to_fahrenheit(heattemp)}) '
                            f'cooltemp={cooltemp}({_to_fahrenheit(cooltemp)})')
                        state = new_sched['id']
                    else:
                        log.debug('already in the desired state')
                except Exception:
                    log.exception('unknown failure')
                break

            await asyncio.sleep(interval)


async def main():
    log = logging.getLogger('thermo')
    log.setLevel(getattr(logging, os.environ.get('LOGLEVEL', 'INFO')))
    thermostats, settings = load_schedule(os.environ.get('SCHEDULE', '/config/schedule.yaml'))
    loop = asyncio.get_event_loop()
    tasks = [loop.create_task(thermo_task(log, thermo['schedule'], settings, thermo['url']))
             for thermo in thermostats]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    logging.basicConfig(format='%(levelname)s:%(asctime)s:%(message)s', level=logging.WARNING)
    asyncio.run(main())
