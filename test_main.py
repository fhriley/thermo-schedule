from unittest import TestCase
from datetime import datetime
import holidays
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
