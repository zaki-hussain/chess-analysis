# Analysis scripts

One script per question in `PLAN.md`, named `<nn>_<slug>.py`. Each one
computes its results and writes them as data, nothing else:

- tables as CSV in `presentation/data/<nn>_<slug>/` (one per sub-question,
  listed with a description in that folder's `_index.csv`);
- charts as SVG in `presentation/figures/<nn>_<slug>/`.

The reading of those results is written by hand in
`presentation/README.md`, and `check_presentation.py` checks every number
in it against the CSV its sub-question links. Run a script with:

```bash
.venv/bin/python analysis/03_time_pressure.py
.venv/bin/python analysis/check_presentation.py
```

Every script follows the same shape (see `_template.py`):

1. `data = load_data()` from `chess_analysis.toolkit`: moves of all
   standard live games with the per-game view joined on (`data.moves`),
   one row per game (`data.games`), and `data.tc_groups`, the time controls
   to report.
2. Compute with the toolkit helpers: `compare_sides` (you vs opponents in
   the same games, CIs bootstrapped by game), `mean_with_ci`, `binned`,
   `per_game`, `elo_expected_score`, `add_elo`, `cluster_ols`,
   `ratio_bootstrap`. Shared machinery lives in the package, not in another
   script: `chess_analysis.clocks` (clock buckets, Q3/Q4) and
   `chess_analysis.sessions` (sessions, Q5/Q6).
3. Write with `Results(slug)`: `table(name, frame, description)` and
   `figure(fig, name)`, charts drawn with `chess_analysis.charts`; finish
   with `results.finish()`.

Rules:

- split by exact time control (`data.tc_groups`: "bullet 1+0",
  "bullet 1+1", ...), looping over the list from the data, never a typed
  list; a control needs `MIN_GROUP_GAMES` (50) games to be reported, and the
  thinner ones (`data.tc_noted`) are mentioned, not analysed;
- every table carries its counts and a `thin` flag; charts mark thin cells
  instead of drawing them as confident; where nearly every per-control cell
  is thin, add a pooled-by-time-class view as well;
- every number about you sits next to the opponents' number from the same
  games; every estimate has a confidence interval, clustered by game (or by
  session for session-level questions);
- a score vs expectation always comes with its expected score and its
  Elo-equivalent (`add_elo`), CI ends included;
- `ep_loss` (expected points lost) is the main move-quality measure,
  `cp_loss` the secondary one; never use post-game ratings as predictors
  (`my_rating_before` is fine);
- low-clock measures come in plain seconds and in `clock_buffer_seconds`
  (clock minus one increment), so 1+0 and 1+1 are comparable;
- no unexplained numbers: every threshold, bin edge and minimum count is a
  named constant with a comment saying why;
- deterministic: fixed seeds, rerunning gives byte-identical files;
- scripts write no prose. After changing results, update the matching
  section of `presentation/README.md`, rerun `check_presentation.py`, and
  update the question's notes in `PLAN.md`.

Question 11 (performance against stronger and weaker opponents) is part of
`02_trajectory.py`. `00_calibration` (the win-chance model check) is written
by `chess-analysis calibrate`, not by a script here.
