import unittest
from datetime import date, datetime

from backtest import (
    continuous_metrics,
    ensemble_rows,
    expected_counts,
    lead_day,
    lead_range,
    required_run_dates,
    rolling_bias_correct,
    verification_window,
    weighted_value,
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

    def test_weighted_day_1_2_3_ensemble(self):
        leads = {
            1: {"forecast": 10.0},
            2: {"forecast": 20.0},
            3: {"forecast": 30.0},
        }
        self.assertAlmostEqual(weighted_value(leads, "forecast", {1: 0.6, 2: 0.3, 3: 0.1}), 15.0)
        rows = [
            {"location": "x", "valid": "2026-01-04", "lead_day": day, "forecast": value, "actual": 12.0}
            for day, value in ((1, 10.0), (2, 20.0), (3, 30.0))
        ]
        baseline, ensemble = ensemble_rows(rows, "valid", ("forecast",), ("actual",))
        self.assertEqual(baseline[0]["forecast"], 10.0)
        self.assertEqual(ensemble[0]["forecast"], 15.0)
        self.assertEqual(ensemble[0]["actual"], 12.0)

    def test_rolling_bias_correction_has_no_lookahead(self):
        rows = [
            {"location": "x", "date": "2026-01-01", "forecast": 12.0, "actual": 10.0},
            {"location": "x", "date": "2026-01-02", "forecast": 15.0, "actual": 11.0},
            {"location": "x", "date": "2026-01-03", "forecast": 17.0, "actual": 12.0},
        ]
        corrected = rolling_bias_correct(
            rows,
            "date",
            (("forecast", "actual"),),
            hourly=False,
            window=2,
            min_history=1,
        )
        self.assertEqual(len(corrected), 2)
        self.assertAlmostEqual(corrected[0]["corrected_forecast"], 13.0)
        self.assertAlmostEqual(corrected[1]["corrected_forecast"], 14.0)


if __name__ == "__main__":
    unittest.main()
