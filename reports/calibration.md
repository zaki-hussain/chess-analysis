# Win-probability calibration

Fitted on 365,561 positions (every move of every standard game, both sides), 5-fold cross-validation grouped by game. Lower is better.

| Metric | Model | Lichess cp curve | Stockfish WDL |
|---|---|---|---|
| Expected points MAE | 0.3668 | 0.3814 | 0.3645 |
| Expected points Brier | 0.1792 | 0.2161 | 0.2667 |
| P(win) log loss | 0.5676 | 0.6949 | |
| P(win) Brier | 0.1958 | 0.2358 | |

## Reliability by decile of the model's prediction

| Decile | Model predicted | Observed | Lichess curve | Stockfish WDL |
|---|---|---|---|---|
| 1 | 0.076 | 0.093 | 0.146 | 0.083 |
| 2 | 0.244 | 0.247 | 0.302 | 0.228 |
| 3 | 0.357 | 0.367 | 0.426 | 0.383 |
| 4 | 0.428 | 0.438 | 0.482 | 0.472 |
| 5 | 0.480 | 0.490 | 0.511 | 0.521 |
| 6 | 0.524 | 0.518 | 0.535 | 0.565 |
| 7 | 0.576 | 0.564 | 0.569 | 0.619 |
| 8 | 0.648 | 0.642 | 0.632 | 0.705 |
| 9 | 0.764 | 0.767 | 0.754 | 0.836 |
| 10 | 0.926 | 0.912 | 0.878 | 0.937 |

Observed = mean game score of the side to move. A well-calibrated model has predicted ≈ observed in every row.
