# Analysis scripts

One script per question in `PLAN.md`, named `<nn>_<slug>.py`, each writing
`reports/<nn>_<slug>.md` (figures under `reports/figures/<nn>_<slug>/`).
Run one with:

```bash
.venv/bin/python analysis/01_decomposition.py
```

Every script follows the same shape (see `_template.py`):

1. `data = load_data()` from `chess_analysis.toolkit`: moves of all standard
   live games with the per-game view joined on; `data.games` for game-level
   questions.
2. Compute with the toolkit helpers: `compare_sides` (player vs opponents in
   the same games, CIs bootstrapped by game), `mean_with_ci`, `binned`,
   `per_game`, `elo_expected_score`, `rating_band`.
3. Write with `Report`: headings, text, tables (`format_table`), figures.

Rules:

- report bullet, blitz and rapid separately (`TIME_CLASSES`);
- every number about the player sits next to the opponents' number from the
  same games;
- every estimate has a confidence interval, clustered by game;
- `ep_loss` is the main move-quality measure, `cp_loss` the secondary one;
- never use post-game ratings as predictors (`my_rating_before` is fine);
- finish by updating the question's status and notes in `PLAN.md`.

`13_findings.py` is the exception to the shape: it computes nothing from the
games. It validates `analysis/findings/*.json` (the findings of every report
with numbers copied as printed) against the reports and writes
`data/<user>/findings.parquet` and `reports/13_findings.md`.
