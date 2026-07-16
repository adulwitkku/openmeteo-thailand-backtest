# Open-Meteo Forecast Backtest Report

Verification period: **2026-04-11 to 2026-07-10 UTC** (91 days). Locations: Khon Kaen, Buriram, and Chaiyaphum, Thailand.

## Results

| Lead range | Temperature MAE / RMSE (°C) | Within ±2°C | Relative humidity MAE / RMSE (percentage points) | Daily precipitation MAE / RMSE (mm) | Daily maximum wind MAE / RMSE (km/h) |
|---|---:|---:|---:|---:|---:|
| 1-3 days | 1.32 / 1.72 | 80.2% | 6.30 / 8.40 | 3.62 / 5.67 | 4.52 / 5.52 |
| 4-5 days | 1.48 / 1.88 | 75.1% | 6.84 / 8.92 | 3.65 / 5.49 | 3.44 / 4.42 |
| 6-7 days | 1.66 / 2.08 | 68.3% | 7.44 / 9.55 | 3.73 / 5.78 | 3.03 / 4.00 |

### Rain-day detection

A rain day is defined as daily precipitation ≥1 mm.

| Lead range | Accuracy | Precision | Recall | F1 |
|---|---:|---:|---:|---:|
| 1-3 days | 71.4% | 74.6% | 81.1% | 77.7% |
| 4-5 days | 66.9% | 70.8% | 77.9% | 74.2% |
| 6-7 days | 66.3% | 69.9% | 78.1% | 73.8% |

## Interpretation

- Forecast error increases with lead time. Temperature forecasts are strongest at 1–3 days and progressively weaker at 4–5 and 6–7 days.
- Daily precipitation is more difficult than temperature and humidity because convective rainfall varies sharply in space and time.
- Daily maximum wind-speed error is not monotonic in this sample: MAE is highest at 1–3 days and lower at 4–5 and 6–7 days, so it should not be interpreted as a simple lead-time trend.
- Results should be interpreted as agreement with ERA5 reanalysis, not direct station-observation accuracy.

## Method

1. Retrieve the ECMWF IFS 00:00 UTC run for every initialization date using the Open-Meteo Single Runs API.
2. Define day 1 as lead hours 24–47, day 2 as 48–71, through day 7 as 168–191.
3. Match each forecast hour with ERA5 at the same UTC valid timestamp.
4. Aggregate precipitation by daily sum and wind speed by daily maximum.
5. Exclude an entire model run if the Open-Meteo archive contains null forecast rows, preserving equal samples across lead days.
6. Calculate MAE, RMSE, bias, correlation, temperature-within-±2°C rate, and rain-day classification metrics.

## Limitations

- ERA5 is a gridded reanalysis product, not a surface weather station measurement.
- Only the daily 00:00 UTC ECMWF IFS run is evaluated.
- Forecast and ERA5 grids differ in spatial resolution; precipitation is especially sensitive to this mismatch.
- These three city coordinates do not represent every location within each province.

Incomplete archived runs excluded: **2026-06-11, 2026-06-23**.
