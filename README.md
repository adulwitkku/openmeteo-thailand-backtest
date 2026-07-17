# Open-Meteo Thailand Forecast Backtest

A reproducible, standard-library Python backtest of Open-Meteo ECMWF IFS forecasts for three cities in northeastern Thailand:

- Khon Kaen
- Buriram
- Chaiyaphum

The project evaluates fixed forecast lead ranges of **1–3 days**, **4–5 days**, and **6–7 days** for:

- 2 m air temperature
- Daily precipitation total
- Daily maximum 10 m wind speed
- 2 m relative humidity

Forecasts are retrieved from the [Open-Meteo Single Runs API](https://open-meteo.com/en/docs/single-runs-api) and verified against ERA5 from the [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api).

## Results

Backtest period: **2026-04-11 to 2026-07-10 UTC** (91 days).

| Lead range | Temperature MAE / RMSE (°C) | Within ±2°C | Relative humidity MAE / RMSE (percentage points) | Daily precipitation MAE / RMSE (mm) | Daily maximum wind MAE / RMSE (km/h) |
|---|---:|---:|---:|---:|---:|
| 1–3 days | 1.32 / 1.72 | 80.2% | 6.30 / 8.40 | 3.62 / 5.67 | 4.52 / 5.52 |
| 4–5 days | 1.48 / 1.88 | 75.1% | 6.84 / 8.92 | 3.65 / 5.49 | 3.44 / 4.42 |
| 6–7 days | 1.66 / 2.08 | 68.3% | 7.44 / 9.55 | 3.73 / 5.78 | 3.03 / 4.00 |

### Rain-day detection

A rain day is defined as daily precipitation ≥1 mm.

| Lead range | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| 1–3 days | 71.4% | 74.6% | 81.1% | 77.7% |
| 4–5 days | 66.9% | 70.8% | 77.9% | 74.2% |
| 6–7 days | 66.3% | 69.9% | 78.1% | 73.8% |

Key findings:

- Temperature skill degrades consistently with lead time: MAE rises from 1.32°C at 1–3 days to 1.66°C at 6–7 days.
- The percentage of temperature forecasts within ±2°C falls from 80.2% to 68.3%.
- Relative-humidity MAE rises from 6.30 to 7.44 percentage points.
- Daily precipitation MAE rises slightly from 3.62 to 3.73 mm, while rain-day F1 falls from 77.7% to 73.8%.
- Daily maximum wind error is not monotonic in this sample; it is highest in the 1–3-day range. This should not be interpreted as a general improvement at longer lead times.

## Weighted ensemble and rolling bias correction

The script also combines forecasts that target the same valid timestamp:

```text
ensemble = 0.60 × Day 1 + 0.30 × Day 2 + 0.10 × Day 3
```

For temperature and relative humidity, it subtracts the mean forecast error from the previous 14 available days for the same location and UTC hour. Daily maximum wind uses the previous 14 daily errors for the same location. Correction starts after at least 7 prior observations. The current observation is appended only after its forecast is scored, so future data cannot leak into the correction.

The corrected evaluation contains 5,616 hourly and 234 daily samples after warm-up. Day-1 baselines in this table use exactly the same timestamps:

| Variable and tolerance | Day-1 baseline MAE / RMSE | Day-1 accuracy | Weighted + rolling bias MAE / RMSE | Corrected accuracy |
|---|---:|---:|---:|---:|
| Temperature, ±2°C | 1.22 / 1.59°C | 83.6% | 0.97 / 1.29°C | **88.9%** |
| Relative humidity, ±10 percentage points | 5.92 / 7.97 | 83.5% | 5.31 / 6.97 | **86.1%** |
| Daily maximum wind, ±5 km/h | 4.68 / 5.69 km/h | 59.0% | 2.51 / 3.17 km/h | **90.2%** |

Additive rolling bias correction is not applied to precipitation because it degraded precipitation performance in this sample. The weighted ensemble alone gives:

| Precipitation metric | Day 1 | Weighted Day-1/2/3 |
|---|---:|---:|
| Daily total MAE / RMSE | 3.53 / 5.81 mm | **3.25 / 5.17 mm** |
| Within ±5 mm | 79.2% | 79.2% |
| Rain-day F1 | 77.9% | **80.1%** |

These are walk-forward or prequential results within the same 91-day study period. They demonstrate no-lookahead performance, but a later untouched period is still required before claiming stable operational accuracy above 85%.

See [results/REPORT.md](results/REPORT.md) for the full methodology, interpretation, and limitations. Machine-readable metrics are in [results/summary.json](results/summary.json).

## Lead-time definition

This project uses one ECMWF IFS model run per day, initialized at 00:00 UTC:

| Forecast day | Hours after initialization |
|---:|---:|
| Day 1 | 24–47 hours |
| Day 2 | 48–71 hours |
| Day 3 | 72–95 hours |
| Day 4 | 96–119 hours |
| Day 5 | 120–143 hours |
| Day 6 | 144–167 hours |
| Day 7 | 168–191 hours |

Grouping is performed only after each forecast hour is matched with ERA5 at the same UTC valid timestamp. This avoids mixing forecasts of unknown age.

## How the backtest works

1. Find the latest UTC day for which ERA5 has 24 complete hourly observations.
2. Select the inclusive three-calendar-month verification window ending on that day.
3. Retrieve every required ECMWF IFS 00:00 UTC model run from Open-Meteo's Single Runs API.
4. Retrieve ERA5 temperature, precipitation, wind speed, and relative humidity for each location.
5. Align forecast and reference data by UTC valid timestamp.
6. Aggregate hourly precipitation into daily totals and hourly wind speed into daily maxima.
7. Group forecasts into 1–3, 4–5, and 6–7 day ranges.
8. Calculate MAE, RMSE, bias, correlation, temperature-within-±2°C, and rain-day classification metrics.
9. Exclude an entire archived model run when any required forecast values are null, keeping sample counts balanced across lead days.
10. Build a recency-weighted Day-1/2/3 lagged ensemble for matching valid timestamps and apply walk-forward rolling bias correction to temperature, humidity, and maximum wind.

For this run, the incomplete archives initialized on **2026-06-11** and **2026-06-23** were excluded. The final dataset contains **44,856 hourly forecast/reference pairs** and **1,869 daily forecast/reference pairs**.

## Requirements

- Python 3.10 or later
- `curl`
- Internet access to Open-Meteo APIs

No third-party Python packages are required.

## Run

```bash
python3 backtest.py
```

The first run downloads and caches API responses under `data/raw/`. Later runs reuse the cache.

Force a fresh download:

```bash
python3 backtest.py --refresh
```

Use a fixed verification end date:

```bash
python3 backtest.py --end-date 2026-07-10
```

Control concurrent API requests:

```bash
python3 backtest.py --workers 2
```

Generated files:

- `results/REPORT.md` — human-readable report
- `results/summary.json` — machine-readable metrics
- `data/hourly.csv` — aligned hourly forecast/reference rows (git-ignored)
- `data/daily.csv` — daily aggregations (git-ignored)
- `data/raw/` — cached API responses (git-ignored)

## Test

```bash
python3 -m unittest -v
```

The tests cover calendar-window selection, fixed lead-day assignment, lead-range grouping, expected balanced sample counts, metric calculations, weighted ensemble construction, and no-lookahead rolling bias correction.

## Important limitations

- ERA5 is gridded reanalysis, not a surface station observation.
- Only ECMWF IFS runs initialized at 00:00 UTC are evaluated.
- Forecast and reference products use different grids; this especially affects convective precipitation.
- City-center coordinates do not represent all locations within each province.
- Results describe this model, period, initialization cycle, and reference dataset; they should not be generalized without additional validation.
- Post-processing is evaluated prequentially within this period; confirm it on a later untouched period before operational use.

## License

MIT
