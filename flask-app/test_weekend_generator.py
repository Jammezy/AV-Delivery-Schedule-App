"""Excluded hours remain signup opportunities without entering rotation."""
import unittest

from weekend_generator import SHIFTS, generate_weekend_schedule


class WeekendSignupTests(unittest.TestCase):
    def test_excluded_fixed_and_rotating_shifts_do_not_consume_assignments(self):
        roster = [dict(id=1, name='Fixed'), dict(id=2, name='Rotating')]
        availability = {e['name']: {f'{day}_{h:02d}': 1
                        for day in ['Fri', 'Sat', 'Sun'] for h in range(7, 22)} for e in roster}
        cfg = dict(start_date='2026-09-04', end_date='2026-09-13',
                   fixed_assignments={'friday_evening': 1}, rotating_employees=[2],
                   excluded_dates=[{'date': '2026-09-04', 'label': 'Extra hours'}, '2026-09-05'])
        result = generate_weekend_schedule(cfg, availability, roster)
        self.assertEqual(len(result['signup_shifts']), 4)
        self.assertTrue(all(s['assigned'] is None and s['origin'] == 'signup'
                            for s in result['signup_shifts']))
        self.assertEqual(result['signup_shifts'][0]['label'], 'Extra hours')
        self.assertEqual(result['rotating_counts'], {2: 2})
        self.assertEqual([s['date'] for s in result['assignments'] if s['assigned'] == 1], ['2026-09-11'])
        self.assertEqual([s['date'] for s in result['assignments'] if s['assigned'] == 2],
                         ['2026-09-06', '2026-09-12'])
        self.assertFalse(any(s['date'] in ['2026-09-04', '2026-09-05'] for s in result['assignments']))

    def test_partial_weekends_duplicates_weekdays_and_outside_range(self):
        for start, end, expected in [('2026-09-05', '2026-09-06', 5),
                                     ('2026-09-06', '2026-09-06', 2)]:
            with self.subTest(start=start):
                result = generate_weekend_schedule(dict(start_date=start, end_date=end,
                    excluded_dates=['2026-09-04', '2026-09-05', '2026-09-05',
                                    '2026-09-06', '2026-09-07', '2026-09-11']), {}, [])
                self.assertEqual(len(result['signup_shifts']), expected)
                self.assertEqual(result['assignments'], [])
                self.assertEqual(result['rotating_counts'], {})
                self.assertEqual(result['effective_pool'], [])
                self.assertTrue(all(start <= s['date'] <= end for s in result['signup_shifts']))

    def test_no_exclusions_preserve_regular_schedule(self):
        result = generate_weekend_schedule(dict(start_date='2026-09-04', end_date='2026-09-06'), {}, [])
        self.assertEqual(result['signup_shifts'], [])
        self.assertEqual(len(result['assignments']), len(SHIFTS))
        self.assertTrue(all(s['unfilled_reason'] for s in result['assignments']))
