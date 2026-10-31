# CLAUDE.md

Personal project for analysing a chess.com player's games. Downloading and
tabulating games (this package) is the first part. Analysis stages will
build on the tables it produces.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/chess-analysis fetch            # download new months, then rebuild tables
.venv/bin/chess-analysis build            # rebuild tables from saved archives (no network)
.venv/bin/chess-analysis fetch --username someone --csv
.venv/bin/pytest -q                       # offline; no network needed
```

## Layout

- `config.toml`: default `username` and `data_dir`. Committed on purpose:
  nothing in it is secret. `--username` overrides it for one run. Don't add
  `.env` files or environment variables for this.
- `src/chess_analysis/`
  - `config.py`: finds `config.toml` from the current directory upwards and validates the username.
  - `api.py`: chess.com client. One request at a time (chess.com rate-limits parallel requests). Retries 429/5xx.
  - `archive.py`: raw monthly JSON in `data/<user>/raw/YYYY-MM.json`, the source of truth. Re-downloads only new months plus the latest saved one.
  - `parse.py`: one archived game to a game row plus move rows. Stores only what chess.com gives.
  - `dataset.py`: builds and loads `games.parquet` and `moves.parquet`. `my_games()` gives a per-player view.
- `data/<user>/`: committed to the repo (the user's choice). CSV exports are gitignored.
- `tests/fixtures/games.json`: five real zakihussa1n games (live with accuracies and tournament, daily, crazyhouse, odds chess, no moves).

## Data facts (verified against real data, don't re-derive)

- `games`: one row per game. Every JSON field, flattened (`white_rating`, `accuracies_white`, ...), and every PGN header as `pgn_<snake_case>`, plus `start_datetime`/`end_datetime` (UTC) and the full `pgn`. Optional fields are present only for some games: `accuracies_*` (reviewed games), `tournament`, `match`, `start_time` (daily only), `pgn_set_up`/`pgn_fen`/`pgn_variant` (variants and odds games).
- `moves`: one row per half-move: `game_uuid, ply, move_number, color, san, clock, clock_seconds`.
- **Ratings in a game are each player's rating after that game.** Checked 100% consistent across every rating pool. A pool is `rules` + `time_class`. Rating before a game = rating after the previous rated game in the same pool (`my_games()` does this). Unrated games don't change the rating. Opponents' before-game ratings aren't available.
- **Live games: `%clk` is the time left on the mover's clock after the move**, including the increment. Time spent on a move = previous clock for that colour − clock + increment (first move: previous clock = base time). Summed, this undercounts wall-clock game length (lag, time before a resignation).
- **Daily games: `%clk` looks like time taken for that move, not time left.** Not yet confirmed; treat with care.
- Games that ended before the first move have no moves and no clocks.
- The PGN movetext only ever contains `%clk` comments. Move counts match `len(tcn) // 2` for every game.

## Conventions

- Parsing stores only what chess.com gives. Derived values (time spent, ratings before) belong in later stages or views like `my_games()`.
- Rebuilds are deterministic (sorted by `end_time`, then `uuid`), so unchanged data produces byte-identical Parquet files and no git diff. Keep it that way.
- Tests must not use the network: use fixtures or a fake client.
