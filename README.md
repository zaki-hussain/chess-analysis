# chess-analysis

Download every game a chess.com player has played, with each move and the
clock reading after it, into tables ready for analysis in pandas.

## Setup

Python 3.11+.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

## Usage

Set your chess.com username and timezone in `config.toml`, then:

```bash
.venv/bin/chess-analysis fetch
```

This downloads any months not already saved and writes:

- `data/<username>/raw/YYYY-MM.json`: monthly archives exactly as chess.com returns them
- `data/<username>/games.parquet`: one row per game, with every field chess.com provides
- `data/<username>/moves.parquet`: one row per move: move number, colour, move (SAN), clock reading
- `data/<username>/moves_enriched.parquet`: the moves plus the position faced (FEN), material, game phase, move kind, time spent and both clocks
- `data/<username>/my_games.parquet`: each game from your side (colour, result, opponent, rating before/after) plus local time, playing session and recent-activity columns

Running it again only downloads new games. Other options:

```bash
.venv/bin/chess-analysis fetch --username someone_else   # any player, without editing config.toml
.venv/bin/chess-analysis fetch --csv                      # also write CSV copies
.venv/bin/chess-analysis build                            # rebuild the tables without downloading
```

## In Python

```python
from chess_analysis import load_games, load_moves, load_moves_enriched, load_my_games

games = load_games()              # username from config.toml
moves = load_moves_enriched()     # or load_moves() for the plain table
mine = load_my_games()            # your colour, result, opponent, rating before/after, sessions, local time
```

See `PLAN.md` for the analyses this is building towards.

Ratings stored by chess.com are each player's rating *after* the game.
`my_games` works out your rating before each game from the previous rated
game in the same rating pool (e.g. rated blitz).

## Tests

```bash
.venv/bin/pytest -q
```
