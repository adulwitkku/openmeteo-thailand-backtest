import unittest
from datetime import date, datetime

from backtest import (
    continuous_metrics,
    expected_counts,
    lead_day,
    lead_range,
    required_run_dates,
    verification_window,
)


class BacktestTest(unittest.TestCase):
    def test_verification_window_is_three_calendar_months(self):
        self.assertEqual(
            verification_window(date(2026, 7, 10)),
            (date(2026, 4, 11), date(2026, 7, 10)),
        )

    def test_lead_day_and_ranges(self):
        run = datetime(2026, 4, 1)
        self.assertEqual(lead_day(run, datetime(2026, 4, 2)), 1)
        self.assertEqual(lead_day(run, datetime(2026, 4, 8, 23)), 7)
        self.assertEqual(lead_range(1), "1-3 days")
        self.assertEqual(lead_range(4), "4-5 days")
        self.assertEqual(lead_range(7), "6-7 days")
        self.assertIsNone(lead_range(0))
        self.assertIsNone(lead_range(8))

    def test_required_runs_and_balanced_counts(self):
        start, end = date(2026, 4, 11), date(2026, 7, 10)
        runs = required_run_dates(start, end)
        self.assertEqual(runs[0], date(2026, 4, 4))
        self.assertEqual(runs[-1], date(2026, 7, 9))
        self.assertEqual(len(runs), 97)
        self.assertEqual(expected_counts(runs, start, end), (45864, 1911))
        complete = [day for day in runs if day not in {date(2026, 6, 11), date(2026, 6, 23)}]
        self.assertEqual(expected_counts(complete, start, end), (44856, 1869))

    def test_continuous_metrics(self):
        metrics = continuous_metrics([1.0, 2.0, 3.0], [2.0, 2.0, 4.0])
        self.assertAlmostEqual(metrics["mae"], 2 / 3)
        self.assertAlmostEqual(metrics["bias"], -2 / 3)
        self.assertAlmostEqual(metrics["rmse"], (2 / 3) ** 0.5)


if __name__ == "__main__":
    unittest.main()
