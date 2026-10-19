# Phase 2 model evaluation

**Backtest (simulated)** · generated 2026-10-19T10:15:08.026339+00:00

This expanding-window evaluation predicts each match before kickoff using only earlier results in that league. It reports model quality, not betting performance or a live record.

Time decay: exp(-0.003 × age in days); score matrix maximum: 10 goals per side.

| League ID | Forecasts | Earlier results (min / median) | Log loss | Brier (3 outcomes) | Calibration error |
|---:|---:|---:|---:|---:|---:|
| - | 0 | - | - | - | - |

No completed fixtures were available for evaluation, or no league had enough earlier results to form a test forecast.

Calibration bins compare mean predicted probability with observed outcome frequency (one-vs-rest, all three outcomes):

## Reading this report

Log loss and Brier score are proper scoring rules; lower is better. Calibration error summarizes the gap between predicted probabilities and observed frequencies. League samples and test forecasts are listed above; small samples are diagnostic only. No edge or future outcome is established by these figures.
