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

Set your chess.com username in `config.toml`, then:

```bash
.venv/bin/chess-analysis fetch
```

This downloads any months not already saved and writes:

- `data/<username>/raw/YYYY-MM.json`: monthly archives exactly as chess.com returns them
- `data/<username>/games.parquet`: one row per game, with every field chess.com provides
- `data/<username>/moves.parquet`: one row per move: move number, colour, move (SAN), clock reading

Running it again only downloads new games. Other options:

```bash
.venv/bin/chess-analysis fetch --username someone_else   # any player, without editing config.toml
.venv/bin/chess-analysis fetch --csv                      # also write CSV copies
.venv/bin/chess-analysis build                            # rebuild the tables without downloading
```

## In Python

```python
from chess_analysis import load_games, load_moves, my_games

games = load_games()                    # username from config.toml
moves = load_moves()
mine = my_games(games, "zakihussa1n")   # your colour, result, opponent, rating before/after each game
```

Ratings stored by chess.com are each player's rating *after* the game.
`my_games` works out your rating before each game from the previous rated
game in the same rating pool (e.g. rated blitz).

## Tests

```bash
.venv/bin/pytest -q
```
