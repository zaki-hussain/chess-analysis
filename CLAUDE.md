# CLAUDE.md

Personal project for analysing a chess.com player's games. Downloading and
tabulating games is the first part; analysis stages build on the tables it
produces. `PLAN.md` lists the questions we want to answer, their status and
notes: update it when plans change or a step finishes.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/chess-analysis fetch            # download new months, then rebuild tables
.venv/bin/chess-analysis build            # rebuild tables from saved archives (no network)
.venv/bin/chess-analysis fetch --username someone --csv
.venv/bin/chess-analysis analyse          # Stockfish on every position; resumable; 1M nodes default
.venv/bin/chess-analysis analyse --limit 300 --workers 4   # quick test run
.venv/bin/chess-analysis tablebase        # exact results for <=5-piece positions (needs syzygy_dir)
.venv/bin/chess-analysis calibrate        # win-probability model -> expected_points.parquet + reports/calibration.md (~3 min)
.venv/bin/pytest -q                       # offline; one test uses stockfish, one the tablebases, if present
```

Pipeline order: fetch/build → analyse → tablebase → build (adds tb_* columns) → calibrate. `build` is deterministic; `calibrate` is too (single-threaded fits, fixed seed).

Stockfish 17.1 is at /opt/stockfish/stockfish (symlinked to /usr/local/bin/stockfish) and the Syzygy 3-4-5 tables (939 MB, from tablebase.lichess.ovh) in /opt/syzygy in this container; neither is in the repo. `scripts/ec2-setup.sh` installs Stockfish elsewhere.

## Layout

- `config.toml`: default `username`, `data_dir`, `timezone` (for local time of
  day) and `session_gap_minutes`. Committed on purpose: nothing in it is
  secret. `--username` overrides it for one run. Don't add `.env` files or
  environment variables for this.
- `src/chess_analysis/`
  - `config.py`: finds `config.toml` from the current directory upwards and validates the username.
  - `api.py`: chess.com client. One request at a time (chess.com rate-limits parallel requests). Retries 429/5xx.
  - `archive.py`: raw monthly JSON in `data/<user>/raw/YYYY-MM.json`, the source of truth. Re-downloads only new months plus the latest saved one.
  - `parse.py`: one archived game to a game row plus move rows. Stores only what chess.com gives.
  - `dataset.py`: builds and loads the tables. `my_games()` gives a per-player view.
  - `enrich.py`: engine-free derived columns: board state via python-chess, time spent, sessions, local time.
  - `engine.py`: Stockfish evaluation of every distinct position into `evals.parquet`; `with_evals()` joins eval before/after onto moves.
  - `analysed.py`: move quality from the mover's view (`moves_analysed.parquet`), including tablebase columns when available.
  - `openings.py`: opening names and book depth from `data/openings/*.tsv` (Lichess chess-openings, CC0).
  - `tablebase.py`: Syzygy probes for every <=5-piece position into `tablebase.parquet`.
  - `calibrate.py`: win-probability model on the player's own games; writes `expected_points.parquet` and `reports/calibration.md`.
- `reports/`: written analysis results (markdown, committed).
- `scripts/ec2-setup.sh`: prepares an Ubuntu EC2 instance (Python, Stockfish, venv) for the full `analyse` run. `scripts/ec2-run.sh` runs it and powers the instance off afterwards (plus a 6-hour safety-net shutdown).
- `data/<user>/`: committed to the repo (the user's choice). CSV exports are gitignored.
- `tests/fixtures/games.json`: five real zakihussa1n games (live with accuracies and tournament, daily, crazyhouse, odds chess, no moves).

## Data facts (verified against real data, don't re-derive)

- `games`: one row per game. Every JSON field, flattened (`white_rating`, `accuracies_white`, ...), and every PGN header as `pgn_<snake_case>`, plus `start_datetime`/`end_datetime` (UTC) and the full `pgn`. Optional fields are present only for some games: `accuracies_*` (reviewed games), `tournament`, `match`, `start_time` (daily only), `pgn_set_up`/`pgn_fen`/`pgn_variant` (variants and odds games).
- `moves`: one row per half-move: `game_uuid, ply, move_number, color, san, clock, clock_seconds`.
- `moves_enriched`: `moves` plus `fen_before` (position the mover faced), `fen_after`, `legal_moves`, `in_check`, `material_white/black` (1/3/3/5/9), `pieces` (non-pawn, non-king pieces), `phase`, `piece`, `is_capture`, `is_castling`, `is_promotion`, `uci`, `gives_check`, `clock_before_seconds`, `time_spent_seconds`, `clock_fraction`, `opponent_clock_seconds`, `clock_diff_seconds`, `time_class`, `base_seconds`, `increment_seconds`, `is_player_move`. Standard-rules games only (34 variant games excluded); daily games have no time columns.
- `evals`: one row per distinct position (`position` = FEN without move counters; 316,076 for the current data, all evaluated at 1M nodes with Stockfish 17.1), from White's view: `score_cp`, `mate_in` (0 with `terminal=True` means the side to move is checkmated; WDL says who won), `wdl_win/draw/loss` (per mille, Stockfish's model), `best_move`, `second_move`, `second_score_cp`, `second_mate_in` (MultiPV 2), `depth`, `seldepth`, `nodes`, `engine`, `nodes_limit`. Fixed nodes + 1 thread + hash cleared per position: re-evaluation is bit-identical (checked). Rows at a different `nodes_limit` are re-evaluated and replaced.
- `my_games`: `my_games()` plus `base_seconds`, `increment_seconds`, `local_start` (naive, in the configured timezone), `local_hour`, `weekday`, `session`, `game_in_session`, `session_games`, `minutes_into_session`, `days_since_last_in_pool`, `pool_games_last_30d`, `opening_eco`, `opening_name`, `book_ply` (deepest named line reached; median 4 plies).
- `moves_analysed` (derived columns keyed by `game_uuid, ply`; `load_analysed()` joins everything): mover's view `cp_before/cp_after` (mates mapped to ±10000 minus plies; Stockfish's ±20000 "decisive" scores clipped), `mate_before/after`, `cp_loss` (on evals clipped to ±1000, floored at 0), `ep_sf_*` (Stockfish WDL expected points), `best_move`, `second_move`, `best_cp`, `second_cp`, `criticality` (best − second-best, NaN with one legal move), `is_best_move`, `is_second_move`, `missed_mate`, `allowed_mate`, `label` (good <50 / inaccuracy <100 / mistake <300 / blunder), `tb_wdl_before/after` (mover's view, 2 win … −2 loss), `tb_dtz_*`, `tb_error` (win→draw/loss or draw→loss by tablebase). 9,940 moves are in tablebase range; the tablebase and the engine never disagree on decisive vs drawn.
- `expected_points` (from `calibrate`, joined by `load_analysed()`): `ep_before/ep_after/ep_loss` (calibrated expected points from the mover's view), `ep_criticality`, `p_win/p_draw/p_loss` before and after. Model: gradient boosting on eval, phase, ply, material, both clocks, time control and **pre-game** ratings (post-game ratings leak the result: using them cut MAE from 0.37 to 0.29 falsely). 5-fold CV by game: Brier 0.179 vs 0.216 (Lichess cp curve) and 0.267 (Stockfish WDL, over-confident at the extremes). Mean ep_loss per move: 0.030, identical for the player and opponents.
- `moves_enriched` also has `in_book` (position after the move is a named opening line; 7.5% of moves).
- Replaying every standard game with python-chess reproduces chess.com's final `fen` piece placement for all 6,047 games (differences only in en-passant notation and four games where chess.com kept a castling right after the rook was captured). Replay of everything takes ~80 s inside `build`.
- **Ratings in a game are each player's rating after that game.** Checked 100% consistent across every rating pool. A pool is `rules` + `time_class`. Rating before a game = rating after the previous rated game in the same pool (`my_games()` does this). Unrated games don't change the rating. Opponents' before-game ratings aren't available.
- **Live games: `%clk` is the time left on the mover's clock after the move**, including the increment. Time spent on a move = previous clock for that colour − clock + increment (first move: previous clock = base time). Summed, this undercounts wall-clock game length (lag, time before a resignation).
- **Daily games: `%clk` looks like time taken for that move, not time left.** Not yet confirmed; treat with care.
- Games that ended before the first move have no moves and no clocks.
- The PGN movetext only ever contains `%clk` comments. Move counts match `len(tcn) // 2` for every game.

## Conventions

- Parsing stores only what chess.com gives. Derived values (time spent, ratings before) belong in later stages or views like `my_games()`.
- Rebuilds are deterministic (sorted by `end_time`, then `uuid`), so unchanged data produces byte-identical Parquet files and no git diff. Keep it that way.
- Tests must not use the network: use fixtures or a fake client.
- Variant games (`rules != "chess"`) are excluded from enrichment and engine analysis.
- Engine workers are plain `multiprocessing` processes (spawn context) that quit their engine themselves before exiting. Don't switch to `ProcessPoolExecutor`: python-chess's engine thread is non-daemon and a 3.13 child waits for threads before any exit hook runs, so pool workers hang forever. Forked workers can also deadlock on inherited pandas/pyarrow locks.
- Benchmarks (1 thread, 2.1 GHz Xeon): 1M nodes ≈ 1.07 s/position; 4 workers here ≈ 4.2 positions/s.
- Analyses should use `ep_loss` (calibrated expected points) as the main move-quality measure and `cp_loss` as the familiar secondary one; never post-game ratings as predictors.
