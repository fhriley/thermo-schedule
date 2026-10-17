import datetime as dt_module
import logging
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import holidays
import main
from main import load_schedule, _is_holiday, _is_peak, _get_active_schedule, _is_peak


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


class TestThermoTaskPeak(TestCase):
    # Monday 2021-10-04 18:00-21:00 is a winter peak window in test.yaml; cool schedule temp is 302, peak temp 68
    PEAK_NOW = datetime(2021, 10, 4, 18, 30)
    SCHEDULE_TEMP = 302
    PEAK_TEMP = 68

    THRESHOLDS = {'solar_prod_thresh': {'cool': 4.0}, 'consumption_thresh': {'cool': -1.0}, 'min_comp_time_s': 300}

    def _run(self, now, last_off, solar_prod=10.0, consumption=1.0, thermo_state=0, settings_overrides=None):
        schedule, settings = load_schedule('test.yaml')
        settings = dict(settings, **(self.THRESHOLDS if settings_overrides is None else settings_overrides))
        data = main.Data(logging.getLogger('test'), {'url': 'http://thermo', 'schedule': schedule[0]['schedule']},
                         settings)
        data.last_off = last_off

        resp = MagicMock()
        resp.json.return_value = {'state': thermo_state, 'mode': 2, 'schedule': 0, 'heattemp': 60.0,
                                  'cooltemp': 80.0}

        class FakeDatetime(dt_module.datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        fake_dt = SimpleNamespace(datetime=FakeDatetime, time=dt_module.time, timedelta=dt_module.timedelta,
                                  date=dt_module.date)

        with patch('main.requests.get', return_value=resp), \
                patch('main.get_solar_prod_cons', return_value=(solar_prod, consumption)), \
                patch('main._set_temp') as set_temp, \
                patch('main.datetime', fake_dt):
            main.thermo_task(data)

        self.assertEqual(set_temp.call_count, 1, 'thermostat was not updated')
        return set_temp.call_args.args[2]['cooltemp'], data

    def test_hold_does_not_carry_over_to_next_day(self):
        # Peak was forced yesterday at 18:29, one minute short of the same time of day
        cooltemp, _ = self._run(self.PEAK_NOW, last_off=self.PEAK_NOW - timedelta(days=1, minutes=1))
        self.assertEqual(cooltemp, self.SCHEDULE_TEMP)

    def test_hold_keeps_peak_temp_within_min_comp_time(self):
        cooltemp, _ = self._run(self.PEAK_NOW, last_off=self.PEAK_NOW - timedelta(seconds=60))
        self.assertEqual(cooltemp, self.PEAK_TEMP)

    def test_hold_ends_after_min_comp_time(self):
        cooltemp, _ = self._run(self.PEAK_NOW, last_off=self.PEAK_NOW - timedelta(seconds=301))
        self.assertEqual(cooltemp, self.SCHEDULE_TEMP)

    def test_low_solar_forces_peak_and_records_time(self):
        cooltemp, data = self._run(self.PEAK_NOW, last_off=datetime(1900, 1, 1), solar_prod=1.0)
        self.assertEqual(cooltemp, self.PEAK_TEMP)
        self.assertEqual(data.last_off, self.PEAK_NOW)

    def test_missing_thresholds_use_defaults(self):
        # Default solar threshold is 100 kW, so 10 kW of production still forces peak
        cooltemp, data = self._run(self.PEAK_NOW, last_off=datetime(1900, 1, 1), settings_overrides={})
        self.assertEqual(cooltemp, self.PEAK_TEMP)
        self.assertEqual(data.min_comp_time_s, 300.0)

    def test_null_thresholds_use_defaults(self):
        overrides = {'solar_prod_thresh': None, 'consumption_thresh': None, 'min_comp_time_s': None}
        cooltemp, data = self._run(self.PEAK_NOW, last_off=datetime(1900, 1, 1), settings_overrides=overrides)
        self.assertEqual(cooltemp, self.PEAK_TEMP)
        self.assertEqual(data.min_comp_time_s, 300.0)

    def test_zero_min_comp_time_disables_hold(self):
        overrides = dict(self.THRESHOLDS, min_comp_time_s=0)
        cooltemp, _ = self._run(self.PEAK_NOW, last_off=self.PEAK_NOW - timedelta(seconds=1),
                                settings_overrides=overrides)
        self.assertEqual(cooltemp, self.SCHEDULE_TEMP)
