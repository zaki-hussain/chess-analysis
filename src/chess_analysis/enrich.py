"""Derived columns that need no engine: board state, time use, sessions, local time.

`enrich_moves` adds, for every move of a standard-rules game, the position
the mover faced (FEN), material and phase, what kind of move it was, and
how much time it took. `enrich_games` extends `my_games()` with the time
control, local start time, playing-session position and rating-reliability
proxies. Both are rebuilt from the games/moves tables on every `build`.
"""

import chess
import pandas as pd

PIECE_VALUES = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9}
PIECE_NAMES = {chess.PAWN: "pawn", chess.KNIGHT: "knight", chess.BISHOP: "bishop", chess.ROOK: "rook", chess.QUEEN: "queen", chess.KING: "king"}

# Phase thresholds on the number of pieces other than kings and pawns (both
# sides together, 14 at the start). The endgame rule is Lichess's; the
# opening ends when some material has gone or development is over.
MIDDLEGAME_MAX_PIECES = 10
ENDGAME_MAX_PIECES = 6
OPENING_MAX_MOVE = 12

REPLAYABLE_RULES = {"chess"}


def parse_time_control(time_control: str) -> tuple[float | None, float]:
    """"180+2" -> (180, 2); "600" -> (600, 0); daily "1/604800" -> (None, 0)."""
    if "/" in time_control:
        return None, 0.0
    base, _, increment = time_control.partition("+")
    return float(base), float(increment or 0)


def phase(pieces: int, move_number: int) -> str:
    if pieces <= ENDGAME_MAX_PIECES:
        return "endgame"
    if pieces <= MIDDLEGAME_MAX_PIECES or move_number > OPENING_MAX_MOVE:
        return "middlegame"
    return "opening"


def _material(board: chess.Board, color: chess.Color) -> int:
    return sum(len(board.pieces(piece, color)) * value for piece, value in PIECE_VALUES.items())


def _piece_count(board: chess.Board) -> int:
    return sum(len(board.pieces(piece, color)) for piece in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN) for color in chess.COLORS)


def replay(initial_fen: str, sans: list[str], move_numbers: list[int]) -> list[dict]:
    """Board-state columns for each move: the position faced and the move's kind."""
    board = chess.Board(initial_fen)
    rows = []
    for san, move_number in zip(sans, move_numbers):
        move = board.parse_san(san)
        piece = board.piece_type_at(move.from_square)
        rows.append(
            {
                "fen_before": board.fen(),
                "legal_moves": board.legal_moves.count(),
                "in_check": board.is_check(),
                "material_white": _material(board, chess.WHITE),
                "material_black": _material(board, chess.BLACK),
                "pieces": _piece_count(board),
                "phase": phase(_piece_count(board), move_number),
                "piece": PIECE_NAMES[piece],
                "is_capture": board.is_capture(move),
                "is_castling": board.is_castling(move),
                "is_promotion": move.promotion is not None,
                "uci": move.uci(),
            }
        )
        board.push(move)
        rows[-1]["gives_check"] = board.is_check()
        rows[-1]["fen_after"] = board.fen()
    return rows


def _time_columns(game_moves: pd.DataFrame, base: float | None, increment: float) -> pd.DataFrame:
    """Clock before the move, time spent, and both clocks when the decision was made.

    Live games: the clock reading is the mover's time left after the move,
    including the increment. Daily games' readings mean something else, so
    their time columns are left empty.
    """
    out = pd.DataFrame(index=game_moves.index)
    if base is None:
        for column in ("clock_before_seconds", "time_spent_seconds", "clock_fraction", "opponent_clock_seconds", "clock_diff_seconds"):
            out[column] = float("nan")
        return out
    clock = game_moves["clock_seconds"]
    is_white = game_moves["color"] == "white"
    # Each side's previous reading; before their first move they had the full base time.
    before = clock.groupby(is_white).shift(1).fillna(base)
    out["clock_before_seconds"] = before
    out["time_spent_seconds"] = before - clock + increment
    out["clock_fraction"] = clock / base
    # The opponent's latest reading at decision time: their clock after their last move.
    latest_white = clock.where(is_white).ffill().shift(1).fillna(base)
    latest_black = clock.where(~is_white).ffill().shift(1).fillna(base)
    out["opponent_clock_seconds"] = latest_black.where(is_white, latest_white)
    out["clock_diff_seconds"] = before - out["opponent_clock_seconds"]
    # Clock readings have 0.1 s resolution; drop floating-point tails from the seconds columns.
    seconds = [c for c in out.columns if c.endswith("_seconds")]
    out[seconds] = out[seconds].round(3)
    return out


def enrich_moves(games: pd.DataFrame, moves: pd.DataFrame, username: str) -> pd.DataFrame:
    """The moves table plus board-state and clock columns, for standard-rules games only."""
    username = username.lower()
    columns = ["uuid", "initial_setup", "time_control", "time_class", "rules", "white_username", "black_username"]
    info = games[columns].set_index("uuid")
    info = info[info["rules"].isin(REPLAYABLE_RULES)]

    frames = []
    for uuid, game_moves in moves.groupby("game_uuid", sort=False):
        if uuid not in info.index:
            continue
        game = info.loc[uuid]
        base, increment = parse_time_control(game["time_control"])
        board_state = pd.DataFrame(replay(game["initial_setup"], list(game_moves["san"]), list(game_moves["move_number"])), index=game_moves.index)
        times = _time_columns(game_moves, base, increment)
        enriched = pd.concat([game_moves, board_state, times], axis=1)
        enriched["time_class"] = game["time_class"]
        enriched["base_seconds"] = base
        enriched["increment_seconds"] = increment
        white_is_player = game["white_username"].lower() == username
        black_is_player = game["black_username"].lower() == username
        enriched["is_player_move"] = (game_moves["color"] == "white") & white_is_player | (game_moves["color"] == "black") & black_is_player
        frames.append(enriched)

    if not frames:
        return pd.DataFrame(columns=list(moves.columns))
    return pd.concat(frames).reset_index(drop=True)


def _sessions(view: pd.DataFrame, gap_minutes: float) -> pd.DataFrame:
    """Session number, position within it, size, and minutes played since it started."""
    ordered = view.sort_values(["start_datetime", "end_datetime"], kind="stable")
    gap = ordered["start_datetime"] - ordered["end_datetime"].shift(1)
    session = (gap > pd.Timedelta(minutes=gap_minutes)).cumsum() + 1
    out = pd.DataFrame(index=ordered.index)
    out["session"] = session
    out["game_in_session"] = session.groupby(session).cumcount() + 1
    out["session_games"] = session.map(session.value_counts())
    session_start = ordered["start_datetime"].groupby(session).transform("min")
    out["minutes_into_session"] = (ordered["start_datetime"] - session_start).dt.total_seconds() / 60
    return out


def _pool_history(view: pd.DataFrame) -> pd.DataFrame:
    """How recently and how much the player had played in this rating pool before each game."""
    ordered = view.sort_values(["end_datetime", "start_datetime"], kind="stable")
    pool = ordered.groupby(["rules", "time_class"], sort=False)
    out = pd.DataFrame(index=ordered.index)
    out["days_since_last_in_pool"] = (ordered["end_datetime"] - pool["end_datetime"].shift(1)).dt.total_seconds() / 86400
    counts = []
    for _, games in pool:
        ends = games["end_datetime"]
        # Games in the same pool that ended in the 30 days before this one started.
        starts = games["start_datetime"]
        counts.append(pd.Series([int(((ends < s) & (ends >= s - pd.Timedelta(days=30))).sum()) for s in starts], index=games.index))
    out["pool_games_last_30d"] = pd.concat(counts)
    return out


def enrich_games(view: pd.DataFrame, timezone: str, session_gap_minutes: float) -> pd.DataFrame:
    """`my_games()` output plus time control, local time, sessions and pool history."""
    view = view.copy()
    parts = view["time_control"].map(parse_time_control)
    view["base_seconds"] = [b for b, _ in parts]
    view["increment_seconds"] = [i for _, i in parts]
    local = view["start_datetime"].dt.tz_convert(timezone)
    view["local_start"] = local.dt.tz_localize(None)
    view["local_hour"] = local.dt.hour
    view["weekday"] = local.dt.day_name()
    view = view.join(_sessions(view, session_gap_minutes)).join(_pool_history(view))
    return view.sort_values(["end_datetime", "uuid"], kind="stable").reset_index(drop=True)
