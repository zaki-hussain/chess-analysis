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
.venv/bin/pytest -q                       # offline; no network needed
```

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
- `data/<user>/`: committed to the repo (the user's choice). CSV exports are gitignored.
- `tests/fixtures/games.json`: five real zakihussa1n games (live with accuracies and tournament, daily, crazyhouse, odds chess, no moves).

## Data facts (verified against real data, don't re-derive)

- `games`: one row per game. Every JSON field, flattened (`white_rating`, `accuracies_white`, ...), and every PGN header as `pgn_<snake_case>`, plus `start_datetime`/`end_datetime` (UTC) and the full `pgn`. Optional fields are present only for some games: `accuracies_*` (reviewed games), `tournament`, `match`, `start_time` (daily only), `pgn_set_up`/`pgn_fen`/`pgn_variant` (variants and odds games).
- `moves`: one row per half-move: `game_uuid, ply, move_number, color, san, clock, clock_seconds`.
- `moves_enriched`: `moves` plus `fen_before` (position the mover faced), `legal_moves`, `in_check`, `material_white/black` (1/3/3/5/9), `pieces` (non-pawn, non-king pieces), `phase`, `piece`, `is_capture`, `is_castling`, `is_promotion`, `uci`, `gives_check`, `clock_before_seconds`, `time_spent_seconds`, `clock_fraction`, `opponent_clock_seconds`, `clock_diff_seconds`, `time_class`, `base_seconds`, `increment_seconds`, `is_player_move`. Standard-rules games only (34 variant games excluded); daily games have no time columns.
- `my_games`: `my_games()` plus `base_seconds`, `increment_seconds`, `local_start` (naive, in the configured timezone), `local_hour`, `weekday`, `session`, `game_in_session`, `session_games`, `minutes_into_session`, `days_since_last_in_pool`, `pool_games_last_30d`.
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
- Variant games (`rules != "chess"`) are excluded from enrichment and will be excluded from engine analysis.
