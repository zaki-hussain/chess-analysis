"""Q9: Blunder anatomy, what kinds of mistakes you make and miss.

Question: how often do you blunder, where (phase, clock, winning positions),
what kind of blunder is it (hung material, allowed mate, walked into a tactic,
a quiet refutation), which pieces do you hang, and do you miss what is on
offer (free material) or what is coming (the opponent's threat)? Always next
to the opponents in the same games.

A blunder is a move with `cp_loss` >= 300 (the project's `label`). Every
blunder is classified by replaying the position with python-chess: the
played move, the engine's best move and the engine's best reply (the stored
best move of the next position), with a small static exchange evaluation
(SEE) to decide whether material was "free". No engine is run.

Computes, per exact time control (`data.tc_groups`), you vs your opponents
in the same games, with 95% CIs from a bootstrap over games (differences are
paired: both sides on the same resampled games):
1. blunders per 100 moves; their cost in expected points; the share of all
   expected points lost that came from blunders; the year trend (per time
   class only: the mix of controls changed from year to year, so a control x
   year split is mostly thin cells and confounds the two);
2. blunders per 100 moves by phase, and each phase's share of the blunders;
3. the taxonomy of what the blunder did (rates and shares), what is in the
   residual "quiet / deeper" class, refutation depth along the game, the
   taxonomy under an expected-points definition, the most common patterns;
4. the piece hung (per 100 games, also pooled by time class) and the blunder
   rate per 100 moves of each piece;
5. blunders that allowed mate per 100 moves, overall and within bands of the
   evaluation before the move (the correction for how often each side is
   losing), mate in 1 vs longer, and forced mates that slipped;
6. what the best move was (free material, saving a threatened piece, a quiet
   move, ...) and how often each side punished the other's blunders;
7. extra blunders per 100 moves at low clock from a regression that holds
   phase, evaluation and move number fixed (you, opponents, paired
   difference), in plain seconds left and in seconds beyond one increment
   (the buffer), also pooled per time class with a fixed effect per control;
   plus the raw rates by clock bucket, also as a share of the base time;
8. blunders from a winning position, wins given away, their cost, and how the
   games ended.

Writes CSVs to presentation/data/09_blunder_anatomy/ and SVGs to
presentation/figures/09_blunder_anatomy/.

Run: .venv/bin/python analysis/09_blunder_anatomy.py  (about 2.5 minutes, of
which ~45 s is the python-chess replay of ~86,000 error positions).
"""

from __future__ import annotations

import time

import chess
import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis.toolkit import MIN_GROUP_GAMES, Results, cluster_ols, compare_sides, dummies, load_data

SLUG = "09_blunder_anatomy"
SEED = 9
N_BOOT = 500  # game-bootstrap resamples per cell: pins a 95% CI end to about +-4% of its width
N_REG_BOOT = 200  # resamples for the clock regression (two sides x ~17 coefficients, 2 x 12 fits); enough for CIs quoted to one decimal
BLUNDER = "blunder"  # the project's label for cp_loss >= 300
REPLAY_EP = 0.1  # moves replayed: every mistake or blunder, every mate flag, and any move losing a tenth of a point (a superset of both definitions)
EP_BIG = 0.2  # alternative, expected-points definition of a big error: a fifth of a point given away in one move
# Rate cells (per 100 moves) need this many moves on each side: a blunder rate near 6 per 100 from 300 moves
# has a 95% CI of about +-2.7 per 100, already half the rate; below that a cell is flagged thin.
MIN_CELL_MOVES = 300
# Rates of a single kind of blunder (one taxonomy category, allowed mate) need this many moves per side: a kind
# seen ~0.5 times per 100 moves is expected only 10 times in 2,000 moves, and its CI is then as wide as the rate.
MIN_RARE_MOVES = 2000
# A composition (shares of blunders across 8-9 categories) needs this many blunders per side: from 100
# blunders a 15% share has a 95% CI of about +-7 percentage points.
MIN_BLUNDERS = 100
# Per-100-games rates of hanging a given piece need this many games: a piece hung ~12 times per 100 games is
# seen ~24 times in 200 games, and its CI already spans about +-40% of the rate.
MIN_GAMES_PIECES = 200
# Shares among positions with a forced mate need this many positions per side (a 15% share from 100 has a CI
# of about +-7 points).
MIN_MATE_CASES = 100
# Shares and scores over games (8c) need this many games per side.
MIN_CASE_GAMES = 50
# Evaluation bands, mover's view, before the move. +-300 cp is the blunder threshold itself: from a 300 cp
# lead one blunder takes you back to level, which is what "winning" has to survive. (At this level a +300
# position is still far from won: the calibrated model gives it about 0.58 expected points.)
WINNING_CP = 300
EVAL_EDGES = [-np.inf, -WINNING_CP, WINNING_CP, np.inf]
EVAL_LABELS = ["losing", "balanced", "winning"]
ALL_MOVES = "all moves"
WIN_SPLIT_CP = 2 * WINNING_CP  # a lead of 600 cp is still winning after one blunder-sized loss; 300-600 is not
ALL_WINNING = "all winning"
GAVE_AWAY = f"wins given away (blunder ending below +{WINNING_CP})"
# Finer evaluation bands used only as controls in the clock regression: +-100 cp (a pawn) splits level
# positions from slightly better or worse ones.
LEVEL_CP = 100
CONTROL_EVAL_EDGES = [-np.inf, -WINNING_CP, -LEVEL_CP, LEVEL_CP, WINNING_CP, np.inf]
# Move-number bands for the same regression: 20 plies (10 moves each) follow the game's arc; 100+ is one band.
PLY_EDGES = [0, 20, 40, 60, 80, 100, np.inf]
# Clock buckets, seconds left before the move (or seconds beyond one increment). Under 5 s is a pure scramble
# in every control; 10 s the classic "low clock" line; 30 s still leaves a few seconds a move for a bullet
# ending; 60/120/300 s split the comfortable range so blitz and rapid have their own low end. Same edges as
# Q3, so the two questions line up. The lowest edge is -inf because the buffer can be slightly negative.
CLOCK_SECONDS_EDGES = [-np.inf, 5, 10, 20, 30, 60, 120, 300, np.inf]
CLOCK_SECONDS_LABELS = ["<5 s", "5-10 s", "10-20 s", "20-30 s", "30-60 s", "1-2 min", "2-5 min", "5+ min"]
# Clock left as a share of the base time (above 1 with increments): comparable across controls.
CLOCK_FRACTION_EDGES = [-np.inf, 0.05, 0.10, 0.20, 0.35, 0.50, 0.75, 1.0, np.inf]
CLOCK_FRACTION_LABELS = ["<5%", "5-10%", "10-20%", "20-35%", "35-50%", "50-75%", "75-100%", ">100%"]
# Coarse buckets for the regression; "30+ s" is the reference (comfortable in bullet, normal in blitz).
CLOCK_COARSE_EDGES = [-np.inf, 5, 10, 30, np.inf]
CLOCK_COARSE_LABELS = ["<5 s", "5-10 s", "10-30 s", "30+ s"]
CLOCK_REFERENCE = "30+ s"
# The chart uses the buffer: in increment games the increment comes back every move, so seconds beyond it are
# the time really left to spend; without increment the two measures are identical. Both are in the table.
CHART_CLOCK_MEASURE = "seconds beyond increment"
CLOCK_MEASURES = {
    "seconds left": "clock_before_seconds",
    "seconds beyond increment": "clock_buffer_seconds",
    "share of base time": "clock_share_of_base",
}
# Years with fewer games than this are merged into the neighbouring year (2021 and 2026 have a handful each).
MIN_YEAR_GAMES = 100
TOP_PATTERNS = 10  # most common (what the move did x what was missed) pairs listed per time class
REFUTATION_PLIES = 3  # refutation depth is followed for up to three moves of the punishing side
PUNISHED_CP = 100  # a reply "punished" a blunder when it lost less than a pawn of the gift (kept most of it)

# Static exchange evaluation thresholds, in pawns (1/3/3/5/9). A capture "wins material" when the swap-off
# nets at least a pawn; a reply that only wins material in a longer swap still counts as hung when the piece
# left en prise is worth at least a minor piece, since losing a knight for nothing is a blunder on its own.
MIN_GAIN_PAWNS = 1
FREE_PIECE_PAWNS = 3

PHASES = ["opening", "middlegame", "endgame"]
SIDES = ["player", "opponent"]
PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_ORDER = ["pawn", "knight", "bishop", "rook", "queen", "king"]
HUNG_PIECES = PIECE_ORDER[:5]

# Taxonomy. WHAT_PLAYED: what the played move did (why it was bad), in order of precedence.
WHAT_PLAYED = [
    "allowed mate in 1",
    "allowed mate (longer)",
    "losing capture",
    "moved piece en prise",
    "ignored a threatened piece",
    "exposed another piece",
    "allowed a check tactic",
    "allowed a combination",
    "quiet refutation / deeper",
]
HUNG_CATEGORIES = WHAT_PLAYED[2:6]
MATE_CATEGORIES = WHAT_PLAYED[:2]
FAMILIES = {
    "allowed mate": MATE_CATEGORIES,
    "hung material": HUNG_CATEGORIES,
    "walked into a tactic": ["allowed a check tactic", "allowed a combination"],
    "quiet refutation / deeper": ["quiet refutation / deeper"],
}
# WHAT_MISSED: what the engine's best move was (what was missed).
WHAT_MISSED = [
    "mate in 1",
    "mate (longer)",
    "free material",
    "saving a threatened piece",
    "capture (even or sacrifice)",
    "check",
    "in check: another reply",
    "quiet move",
]
# The 9.6 chart: what was on offer (free material) vs what was coming (a threat to parry, a quiet move).
MISSED_CHART = {"free material": "free capture", "saving a threatened piece": "save a piece", "quiet move": "quiet move"}
DEPTHS = ["immediate", "within 2-3 moves", "mate within 3 moves", "not within 3 moves", "line left / unknown"]
RESOLVED = DEPTHS[:4]
TAXONOMY_COLOURS = {
    "allowed mate in 1": "#e34948",
    "allowed mate (longer)": "#e87ba4",
    "losing capture": "#eda100",
    "moved piece en prise": "#eb6834",
    "ignored a threatened piece": "#6250d6",
    "exposed another piece": "#2a78d6",
    "allowed a check tactic": "#1baf7a",
    "allowed a combination": "#008300",
    "quiet refutation / deeper": "#c3c2b7",
}
RATE_COLS = ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"]


# --- static exchange evaluation (coarse, ignores promotions beyond a queen) -------------------


def _least_valuable_legal_attacker(board: chess.Board, square: int):
    best = None
    for frm in board.attackers(board.turn, square):
        piece_type = board.piece_type_at(frm)
        promotion = chess.QUEEN if piece_type == chess.PAWN and chess.square_rank(square) in (0, 7) else None
        move = chess.Move(frm, square, promotion=promotion)
        if not board.is_legal(move):
            continue
        if best is None:
            best = (move, piece_type)
        elif piece_type != chess.KING and (best[1] == chess.KING or PIECE_VALUE[piece_type] < PIECE_VALUE[best[1]]):
            best = (move, piece_type)
    return best


def see_square(board: chess.Board, square: int) -> int:
    """Material the side to move wins by capturing on `square` (0 if it should not capture)."""
    victim = board.piece_type_at(square)
    if victim is None:
        return 0
    attacker = _least_valuable_legal_attacker(board, square)
    if attacker is None:
        return 0
    move, _ = attacker
    board.push(move)
    gain = PIECE_VALUE[victim] - see_square(board, square)
    board.pop()
    return max(0, gain)


def see_capture(board: chess.Board, move: chess.Move) -> int:
    """Net material the mover gains with this capture after the swap-off (negative = losing capture)."""
    victim = 1 if board.is_en_passant(move) else PIECE_VALUE[board.piece_type_at(move.to_square)]
    board.push(move)
    gain = victim - see_square(board, move.to_square)
    board.pop()
    return gain


def victim_name(board: chess.Board, move: chess.Move) -> str | None:
    if not board.is_capture(move):
        return None
    return "pawn" if board.is_en_passant(move) else chess.piece_name(board.piece_type_at(move.to_square))


def best_threat(board: chess.Board) -> tuple[int, int | None, str | None]:
    """Largest SEE gain available to the side to move, with the victim's square and name."""
    best, square, victim = 0, None, None
    for move in board.legal_moves:
        if board.is_capture(move):
            gain = see_capture(board, move)
            if gain > best:
                best, square, victim = gain, move.to_square, victim_name(board, move)
    return best, square, victim


def threat_against_mover(board: chess.Board) -> tuple[int, int | None]:
    """What the opponent could win if it were their move (null move); 0 if the mover is in check."""
    if board.is_check():
        return 0, None
    board.push(chess.Move.null())
    gain, square, _ = best_threat(board)
    board.pop()
    return gain, square


def classify_position(fen: str, uci: str, best: str | None, reply: str | None) -> dict:
    """Rule-based features of one move: the position, the played move, the best move, the best reply."""
    board = chess.Board(fen)
    played = chess.Move.from_uci(uci)
    out: dict = {"mover_in_check": board.is_check()}
    threat_gain, threat_square = threat_against_mover(board)
    out["threat_before"] = threat_gain
    out["played_capture"] = board.is_capture(played)
    out["played_see"] = see_capture(board, played) if out["played_capture"] else np.nan

    best_move = chess.Move.from_uci(best) if isinstance(best, str) else None
    if best_move is not None and best_move in board.legal_moves:
        out["best_capture"] = board.is_capture(best_move)
        out["best_see"] = see_capture(board, best_move) if out["best_capture"] else np.nan
        out["best_check"] = board.gives_check(best_move)
        out["best_victim"] = victim_name(board, best_move)
        board.push(best_move)
        out["threat_after_best"] = best_threat(board)[0]
        board.pop()
    else:
        out.update(best_capture=False, best_see=np.nan, best_check=False, best_victim=None, threat_after_best=np.nan)

    board.push(played)
    hung_gain, hung_square, hung_victim = best_threat(board)
    out["hung_gain"] = hung_gain
    out["hung_victim"] = hung_victim
    out["hung_is_moved"] = hung_square == played.to_square if hung_square is not None else False
    out["hung_was_threatened"] = hung_square == threat_square if hung_square is not None else False
    reply_move = chess.Move.from_uci(reply) if isinstance(reply, str) else None
    if reply_move is not None and reply_move in board.legal_moves:
        out["reply_known"] = True
        out["reply_capture"] = board.is_capture(reply_move)
        out["reply_see"] = see_capture(board, reply_move) if out["reply_capture"] else np.nan
        out["reply_check"] = board.gives_check(reply_move)
        out["reply_victim"] = victim_name(board, reply_move)
        out["reply_to_moved"] = reply_move.to_square == played.to_square
    else:
        out.update(reply_known=False, reply_capture=False, reply_see=np.nan, reply_check=False, reply_victim=None, reply_to_moved=False)
    return out


def assign_taxonomy(frame: pd.DataFrame) -> pd.DataFrame:
    """Category assignment from the replay features plus the stored evals.

    What the move did: allowed mate (in 1 / longer); hung material (the best
    reply wins material by SEE, or a piece worth >= 3 is free), split into
    losing capture / moved piece en prise / ignored a threatened piece /
    exposed another piece; allowed a check tactic (best reply is a check);
    allowed a combination (best reply is a capture that does not win
    material by SEE); otherwise a quiet refutation or something deeper.
    What was missed: a mate; free material (a capture with positive SEE);
    saving a threatened piece; another capture; a check; another reply to
    check; a quiet move.
    """
    f = frame
    allowed = f["allowed_mate"].fillna(False).astype(bool)
    mate_after = f["mate_after"].astype("float64")
    reply_wins = f["reply_capture"].astype(bool) & (f["reply_see"].fillna(0) >= MIN_GAIN_PAWNS)
    material_lost = reply_wins | (f["hung_gain"] >= FREE_PIECE_PAWNS)
    losing_capture = f["played_capture"].astype(bool) & (f["played_see"].fillna(0) < 0)
    moved_en_prise = f["hung_is_moved"].astype(bool) | f["reply_to_moved"].astype(bool)
    ignored = f["hung_was_threatened"].astype(bool) & (f["threat_before"] >= MIN_GAIN_PAWNS)
    reply_check = f["reply_check"].astype(bool)
    reply_capture = f["reply_capture"].astype(bool)
    played = np.select(
        [
            allowed & (mate_after == -1),
            allowed,
            material_lost & losing_capture,
            material_lost & moved_en_prise,
            material_lost & ignored,
            material_lost,
            reply_check,
            reply_capture,
        ],
        WHAT_PLAYED[:8],
        default=WHAT_PLAYED[8],
    )
    gain = np.where(reply_wins, f["reply_see"].fillna(0), f["hung_gain"])
    victim = np.where(reply_wins, f["reply_victim"], f["hung_victim"])

    missed_mate = f["missed_mate"].fillna(False).astype(bool)
    mate_before = f["mate_before"].astype("float64")
    mate_available = missed_mate | (mate_before > 0)
    free_material = f["best_capture"].astype(bool) & (f["best_see"].fillna(0) >= MIN_GAIN_PAWNS)
    # an unknown best move (NaN threat after it) never counts as parrying the threat
    defensive = (f["threat_before"] >= MIN_GAIN_PAWNS) & (f["threat_after_best"].fillna(np.inf) < f["threat_before"])
    missed = np.select(
        [
            mate_available & (mate_before == 1),
            mate_available,
            free_material,
            defensive,
            f["best_capture"].astype(bool),
            f["best_check"].astype(bool),
            f["mover_in_check"].astype(bool),
        ],
        WHAT_MISSED[:7],
        default=WHAT_MISSED[7],
    )
    out = f.copy()
    out["what_played"] = pd.Categorical(played, categories=WHAT_PLAYED)
    out["what_missed"] = pd.Categorical(missed, categories=WHAT_MISSED)
    out["material_given"] = gain.astype(float)
    out["victim"] = pd.Categorical(victim, categories=PIECE_ORDER)
    return out


def material_balance(board: chess.Board) -> int:
    total = 0
    for piece_type, value in PIECE_VALUE.items():
        total += value * (len(board.pieces(piece_type, chess.WHITE)) - len(board.pieces(piece_type, chess.BLACK)))
    return total


def refutation_depth(moves: pd.DataFrame, blunders: pd.DataFrame) -> pd.Series:
    """Depth of the refutation along the game, while the punishing side played the engine's best move.

    Material is compared after each pair of moves (the punisher's reply and the blunderer's
    answer), so an even trade does not count as material lost; mate ends the search.
    """
    keys = list(zip(moves["game_uuid"], moves["ply"]))
    balance = dict(zip(keys, (moves["material_white"] - moves["material_black"]).to_numpy()))  # at fen_before
    is_best = dict(zip(keys, moves["is_best_move"].to_numpy()))
    fen_after = dict(zip(keys, moves["fen_after"].to_numpy()))

    def balance_after(game: str, ply: int) -> tuple[float, bool]:
        nxt = (game, ply + 1)
        if nxt in balance:
            return balance[nxt], False
        board = chess.Board(fen_after[(game, ply)])
        return material_balance(board), board.is_checkmate()

    result = []
    for game, ply, color in zip(blunders["game_uuid"], blunders["ply"], blunders["color"]):
        sign = 1 if color == "white" else -1
        base, mate = balance_after(game, ply)
        base *= sign
        if mate:
            result.append("not within 3 moves")
            continue
        depth = "not within 3 moves"
        for k in range(1, REFUTATION_PLIES + 1):
            reply = (game, ply + 2 * k - 1)
            if reply not in is_best or not bool(is_best[reply]):
                depth = "line left / unknown"
                break
            after_reply, mate = balance_after(game, reply[1])
            if mate:
                depth = "mate within 3 moves"
                break
            own = (game, reply[1] + 1)
            if own in is_best:
                after_own, _ = balance_after(game, own[1])
                net = sign * after_own
            else:  # the blunderer did not move again (resigned, flagged): judge by the reply alone
                net = sign * after_reply
            if net <= base - 1:
                depth = "immediate" if k == 1 else "within 2-3 moves"
                break
            if own not in is_best:
                depth = "line left / unknown"
                break
        result.append(depth)
    return pd.Series(result, index=blunders.index)


# --- table helpers ----------------------------------------------------------------------------


def side_counts(frame: pd.DataFrame, column: str, by: list[str], name: str) -> pd.DataFrame:
    """Non-missing rows of `column` per `by` cell and side, as n_player_<name> / n_opponent_<name>."""
    counts = frame.groupby([*by, "side"], observed=True)[column].count().unstack("side", fill_value=0)
    counts = counts.reindex(columns=SIDES, fill_value=0).rename(columns={s: f"n_{s}_{name}" for s in SIDES})
    counts.columns.name = None
    return counts.reset_index()


def side_rate(frame: pd.DataFrame, column: str, by: list[str], scale: float = 100.0, unit: str = "moves", min_n: int = MIN_CELL_MOVES, unit_text: str | None = None) -> pd.DataFrame:
    """compare_sides on `column` scaled by `scale` (per 100 moves for 0/1 indicators), with per-side counts, `thin` and a `unit` label."""
    table = compare_sides(frame, column, by=by, n_boot=N_BOOT).drop(columns="n")
    table[RATE_COLS] = table[RATE_COLS] * scale
    table = table.merge(side_counts(frame, column, by, unit), on=by, how="left")
    table["thin"] = (table[f"n_player_{unit}"] < min_n) | (table[f"n_opponent_{unit}"] < min_n)
    table["unit"] = unit_text or (f"per {scale:g} {unit}" if scale != 1 else f"share of {unit}")
    return table


def blunder_counts(blunders: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Blunders per side in each `by` cell (blunders_player / blunders_opponent)."""
    counts = blunders.groupby([*by, "side"], observed=True).size().unstack("side", fill_value=0)
    counts = counts.reindex(columns=SIDES, fill_value=0).rename(columns={s: f"blunders_{s}" for s in SIDES})
    counts.columns.name = None
    return counts.reset_index()


def shares(blunders: pd.DataFrame, column: str, by: list[str]) -> pd.DataFrame:
    """Share of each side's blunders in each level of `column`, within `by` (share_player / share_opponent)."""
    counts = blunders.groupby([*by, column, "side"], observed=True).size().unstack("side", fill_value=0)
    counts = counts.reindex(columns=SIDES, fill_value=0)
    totals = counts.groupby(level=by, observed=True).transform("sum")
    out = (counts / totals.where(totals > 0)).rename(columns={s: f"share_{s}" for s in SIDES})
    out = out.join(counts.rename(columns={s: f"count_{s}" for s in SIDES}))
    out.columns.name = None
    return out.reset_index()


def paired_ratio(frame: pd.DataFrame, num: str, den: str, by: list[str], seed: int = SEED) -> pd.DataFrame:
    """Ratio of sums sum(num)/sum(den) for each side, with the paired difference; CIs resample whole games."""
    rows = []
    for key, part in frame.groupby(by, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        agg = part.groupby(["game_uuid", "is_player_move"])[[num, den]].sum().unstack("is_player_move", fill_value=0)
        nums = agg[num].reindex(columns=[True, False], fill_value=0)
        dens = agg[den].reindex(columns=[True, False], fill_value=0)
        sums = {side: (nums[flag].to_numpy(float), dens[flag].to_numpy(float)) for side, flag in [("player", True), ("opponent", False)]}
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(agg), size=(N_BOOT, len(agg)))
        row = dict(zip(by, key))
        boots = {}
        for side, (n, d) in sums.items():
            est = n.sum() / d.sum() if d.sum() else np.nan
            with np.errstate(invalid="ignore", divide="ignore"):
                boots[side] = n[idx].sum(1) / d[idx].sum(1)
            row[side] = est
            row[f"{side}_lo"], row[f"{side}_hi"] = np.nanquantile(boots[side], [0.025, 0.975])
        row["diff"] = row["player"] - row["opponent"]
        row["diff_lo"], row["diff_hi"] = np.nanquantile(boots["player"] - boots["opponent"], [0.025, 0.975])
        row["games"] = int(part["game_uuid"].nunique())
        rows.append(row)
    return pd.DataFrame(rows)


def pooled_by_class(frame: pd.DataFrame, class_of: dict[str, str], order: list[str]) -> pd.DataFrame:
    """The same rows relabelled 'all <time class>' (tc_group categorical with the full group order)."""
    out = frame.copy()
    out["tc_group"] = pd.Categorical(["all " + class_of[str(g)] for g in out["tc_group"]], categories=order)
    return out


def with_pooled(frame: pd.DataFrame, class_of: dict[str, str], order: list[str]) -> pd.DataFrame:
    return pd.concat([frame, pooled_by_class(frame, class_of, order)], ignore_index=True)


def band_labels(edges: list[float]) -> list[str]:
    """Labels like '<-300', '-300..-100', '>=300' for left-closed bins with infinite outer edges."""
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        if np.isinf(lo):
            out.append(f"<{hi:g}")
        elif np.isinf(hi):
            out.append(f">={lo:g}")
        else:
            out.append(f"{lo:g}..{hi:g}")
    return out


def year_blocks(years: pd.Series) -> dict[int, str]:
    """Calendar years with fewer than MIN_YEAR_GAMES games merged into the neighbouring year; labels like '2021-22'."""
    counts = years.value_counts().sort_index()
    blocks = [[int(y)] for y in counts.index]
    while len(blocks) > 1:
        sizes = [int(counts.loc[b].sum()) for b in blocks]
        thin = [i for i, s in enumerate(sizes) if s < MIN_YEAR_GAMES]
        if not thin:
            break
        i = thin[0]
        j = i + 1 if i == 0 else i - 1
        lo, hi = sorted((i, j))
        blocks[lo:hi + 1] = [blocks[lo] + blocks[hi]]
    labels = {}
    for b in blocks:
        label = str(b[0]) if len(b) == 1 else f"{b[0]}-{str(b[-1])[2:]}"
        for y in b:
            labels[y] = label
    return labels


# Display order of every level column the tables sort by; tc_group's order is filled in by main() from the data.
LEVEL_ORDERS: dict[str, list[str]] = {
    "phase": PHASES,
    "eval_band": [ALL_MOVES, *EVAL_LABELS],
    "piece": PIECE_ORDER,
    "depth": DEPTHS,
    "category": list(dict.fromkeys([*WHAT_PLAYED, *WHAT_MISSED])),
    "family": list(FAMILIES),
    "side": SIDES,
}


def ordered(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Sorts by the given level columns in their display order (not alphabetically) and resets the index."""
    out = frame.copy()
    for column in columns:
        if column in LEVEL_ORDERS and not isinstance(out[column].dtype, pd.CategoricalDtype):
            out[column] = pd.Categorical(out[column].astype(str), categories=LEVEL_ORDERS[column])
    return out.sort_values(columns, kind="stable").reset_index(drop=True)


def blank_thin(frame: pd.DataFrame, columns: list[str] = RATE_COLS) -> pd.DataFrame:
    """Estimates of thin cells set to NaN, so charts leave them out."""
    out = frame.copy()
    out.loc[out["thin"].astype(bool), columns] = np.nan
    return out


# --- charts -------------------------------------------------------------------------------------


def chart_rate_by_group(table: pd.DataFrame, groups: list[str]):
    fig, ax = charts.single("How often do you blunder? Blunders per 100 moves", size=(6.5, 4.4))
    charts.side_bars(ax, table, "tc_group", order=groups, horizontal=True)
    ax.set_xlabel("blunders per 100 moves (95% CI)")
    charts.legend(ax, loc="lower right")
    charts.footnote(fig, "Blunder: a move that loses at least 300 centipawns. Opponents: the same games. CIs from resampling whole games.")
    return fig


def thin_panel(ax, text: str) -> None:
    """charts.thin_note without clearing the y ticks, which panels sharing a y axis would all lose."""
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
    ax.set_xticks([])
    ax.tick_params(axis="y", labelleft=False, left=False)
    ax.grid(False)


def chart_panels(table: pd.DataFrame, panels: list[str], x: str, order: list[str], title: str, ylabel: str, note: str, sharey=True, panel=(2.9, 2.3), rotate: int = 0, thin_text: str = "too few games"):
    """group_grid of you-vs-opponents bars, one panel per group; thin cells left out, all-thin panels noted."""
    fig, axes = charts.group_grid(panels, panel=panel, sharey=sharey, title=title)
    for g, ax in axes.items():
        part = table[table["tc_group"].astype(str) == g]
        if part.empty or part["thin"].all():
            thin_panel(ax, thin_text)
            continue
        charts.side_bars(ax, blank_thin(part), x, order=order)
        charts.zero_line(ax)
        ax.set_xlim(-0.5, len(order) - 0.5)  # keep every slot, so a thin one at the edge still shows its n/a
        thin_levels = set(part.loc[part["thin"].astype(bool), x].astype(str)) | (set(order) - set(part[x].astype(str)))
        for i, level in enumerate(order):
            if level in thin_levels:
                ax.text(i, 0, "n/a", ha="center", va="bottom", color=charts.INK_MUTED, fontsize=7.5)
        ax.tick_params(axis="x", labelrotation=rotate)
        if rotate:
            for label in ax.get_xticklabels():
                label.set_ha("right")
    for g, ax in axes.items():
        if ax.get_subplotspec().is_first_col():
            ax.set_ylabel(ylabel)
    charts.legend(fig, loc="upper right")
    charts.footnote(fig, note)
    fig.tight_layout()
    return fig


def chart_taxonomy(table: pd.DataFrame, groups: list[str]):
    """Stacked shares of each side's blunders by what the move did, two bars per time control."""
    fig, ax = charts.single("What kind of blunder? Share of each side's blunders", size=(7.2, 6.2))
    labels, y = [], 0.0
    positions = []
    for g in groups:
        for side in SIDES:
            positions.append((g, side, y))
            labels.append(f"{g}  {charts.SIDE_LABELS[side]}")
            y += 1
        y += 0.6
    for g, side, pos in positions:
        part = table[table["tc_group"].astype(str) == g].set_index("category")
        left = 0.0
        thin = bool(part["thin_composition"].any()) if "thin_composition" in part else False
        for cat in WHAT_PLAYED:
            share = float(part.loc[cat, f"share_{side}"]) if cat in part.index else 0.0
            share = 0.0 if np.isnan(share) else share
            ax.barh(pos, share, left=left, height=0.85, color=TAXONOMY_COLOURS[cat], label=cat, alpha=0.45 if thin else 1.0, edgecolor=charts.SURFACE, linewidth=0.5)
            left += share
    ax.set_yticks([p for _, _, p in positions], labels)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax.grid(axis="y", visible=False)
    handles, names = ax.get_legend_handles_labels()
    seen = dict(zip(names, handles))
    fig.legend([seen[c] for c in WHAT_PLAYED], WHAT_PLAYED, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.12), fontsize=8)
    note = "Each side's blunders in that time control add up to 100%. Opponents: the same games."
    if bool(table["thin_composition"].any()):
        note += f" Faded rows have fewer than {MIN_BLUNDERS} blunders per side."
    charts.footnote(fig, note)
    fig.texts[-1].set_position((0.01, -0.17))
    fig.tight_layout()
    return fig


# --- sections -----------------------------------------------------------------------------------


def section_1(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, ctx: dict) -> None:
    rate = side_rate(moves, "is_blunder", ["tc_group"])
    rate = rate.merge(blunder_counts(blunders, ["tc_group"]), on="tc_group", how="left")
    results.table("1_blunder_rate", ordered(rate, ["tc_group"]), f"Blunders (cp_loss >= 300) per 100 moves, you vs opponents in the same games, per time control; paired difference (you minus them); CIs from {N_BOOT} game resamples; thin below {MIN_CELL_MOVES} moves per side.")
    results.figure(chart_rate_by_group(rate, ctx["groups"]), "1_blunder_rate")

    cost = compare_sides(blunders, "ep_loss", by=["tc_group"], n_boot=N_BOOT).drop(columns="n")
    cost.insert(1, "measure", "expected points lost per blunder")
    cost = cost.merge(blunder_counts(blunders, ["tc_group"]), on="tc_group", how="left")
    share = paired_ratio(moves.assign(blunder_ep=moves["ep_loss"].where(moves["is_blunder"] == 1, 0.0)), "blunder_ep", "ep_loss", ["tc_group"])
    share.insert(1, "measure", "share of all expected points lost that came from blunders")
    share = share.merge(blunder_counts(blunders, ["tc_group"]), on="tc_group", how="left")
    big = side_rate(moves, "is_big_ep", ["tc_group"]).drop(columns=["n_player_moves", "n_opponent_moves", "thin"])
    big.insert(1, "measure", f"big errors (ep_loss >= {EP_BIG}) per 100 moves")
    big = big.merge(blunder_counts(blunders, ["tc_group"]), on="tc_group", how="left")
    table = pd.concat([cost, share, big], ignore_index=True)
    table["thin"] = (table["blunders_player"] < MIN_BLUNDERS) | (table["blunders_opponent"] < MIN_BLUNDERS)
    table["unit"] = table["measure"].map({cost["measure"].iloc[0]: "expected points", share["measure"].iloc[0]: "share", big["measure"].iloc[0]: "per 100 moves"})
    table["measure"] = pd.Categorical(table["measure"], categories=list(dict.fromkeys(table["measure"])))
    results.table("1b_blunder_cost", ordered(table, ["tc_group", "measure"]), f"What blunders cost, per time control, you vs opponents: mean expected points lost per blunder; share of each side's total expected points lost (all moves) that came from blunders; the alternative definition (ep_loss >= {EP_BIG}) per 100 moves. Paired CIs by game; thin below {MIN_BLUNDERS} blunders per side.")

    frame = pooled_by_class(moves, ctx["class_of"], ctx["order"])
    frame["year"] = frame["start_datetime"].dt.year.map(ctx["year_label"])
    frame["year"] = pd.Categorical(frame["year"], categories=list(dict.fromkeys(sorted(ctx["year_label"].values()))))
    parts = []
    for indicator, name in [("is_blunder", "blunders"), ("F:hung material", "hung material"), ("F:allowed mate", "allowed mate")]:
        t = side_rate(frame, indicator, ["tc_group", "year"], min_n=MIN_CELL_MOVES if indicator == "is_blunder" else MIN_RARE_MOVES)
        t.insert(1, "measure", name)
        parts.append(t)
    years = pd.concat(parts, ignore_index=True)
    years["measure"] = pd.Categorical(years["measure"], categories=["blunders", "hung material", "allowed mate"])
    results.table("1c_blunders_by_year", ordered(years, ["tc_group", "measure", "year"]), f"Blunders, hung material and allowed mate per 100 moves by year, per time class only (controls pooled: the mix of controls changed by year and control x year cells are mostly thin); years with under {MIN_YEAR_GAMES} games merged into the neighbouring year.")


def section_2(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, ctx: dict) -> None:
    table = side_rate(moves, "is_blunder", ["tc_group", "phase"])
    sh = shares(blunders, "phase", ["tc_group"]).rename(columns={"share_player": "share_of_player_blunders", "share_opponent": "share_of_opponent_blunders", "count_player": "blunders_player", "count_opponent": "blunders_opponent"})
    table = table.merge(sh, on=["tc_group", "phase"], how="left")
    results.table("2_blunders_by_phase", ordered(table, ["tc_group", "phase"]), f"Blunders per 100 moves played in each phase, you vs opponents, per time control, and each phase's share of the side's blunders; thin below {MIN_CELL_MOVES} moves per side.")
    results.figure(chart_panels(table, ctx["groups"], "phase", PHASES, "In which phase do you blunder? Blunders per 100 moves in that phase", "per 100 moves", f"Opening / middlegame / endgame as in the project's phase column. Bars: 95% CI by game. Cells under {MIN_CELL_MOVES} moves per side left out."), "2_blunders_by_phase")
    parts = []
    for indicator, name in [("F:hung material", "hung material"), ("F:allowed mate", "allowed mate")]:
        t = side_rate(moves, indicator, ["tc_group", "phase"], min_n=MIN_RARE_MOVES)
        t.insert(1, "measure", name)
        parts.append(t)
    fam = pd.concat(parts, ignore_index=True)
    fam["measure"] = pd.Categorical(fam["measure"], categories=["hung material", "allowed mate"])
    results.table("2b_families_by_phase", ordered(fam, ["tc_group", "measure", "phase"]), "Blunders that hung material and blunders that allowed mate, per 100 moves in each phase, you vs opponents, per time control.")


def section_3(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, classified: pd.DataFrame, ctx: dict) -> None:
    by = ["tc_group"]
    parts = []
    for cat in WHAT_PLAYED:
        t = side_rate(moves, f"P:{cat}", by, min_n=MIN_RARE_MOVES)
        t.insert(1, "category", cat)
        parts.append(t)
    table = pd.concat(parts, ignore_index=True)
    table["category"] = pd.Categorical(table["category"], categories=WHAT_PLAYED)
    sh = shares(blunders, "what_played", by).rename(columns={"what_played": "category"})
    table = table.merge(sh.drop(columns=["count_player", "count_opponent"]), on=["tc_group", "category"], how="left")
    table = table.merge(blunder_counts(blunders, by), on="tc_group", how="left")
    table["thin_composition"] = (table["blunders_player"] < MIN_BLUNDERS) | (table["blunders_opponent"] < MIN_BLUNDERS)
    table["thin"] = table["thin"] | table["thin_composition"]
    table = ordered(table, ["tc_group", "category"])
    results.table("3_what_played", table, f"What the blunder did (taxonomy of the played move), per time control: blunders of each kind per 100 moves (you, opponents, paired difference, CIs by game) and the share of each side's blunders; thin below {MIN_BLUNDERS} blunders per side.")
    results.figure(chart_taxonomy(table, ctx["groups"]), "3_what_played")

    parts = []
    for fam in FAMILIES:
        t = side_rate(moves, f"F:{fam}", by, min_n=MIN_RARE_MOVES)
        t.insert(1, "family", fam)
        parts.append(t)
    families = pd.concat(parts, ignore_index=True)
    families["family"] = pd.Categorical(families["family"], categories=list(FAMILIES))
    fam_share = shares(blunders.assign(family=blunders["what_played"].map({c: f for f, cs in FAMILIES.items() for c in cs}).astype(pd.CategoricalDtype(list(FAMILIES)))), "family", by)
    families = families.merge(fam_share.drop(columns=["count_player", "count_opponent"]), on=["tc_group", "family"], how="left")
    families = families.merge(blunder_counts(blunders, by), on="tc_group", how="left")
    families["thin"] = families["thin"] | (families["blunders_player"] < MIN_BLUNDERS) | (families["blunders_opponent"] < MIN_BLUNDERS)
    results.table("3b_families", ordered(families, ["tc_group", "family"]), "The taxonomy rolled up into four families (allowed mate; hung material = losing capture + moved en prise + ignored a threatened piece + exposed another piece; walked into a tactic = check tactic + combination; quiet refutation / deeper): per 100 moves and share of blunders, you vs opponents.")

    residual = blunders[blunders["what_played"] == "quiet refutation / deeper"].assign(
        already_winning=lambda f: (f["cp_before"] >= WINNING_CP).astype(float),
        missed_a_mate=lambda f: f["missed_mate"].astype(float),
        in_endgame=lambda f: (f["phase"] == "endgame").astype(float),
        best_move_won_free_material=lambda f: (f["what_missed"] == "free material").astype(float),
    )
    rows = []
    for measure in ["already_winning", "missed_a_mate", "in_endgame", "best_move_won_free_material", "ep_loss"]:
        t = compare_sides(residual, measure, by=by, n_boot=N_BOOT).drop(columns="n")
        t.insert(1, "measure", measure if measure != "ep_loss" else "expected points lost per blunder")
        rows.append(t)
    res = pd.concat(rows, ignore_index=True)
    res = res.merge(blunder_counts(residual, by), on="tc_group", how="left")
    res["thin"] = (res["blunders_player"] < MIN_BLUNDERS) | (res["blunders_opponent"] < MIN_BLUNDERS)
    res["unit"] = np.where(res["measure"] == "expected points lost per blunder", "expected points", "share of blunders")
    res["measure"] = pd.Categorical(res["measure"], categories=list(dict.fromkeys(res["measure"])))
    results.table("3c_quiet_residual", ordered(res, ["tc_group", "measure"]), f"What is in the residual 'quiet refutation / deeper' class, per time control: share played from a position already winning (>= {WINNING_CP} cp), share where a forced mate was missed, share in the endgame, share where the best move won free material, and expected points lost per such blunder; you vs opponents, CIs by game.")

    depth = shares(blunders, "depth", by).rename(columns={"count_player": "n_player", "count_opponent": "n_opponent"})
    resolved = shares(blunders[blunders["depth"].isin(RESOLVED)], "depth", by)[["tc_group", "depth", "share_player", "share_opponent"]]
    resolved = resolved.rename(columns={"share_player": "share_of_resolved_player", "share_opponent": "share_of_resolved_opponent"})
    depth = depth.merge(resolved, on=["tc_group", "depth"], how="left").merge(blunder_counts(blunders, by), on="tc_group", how="left")
    depth["thin"] = (depth["blunders_player"] < MIN_BLUNDERS) | (depth["blunders_opponent"] < MIN_BLUNDERS)
    results.table("3d_refutation_depth", ordered(depth, ["tc_group", "depth"]), f"How deep the refutation was, read off the game while the punishing side played the engine's best move (up to {REFUTATION_PLIES} of its moves): share of each side's blunders, and share among those whose refutation could be followed ('resolved').")

    big = classified[classified["ep_loss"] >= EP_BIG]
    comp = []
    for name, frame in [(f"ep_loss >= {EP_BIG}", big), ("cp_loss >= 300 (blunder)", blunders)]:
        s = shares(with_pooled(frame, ctx["class_of"], ctx["order"]), "what_played", by).rename(columns={"what_played": "category"})
        s.insert(1, "definition", name)
        comp.append(s)
    comp = pd.concat(comp, ignore_index=True)
    totals = comp.groupby(["tc_group", "definition"], observed=True)[["count_player", "count_opponent"]].transform("sum")
    comp["thin"] = (totals["count_player"] < MIN_BLUNDERS) | (totals["count_opponent"] < MIN_BLUNDERS)
    comp["definition"] = pd.Categorical(comp["definition"], categories=[f"ep_loss >= {EP_BIG}", "cp_loss >= 300 (blunder)"])
    results.table("3e_taxonomy_ep_definition", ordered(comp, ["tc_group", "definition", "category"]), f"Robustness: shares of each side's errors by what the move did, under the expected-points definition (ep_loss >= {EP_BIG}) next to the centipawn one, per time control and pooled per time class ('all bullet' etc.).")

    pooled = pooled_by_class(blunders, ctx["class_of"], ctx["order"])
    combo = pooled.groupby(["tc_group", "what_played", "what_missed", "side"], observed=True).agg(n=("ep_loss", "size"), ep=("ep_loss", "mean")).reset_index()
    rows = []
    for g, part in combo.groupby("tc_group", observed=True):
        you = part[part["side"] == "player"].copy()
        them = part[part["side"] == "opponent"].set_index(["what_played", "what_missed"])
        you["share_player"] = you["n"] / you["n"].sum()
        you["share_opponent"] = [them["n"].get((a, b), 0) / max(them["n"].sum(), 1) for a, b in zip(you["what_played"], you["what_missed"])]
        you = you.sort_values(["n", "what_played", "what_missed"], ascending=[False, True, True]).head(TOP_PATTERNS)
        you["rank"] = np.arange(1, len(you) + 1)
        rows.append(you)
    top = pd.concat(rows, ignore_index=True).rename(columns={"n": "blunders_player", "ep": "mean_ep_loss_player"})
    top = top[["tc_group", "rank", "what_played", "what_missed", "share_player", "share_opponent", "blunders_player", "mean_ep_loss_player"]]
    results.table("3f_top_patterns", top, f"The {TOP_PATTERNS} most common (what the move did x what the best move was) pairs among your blunders, pooled per time class: share of your blunders, the opponents' share of theirs, count and mean expected points lost.")


def section_4(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, ctx: dict) -> None:
    hung = blunders[blunders["what_played"].isin(HUNG_CATEGORIES)]
    game_sets = {g: np.sort(moves.loc[moves["tc_group"] == g, "game_uuid"].unique()) for g in ctx["groups"]}
    for cls in ctx["classes"]:
        game_sets[f"all {cls}"] = np.sort(np.concatenate([game_sets[g] for g in ctx["groups"] if ctx["class_of"][g] == cls]))
    group_of = dict(zip(moves["game_uuid"], moves["tc_group"].astype(str)))
    hung = hung.assign(group=hung["game_uuid"].map(group_of))
    rows = []
    for label, uuids in game_sets.items():
        part = hung[hung["game_uuid"].isin(uuids)]
        rng = np.random.default_rng(SEED)
        idx = rng.integers(0, len(uuids), size=(N_BOOT, len(uuids)))
        for piece in HUNG_PIECES:
            row = {"tc_group": label, "piece": piece}
            counts = {}
            for side in SIDES:
                sub = part[(part["side"] == side) & (part["victim"] == piece)]
                counts[side] = sub.groupby("game_uuid").size().reindex(uuids, fill_value=0).to_numpy(float)
                boot = counts[side][idx].mean(1) * 100
                row[side] = counts[side].mean() * 100
                row[f"{side}_lo"], row[f"{side}_hi"] = np.quantile(boot, [0.025, 0.975])
            diff = counts["player"] - counts["opponent"]
            row["diff"] = diff.mean() * 100
            row["diff_lo"], row["diff_hi"] = np.quantile(diff[idx].mean(1) * 100, [0.025, 0.975])
            for side in SIDES:
                sub = part[(part["side"] == side) & (part["victim"] == piece)]
                row[f"hangs_{side}"] = len(sub)
                row[f"mean_ep_loss_{side}"] = sub["ep_loss"].mean()
            row["games"] = len(uuids)
            row["unit"] = "per 100 games"
            row["thin"] = len(uuids) < MIN_GAMES_PIECES
            rows.append(row)
    table = pd.DataFrame(rows)
    table["tc_group"] = pd.Categorical(table["tc_group"], categories=ctx["order"])
    table["piece"] = pd.Categorical(table["piece"], categories=HUNG_PIECES)
    table = ordered(table, ["tc_group", "piece"])
    results.table("4_pieces_hung", table, f"Blunders that hung material, by the piece that could be taken (the engine's reply, else the SEE victim): per 100 games for you and the opponents, paired difference, counts and mean expected points lost per hang; per time control and pooled per time class ('all bullet' etc.); thin below {MIN_GAMES_PIECES} games.")
    panels = [*ctx["groups"], *[f"all {c}" for c in ctx["classes"]]]
    results.figure(chart_panels(table, panels, "piece", HUNG_PIECES, "Which pieces do you hang? Hangs per 100 games", "per 100 games", f"Bottom row: each time class pooled. Panels with fewer than {MIN_GAMES_PIECES} games are not drawn. Bars: 95% CI by game.", rotate=45), "4_pieces_hung")

    piece = side_rate(moves.assign(piece=pd.Categorical(moves["piece"], categories=PIECE_ORDER)), "is_blunder", ["tc_group", "piece"])
    results.table("4b_blunders_by_piece_moved", ordered(piece, ["tc_group", "piece"]), f"Blunders per 100 moves of each piece moved, you vs opponents, per time control (rows comparable across pieces); thin below {MIN_CELL_MOVES} moves per side.")


def section_5(results: Results, moves: pd.DataFrame, ctx: dict) -> None:
    by_band = side_rate(moves, "F:allowed mate", ["tc_group", "eval_band"], min_n=MIN_RARE_MOVES)
    overall = side_rate(moves, "F:allowed mate", ["tc_group"], min_n=MIN_RARE_MOVES)
    overall.insert(1, "eval_band", ALL_MOVES)
    table = pd.concat([overall, by_band], ignore_index=True)
    exposure = moves.groupby(["tc_group", "eval_band", "side"], observed=True).size().unstack("side", fill_value=0)
    exposure = (exposure / exposure.groupby(level="tc_group", observed=True).transform("sum")).reindex(columns=SIDES)
    exposure.columns = [f"share_of_{s}_moves" for s in SIDES]
    exposure = exposure.reset_index()
    exposure["eval_band"] = exposure["eval_band"].astype(str)
    table["eval_band"] = table["eval_band"].astype(str)
    table = table.merge(exposure, on=["tc_group", "eval_band"], how="left")
    table.loc[table["eval_band"] == ALL_MOVES, ["share_of_player_moves", "share_of_opponent_moves"]] = 1.0
    table["eval_band"] = pd.Categorical(table["eval_band"], categories=[ALL_MOVES, *EVAL_LABELS])
    table = ordered(table, ["tc_group", "eval_band"])
    results.table("5_allowed_mate", table, f"Blunders that allowed a forced mate, per 100 moves, you vs opponents, per time control: over all moves and within bands of the evaluation before the move (mover's view: losing < -{WINNING_CP} cp, balanced, winning >= {WINNING_CP}), with the share of each side's moves in each band (the exposure correction); paired CIs by game.")
    results.figure(chart_panels(table, ctx["groups"], "eval_band", [ALL_MOVES, *EVAL_LABELS], "How often do you allow mate? Blunders allowing mate per 100 moves, by position before the move", "per 100 moves", f"Losing / winning: evaluation before the move beyond {WINNING_CP} centipawns against / for the mover. Bars: 95% CI by game.", panel=(3.1, 2.4), rotate=30), "5_allowed_mate")

    parts = []
    for indicator, name in [("P:allowed mate in 1", "mate in 1 (blunder)"), ("P:allowed mate (longer)", "longer mate (blunder)"), ("allowed_mate_any", "allowed a forced mate (any move, incl. lost positions)")]:
        t = side_rate(moves, indicator, ["tc_group"], min_n=MIN_RARE_MOVES)
        t.insert(1, "measure", name)
        parts.append(t)
    dist = pd.concat(parts, ignore_index=True)
    dist["measure"] = pd.Categorical(dist["measure"], categories=list(dict.fromkeys(dist["measure"])))
    results.table("5b_allowed_mate_by_distance", ordered(dist, ["tc_group", "measure"]), "Allowed mate per 100 moves split into mate in 1 and longer mates (both blunders), and the raw allowed_mate flag on any move (including moves in already-lost positions, which are not blunders on clipped evals); you vs opponents, paired CIs by game.")

    had = moves[moves["mate_before"].astype("float64") > 0].assign(slipped=lambda f: f["missed_mate"].astype(float))
    parts = []
    for name, frame in [("any forced mate", had), ("mate in 1", had[had["mate_before"].astype("float64") == 1])]:
        t = side_rate(frame, "slipped", ["tc_group"], scale=1.0, unit="positions", min_n=MIN_MATE_CASES)
        t.insert(1, "measure", name)
        parts.append(t)
    slipped = pd.concat(parts, ignore_index=True)
    slipped["measure"] = pd.Categorical(slipped["measure"], categories=["any forced mate", "mate in 1"])
    results.table("5c_mate_slipped", ordered(slipped, ["tc_group", "measure"]), f"Among positions where the mover had a forced mate (and among mates in 1), the share where the mate slipped away (missed_mate), you vs opponents, paired CIs by game; thin below {MIN_MATE_CASES} positions per side.")


def section_6(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, ctx: dict) -> None:
    by = ["tc_group"]
    parts = []
    for cat in WHAT_MISSED:
        t = side_rate(moves, f"M:{cat}", by, min_n=MIN_RARE_MOVES)
        t.insert(1, "category", cat)
        parts.append(t)
    table = pd.concat(parts, ignore_index=True)
    table["category"] = pd.Categorical(table["category"], categories=WHAT_MISSED)
    sh = shares(blunders, "what_missed", by).rename(columns={"what_missed": "category"})
    table = table.merge(sh.drop(columns=["count_player", "count_opponent"]), on=["tc_group", "category"], how="left")
    table = table.merge(blunder_counts(blunders, by), on="tc_group", how="left")
    table["thin"] = table["thin"] | (table["blunders_player"] < MIN_BLUNDERS) | (table["blunders_opponent"] < MIN_BLUNDERS)
    table = ordered(table, ["tc_group", "category"])
    results.table("6_what_missed", table, f"What the blunder missed (the engine's best move: a mate, free material, saving a threatened piece, another capture, a check, another reply to check, a quiet move): blunders of each kind per 100 moves, you vs opponents, paired difference, share of each side's blunders; thin below {MIN_BLUNDERS} blunders per side.")
    chart = table[table["category"].isin(list(MISSED_CHART))].assign(label=lambda f: f["category"].astype(str).map(MISSED_CHART))
    results.figure(chart_panels(chart, ctx["groups"], "label", list(MISSED_CHART.values()), "Do you miss what is on offer or what is coming? Blunders per 100 moves by what the best move was", "per 100 moves", "free capture: the best move took material for free. save a piece: it rescued a threatened piece. quiet move: a non-capture, non-check move. Bars: 95% CI by game.", panel=(3.0, 2.4), rotate=30), "6_what_missed")

    frame = blunders.assign(
        punished=(blunders["next_cp_loss"] < PUNISHED_CP).astype(float).where(blunders["next_cp_loss"].notna()),
        reply_was_best=blunders["next_is_best"].astype("float64"),
    )
    parts = []
    for kind, part in [("all blunders", frame), ("blunders that hung material", frame[frame["what_played"].isin(HUNG_CATEGORIES)])]:
        for measure in ["punished", "reply_was_best"]:
            t = compare_sides(part, measure, by=by, n_boot=N_BOOT).drop(columns="n")
            t.insert(1, "blunders", kind)
            t.insert(2, "measure", "punished (reply kept the gift)" if measure == "punished" else "reply was the engine's best move")
            t = t.merge(side_counts(part, measure, by, "blunders"), on=by, how="left")
            parts.append(t)
    pun = pd.concat(parts, ignore_index=True)
    pun = pun.rename(columns={c: c.replace("player", "your_blunders").replace("opponent", "their_blunders") for c in pun.columns if c.startswith(("player", "opponent", "n_"))})
    pun["thin"] = (pun["n_your_blunders_blunders"] < MIN_BLUNDERS) | (pun["n_their_blunders_blunders"] < MIN_BLUNDERS)
    pun = pun.rename(columns={"n_your_blunders_blunders": "n_your_blunders", "n_their_blunders_blunders": "n_their_blunders"})
    pun["unit"] = "share of blunders"
    pun["blunders"] = pd.Categorical(pun["blunders"], categories=["all blunders", "blunders that hung material"])
    results.table("6b_punished", ordered(pun, ["tc_group", "blunders", "measure"]), f"How often a blunder was punished on the next move (the reply lost less than {PUNISHED_CP} cp of the gift) and how often the reply was the engine's best: 'your_blunders' columns = your blunders, punished by your opponents; 'their_blunders' = their blunders, punished by you; diff = yours minus theirs, paired by game.")


def section_7(results: Results, moves: pd.DataFrame, ctx: dict) -> None:
    parts = []
    for measure, column in CLOCK_MEASURES.items():
        edges, labels = (CLOCK_FRACTION_EDGES, CLOCK_FRACTION_LABELS) if measure == "share of base time" else (CLOCK_SECONDS_EDGES, CLOCK_SECONDS_LABELS)
        frame = moves[moves[column].notna()].copy()
        frame["clock_bucket"] = pd.cut(frame[column], edges, labels=labels, right=False)
        t = side_rate(frame, "is_blunder", ["tc_group", "clock_bucket"])
        t.insert(1, "measure", measure)
        t["clock_bucket"] = t["clock_bucket"].astype(str)
        parts.append(t)
    table = pd.concat(parts, ignore_index=True)
    table["measure"] = pd.Categorical(table["measure"], categories=list(CLOCK_MEASURES))
    table["clock_bucket"] = pd.Categorical(table["clock_bucket"], categories=list(dict.fromkeys([*CLOCK_SECONDS_LABELS, *CLOCK_FRACTION_LABELS])))
    table = ordered(table, ["tc_group", "measure", "clock_bucket"])
    adjusted = clock_regression(moves, ctx)
    results.table("7_clock_adjusted", adjusted, f"Extra blunders per 100 moves in each clock bucket relative to {CLOCK_REFERENCE} on the clock, holding phase, evaluation band before the move and move-number band fixed: linear probability model per time control with every coefficient split by side, you vs opponents and the paired difference, CIs from {N_REG_BOOT} game resamples; seconds left and seconds beyond the increment. Rows 'all <class>' pool the controls of a time class with a fixed effect per control (most single controls have too few low-clock moves); classes with a single control are not repeated. Thin below {MIN_CELL_MOVES} moves per side in the bucket.")
    low = [b for b in CLOCK_COARSE_LABELS if b != CLOCK_REFERENCE]
    panels = [*ctx["groups"], *[f"all {c}" for c in pooled_classes(ctx)]]
    results.figure(chart_panels(adjusted[adjusted["measure"] == CHART_CLOCK_MEASURE], panels, "clock_bucket", low, f"Does the clock change your blunder rate? Extra blunders per 100 moves compared with {CLOCK_REFERENCE}", "extra per 100 moves", f"Seconds beyond one increment on the mover's clock before the move (equal to seconds left without increment); same phase, evaluation and move number. Bottom row: bullet and blitz each pooled, with a fixed effect per control. n/a: fewer than {MIN_CELL_MOVES} moves per side in the bucket. Bars: 95% CI by game.", thin_text="too few low-clock moves"), "7_clock_adjusted")
    results.table("7b_blunders_by_clock", table, f"Raw blunders per 100 moves by the mover's clock before the move (no adjustment: the full-clock buckets are mostly the opening, where blunders are rare), you vs opponents, per time control, three ways: seconds left; seconds beyond one increment (the buffer: equal to seconds left without increment, 0 = living on the increment); share of the base time. Thin below {MIN_CELL_MOVES} moves per side.")


def pooled_classes(ctx: dict) -> dict[str, list[str]]:
    """Time classes with more than one reported control, and their controls (a one-control pool would repeat it)."""
    members = {c: [g for g in ctx["groups"] if ctx["class_of"][g] == c] for c in ctx["classes"]}
    return {c: m for c, m in members.items() if len(m) > 1}


def clock_regression(moves: pd.DataFrame, ctx: dict) -> pd.DataFrame:
    """Linear probability model of a blunder (x100) on coarse clock bucket, with phase, evaluation band and
    move-number band as controls; both sides in one fit with every coefficient split by side (identical to
    two separate fits), so the bootstrap over games pairs your coefficient with the opponents'. Also one fit
    per time class pooling its controls, with a fixed effect per control."""
    groups = ctx["groups"]
    cells = [(g, [g]) for g in groups] + [(f"all {c}", members) for c, members in pooled_classes(ctx).items()]
    rows = []
    base = moves[moves["cp_before"].notna()].copy()
    base["control_eval"] = pd.cut(base["cp_before"], CONTROL_EVAL_EDGES, labels=band_labels(CONTROL_EVAL_EDGES), right=False).astype(str)
    base["ply_band"] = pd.cut(base["ply"], PLY_EDGES, labels=[f"{int(a)}+" for a in PLY_EDGES[:-1]], right=False).astype(str)
    for measure in ["seconds left", "seconds beyond increment"]:
        column = CLOCK_MEASURES[measure]
        frame = base[base[column].notna()].copy()
        frame["bucket"] = pd.cut(frame[column], CLOCK_COARSE_EDGES, labels=CLOCK_COARSE_LABELS, right=False).astype(str)
        for g, members in cells:
            part = frame[frame["tc_group"].isin(members)]
            if part.empty:
                continue
            fixed = dummies(part["tc_group"].astype(str), members[0], "tc") if len(members) > 1 else pd.DataFrame(index=part.index)
            clock = dummies(part["bucket"], CLOCK_REFERENCE, "clock")
            clock = clock.reindex(columns=[f"clock={b}" for b in CLOCK_COARSE_LABELS if b != CLOCK_REFERENCE], fill_value=0.0)
            X = pd.concat([pd.Series(1.0, index=part.index, name="const"), clock, dummies(part["phase"], "middlegame", "phase"), dummies(part["control_eval"], band_labels(CONTROL_EVAL_EDGES)[2], "eval"), dummies(part["ply_band"], "0+", "ply"), fixed], axis=1)
            X = X.reindex(columns=sorted(X.columns, key=lambda c: (c != "const", not c.startswith("clock="), c)))
            is_player = (part["side"] == "player").to_numpy(float)[:, None]
            design = np.hstack([X.to_numpy(float) * is_player, X.to_numpy(float) * (1 - is_player)])
            beta, draws = cluster_ols(design, part["is_blunder"].to_numpy(float) * 100, part["game_uuid"].to_numpy(), n_boot=N_REG_BOOT, seed=SEED)
            width = X.shape[1]
            counts = part.groupby(["bucket", "side"]).size()
            for k, name in enumerate(X.columns):
                if not name.startswith("clock="):
                    continue
                bucket = name.split("=", 1)[1]
                row = {"tc_group": g, "measure": measure, "clock_bucket": bucket}
                series = {"player": (beta[k], draws[:, k]), "opponent": (beta[width + k], draws[:, width + k])}
                series["diff"] = (beta[k] - beta[width + k], draws[:, k] - draws[:, width + k])
                for side, (est, d) in series.items():
                    row[side] = est
                    row[f"{side}_lo"], row[f"{side}_hi"] = np.nanquantile(d, [0.025, 0.975]) if np.isfinite(d).sum() > 1 else (np.nan, np.nan)
                for side in SIDES:
                    row[f"n_{side}_moves"] = int(counts.get((bucket, side), 0))
                row["games"] = int(part["game_uuid"].nunique())
                row["thin"] = min(row["n_player_moves"], row["n_opponent_moves"]) < MIN_CELL_MOVES
                row["unit"] = f"extra blunders per 100 moves vs {CLOCK_REFERENCE}"
                rows.append(row)
    table = pd.DataFrame(rows)
    table["measure"] = pd.Categorical(table["measure"], categories=list(CLOCK_MEASURES))
    table["clock_bucket"] = pd.Categorical(table["clock_bucket"], categories=CLOCK_COARSE_LABELS)
    return ordered(table, ["tc_group", "measure", "clock_bucket"])


def section_8(results: Results, moves: pd.DataFrame, blunders: pd.DataFrame, ctx: dict) -> None:
    winning = moves[moves["eval_band"] == "winning"]
    # the same rates within a modest and a large lead, so a side that is more often far ahead is not flattered
    bands = {ALL_WINNING: winning, f"{WINNING_CP}-{WIN_SPLIT_CP} cp": winning[winning["cp_before"] < WIN_SPLIT_CP], f"{WIN_SPLIT_CP}+ cp": winning[winning["cp_before"] >= WIN_SPLIT_CP]}
    parts = []
    for band, frame in bands.items():
        for indicator, name in [("is_blunder", "blunders"), ("gave_away_win", GAVE_AWAY)]:
            t = side_rate(frame, indicator, ["tc_group"])
            t.insert(1, "lead", band)
            t.insert(2, "measure", name)
            mean_ep = frame.groupby(["tc_group", "side"], observed=True)["ep_before"].mean().unstack("side").reindex(columns=SIDES)
            mean_ep.columns = [f"mean_ep_before_{s}" for s in SIDES]
            parts.append(t.merge(mean_ep.reset_index(), on="tc_group", how="left"))
    table = pd.concat(parts, ignore_index=True)
    table["lead"] = pd.Categorical(table["lead"], categories=list(bands))
    table["measure"] = pd.Categorical(table["measure"], categories=["blunders", GAVE_AWAY])
    totals = side_counts(moves, "is_blunder", ["tc_group"], "all_moves")
    table = table.merge(totals, on="tc_group", how="left")
    table["share_of_player_moves_in_band"] = table["n_player_moves"] / table["n_player_all_moves"]
    table["share_of_opponent_moves_in_band"] = table["n_opponent_moves"] / table["n_opponent_all_moves"]
    table = ordered(table, ["tc_group", "lead", "measure"])
    results.table("8_winning_blunders", table, f"Moves played from a winning position (>= {WINNING_CP} cp for the mover before the move; also split at {WIN_SPLIT_CP} cp): blunders per 100 such moves, and wins given away per 100 such moves (a blunder that left the mover below {WINNING_CP} cp); you vs opponents, paired CIs by game; mean calibrated expected points before the move and the share of each side's moves played with that lead.")
    # charted by lead band, not 'all winning': the sides differ in how often they are far ahead, where blunders are rarer
    short = {"blunders": "blunder", GAVE_AWAY: "win given away"}
    lead_text = {f"{WINNING_CP}-{WIN_SPLIT_CP} cp": f"+{WINNING_CP} to +{WIN_SPLIT_CP}", f"{WIN_SPLIT_CP}+ cp": f"+{WIN_SPLIT_CP} or more"}
    chart = table[table["lead"] != ALL_WINNING].assign(label=lambda f: f["measure"].map(short).astype(str) + "\n" + f["lead"].astype(str).map(lead_text))
    # below +600 every blunder gives the win away (it costs at least 300), so that bar would repeat the first one
    modest = f"{WINNING_CP}-{WIN_SPLIT_CP} cp"
    chart = chart[~((chart["lead"].astype(str) == modest) & (chart["measure"] == GAVE_AWAY))]
    levels = list(dict.fromkeys(chart.sort_values(["measure", "lead"])["label"]))
    results.figure(chart_panels(chart, ctx["groups"], "label", levels, "How often do you blunder from a winning position? Per 100 moves played with that lead", "per 100 moves with that lead", f"Lead in centipawns before the move, split at +{WIN_SPLIT_CP}. Win given away: the blunder left the mover under +{WINNING_CP} (with a lead under +{WIN_SPLIT_CP} every blunder does). n/a: fewer than {MIN_CELL_MOVES} moves per side. Bars: 95% CI by game.", panel=(3.3, 2.5), rotate=30), "8_winning_blunders")

    cost = compare_sides(blunders[blunders["eval_band"].notna()], "ep_loss", by=["tc_group", "eval_band"], n_boot=N_BOOT).drop(columns="n")
    cost = cost.merge(blunder_counts(blunders[blunders["eval_band"].notna()], ["tc_group", "eval_band"]), on=["tc_group", "eval_band"], how="left")
    cost["thin"] = (cost["blunders_player"] < MIN_BLUNDERS) | (cost["blunders_opponent"] < MIN_BLUNDERS)
    cost["unit"] = "expected points per blunder"
    results.table("8b_cost_by_eval_band", ordered(cost, ["tc_group", "eval_band"]), f"Expected points lost per blunder by the evaluation before it (losing / balanced / winning at +-{WINNING_CP} cp), you vs opponents, paired CIs by game; thin below {MIN_BLUNDERS} blunders per side.")

    per_game = (
        moves.assign(win_pos=(moves["eval_band"] == "winning").astype(float))
        .groupby(["tc_group", "game_uuid", "side"], observed=True)
        .agg(had_winning=("win_pos", "max"), gave_away=("gave_away_win", "max"))
        .reset_index()
    )
    per_game["score"] = per_game["game_uuid"].map(ctx["score"])
    per_game.loc[per_game["side"] == "opponent", "score"] = 1 - per_game["score"]
    per_game["is_player_move"] = per_game["side"] == "player"
    had = per_game[per_game["had_winning"] == 1].copy()
    gave = had[had["gave_away"] == 1].copy()
    kept = had[had["gave_away"] == 0].copy()
    parts = []
    for name, frame, column, unit_text in [
        ("share of games with a winning position where the win was given away at least once", had, "gave_away", "share of games"),
        ("score in games where the win was given away", gave, "score", "score (win 1, draw 0.5)"),
        ("score in games where a winning position was never given away", kept, "score", "score (win 1, draw 0.5)"),
    ]:
        t = side_rate(frame, column, ["tc_group"], scale=1.0, unit="games", min_n=MIN_CASE_GAMES, unit_text=unit_text)
        t.insert(1, "measure", name)
        parts.append(t)
    after = pd.concat(parts, ignore_index=True)
    after["measure"] = pd.Categorical(after["measure"], categories=list(dict.fromkeys(after["measure"])))
    results.table("8c_after_giving_away", ordered(after, ["tc_group", "measure"]), f"Per game: of the games in which a side reached a winning position (>= {WINNING_CP} cp), the share in which it gave the win away with a blunder at least once, and that side's score (win 1, draw 0.5) in games where it did and did not; you vs opponents, CIs by game; thin below {MIN_CASE_GAMES} games per side.")


def data_table(data, moves: pd.DataFrame, blunders: pd.DataFrame, error_set_size: int) -> pd.DataFrame:
    rows = []
    for g in data.tc_groups:
        part = moves[moves["tc_group"] == g]
        b = blunders[blunders["tc_group"] == g]
        rows.append({"tc_group": g, "included": True, "games": int(part["game_uuid"].nunique()), "moves_player": int(part["is_player_move"].sum()), "moves_opponent": int((~part["is_player_move"]).sum()), "blunders_player": int(b["is_player_move"].sum()), "blunders_opponent": int((~b["is_player_move"]).sum())})
    for g, n in data.tc_noted.items():
        rows.append({"tc_group": g, "included": False, "games": n, "moves_player": 0, "moves_opponent": 0, "blunders_player": 0, "blunders_opponent": 0})
    table = pd.DataFrame(rows)
    table["error_positions_replayed"] = error_set_size
    return table


# --- main ---------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    groups = list(data.tc_groups)
    class_of = data.tc_class
    classes = list(dict.fromkeys(class_of[g] for g in groups))
    order = [*groups, *[f"all {c}" for c in classes]]
    ctx = {"groups": groups, "class_of": class_of, "classes": classes, "order": order}
    LEVEL_ORDERS["tc_group"] = order

    moves = data.moves[data.moves["tc_group"].isin(groups)].sort_values(["game_uuid", "ply"]).reset_index(drop=True)
    moves["tc_group"] = pd.Categorical(moves["tc_group"].astype(str), categories=order)
    moves["phase"] = pd.Categorical(moves["phase"], categories=PHASES)
    games = data.games[data.games["tc_group"].isin(groups)]
    ctx["score"] = dict(zip(games["uuid"], games["my_score"]))
    ctx["year_label"] = year_blocks(games["year"])
    # the engine's best reply to a move is the stored best move of the next position in the game
    by_game = moves.groupby("game_uuid")
    moves["reply_best"] = by_game["best_move"].shift(-1)
    moves["next_cp_loss"] = by_game["cp_loss"].shift(-1)
    moves["next_is_best"] = by_game["is_best_move"].shift(-1)
    moves["missed_mate"] = moves["missed_mate"].fillna(False).astype(bool)
    moves["allowed_mate_any"] = moves["allowed_mate"].fillna(False).astype(float)
    moves["is_blunder"] = (moves["label"] == BLUNDER).astype(float)
    moves["is_big_ep"] = (moves["ep_loss"] >= EP_BIG).astype(float).where(moves["ep_loss"].notna())
    moves["eval_band"] = pd.cut(moves["cp_before"].astype("float64"), EVAL_EDGES, labels=EVAL_LABELS, right=False)
    moves["gave_away_win"] = ((moves["is_blunder"] == 1) & (moves["cp_before"] >= WINNING_CP) & (moves["cp_after"] < WINNING_CP)).astype(float)
    moves["clock_share_of_base"] = moves["clock_before_seconds"] / moves["base_seconds"]

    # --- replay and classify the error set ---------------------------------------------------
    pool = moves["label"].isin(["mistake", BLUNDER]) | (moves["ep_loss"] >= REPLAY_EP) | moves["missed_mate"] | moves["allowed_mate"].fillna(False).astype(bool)
    error_set = moves[pool]
    t1 = time.time()
    features = pd.DataFrame(
        [classify_position(r.fen_before, r.uci, r.best_move, r.reply_best) for r in error_set[["fen_before", "uci", "best_move", "reply_best"]].itertuples(index=False)],
        index=error_set.index,
    )
    classified = assign_taxonomy(pd.concat([error_set, features], axis=1))
    replay_seconds = time.time() - t1
    blunders = classified[classified["label"] == BLUNDER].copy()
    blunders["depth"] = pd.Categorical(refutation_depth(moves, blunders), categories=DEPTHS)

    # indicator columns on every move, for the per-100-moves rates
    for cat in WHAT_PLAYED:
        moves[f"P:{cat}"] = 0.0
        moves.loc[blunders.index[blunders["what_played"] == cat], f"P:{cat}"] = 1.0
    for cat in WHAT_MISSED:
        moves[f"M:{cat}"] = 0.0
        moves.loc[blunders.index[blunders["what_missed"] == cat], f"M:{cat}"] = 1.0
    for fam, cats in FAMILIES.items():
        moves[f"F:{fam}"] = moves[[f"P:{c}" for c in cats]].sum(axis=1)

    results = Results(SLUG)
    section_1(results, moves, blunders, ctx)
    section_2(results, moves, blunders, ctx)
    section_3(results, moves, blunders, classified, ctx)
    section_4(results, moves, blunders, ctx)
    section_5(results, moves, ctx)
    section_6(results, moves, blunders, ctx)
    section_7(results, moves, ctx)
    section_8(results, moves, blunders, ctx)
    results.table("9_data", data_table(data, moves, blunders, len(error_set)), f"Games, moves and blunders per time control (included = reported; the rest have fewer than {MIN_GROUP_GAMES} games), and the number of error positions replayed with python-chess.")
    results.finish()
    print(f"replay {replay_seconds:.0f} s over {len(error_set):,} positions; total {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
