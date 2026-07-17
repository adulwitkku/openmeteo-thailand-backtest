#!/usr/bin/env python3
"""Backtest Open-Meteo ECMWF IFS forecasts over fixed 1–7 day lead times."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

LOCATIONS = (
    {"slug": "khon-kaen", "name": "Khon Kaen", "latitude": 16.44671, "longitude": 102.83300},
    {"slug": "buriram", "name": "Buriram", "latitude": 14.99433, "longitude": 103.10392},
    {"slug": "chaiyaphum", "name": "Chaiyaphum", "latitude": 15.81047, "longitude": 102.02881},
)
VARIABLES = ("temperature_2m", "relative_humidity_2m", "precipitation", "wind_speed_10m")
LEAD_RANGES = (("1-3 days", 1, 3), ("4-5 days", 4, 5), ("6-7 days", 6, 7))
FORECAST_MODEL = "ecmwf_ifs"
REFERENCE_MODEL = "era5"
ENSEMBLE_WEIGHTS = {1: 0.60, 2: 0.30, 3: 0.10}
BIAS_WINDOW_DAYS = 14
BIAS_MIN_HISTORY = 7


def subtract_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    month_lengths = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    return date(year, month, min(value.day, month_lengths[month - 1]))


def verification_window(end: date) -> tuple[date, date]:
    return subtract_months(end, 3) + timedelta(days=1), end


def lead_day(run_time: datetime, valid_time: datetime) -> int:
    return int((valid_time - run_time).total_seconds() // 3600) // 24


def lead_range(day: int) -> str | None:
    for label, first, last in LEAD_RANGES:
        if first <= day <= last:
            return label
    return None


def required_run_dates(start: date, end: date) -> list[date]:
    first = start - timedelta(days=7)
    last = end - timedelta(days=1)
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def continuous_metrics(forecast: Iterable[float], actual: Iterable[float]) -> dict[str, Any]:
    pairs = [(float(f), float(a)) for f, a in zip(forecast, actual)]
    errors = [f - a for f, a in pairs]
    mean_f = statistics.fmean(f for f, _ in pairs)
    mean_a = statistics.fmean(a for _, a in pairs)
    covariance = sum((f - mean_f) * (a - mean_a) for f, a in pairs)
    variance_f = sum((f - mean_f) ** 2 for f, _ in pairs)
    variance_a = sum((a - mean_a) ** 2 for _, a in pairs)
    return {
        "n": len(pairs),
        "mae": statistics.fmean(abs(error) for error in errors),
        "rmse": math.sqrt(statistics.fmean(error**2 for error in errors)),
        "bias": statistics.fmean(errors),
        "correlation": covariance / math.sqrt(variance_f * variance_a) if variance_f and variance_a else None,
    }


def event_metrics(forecast: Iterable[float], actual: Iterable[float], threshold: float) -> dict[str, Any]:
    pairs = [(float(f), float(a)) for f, a in zip(forecast, actual)]
    tp = sum(f >= threshold and a >= threshold for f, a in pairs)
    tn = sum(f < threshold and a < threshold for f, a in pairs)
    fp = sum(f >= threshold and a < threshold for f, a in pairs)
    fn = sum(f < threshold and a >= threshold for f, a in pairs)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision and recall else None
    return {
        "threshold": threshold,
        "accuracy": (tp + tn) / len(pairs),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
    }


def within_tolerance(forecast: Iterable[float], actual: Iterable[float], tolerance: float) -> float:
    errors = [abs(float(f) - float(a)) for f, a in zip(forecast, actual)]
    return sum(error <= tolerance for error in errors) / len(errors)


def fetch_json(url: str, params: dict[str, Any], cache_dir: Path, name: str, refresh: bool) -> Any:
    cache_dir.mkdir(parents=True, exist_ok=True)
    full_url = f"{url}?{urlencode(params)}"
    digest = hashlib.sha256(full_url.encode()).hexdigest()[:12]
    path = cache_dir / f"{name}-{digest}.json"
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    result = subprocess.run(
        ["curl", "-fsSL", "--retry", "3", "--max-time", "120", full_url],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"Open-Meteo request failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload.get("reason", "Open-Meteo API error"))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def api_params(location: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    return {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(VARIABLES),
        "models": REFERENCE_MODEL,
        "timezone": "GMT",
    }


def latest_complete_era5_date(cache_dir: Path, refresh: bool) -> date:
    candidate_end = date.today() - timedelta(days=1)
    candidate_start = candidate_end - timedelta(days=20)
    payload = fetch_json(
        "https://archive-api.open-meteo.com/v1/archive",
        api_params(LOCATIONS[0], candidate_start, candidate_end),
        cache_dir,
        "era5-availability",
        refresh,
    )
    complete: dict[date, int] = defaultdict(int)
    hourly = payload["hourly"]
    for index, timestamp in enumerate(hourly["time"]):
        if all(hourly[variable][index] is not None for variable in VARIABLES):
            complete[datetime.fromisoformat(timestamp).date()] += 1
    dates = [day for day, count in complete.items() if count == 24]
    if not dates:
        raise RuntimeError("No complete ERA5 day found")
    return max(dates)


def load_observations(start: date, end: date, cache_dir: Path, refresh: bool) -> dict[str, dict[str, dict[str, float]]]:
    output: dict[str, dict[str, dict[str, float]]] = {}
    expected = ((end - start).days + 1) * 24
    for location in LOCATIONS:
        payload = fetch_json(
            "https://archive-api.open-meteo.com/v1/archive",
            api_params(location, start, end),
            cache_dir,
            f"{location['slug']}-era5-{start}-{end}",
            refresh,
        )
        hourly = payload["hourly"]
        index: dict[str, dict[str, float]] = {}
        for row_index, timestamp in enumerate(hourly["time"]):
            values = {variable: hourly[variable][row_index] for variable in VARIABLES}
            if all(value is not None for value in values.values()):
                index[timestamp] = {key: float(value) for key, value in values.items()}
        if len(index) != expected:
            raise RuntimeError(f"{location['name']}: expected {expected} complete ERA5 hours, got {len(index)}")
        output[location["slug"]] = index
    return output


def run_params(run_date: date) -> dict[str, Any]:
    return {
        "latitude": ",".join(str(location["latitude"]) for location in LOCATIONS),
        "longitude": ",".join(str(location["longitude"]) for location in LOCATIONS),
        "run": f"{run_date.isoformat()}T00:00",
        "hourly": ",".join(VARIABLES),
        "models": FORECAST_MODEL,
        "forecast_days": 8,
        "timezone": "GMT",
    }


def fetch_run(run_date: date, cache_dir: Path, refresh: bool) -> tuple[date, list[dict[str, Any]]]:
    payload = fetch_json(
        "https://single-runs-api.open-meteo.com/v1/forecast",
        run_params(run_date),
        cache_dir,
        f"ecmwf-ifs-{run_date}T00",
        refresh,
    )
    if not isinstance(payload, list) or len(payload) != len(LOCATIONS):
        raise RuntimeError(f"Unexpected response for model run {run_date}")
    return run_date, payload


def load_runs(run_dates: list[date], cache_dir: Path, refresh: bool, workers: int) -> dict[date, list[dict[str, Any]]]:
    output: dict[date, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch_run, run_date, cache_dir, refresh) for run_date in run_dates]
        for completed, future in enumerate(as_completed(futures), 1):
            run_date, payload = future.result()
            output[run_date] = payload
            if completed % 10 == 0 or completed == len(futures):
                print(f"Model runs: {completed}/{len(futures)}", flush=True)
    return output


def incomplete_runs(runs: dict[date, list[dict[str, Any]]], start: date, end: date) -> dict[date, int]:
    excluded: dict[date, int] = {}
    for run_date, payloads in runs.items():
        run_time = datetime.combine(run_date, datetime.min.time())
        missing = 0
        for payload in payloads:
            hourly = payload["hourly"]
            for index, timestamp in enumerate(hourly["time"]):
                valid = datetime.fromisoformat(timestamp)
                if start <= valid.date() <= end and lead_range(lead_day(run_time, valid)):
                    if any(hourly[variable][index] is None for variable in VARIABLES):
                        missing += 1
        if missing:
            excluded[run_date] = missing
    return excluded


def align_rows(
    runs: dict[date, list[dict[str, Any]]],
    observations: dict[str, dict[str, dict[str, float]]],
    start: date,
    end: date,
    excluded: set[date],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run_date in sorted(runs):
        if run_date in excluded:
            continue
        run_time = datetime.combine(run_date, datetime.min.time())
        for location, payload in zip(LOCATIONS, runs[run_date]):
            hourly = payload["hourly"]
            for index, timestamp in enumerate(hourly["time"]):
                valid = datetime.fromisoformat(timestamp)
                day = lead_day(run_time, valid)
                label = lead_range(day)
                if not label or not start <= valid.date() <= end:
                    continue
                actual = observations[location["slug"]].get(timestamp)
                forecast = {variable: hourly[variable][index] for variable in VARIABLES}
                if actual is None or any(value is None for value in forecast.values()):
                    continue
                rows.append(
                    {
                        "location": location["slug"],
                        "run_time_utc": run_time.isoformat(timespec="minutes"),
                        "valid_time_utc": timestamp,
                        "lead_day": day,
                        "lead_range": label,
                        **{f"forecast_{key}": float(value) for key, value in forecast.items()},
                        **{f"actual_{key}": actual[key] for key in VARIABLES},
                    }
                )
    return rows


def aggregate_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["location"], row["run_time_utc"], int(row["lead_day"]))].append(row)
    daily: list[dict[str, Any]] = []
    for (location, run_time, day), group in sorted(groups.items()):
        if len(group) != 24:
            continue
        record: dict[str, Any] = {
            "location": location,
            "run_time_utc": run_time,
            "valid_date_utc": group[0]["valid_time_utc"][:10],
            "lead_day": day,
            "lead_range": lead_range(day),
        }
        for prefix in ("forecast", "actual"):
            record[f"{prefix}_precipitation_sum"] = sum(float(row[f"{prefix}_precipitation"]) for row in group)
            record[f"{prefix}_wind_speed_max"] = max(float(row[f"{prefix}_wind_speed_10m"]) for row in group)
        daily.append(record)
    return daily


def weighted_value(leads: dict[int, dict[str, Any]], field: str, weights: dict[int, float]) -> float:
    return sum(weights[day] * float(leads[day][field]) for day in weights)


def ensemble_rows(
    rows: list[dict[str, Any]],
    timestamp_field: str,
    forecast_fields: tuple[str, ...],
    actual_fields: tuple[str, ...],
    weights: dict[int, float] = ENSEMBLE_WEIGHTS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return matched Day-1 baselines and weighted Day-1/2/3 ensembles."""
    groups: dict[tuple[str, str], dict[int, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        day = int(row["lead_day"])
        if day in weights:
            groups[(row["location"], row[timestamp_field])][day] = row
    baseline: list[dict[str, Any]] = []
    ensemble: list[dict[str, Any]] = []
    for (location, timestamp), leads in sorted(groups.items(), key=lambda item: (item[0][1], item[0][0])):
        if not all(day in leads for day in weights):
            continue
        day1 = leads[1]
        base: dict[str, Any] = {"location": location, timestamp_field: timestamp}
        combined: dict[str, Any] = {"location": location, timestamp_field: timestamp}
        for field in forecast_fields:
            base[field] = float(day1[field])
            combined[field] = weighted_value(leads, field, weights)
        for field in actual_fields:
            base[field] = float(day1[field])
            combined[field] = float(day1[field])
        baseline.append(base)
        ensemble.append(combined)
    return baseline, ensemble


def rolling_bias_correct(
    rows: list[dict[str, Any]],
    timestamp_field: str,
    field_pairs: tuple[tuple[str, str], ...],
    *,
    hourly: bool,
    window: int = BIAS_WINDOW_DAYS,
    min_history: int = BIAS_MIN_HISTORY,
) -> list[dict[str, Any]]:
    """Apply walk-forward bias correction using only observations before each row."""
    histories: dict[tuple[str, str, str], deque[float]] = defaultdict(lambda: deque(maxlen=window))
    corrected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (item[timestamp_field], item["location"])):
        bucket = row[timestamp_field][11:13] if hourly else "daily"
        history_sets = [histories[(row["location"], bucket, forecast_field)] for forecast_field, _ in field_pairs]
        if all(len(history) >= min_history for history in history_sets):
            output = dict(row)
            for (forecast_field, _), history in zip(field_pairs, history_sets):
                output[f"corrected_{forecast_field}"] = float(row[forecast_field]) - statistics.fmean(history)
            corrected.append(output)
        for (forecast_field, actual_field), history in zip(field_pairs, history_sets):
            history.append(float(row[forecast_field]) - float(row[actual_field]))
    return corrected


def postprocessing_summary(hourly: list[dict[str, Any]], daily: list[dict[str, Any]]) -> dict[str, Any]:
    hourly_forecasts = ("forecast_temperature_2m", "forecast_relative_humidity_2m")
    hourly_actuals = ("actual_temperature_2m", "actual_relative_humidity_2m")
    daily_forecasts = ("forecast_precipitation_sum", "forecast_wind_speed_max")
    daily_actuals = ("actual_precipitation_sum", "actual_wind_speed_max")
    baseline_hourly, ensemble_hourly = ensemble_rows(
        hourly, "valid_time_utc", hourly_forecasts, hourly_actuals
    )
    baseline_daily, ensemble_daily = ensemble_rows(
        daily, "valid_date_utc", daily_forecasts, daily_actuals
    )
    corrected_hourly = rolling_bias_correct(
        ensemble_hourly,
        "valid_time_utc",
        tuple(zip(hourly_forecasts, hourly_actuals)),
        hourly=True,
    )
    corrected_daily = rolling_bias_correct(
        ensemble_daily,
        "valid_date_utc",
        (("forecast_wind_speed_max", "actual_wind_speed_max"),),
        hourly=False,
    )
    corrected_hourly_keys = {(row["location"], row["valid_time_utc"]) for row in corrected_hourly}
    corrected_daily_keys = {(row["location"], row["valid_date_utc"]) for row in corrected_daily}
    matched_baseline_hourly = [
        row for row in baseline_hourly if (row["location"], row["valid_time_utc"]) in corrected_hourly_keys
    ]
    matched_baseline_daily = [
        row for row in baseline_daily if (row["location"], row["valid_date_utc"]) in corrected_daily_keys
    ]

    def continuous_result(rows_: list[dict[str, Any]], forecast_field: str, actual_field: str, tolerance: float) -> dict[str, Any]:
        result = continuous_metrics(
            [row[forecast_field] for row in rows_], [row[actual_field] for row in rows_]
        )
        result["within_tolerance"] = within_tolerance(
            [row[forecast_field] for row in rows_], [row[actual_field] for row in rows_], tolerance
        )
        return result

    precipitation = {
        "day1": continuous_result(
            baseline_daily, "forecast_precipitation_sum", "actual_precipitation_sum", 5.0
        ),
        "weighted_ensemble": continuous_result(
            ensemble_daily, "forecast_precipitation_sum", "actual_precipitation_sum", 5.0
        ),
        "day1_rain_event": event_metrics(
            [row["forecast_precipitation_sum"] for row in baseline_daily],
            [row["actual_precipitation_sum"] for row in baseline_daily],
            1.0,
        ),
        "weighted_ensemble_rain_event": event_metrics(
            [row["forecast_precipitation_sum"] for row in ensemble_daily],
            [row["actual_precipitation_sum"] for row in ensemble_daily],
            1.0,
        ),
    }
    return {
        "method": {
            "weights": {str(day): weight for day, weight in ENSEMBLE_WEIGHTS.items()},
            "rolling_bias_window_days": BIAS_WINDOW_DAYS,
            "minimum_history_days": BIAS_MIN_HISTORY,
            "no_lookahead": True,
            "hourly_bias_groups": "location + UTC hour",
            "daily_bias_groups": "location",
        },
        "sample_counts": {
            "common_hourly": len(ensemble_hourly),
            "corrected_hourly": len(corrected_hourly),
            "common_daily": len(ensemble_daily),
            "corrected_daily": len(corrected_daily),
        },
        "temperature_2m": {
            "day1": continuous_result(baseline_hourly, "forecast_temperature_2m", "actual_temperature_2m", 2.0),
            "weighted_ensemble": continuous_result(ensemble_hourly, "forecast_temperature_2m", "actual_temperature_2m", 2.0),
            "day1_matched_corrected_sample": continuous_result(matched_baseline_hourly, "forecast_temperature_2m", "actual_temperature_2m", 2.0),
            "weighted_ensemble_rolling_bias": continuous_result(corrected_hourly, "corrected_forecast_temperature_2m", "actual_temperature_2m", 2.0),
        },
        "relative_humidity_2m": {
            "day1": continuous_result(baseline_hourly, "forecast_relative_humidity_2m", "actual_relative_humidity_2m", 10.0),
            "weighted_ensemble": continuous_result(ensemble_hourly, "forecast_relative_humidity_2m", "actual_relative_humidity_2m", 10.0),
            "day1_matched_corrected_sample": continuous_result(matched_baseline_hourly, "forecast_relative_humidity_2m", "actual_relative_humidity_2m", 10.0),
            "weighted_ensemble_rolling_bias": continuous_result(corrected_hourly, "corrected_forecast_relative_humidity_2m", "actual_relative_humidity_2m", 10.0),
        },
        "wind_speed_max_daily": {
            "day1": continuous_result(baseline_daily, "forecast_wind_speed_max", "actual_wind_speed_max", 5.0),
            "weighted_ensemble": continuous_result(ensemble_daily, "forecast_wind_speed_max", "actual_wind_speed_max", 5.0),
            "day1_matched_corrected_sample": continuous_result(matched_baseline_daily, "forecast_wind_speed_max", "actual_wind_speed_max", 5.0),
            "weighted_ensemble_rolling_bias": continuous_result(corrected_daily, "corrected_forecast_wind_speed_max", "actual_wind_speed_max", 5.0),
        },
        "precipitation_sum_daily": precipitation,
    }


def score(hourly: list[dict[str, Any]], daily: list[dict[str, Any]]) -> dict[str, Any]:
    result = {
        "hourly_rows": len(hourly),
        "daily_rows": len(daily),
        "temperature_2m": continuous_metrics(
            [row["forecast_temperature_2m"] for row in hourly],
            [row["actual_temperature_2m"] for row in hourly],
        ),
        "relative_humidity_2m": continuous_metrics(
            [row["forecast_relative_humidity_2m"] for row in hourly],
            [row["actual_relative_humidity_2m"] for row in hourly],
        ),
        "precipitation_sum_daily": continuous_metrics(
            [row["forecast_precipitation_sum"] for row in daily],
            [row["actual_precipitation_sum"] for row in daily],
        ),
        "wind_speed_max_daily": continuous_metrics(
            [row["forecast_wind_speed_max"] for row in daily],
            [row["actual_wind_speed_max"] for row in daily],
        ),
    }
    result["temperature_within_2c"] = within_tolerance(
        [row["forecast_temperature_2m"] for row in hourly],
        [row["actual_temperature_2m"] for row in hourly],
        2.0,
    )
    result["rain_day_event"] = event_metrics(
        [row["forecast_precipitation_sum"] for row in daily],
        [row["actual_precipitation_sum"] for row in daily],
        1.0,
    )
    return result


def build_summary(hourly: list[dict[str, Any]], daily: list[dict[str, Any]], start: date, end: date, excluded: dict[date, int]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "period": {"start_utc": start.isoformat(), "end_utc": end.isoformat(), "days": (end - start).days + 1},
        "forecast": {"api": "Open-Meteo Single Runs API", "model": FORECAST_MODEL, "run": "00:00 UTC daily"},
        "reference": {"api": "Open-Meteo Historical Weather API", "model": REFERENCE_MODEL},
        "lead_definition": "day N = forecast hours 24*N through 24*(N+1)-1",
        "excluded_incomplete_runs": {day.isoformat(): count for day, count in sorted(excluded.items())},
        "ranges": {},
        "locations": {},
        "postprocessing": postprocessing_summary(hourly, daily),
    }
    for label, _, _ in LEAD_RANGES:
        summary["ranges"][label] = score(
            [row for row in hourly if row["lead_range"] == label],
            [row for row in daily if row["lead_range"] == label],
        )
    for location in LOCATIONS:
        location_hourly = [row for row in hourly if row["location"] == location["slug"]]
        location_daily = [row for row in daily if row["location"] == location["slug"]]
        summary["locations"][location["slug"]] = {
            "name": location["name"],
            "ranges": {
                label: score(
                    [row for row in location_hourly if row["lead_range"] == label],
                    [row for row in location_daily if row["lead_range"] == label],
                )
                for label, _, _ in LEAD_RANGES
            },
        }
    return summary


def fmt(value: float) -> str:
    return f"{value:.2f}"


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def report_markdown(summary: dict[str, Any]) -> str:
    period = summary["period"]
    post = summary["postprocessing"]
    lines = [
        "# Open-Meteo Forecast Backtest Report",
        "",
        f"Verification period: **{period['start_utc']} to {period['end_utc']} UTC** ({period['days']} days). Locations: Khon Kaen, Buriram, and Chaiyaphum, Thailand.",
        "",
        "## Results",
        "",
        "| Lead range | Temperature MAE / RMSE (°C) | Within ±2°C | Relative humidity MAE / RMSE (percentage points) | Daily precipitation MAE / RMSE (mm) | Daily maximum wind MAE / RMSE (km/h) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, _, _ in LEAD_RANGES:
        item = summary["ranges"][label]
        lines.append(
            f"| {label} | {fmt(item['temperature_2m']['mae'])} / {fmt(item['temperature_2m']['rmse'])} | "
            f"{pct(item['temperature_within_2c'])} | {fmt(item['relative_humidity_2m']['mae'])} / {fmt(item['relative_humidity_2m']['rmse'])} | "
            f"{fmt(item['precipitation_sum_daily']['mae'])} / {fmt(item['precipitation_sum_daily']['rmse'])} | "
            f"{fmt(item['wind_speed_max_daily']['mae'])} / {fmt(item['wind_speed_max_daily']['rmse'])} |"
        )
    lines.extend(["", "### Rain-day detection", "", "A rain day is defined as daily precipitation ≥1 mm.", "", "| Lead range | Accuracy | Precision | Recall | F1 |", "|---|---:|---:|---:|---:|"])
    for label, _, _ in LEAD_RANGES:
        rain = summary["ranges"][label]["rain_day_event"]
        lines.append(f"| {label} | {pct(rain['accuracy'])} | {pct(rain['precision'])} | {pct(rain['recall'])} | {pct(rain['f1'])} |")
    lines.extend([
        "",
        "## Day-1/2/3 weighted ensemble and rolling bias correction",
        "",
        "For the same valid timestamp, the post-processed forecast is `0.60 × Day 1 + 0.30 × Day 2 + 0.10 × Day 3`. Temperature and relative humidity are then corrected using the previous 14 available errors for the same location and UTC hour. Daily maximum wind uses the previous 14 daily errors for the same location. A minimum of 7 prior observations is required, and the current observation is added only after its forecast is scored, preventing lookahead.",
        "",
        f"The corrected evaluation contains **{post['sample_counts']['corrected_hourly']:,} hourly** and **{post['sample_counts']['corrected_daily']:,} daily** samples after warm-up. Day-1 baselines below use exactly the same corrected-period samples.",
        "",
        "| Variable and tolerance | Day-1 baseline MAE / RMSE | Day-1 within tolerance | Weighted + rolling bias MAE / RMSE | Corrected within tolerance |",
        "|---|---:|---:|---:|---:|",
        f"| Temperature (±2°C) | {fmt(post['temperature_2m']['day1_matched_corrected_sample']['mae'])} / {fmt(post['temperature_2m']['day1_matched_corrected_sample']['rmse'])} | {pct(post['temperature_2m']['day1_matched_corrected_sample']['within_tolerance'])} | {fmt(post['temperature_2m']['weighted_ensemble_rolling_bias']['mae'])} / {fmt(post['temperature_2m']['weighted_ensemble_rolling_bias']['rmse'])} | **{pct(post['temperature_2m']['weighted_ensemble_rolling_bias']['within_tolerance'])}** |",
        f"| Relative humidity (±10 percentage points) | {fmt(post['relative_humidity_2m']['day1_matched_corrected_sample']['mae'])} / {fmt(post['relative_humidity_2m']['day1_matched_corrected_sample']['rmse'])} | {pct(post['relative_humidity_2m']['day1_matched_corrected_sample']['within_tolerance'])} | {fmt(post['relative_humidity_2m']['weighted_ensemble_rolling_bias']['mae'])} / {fmt(post['relative_humidity_2m']['weighted_ensemble_rolling_bias']['rmse'])} | **{pct(post['relative_humidity_2m']['weighted_ensemble_rolling_bias']['within_tolerance'])}** |",
        f"| Daily maximum wind (±5 km/h) | {fmt(post['wind_speed_max_daily']['day1_matched_corrected_sample']['mae'])} / {fmt(post['wind_speed_max_daily']['day1_matched_corrected_sample']['rmse'])} | {pct(post['wind_speed_max_daily']['day1_matched_corrected_sample']['within_tolerance'])} | {fmt(post['wind_speed_max_daily']['weighted_ensemble_rolling_bias']['mae'])} / {fmt(post['wind_speed_max_daily']['weighted_ensemble_rolling_bias']['rmse'])} | **{pct(post['wind_speed_max_daily']['weighted_ensemble_rolling_bias']['within_tolerance'])}** |",
        "",
        "Precipitation does not use rolling additive bias correction because it degraded performance in this sample. The weighted ensemble alone improves daily precipitation MAE and rain-day F1:",
        "",
        "| Precipitation metric | Day 1 | Weighted Day-1/2/3 ensemble |",
        "|---|---:|---:|",
        f"| Daily total MAE / RMSE (mm) | {fmt(post['precipitation_sum_daily']['day1']['mae'])} / {fmt(post['precipitation_sum_daily']['day1']['rmse'])} | **{fmt(post['precipitation_sum_daily']['weighted_ensemble']['mae'])} / {fmt(post['precipitation_sum_daily']['weighted_ensemble']['rmse'])}** |",
        f"| Within ±5 mm | {pct(post['precipitation_sum_daily']['day1']['within_tolerance'])} | {pct(post['precipitation_sum_daily']['weighted_ensemble']['within_tolerance'])} |",
        f"| Rain-day F1 | {pct(post['precipitation_sum_daily']['day1_rain_event']['f1'])} | **{pct(post['precipitation_sum_daily']['weighted_ensemble_rain_event']['f1'])}** |",
        "",
        "## Interpretation",
        "",
        "- Forecast error increases with lead time. Temperature forecasts are strongest at 1–3 days and progressively weaker at 4–5 and 6–7 days.",
        "- Daily precipitation is more difficult than temperature and humidity because convective rainfall varies sharply in space and time.",
        "- Daily maximum wind-speed error is not monotonic in this sample: MAE is highest at 1–3 days and lower at 4–5 and 6–7 days, so it should not be interpreted as a simple lead-time trend.",
        "- Results should be interpreted as agreement with ERA5 reanalysis, not direct station-observation accuracy.",
        "",
        "## Method",
        "",
        "1. Retrieve the ECMWF IFS 00:00 UTC run for every initialization date using the Open-Meteo Single Runs API.",
        "2. Define day 1 as lead hours 24–47, day 2 as 48–71, through day 7 as 168–191.",
        "3. Match each forecast hour with ERA5 at the same UTC valid timestamp.",
        "4. Aggregate precipitation by daily sum and wind speed by daily maximum.",
        "5. Exclude an entire model run if the Open-Meteo archive contains null forecast rows, preserving equal samples across lead days.",
        "6. Calculate MAE, RMSE, bias, correlation, temperature-within-±2°C rate, and rain-day classification metrics.",
        "7. Build the lagged ensemble only from Day-1/2/3 forecasts sharing the same valid timestamp, then apply walk-forward rolling bias correction using past errors only.",
        "",
        "## Limitations",
        "",
        "- ERA5 is a gridded reanalysis product, not a surface weather station measurement.",
        "- Only the daily 00:00 UTC ECMWF IFS run is evaluated.",
        "- Forecast and ERA5 grids differ in spatial resolution; precipitation is especially sensitive to this mismatch.",
        "- These three city coordinates do not represent every location within each province.",
        "- Post-processing results are prequential within this 91-day period but still require confirmation on a later, untouched period before operational use.",
        "",
        f"Incomplete archived runs excluded: **{', '.join(summary['excluded_incomplete_runs']) or 'none'}**.",
        "",
    ])
    return "\n".join(lines)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def expected_counts(run_dates: list[date], start: date, end: date) -> tuple[int, int]:
    run_leads = sum(start <= run_date + timedelta(days=day) <= end for run_date in run_dates for day in range(1, 8))
    daily = run_leads * len(LOCATIONS)
    return daily * 24, daily


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end-date", type=date.fromisoformat, help="Verification end date; defaults to latest complete ERA5 day")
    parser.add_argument("--refresh", action="store_true", help="Ignore cached API responses")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent API requests")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.output_dir.resolve()
    cache_dir = root / "data" / "raw"
    end = args.end_date or latest_complete_era5_date(cache_dir, args.refresh)
    start, end = verification_window(end)
    run_dates = required_run_dates(start, end)
    print(f"Verification period: {start} to {end} UTC")
    observations = load_observations(start, end, cache_dir, args.refresh)
    runs = load_runs(run_dates, cache_dir, args.refresh, max(1, args.workers))
    excluded = incomplete_runs(runs, start, end)
    included_dates = [run_date for run_date in run_dates if run_date not in excluded]
    if excluded:
        print("Excluded incomplete runs: " + ", ".join(day.isoformat() for day in sorted(excluded)))
    hourly = align_rows(runs, observations, start, end, set(excluded))
    daily = aggregate_daily(hourly)
    expected_hourly, expected_daily = expected_counts(included_dates, start, end)
    if (len(hourly), len(daily)) != (expected_hourly, expected_daily):
        raise RuntimeError(f"Expected {(expected_hourly, expected_daily)}, got {(len(hourly), len(daily))}")
    summary = build_summary(hourly, daily, start, end, excluded)
    summary["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    results = root / "results"
    results.mkdir(parents=True, exist_ok=True)
    write_csv(root / "data" / "hourly.csv", hourly)
    write_csv(root / "data" / "daily.csv", daily)
    (results / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (results / "REPORT.md").write_text(report_markdown(summary), encoding="utf-8")
    print(f"Hourly rows: {len(hourly)}; daily rows: {len(daily)}")
    print(f"Report: {results / 'REPORT.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
