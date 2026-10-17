import datetime as dt_module
import logging
import os
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import ANY, MagicMock, patch
from datetime import datetime, time, timedelta
import holidays
import psycopg
import requests
import yaml
import main
from main import load_schedule, _is_holiday, _is_peak, _get_active_schedule, _is_peak

# Keep expected error logs from tests out of the output; assertLogs still captures them
logging.getLogger('test').addHandler(logging.NullHandler())
logging.getLogger('test').propagate = False

DAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']

# Monday 2021-10-04 in test.yaml: 18:00-21:00 is a winter peak window. At 18:30 the cool schedule temp is 302 (cool
# peak temp 68, heat peak temp 67). At 12:00 it is off-peak: cool 284, heat 212.
PEAK_NOW = datetime(2021, 10, 4, 18, 30)
OFF_PEAK_NOW = datetime(2021, 10, 4, 12, 0)
COOL_SCHEDULE_TEMP = 302
COOL_PEAK_TEMP = 68
HEAT_PEAK_TEMP = 67
COOL_OFF_PEAK_TEMP = 284
HEAT_OFF_PEAK_TEMP = 212

THRESHOLDS = {'solar_prod_thresh': {'heat': 1.0, 'cool': 4.0}, 'consumption_thresh': {'heat': -1.0, 'cool': -1.0},
              'min_comp_time_s': 300}


def make_data(settings_overrides=None):
    schedule, settings = load_schedule('test.yaml')
    settings = dict(settings, **(THRESHOLDS if settings_overrides is None else settings_overrides))
    return main.Data(logging.getLogger('test.thermo'), {'url': 'http://thermo', 'schedule': schedule[0]['schedule']},
                     settings)


def fake_datetime(now):
    """A stand-in for main's datetime module whose datetime.now() returns `now`."""
    class FakeDatetime(dt_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    return SimpleNamespace(datetime=FakeDatetime, time=dt_module.time, timedelta=dt_module.timedelta,
                           date=dt_module.date)


def run_thermo_task(data, now, mode=2, state=0, schedule_on=0, heattemp=60.0, cooltemp=80.0, solar=(10.0, 1.0),
                    set_temp_effect=None, get_effect=None):
    """Run thermo_task once at `now` against a fake thermostat.

    solar=None leaves get_solar_prod_cons unpatched. Returns the _set_temp and get_solar_prod_cons mocks.
    """
    resp = MagicMock()
    resp.json.return_value = {'state': state, 'mode': mode, 'schedule': schedule_on, 'heattemp': heattemp,
                              'cooltemp': cooltemp}
    with ExitStack() as stack:
        stack.enter_context(patch('main.requests.get', return_value=resp, side_effect=get_effect))
        get_solar = None
        if solar is not None:
            get_solar = stack.enter_context(patch('main.get_solar_prod_cons', return_value=solar))
        set_temp = stack.enter_context(patch('main._set_temp', side_effect=set_temp_effect))
        stack.enter_context(patch('main.datetime', fake_datetime(now)))
        main.thermo_task(data)
    return set_temp, get_solar


def week(times, days=DAYS):
    return {day: dict(times) for day in days}


def season(start, heat=None, cool=None, peak=()):
    sched = {'start': start, 'peak': [{'start': begin, 'end': end} for begin, end in peak]}
    if heat is not None:
        sched['heat'] = heat
    if cool is not None:
        sched['cool'] = cool
    return sched


class ScheduleFileMixin:
    def write_schedule(self, schedules, url='http://thermo', settings=None):
        fd, path = tempfile.mkstemp(suffix='.yaml')
        os.close(fd)
        self.addCleanup(os.remove, path)
        with open(path, 'w') as outf:
            yaml.safe_dump({'thermostats': [{'url': url, 'schedules': schedules}], 'settings': settings or {}}, outf,
                           sort_keys=False)
        return path


class Test(TestCase):
    def test__is_holiday(self):
        us_holidays = holidays.UnitedStates(observed=True)
        schedule, settings = load_schedule('test.yaml')
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2022, 5, 30)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2022, 9, 5)))
        self.assertFalse(_is_holiday(us_holidays, settings, datetime(2022, 7, 3)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2022, 7, 4)))
        self.assertFalse(_is_holiday(us_holidays, settings, datetime(2022, 11, 11)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2021, 11, 25)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2021, 12, 24)))
        self.assertFalse(_is_holiday(us_holidays, settings, datetime(2021, 12, 25)))
        self.assertFalse(_is_holiday(us_holidays, settings, datetime(2022, 12, 25)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2022, 12, 26)))
        self.assertTrue(_is_holiday(us_holidays, settings, datetime(2023, 12, 25)))

    def test__is_peak(self):
        us_holidays = holidays.UnitedStates(observed=True)
        schedule, settings = load_schedule('test.yaml')
        peak = [{'start': 630, 'end': 900}, {'start': 1500, 'end': 1900}]
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 5, 30)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 9, 5)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 7, 3, 6, 30)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 7, 4)))
        self.assertTrue(_is_peak(us_holidays, settings, peak, datetime(2022, 11, 11, 15)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2021, 11, 25)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2021, 12, 24)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2021, 12, 25, 7)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 12, 25, 16)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 12, 26)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2023, 12, 25)))
        self.assertFalse(_is_peak(us_holidays, settings, peak, datetime(2022, 12, 25, 9)))

    def test__get_active_schedule(self):
        us_holidays = holidays.UnitedStates(observed=True)
        schedule, settings = load_schedule('test.yaml')
        tests = [
            (datetime(2021, 4, 30, 23, 59, 59), 'heat', 212, False),
            (datetime(2021, 5, 1), 'heat', 212, False),
            (datetime(2021, 5, 1, 5, 30), 'heat', 86, False),
            (datetime(2021, 5, 1, 6, 0), 'heat', 86, False),
            (datetime(2021, 5, 1, 20, 0), 'heat', 104, False),
            (datetime(2021, 5, 1, 20, 1), 'heat', 104, False),
            (datetime(2021, 5, 3, 15, 0), 'heat', 50, True),
            (datetime(2021, 5, 3, 16, 0), 'heat', 50, True),
            (datetime(2021, 5, 3, 19, 0), 'heat', 68, False),
            (datetime(2021, 5, 3, 20, 0), 'heat', 68, False),

            (datetime(2021, 9, 30, 23, 59, 59), 'cool', 140, False),
            (datetime(2021, 10, 1), 'cool', 140, False),
            (datetime(2021, 10, 1, 0, 50), 'cool', 266, False),
            (datetime(2021, 10, 1, 0, 51), 'cool', 266, False),
            (datetime(2021, 10, 1, 2, 40), 'cool', 284, False),
            (datetime(2021, 10, 1, 2, 41), 'cool', 284, False),
            (datetime(2021, 10, 2, 15, 0), 'cool', 284, False),
            (datetime(2021, 10, 2, 18, 0), 'cool', 302, False),
            (datetime(2021, 10, 2, 18, 1), 'cool', 302, False),
            (datetime(2021, 10, 2, 20, 0), 'cool', 320, False),
            (datetime(2021, 10, 3, 22, 0), 'cool', 320, False),
            (datetime(2021, 10, 4, 0, 49), 'cool', 320, False),
            (datetime(2021, 10, 4, 18, 0), 'cool', 302, True),
            (datetime(2021, 10, 4, 18, 1), 'cool', 302, True),
            (datetime(2021, 10, 4, 21, 0), 'cool', 284, False),
        ]
        for ii, tt in enumerate(tests):
            dt, mode, result, is_peak = tt
            new_sched = _get_active_schedule(us_holidays, settings, schedule[0]['schedule'], dt, mode)
            self.assertAlmostEqual(new_sched['temp'], result, 2, f'test {ii} failed')
            self.assertEqual(new_sched['is_peak'], is_peak, f'test {ii} failed')


class TestMain(TestCase):
    def test_each_job_gets_its_own_thermostat(self):
        schedule, settings = load_schedule('test.yaml')
        thermostats = [
            {'url': 'http://thermo-a', 'schedule': schedule[0]['schedule']},
            {'url': 'http://thermo-b', 'schedule': schedule[0]['schedule']},
        ]
        with patch('main.load_schedule', return_value=(thermostats, settings)), \
                patch('main.BlockingScheduler') as scheduler_cls, \
                patch('main.thermo_task') as thermo_task:
            main.main()
            # Run each registered job the way APScheduler would
            for call in scheduler_cls.return_value.add_job.call_args_list:
                func = call.args[0]
                func(*call.kwargs.get('args', ()), **call.kwargs.get('kwargs', {}))

        uris = [call.args[0].uri for call in thermo_task.call_args_list]
        self.assertEqual(uris, ['http://thermo-a', 'http://thermo-b'])

    def test_sigterm_shuts_down_scheduler(self):
        schedule, settings = load_schedule('test.yaml')
        with patch('main.load_schedule', return_value=(schedule, settings)), \
                patch('main.BlockingScheduler') as scheduler_cls, \
                patch('main.signal.signal') as signal_fn:
            main.main()

        signal_fn.assert_called_once_with(main.signal.SIGTERM, ANY)
        scheduler = scheduler_cls.return_value
        scheduler.shutdown.assert_not_called()
        handler = signal_fn.call_args.args[1]
        handler(main.signal.SIGTERM, None)
        scheduler.shutdown.assert_called_once_with(wait=False)


class TestThermoTaskPeak(TestCase):
    def _run(self, now, last_off, solar_prod=10.0, consumption=1.0, thermo_state=0, settings_overrides=None):
        data = make_data(settings_overrides)
        data.last_off = last_off
        set_temp, _ = run_thermo_task(data, now, state=thermo_state, solar=(solar_prod, consumption))
        self.assertEqual(set_temp.call_count, 1, 'thermostat was not updated')
        return set_temp.call_args.args[2]['cooltemp'], data

    def test_hold_does_not_carry_over_to_next_day(self):
        # Peak was forced yesterday at 18:29, one minute short of the same time of day
        cooltemp, _ = self._run(PEAK_NOW, last_off=PEAK_NOW - timedelta(days=1, minutes=1))
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_hold_keeps_peak_temp_within_min_comp_time(self):
        cooltemp, _ = self._run(PEAK_NOW, last_off=PEAK_NOW - timedelta(seconds=60))
        self.assertEqual(cooltemp, COOL_PEAK_TEMP)

    def test_hold_ends_after_min_comp_time(self):
        cooltemp, _ = self._run(PEAK_NOW, last_off=PEAK_NOW - timedelta(seconds=301))
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_low_solar_forces_peak_and_records_time(self):
        cooltemp, data = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=1.0)
        self.assertEqual(cooltemp, COOL_PEAK_TEMP)
        self.assertEqual(data.last_off, PEAK_NOW)

    def test_idle_at_solar_threshold_keeps_schedule(self):
        cooltemp, _ = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=4.0, consumption=0.0)
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_running_below_consumption_threshold_forces_peak(self):
        cooltemp, data = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=3.0, consumption=5.0,
                                   thermo_state=2)
        self.assertEqual(cooltemp, COOL_PEAK_TEMP)
        self.assertEqual(data.last_off, PEAK_NOW)

    def test_running_at_consumption_threshold_keeps_schedule(self):
        cooltemp, _ = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=4.0, consumption=5.0,
                                thermo_state=2)
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_running_above_consumption_threshold_keeps_schedule(self):
        cooltemp, _ = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=5.0, consumption=5.0,
                                thermo_state=2)
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_running_ignores_solar_threshold(self):
        # Production is below the 4 kW idle threshold, but it still covers consumption
        cooltemp, _ = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=1.0, consumption=0.5,
                                thermo_state=2)
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)

    def test_heating_below_consumption_threshold_forces_heat_peak_temp(self):
        set_temp, _ = run_thermo_task(make_data(), PEAK_NOW, mode=1, state=1, solar=(0.5, 3.0))
        self.assertEqual(set_temp.call_args.args[2]['heattemp'], HEAT_PEAK_TEMP)

    def test_missing_thresholds_use_defaults(self):
        # Default solar threshold is 100 kW, so 10 kW of production still forces peak
        cooltemp, data = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), settings_overrides={})
        self.assertEqual(cooltemp, COOL_PEAK_TEMP)
        self.assertEqual(data.min_comp_time_s, 300.0)

    def test_null_thresholds_use_defaults(self):
        overrides = {'solar_prod_thresh': None, 'consumption_thresh': None, 'min_comp_time_s': None}
        cooltemp, data = self._run(PEAK_NOW, last_off=datetime(1900, 1, 1), settings_overrides=overrides)
        self.assertEqual(cooltemp, COOL_PEAK_TEMP)
        self.assertEqual(data.min_comp_time_s, 300.0)

    def test_zero_min_comp_time_disables_hold(self):
        overrides = dict(THRESHOLDS, min_comp_time_s=0)
        cooltemp, _ = self._run(PEAK_NOW, last_off=PEAK_NOW - timedelta(seconds=1), settings_overrides=overrides)
        self.assertEqual(cooltemp, COOL_SCHEDULE_TEMP)


class TestThermoTask(TestCase):
    def test_heat_mode_sets_heattemp_and_keeps_cooltemp(self):
        set_temp, _ = run_thermo_task(make_data(), OFF_PEAK_NOW, mode=1, heattemp=60.0, cooltemp=80.0)
        set_temp.assert_called_once_with(ANY, 'http://thermo', {'mode': 1, 'heattemp': HEAT_OFF_PEAK_TEMP,
                                                                'cooltemp': 80.0}, 3.0)

    def test_cool_mode_sets_cooltemp_and_keeps_heattemp(self):
        set_temp, _ = run_thermo_task(make_data(), OFF_PEAK_NOW, mode=2, heattemp=60.0, cooltemp=80.0)
        set_temp.assert_called_once_with(ANY, 'http://thermo', {'mode': 2, 'heattemp': 60.0,
                                                                'cooltemp': COOL_OFF_PEAK_TEMP}, 3.0)

    def test_does_nothing_when_schedule_on_or_mode_off_or_auto(self):
        for thermostat in ({'schedule_on': 1}, {'mode': 0}, {'mode': 3}):
            with self.subTest(**thermostat):
                set_temp, get_solar = run_thermo_task(make_data(), PEAK_NOW, **thermostat)
                set_temp.assert_not_called()
                get_solar.assert_not_called()

    def test_off_peak_ignores_solar(self):
        set_temp, _ = run_thermo_task(make_data(), OFF_PEAK_NOW, solar=(0.0, 5.0))
        self.assertEqual(set_temp.call_args.args[2]['cooltemp'], COOL_OFF_PEAK_TEMP)

    def test_does_not_resend_unchanged_state(self):
        data = make_data()
        first, _ = run_thermo_task(data, OFF_PEAK_NOW)
        second, _ = run_thermo_task(data, OFF_PEAK_NOW + timedelta(seconds=30))
        first.assert_called_once()
        second.assert_not_called()

    def test_switches_to_peak_and_back(self):
        data = make_data()
        high, low = (10.0, 1.0), (1.0, 0.0)
        runs = [
            (PEAK_NOW, high, COOL_SCHEDULE_TEMP),
            (PEAK_NOW + timedelta(seconds=30), low, COOL_PEAK_TEMP),
            (PEAK_NOW + timedelta(seconds=60), high, None),  # held at peak; nothing new to send
            (PEAK_NOW + timedelta(seconds=331), high, COOL_SCHEDULE_TEMP),
        ]
        for now, solar, expected in runs:
            with self.subTest(now=now):
                set_temp, _ = run_thermo_task(data, now, solar=solar)
                if expected is None:
                    set_temp.assert_not_called()
                else:
                    self.assertEqual(set_temp.call_args.args[2]['cooltemp'], expected)

    def test_last_off_kept_when_peak_already_set(self):
        data = make_data()
        run_thermo_task(data, PEAK_NOW, solar=(1.0, 0.0))
        set_temp, _ = run_thermo_task(data, PEAK_NOW + timedelta(seconds=400), solar=(1.0, 0.0))
        set_temp.assert_not_called()
        self.assertEqual(data.last_off, PEAK_NOW)

    def test_failed_update_is_retried_next_run(self):
        data = make_data()
        with self.assertLogs('test.thermo', logging.ERROR) as logs:
            first, _ = run_thermo_task(data, PEAK_NOW, solar=(1.0, 0.0), set_temp_effect=Exception('boom'))
        first.assert_called_once()
        self.assertIn('unknown failure', logs.output[0])
        self.assertEqual(data.state, ())
        self.assertEqual(data.last_off, datetime(1900, 1, 1))

        second, _ = run_thermo_task(data, PEAK_NOW + timedelta(seconds=30), solar=(1.0, 0.0))
        second.assert_called_once()
        self.assertEqual(data.last_off, PEAK_NOW + timedelta(seconds=30))

    def test_thermostat_unreachable(self):
        with self.assertLogs('test.thermo', logging.ERROR) as logs:
            set_temp, get_solar = run_thermo_task(make_data(), PEAK_NOW,
                                                  get_effect=requests.ConnectionError('down'))
        self.assertIn('unknown failure', logs.output[0])
        set_temp.assert_not_called()
        get_solar.assert_not_called()

    def test_solar_unavailable(self):
        # Pins current behavior (review issue 7): with no solar data an idle compressor is held at the peak temp,
        # but a running one keeps the schedule temp because 0 - 0 is above the -1 kW consumption threshold
        settings = dict(THRESHOLDS, timescaledb={'host': 'db', 'dbname': 'solar', 'user': 'u', 'password': 'p'})
        for state, expected in ((0, COOL_PEAK_TEMP), (2, COOL_SCHEDULE_TEMP)):
            with self.subTest(state=state), \
                    patch('main.psycopg.connect', side_effect=psycopg.OperationalError('down')), \
                    self.assertLogs('test.thermo', logging.ERROR):
                set_temp, _ = run_thermo_task(make_data(settings), PEAK_NOW, state=state, solar=None)
                self.assertEqual(set_temp.call_args.args[2]['cooltemp'], expected)


def ok_resp():
    return MagicMock()


def info_resp(**info):
    resp = MagicMock()
    resp.json.return_value = info
    return resp


class TestSetTemp(TestCase):
    COOL = {'mode': 2, 'heattemp': 60.0, 'cooltemp': 74}
    HEAT = {'mode': 1, 'heattemp': 68, 'cooltemp': 80.0}

    def _call(self, data, post, get=()):
        """Run _set_temp with side_effect lists for POST and GET; returns the mocks and any raised exception."""
        exc = None
        with patch('main.requests.post', side_effect=list(post)) as post_fn, \
                patch('main.requests.get', side_effect=list(get)) as get_fn, \
                patch('main.sleep') as sleep_fn:
            try:
                main._set_temp(logging.getLogger('test.set_temp'), 'http://thermo', data, 3)
            except Exception as err:
                exc = err
        return post_fn, get_fn, sleep_fn, exc

    def test_success_posts_then_verifies(self):
        post_fn, get_fn, sleep_fn, exc = self._call(self.COOL, [ok_resp()], [info_resp(heattemp=0.0, cooltemp=74.0)])
        self.assertIsNone(exc)
        post_fn.assert_called_once_with('http://thermo/control', data=self.COOL, timeout=3)
        get_fn.assert_called_once_with('http://thermo/query/info', timeout=3)
        sleep_fn.assert_called_once_with(3)

    def test_heat_mode_verifies_heattemp(self):
        _, _, _, exc = self._call(self.HEAT, [ok_resp()], [info_resp(heattemp=68.0, cooltemp=99.0)])
        self.assertIsNone(exc)

    def test_heat_mode_mismatch_retries_then_raises(self):
        # cooltemp matches the heat target, so this only passes if heattemp is the value checked
        post_fn, _, _, exc = self._call(self.HEAT, [ok_resp()] * 3, [info_resp(heattemp=60.0, cooltemp=68.0)] * 3)
        self.assertEqual(str(exc), 'failed to set new state')
        self.assertEqual(post_fn.call_count, 3)

    def test_invalid_mode_raises(self):
        post_fn, _, _, exc = self._call(dict(self.COOL, mode=3), [ok_resp()], [info_resp(heattemp=60.0, cooltemp=74.0)])
        self.assertEqual(str(exc), 'invalid mode: 3')
        post_fn.assert_called_once()

    def test_mismatch_then_match(self):
        post_fn, _, _, exc = self._call(self.COOL, [ok_resp()] * 2,
                                        [info_resp(heattemp=60.0, cooltemp=76.0), info_resp(heattemp=60.0, cooltemp=74.0)])
        self.assertIsNone(exc)
        self.assertEqual(post_fn.call_count, 2)

    def test_post_error_is_retried(self):
        with self.assertLogs('test.set_temp', logging.ERROR):
            post_fn, _, sleep_fn, exc = self._call(self.COOL, [requests.ConnectionError('down'), ok_resp()],
                                                   [info_resp(heattemp=60.0, cooltemp=74.0)])
        self.assertIsNone(exc)
        self.assertEqual(post_fn.call_count, 2)
        self.assertEqual(sleep_fn.call_count, 2)

    def test_http_error_status_is_retried(self):
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.HTTPError('500 Server Error')
        with self.assertLogs('test.set_temp', logging.ERROR):
            post_fn, _, _, exc = self._call(self.COOL, [bad, ok_resp()], [info_resp(heattemp=60.0, cooltemp=74.0)])
        self.assertIsNone(exc)
        self.assertEqual(post_fn.call_count, 2)

    def test_post_fails_every_time(self):
        with self.assertLogs('test.set_temp', logging.ERROR):
            post_fn, get_fn, _, exc = self._call(self.COOL, [requests.ConnectionError('down')] * 3)
        self.assertEqual(str(exc), 'failed to set new state: down')
        self.assertEqual(post_fn.call_count, 3)
        get_fn.assert_not_called()

    def test_get_error_is_retried(self):
        with self.assertLogs('test.set_temp', logging.ERROR):
            post_fn, _, _, exc = self._call(self.COOL, [ok_resp()] * 2,
                                            [requests.ConnectionError('down'), info_resp(heattemp=60.0, cooltemp=74.0)])
        self.assertIsNone(exc)
        self.assertEqual(post_fn.call_count, 2)

    def test_get_fails_every_time(self):
        with self.assertLogs('test.set_temp', logging.ERROR):
            post_fn, _, _, exc = self._call(self.COOL, [ok_resp()] * 3, [requests.ConnectionError('down')] * 3)
        self.assertEqual(str(exc), 'failed to set new state: down')
        self.assertEqual(post_fn.call_count, 3)

    def test_equiv_temps_rounds_to_tenths(self):
        self.assertTrue(main._equiv_temps(74, 74.0))
        self.assertTrue(main._equiv_temps(74, 74.04))
        self.assertFalse(main._equiv_temps(74, 74.1))


class TestGetSolarProdCons(TestCase):
    DB = {'host': 'timescaledb', 'port': 5433, 'dbname': 'solar', 'user': 'telegraf', 'password': 'pw'}

    def _call(self, db=DB, row=(6500.0, 2500.0), connect_effect=None):
        settings = {} if db is None else {'timescaledb': db}
        data = main.Data(logging.getLogger('test.solar'), {'url': None, 'schedule': []}, settings)
        with patch('main.psycopg.connect', side_effect=connect_effect) as connect:
            conn = connect.return_value.__enter__.return_value
            conn.execute.return_value.fetchone.return_value = row
            return main.get_solar_prod_cons(data), connect

    def test_converts_watts_to_kw(self):
        result, connect = self._call()
        self.assertEqual(result, (6.5, 2.5))
        connect.assert_called_once_with(host='timescaledb', port=5433, dbname='solar', user='telegraf', password='pw',
                                        connect_timeout=3)
        connect.return_value.__enter__.return_value.execute.assert_called_once_with(main.SOLAR_PROD_QUERY)

    def test_port_defaults_to_5432(self):
        db = {k: v for k, v in self.DB.items() if k != 'port'}
        _, connect = self._call(db=db)
        self.assertEqual(connect.call_args.kwargs['port'], 5432)

    def test_no_recent_data(self):
        with self.assertLogs('test.solar', logging.ERROR):
            result, _ = self._call(row=(None, None))
        self.assertEqual(result, (0, 0))

    def test_connection_failure(self):
        with self.assertLogs('test.solar', logging.ERROR):
            result, _ = self._call(connect_effect=psycopg.OperationalError('down'))
        self.assertEqual(result, (0, 0))

    def test_missing_settings(self):
        with self.assertLogs('test.solar', logging.ERROR):
            result, connect = self._call(db=None)
        self.assertEqual(result, (0, 0))
        connect.assert_not_called()


class TestActiveScheduleEdges(ScheduleFileMixin, TestCase):
    def setUp(self):
        self.us_holidays = holidays.UnitedStates(observed=True)
        schedule, self.settings = load_schedule('test.yaml')
        self.schedule = schedule[0]['schedule']

    def _active(self, dt, mode='heat'):
        return _get_active_schedule(self.us_holidays, self.settings, self.schedule, dt, mode)

    def test_holiday_uses_holiday_schedule(self):
        # Memorial Day 2021 is a Monday; at 16:00 the weekday heat temp would be 50 and it would be peak
        sched = self._active(datetime(2021, 5, 31, 16, 0))
        self.assertEqual(sched['temp'], 86)
        self.assertTrue(sched['is_holiday'])
        self.assertFalse(sched['is_peak'])

    def test_morning_after_holiday_uses_holiday_last_temp(self):
        # Weekday's last temp would be 68; the holiday's last temp is 104
        sched = self._active(datetime(2021, 6, 1, 1, 0))
        self.assertEqual(sched['temp'], 104)
        self.assertFalse(sched['is_holiday'])

    def test_new_years_day(self):
        # Jan 1 2021 is a Friday holiday in the winter season (which started the previous Oct 1)
        self.assertEqual(self._active(datetime(2021, 1, 1, 0, 20))['temp'], 230)
        # Before the holiday's first entry (00:10), Thursday Dec 31's last weekday temp applies
        self.assertEqual(self._active(datetime(2021, 1, 1, 0, 5))['temp'], 212)

    def test_mode_without_schedule_returns_none(self):
        path = self.write_schedule({'all': season('01/01', heat=dict(week({600: 64}), peak=60))})
        schedule, settings = load_schedule(path)
        self.assertIsNone(_get_active_schedule(self.us_holidays, settings, schedule[0]['schedule'], OFF_PEAK_NOW,
                                               'cool'))
        self.assertEqual(_get_active_schedule(self.us_holidays, settings, schedule[0]['schedule'], OFF_PEAK_NOW,
                                              'heat')['temp'], 64)


class TestLoadSchedule(ScheduleFileMixin, TestCase):
    def test_missing_weekday_raises(self):
        path = self.write_schedule({'all': season('01/01', heat=week({600: 64}, days=DAYS[:6]))})
        with self.assertRaisesRegex(Exception, 'all days of the week'):
            load_schedule(path)

    def test_day_names_are_case_insensitive(self):
        path = self.write_schedule({'all': season('01/01', heat={day.capitalize(): {600: 64} for day in DAYS})})
        schedule, _ = load_schedule(path)
        self.assertEqual(len(schedule[0]['schedule'][0]['heat']['days']), 7)

    def test_times_are_parsed_and_sorted(self):
        path = self.write_schedule({'all': season('01/01', heat=week({1900: 68, 530: 64, 1200: 66}))})
        schedule, _ = load_schedule(path)
        monday = schedule[0]['schedule'][0]['heat']['days'][0]
        self.assertEqual([t.time for t in monday], [time(5, 30), time(12, 0), time(19, 0)])
        self.assertEqual([t.temperature for t in monday], [64, 66, 68])

    def test_seasons_are_sorted_by_start(self):
        path = self.write_schedule({'winter': season('10/01', heat=week({600: 64})),
                                    'summer': season('05/01', heat=week({600: 70}))})
        schedule, _ = load_schedule(path)
        self.assertEqual([s['start'] for s in schedule[0]['schedule']], [(5, 1), (10, 1)])

    def test_holiday_and_peak_are_parsed(self):
        heat = dict(week({600: 64}), holiday={2200: 62, 700: 70}, peak=60)
        path = self.write_schedule({'all': season('01/01', heat=heat, peak=[(1500, 1900)])},
                                   url='http://thermo-x', settings={'interval': 30})
        schedule, settings = load_schedule(path)
        self.assertEqual(schedule[0]['url'], 'http://thermo-x')
        self.assertEqual(settings, {'interval': 30})
        sched = schedule[0]['schedule'][0]
        self.assertEqual(sched['start'], (1, 1))
        self.assertEqual(sched['peak'], [{'start': 1500, 'end': 1900}])
        self.assertEqual(sched['heat']['peak'], 60)
        self.assertEqual([(t.time, t.temperature) for t in sched['heat']['holiday']],
                         [(time(7, 0), 70), (time(22, 0), 62)])

    def test_optional_fields_default_to_none(self):
        path = self.write_schedule({'all': season('01/01', heat=week({600: 64}))})
        schedule, _ = load_schedule(path)
        sched = schedule[0]['schedule'][0]
        self.assertIsNone(sched['heat']['holiday'])
        self.assertIsNone(sched['heat']['peak'])
        self.assertNotIn('cool', sched)


class TestInterval(TestCase):
    def _main(self, settings):
        with patch('main.load_schedule', return_value=([], settings)), \
                patch('main.BlockingScheduler'), \
                patch('main.signal.signal'), \
                patch('main.CronTrigger') as cron:
            main.main()
        return cron

    def test_intervals(self):
        # (settings, expected CronTrigger kwargs, expected warning or None)
        cases = [
            ({'interval': 30}, {'second': '*/30'}, None),
            ({'interval': 10}, {'second': '*/10'}, None),
            ({'interval': 7}, {'second': '*/6'}, 'interval was adjusted to 6'),
            ({'interval': 45}, {'minute': '*'}, 'interval was adjusted to 60'),
            ({'interval': 60}, {'minute': '*'}, None),
            # Pins current behavior: values over 60 are clamped to 60 without a warning
            ({'interval': 90}, {'minute': '*'}, None),
            ({}, {'minute': '*'}, None),
        ]
        for settings, cron_kwargs, warning in cases:
            with self.subTest(settings=settings):
                if warning:
                    with self.assertLogs('thermo', logging.WARNING) as logs:
                        cron = self._main(settings)
                    self.assertEqual(logs.output, [f'WARNING:thermo:{warning}'])
                else:
                    with self.assertNoLogs('thermo', logging.WARNING):
                        cron = self._main(settings)
                cron.assert_called_once_with(**cron_kwargs)
