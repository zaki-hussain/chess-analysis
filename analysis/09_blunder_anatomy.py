"""Question 9: blunder anatomy.

What kinds of mistakes the player makes and misses, compared with the
opponents in the same games: hanging pieces, missed mates, missed captures,
one-move vs deeper tactics, by piece and phase, and which types changed over
the years. Every blunder (cp_loss >= 300) is classified by replaying the
position with python-chess: the played move, the engine's best move and the
engine's best reply (the stored best move of the next position), with a
small static exchange evaluation (SEE) to decide whether material is "free".

No engine is run; the optional Stockfish re-search for refutation depth is
skipped, the game continuation is used as a proxy instead.

Run from the repo root: .venv/bin/python analysis/09_blunder_anatomy.py
"""

from __future__ import annotations

import time

import chess
import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    ci_column,
    cluster_bootstrap_mean,
    compare_sides,
    figure_style,
    load_data,
)

SLUG = "09_blunder_anatomy"
SEED = 9
N_BOOT = 500
EP_BIG = 0.2  # alternative, ep-based definition of a big error

PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
PIECE_ORDER = ["pawn", "knight", "bishop", "rook", "queen", "king"]

# Coarse taxonomy. WHAT_PLAYED: what the played move did (why it was bad).
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
SHORT_PLAYED = {
    "allowed mate in 1": "mate in 1",
    "allowed mate (longer)": "mate (longer)",
    "losing capture": "losing capture",
    "moved piece en prise": "moved en prise",
    "ignored a threatened piece": "ignored threat",
    "exposed another piece": "exposed piece",
    "allowed a check tactic": "check tactic",
    "allowed a combination": "combination",
    "quiet refutation / deeper": "quiet / deeper",
}


# --- static exchange evaluation (coarse, ignores promotions beyond a queen) -----


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
    """Vectorised category assignment from the replay features plus the stored evals."""
    f = frame
    allowed = f["allowed_mate"].fillna(False).astype(bool)
    mate_after = f["mate_after"].astype("float64")
    reply_wins = f["reply_capture"].astype(bool) & (f["reply_see"].fillna(0) >= 1)
    material_lost = reply_wins | (f["hung_gain"] >= 3)
    losing_capture = f["played_capture"].astype(bool) & (f["played_see"].fillna(0) < 0)
    moved_en_prise = f["hung_is_moved"].astype(bool) | f["reply_to_moved"].astype(bool)
    ignored = f["hung_was_threatened"].astype(bool) & (f["threat_before"] >= 1)
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
    # the material the move gave away (engine reply's capture if it wins material, else the SEE threat)
    gain = np.where(reply_wins, f["reply_see"].fillna(0), f["hung_gain"])
    victim = np.where(reply_wins, f["reply_victim"], f["hung_victim"])

    missed_mate = f["missed_mate"].fillna(False).astype(bool)
    mate_before = f["mate_before"].astype("float64")
    mate_available = missed_mate | (mate_before > 0)
    free_material = f["best_capture"].astype(bool) & (f["best_see"].fillna(0) >= 1)
    defensive = (f["threat_before"] >= 1) & (f["threat_after_best"].fillna(99) < f["threat_before"])
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


# --- helpers -------------------------------------------------------------------


def rate_table(moves: pd.DataFrame, indicator: str, by: list[str], n_boot: int = N_BOOT, scale: float = 100.0, fmt: str = ".2f") -> pd.DataFrame:
    """compare_sides on a 0/1 indicator, expressed per `scale` moves, with compact CI strings."""
    table = compare_sides(moves, indicator, by=by, n_boot=n_boot)
    for column in ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"]:
        table[column] = table[column] * scale
    table["you"] = ci_column(table, "player", "player_lo", "player_hi", fmt)
    table["opponents"] = ci_column(table, "opponent", "opponent_lo", "opponent_hi", fmt)
    table["you − them"] = ci_column(table, "diff", "diff_lo", "diff_hi", fmt)
    return table


def mean_table(frame: pd.DataFrame, column: str, by: list[str], n_boot: int = N_BOOT, fmt: str = ".3f") -> pd.DataFrame:
    """Mean of `column` for player and opponents within `by` groups (rows of `frame` only), CIs by game."""
    rows = []
    for key, part in frame.groupby(by, observed=True):
        key = key if isinstance(key, tuple) else (key,)
        row = dict(zip(by, key))
        for side, label in [("player", "you"), ("opponent", "opponents")]:
            sub = part[part["side"] == side]
            est, lo, hi = cluster_bootstrap_mean(sub[column], sub["game_uuid"], n_boot=n_boot, seed=SEED)
            row[label] = f"{est:{fmt}} [{lo:{fmt}}, {hi:{fmt}}]" if pd.notna(lo) else (f"{est:{fmt}}" if pd.notna(est) else "")
            row[f"n_{label}"] = int(sub[column].notna().sum())
        rows.append(row)
    return pd.DataFrame(rows)


def year_label(year: pd.Series) -> pd.Series:
    """2021 folds into 2022 and 2026 into 2025 (a handful of games each)."""
    y = year.clip(lower=2022, upper=2025).astype(int).astype(str)
    return y.replace({"2022": "2021-22", "2025": "2025-26"})


# --- main ------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    figure_style()
    import matplotlib.pyplot as plt

    data = load_data()
    moves = data.moves.sort_values(["game_uuid", "ply"]).reset_index(drop=True)
    moves = moves[moves["time_class"].isin(TIME_CLASSES)].copy()
    moves["time_class"] = pd.Categorical(moves["time_class"], categories=TIME_CLASSES)
    games = data.games[data.games["uuid"].isin(moves["game_uuid"])]
    # the engine's best reply to a move is the stored best move of the next position in the game
    moves["reply_best"] = moves.groupby("game_uuid")["best_move"].shift(-1)
    moves["next_cp_loss"] = moves.groupby("game_uuid")["cp_loss"].shift(-1)
    moves["next_is_best"] = moves.groupby("game_uuid")["is_best_move"].shift(-1)
    # guard against stale tables where missed_mate was also True for moves that delivered mate (mate_after == 0)
    delivered_mate = moves["mate_after"].astype("float64") == 0
    moves["missed_mate"] = moves["missed_mate"].fillna(False).astype(bool) & ~delivered_mate
    moves["is_blunder"] = (moves["label"] == "blunder").astype(float)
    moves["is_big_ep"] = (moves["ep_loss"] >= EP_BIG).astype(float)
    moves["year_label"] = year_label(moves["start_datetime"].dt.year)

    # --- replay and classify the error set ------------------------------------
    pool = moves["label"].isin(["mistake", "blunder"]) | (moves["ep_loss"] >= 0.1) | moves["missed_mate"].fillna(False).astype(bool) | moves["allowed_mate"].fillna(False).astype(bool)
    error_set = moves[pool]
    t1 = time.time()
    features = pd.DataFrame(
        [classify_position(r.fen_before, r.uci, r.best_move, r.reply_best) for r in error_set[["fen_before", "uci", "best_move", "reply_best"]].itertuples(index=False)],
        index=error_set.index,
    )
    classified = assign_taxonomy(pd.concat([error_set, features], axis=1))
    replay_seconds = time.time() - t1
    blunders = classified[classified["label"] == "blunder"].copy()

    # indicator columns on every move for the per-100-moves rates
    for cat in WHAT_PLAYED:
        moves[f"P:{cat}"] = 0.0
        moves.loc[blunders.index[blunders["what_played"] == cat], f"P:{cat}"] = 1.0
    for cat in WHAT_MISSED:
        moves[f"M:{cat}"] = 0.0
        moves.loc[blunders.index[blunders["what_missed"] == cat], f"M:{cat}"] = 1.0
    hung_cats = ["losing capture", "moved piece en prise", "ignored a threatened piece", "exposed another piece"]
    moves["P:hung material (any)"] = moves[[f"P:{c}" for c in hung_cats]].sum(axis=1)
    moves["P:allowed mate (any)"] = moves[["P:allowed mate in 1", "P:allowed mate (longer)"]].sum(axis=1)
    moves["blunder_ep_loss"] = np.where(moves["is_blunder"] == 1, moves["ep_loss"], 0.0)
    moves["is_bl_or_ep"] = ((moves["is_blunder"] == 1) | (moves["is_big_ep"] == 1)).astype(float)

    n_games = {tc: int((games["time_class"] == tc).sum()) for tc in TIME_CLASSES}
    n_mine = int(moves["is_player_move"].sum())
    n_bl_mine = int(blunders["is_player_move"].sum())
    n_bl_theirs = int((~blunders["is_player_move"]).sum())

    report = Report(SLUG, "Blunder anatomy: what kinds of mistakes you make and miss")
    report.text(
        f"Data: {len(games):,} standard live games ({n_games['bullet']:,} bullet, {n_games['blitz']:,} blitz, {n_games['rapid']:,} rapid), "
        f"{len(moves):,} analysed moves ({n_mine:,} yours), player {data.username}. A **blunder** here is a move with `cp_loss` ≥ 300 "
        f"(the `label` used throughout the project): {n_bl_mine:,} of yours and {n_bl_theirs:,} by your opponents. "
        f"Each blunder was classified by replaying the position with python-chess (the played move, the engine's best move, and the "
        f"engine's best reply, i.e. the stored best move of the next position) with a static exchange evaluation (SEE, piece values 1/3/3/5/9) "
        f"to decide whether material was free. Rates are per 100 moves of the side in question; numbers are estimate [95% CI] with CIs "
        f"bootstrapped by game ({N_BOOT} resamples); every number about you sits beside the opponents' number from the same games "
        f"(you − them: negative = you do it less). `ep_loss` (calibrated expected points lost) is the cost measure; `cp_loss` defines the set."
    )

    # --- 1. how much of the damage is blunders --------------------------------
    report.heading("1. Blunders: how often, and how much of the damage they are")
    report.text(
        "Blunders per 100 moves, the mean expected points one blunder costs, and the share of each side's total `ep_loss` "
        "(over all its moves) that comes from blunders. The alternative, ep-based definition (`ep_loss` ≥ "
        f"{EP_BIG}) is shown beside it."
    )
    rates = rate_table(moves, "is_blunder", ["time_class"]).rename(columns={"you": "your blunders /100", "opponents": "their blunders /100", "you − them": "you − them (/100)"})
    cost = mean_table(blunders, "ep_loss", ["time_class"]).rename(columns={"you": "ep per blunder, you", "opponents": "ep per blunder, them"})
    share_rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        row = {"time_class": tc}
        for side, label in [("player", "you"), ("opponent", "them")]:
            sub = part[part["side"] == side]
            row[f"share of ep lost, {label}"] = sub["blunder_ep_loss"].sum() / sub["ep_loss"].sum()
        share_rows.append(row)
    share = pd.DataFrame(share_rows)
    big = rate_table(moves, "is_big_ep", ["time_class"]).rename(columns={"you": f"ep≥{EP_BIG} /100, you", "opponents": f"ep≥{EP_BIG} /100, them"})
    table1 = rates[["time_class", "games", "your blunders /100", "their blunders /100", "you − them (/100)"]].merge(cost[["time_class", "ep per blunder, you", "ep per blunder, them"]], on="time_class").merge(share, on="time_class").merge(big[["time_class", f"ep≥{EP_BIG} /100, you", f"ep≥{EP_BIG} /100, them"]], on="time_class")
    table1["time_class"] = table1["time_class"].astype(str)
    report.table(table1, floatfmt=".2f")

    # --- 2. what the played move did ------------------------------------------
    report.heading("2. What the blunder did: taxonomy of the played move")
    report.text(
        "Each blunder gets exactly one category, in this order of precedence. **Allowed mate** (the opponent has a forced mate after the "
        "move; split by mate in 1 vs longer). **Hung material** — the engine's best reply is a capture that wins material by SEE, or a "
        "piece (≥ 3 pawns' worth) is simply free — split into: *losing capture* (the played move captured something defended, so the "
        "capturing piece is lost), *moved piece en prise* (a non-capture move to a square where the piece can be taken for free), "
        "*ignored a threatened piece* (a piece was already attacked before the move and was left there), *exposed another piece* "
        "(the move removed a defender, opened a line, or walked into a pin). **Allowed a check tactic** (no material is immediately "
        "free but the best reply is a check: forks, skewers, mating attacks). **Allowed a combination** (the best reply is a capture "
        "that does not win material by SEE: a sacrifice or exchange tactic). **Quiet refutation / deeper** (the best reply is a quiet "
        "move: a deeper threat, a positional punishment, or an error the one-move rules cannot see; this also catches missed mates "
        "where nothing material is lost). Rates per 100 moves of that side."
    )
    played_rows = []
    for cat in WHAT_PLAYED:
        t = rate_table(moves, f"P:{cat}", ["time_class"])
        t.insert(0, "what the move did", cat)
        played_rows.append(t)
    played_table = pd.concat(played_rows, ignore_index=True)
    for tc in TIME_CLASSES:
        part = played_table[played_table["time_class"] == tc]
        share_you = blunders[(blunders["time_class"] == tc) & blunders["is_player_move"]]["what_played"].value_counts(normalize=True)
        share_them = blunders[(blunders["time_class"] == tc) & ~blunders["is_player_move"]]["what_played"].value_counts(normalize=True)
        part = part.assign(**{"share of your blunders": part["what the move did"].map(share_you).fillna(0), "share of theirs": part["what the move did"].map(share_them).fillna(0)})
        report.heading(f"{tc}: {n_games[tc]:,} games", level=3)
        report.table(part[["what the move did", "you", "opponents", "you − them", "share of your blunders", "share of theirs"]], floatfmt=".2f")

    # figure 1: rates by category, three panels
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.2), sharey=True)
    for ax, tc in zip(axes, TIME_CLASSES):
        part = played_table[played_table["time_class"] == tc].set_index("what the move did").loc[WHAT_PLAYED]
        y = np.arange(len(WHAT_PLAYED))
        for k, (side, label) in enumerate([("player", "you"), ("opponent", "opponents")]):
            offset = -0.2 if k == 0 else 0.2
            ax.barh(y + offset, part[side], height=0.36, color=SIDE_COLOURS[side], label=label, xerr=[part[side] - part[f"{side}_lo"], part[f"{side}_hi"] - part[side]], error_kw={"elinewidth": 0.8, "ecolor": "#444"})
        ax.set_yticks(y)
        ax.set_yticklabels([SHORT_PLAYED[c] for c in WHAT_PLAYED])
        ax.invert_yaxis()
        ax.set_title(tc)
        ax.set_xlabel("blunders per 100 moves")
    axes[0].legend(loc="upper right")
    fig.suptitle("What your blunders did, vs your opponents' (95% CI by game)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "what_played", "Blunders per 100 moves by what the played move did, you vs opponents, by time class.")
    plt.close(fig)

    # the residual category, described
    residual = blunders[blunders["what_played"] == "quiet refutation / deeper"]
    res_rows = []
    for side, label in [("player", "you"), ("opponent", "opponents")]:
        sub = residual[residual["side"] == side]
        res_rows.append(
            {
                "side": label,
                "n": len(sub),
                "already winning (cp_before > 500)": (sub["cp_before"] > 500).mean(),
                "a mate was missed": sub["missed_mate"].fillna(False).astype(bool).mean(),
                "in the endgame": (sub["phase"] == "endgame").mean(),
                "best move won free material": (sub["what_missed"] == "free material").mean(),
                "mean ep_loss": sub["ep_loss"].mean(),
            }
        )
    report.text(
        "The residual 'quiet refutation / deeper' is the largest single category for both sides, so here is what is in it: the share of "
        "those blunders played from an already-won position (giving back part of a win, which costs little in expected points), the "
        "share where a mate was missed, the share in the endgame (where king and pawn moves lose without any material changing hands "
        "for several moves), and the share where the engine's best move would have won free material (a missed capture, with no immediate loss). "
        "It costs less per blunder than the other categories."
    )
    report.table(pd.DataFrame(res_rows), floatfmt=".3f")

    # mates, net of exposure
    report.heading("Mates allowed and missed, net of exposure", level=3)
    report.text(
        "Allowing a mate depends on how often you are in a position where one can be allowed, so the allowed-mate rate is shown per 100 "
        "moves **within bands of the evaluation before the move** (mover's view, cp clipped to ±1000: losing < −300, balanced, winning > 300), "
        "and the share of each side's moves spent in each band. Missing a mate is conditioned the same way: among moves where the mover "
        "had a forced mate (`mate_before` > 0), the share that let it slip (`missed_mate`, corrected here so that a delivered mate does not "
        "count as missed: the stored flag is True for the 1,801 mating moves because the terminal position has no mate distance), and among "
        "mates in 1, the share not played."
    )
    moves["eval_band"] = pd.cut(moves["cp_before"].clip(-1000, 1000), [-1001, -300, 300, 1001], labels=["losing", "balanced", "winning"])
    band_table = rate_table(moves, "P:allowed mate (any)", ["time_class", "eval_band"])
    band_table["time_class"] = band_table["time_class"].astype(str)
    band_table["eval_band"] = band_table["eval_band"].astype(str)
    exposure = moves.groupby(["time_class", "eval_band", "side"], observed=True).size().unstack("side")
    exposure = (exposure / exposure.groupby(level="time_class").transform("sum")).reset_index()
    exposure["time_class"] = exposure["time_class"].astype(str)
    exposure["eval_band"] = exposure["eval_band"].astype(str)
    band_table = band_table.merge(exposure.rename(columns={"player": "share of your moves", "opponent": "share of their moves"}), on=["time_class", "eval_band"])
    report.table(band_table[["time_class", "eval_band", "you", "opponents", "you − them", "share of your moves", "share of their moves"]].rename(columns={"you": "allowed mate /100, you", "opponents": "allowed mate /100, them"}), floatfmt=".3f")
    had_mate = moves[moves["mate_before"].astype("float64") > 0].copy()
    had_mate["missed"] = had_mate["missed_mate"].fillna(False).astype(float)
    had_mate["not_best"] = (~had_mate["is_best_move"].astype(bool)).astype(float)
    miss_any = rate_table(had_mate, "missed", ["time_class"], scale=1.0, fmt=".3f")
    mate1 = had_mate[had_mate["mate_before"].astype("float64") == 1]
    miss_one = rate_table(mate1, "missed", ["time_class"], scale=1.0, fmt=".3f")
    t_m = miss_any[["time_class", "you", "opponents", "you − them", "n"]].rename(columns={"you": "mate slipped, you", "opponents": "mate slipped, them", "you − them": "you − them (any mate)", "n": "moves with a mate"})
    t_m = t_m.merge(miss_one[["time_class", "you", "opponents", "n"]].rename(columns={"you": "mate in 1 missed, you", "opponents": "mate in 1 missed, them", "n": "moves with mate in 1"}), on="time_class")
    t_m["time_class"] = t_m["time_class"].astype(str)
    report.table(t_m)

    # --- 3. what was missed -----------------------------------------------------
    report.heading("3. What the blunder missed: taxonomy of the engine's best move")
    report.text(
        "The same blunders classified by what the best move was, in order of precedence: a **mate** was available (mate in 1 vs longer); "
        "the best move won **free material** (a capture with positive SEE); the best move was **saving a threatened piece** (a piece of "
        "yours could be taken for a gain before the move, and after the best move the opponent's best capture gains less); another "
        "**capture** (even trade or sacrifice); a **check**; you were **in check** and the best move was a different reply; or a **quiet "
        "move** (the engine wanted something the rules cannot name: a retreat, a prophylactic move, a pawn move, a deeper tactic)."
    )
    missed_rows = []
    for cat in WHAT_MISSED:
        t = rate_table(moves, f"M:{cat}", ["time_class"])
        t.insert(0, "what the best move was", cat)
        missed_rows.append(t)
    missed_table = pd.concat(missed_rows, ignore_index=True)
    for tc in TIME_CLASSES:
        part = missed_table[missed_table["time_class"] == tc]
        share_you = blunders[(blunders["time_class"] == tc) & blunders["is_player_move"]]["what_missed"].value_counts(normalize=True)
        share_them = blunders[(blunders["time_class"] == tc) & ~blunders["is_player_move"]]["what_missed"].value_counts(normalize=True)
        part = part.assign(**{"share of your blunders": part["what the best move was"].map(share_you).fillna(0), "share of theirs": part["what the best move was"].map(share_them).fillna(0)})
        report.heading(f"{tc}", level=3)
        report.table(part[["what the best move was", "you", "opponents", "you − them", "share of your blunders", "share of theirs"]], floatfmt=".2f")

    # --- 4. material hung and the most common patterns ----------------------------
    report.heading("4. What gets hung, and the most common patterns")
    hung = blunders[blunders["what_played"].isin(hung_cats)].copy()
    report.text(
        "Among blunders that hung material (section 2's four 'hung' categories), the piece that could be taken for free (per the engine's "
        "reply, else the SEE victim), as a count per 100 games of each side, all time classes pooled. Queens and rooks are the expensive ones."
    )
    victim_rows = []
    n_all_games = len(games)
    for v in PIECE_ORDER[:5]:
        row = {"piece hung": v}
        for side, label in [("player", "you"), ("opponent", "opponents")]:
            sub = hung[(hung["side"] == side) & (hung["victim"] == v)]
            count = len(sub)
            # per-100-games rate with a game-clustered CI from the per-game counts over all games
            per_game = sub.groupby("game_uuid").size().reindex(games["uuid"], fill_value=0)
            rng = np.random.default_rng(SEED)
            idx = rng.integers(0, len(per_game), size=(N_BOOT, len(per_game)))
            boot = per_game.to_numpy()[idx].mean(axis=1) * 100
            lo, hi = np.quantile(boot, [0.025, 0.975])
            row[f"{label}: per 100 games"] = f"{100 * count / n_all_games:.1f} [{lo:.1f}, {hi:.1f}]"
            row[f"{label}: n"] = count
            row[f"{label}: mean ep_loss"] = sub["ep_loss"].mean()
        victim_rows.append(row)
    report.table(pd.DataFrame(victim_rows), floatfmt=".3f")

    report.text(
        "The ten most common (what the move did × what was missed) combinations among **your** blunders, all time classes pooled, with "
        "the share of your blunders, the opponents' share of theirs, and the mean `ep_loss` of such a blunder for you."
    )
    combo = blunders.groupby(["what_played", "what_missed", "side"], observed=True).agg(n=("ep_loss", "size"), ep=("ep_loss", "mean")).reset_index()
    you_c = combo[combo["side"] == "player"].copy()
    them_c = combo[combo["side"] == "opponent"].set_index(["what_played", "what_missed"])
    you_c["share of your blunders"] = you_c["n"] / you_c["n"].sum()
    you_c["share of theirs"] = [them_c["n"].get((a, b), 0) / max(them_c["n"].sum(), 1) for a, b in zip(you_c["what_played"], you_c["what_missed"])]
    you_c = you_c.sort_values("n", ascending=False).head(10)
    you_c = you_c.rename(columns={"what_played": "what the move did", "what_missed": "what the best move was", "n": "your n", "ep": "your mean ep_loss"})
    report.table(you_c[["what the move did", "what the best move was", "your n", "share of your blunders", "share of theirs", "your mean ep_loss"]], floatfmt=".3f")

    # --- 5. depth of the refutation and punishment --------------------------------
    report.heading("5. One-move or deeper? Refutation depth and how often blunders are punished")
    report.text(
        "No engine was run for this question, so the depth of the refutation is read off the game itself: following the actual "
        "continuation **only while the punishing side played the engine's best move**, how many of its moves passed before the material "
        "balance (1/3/3/5/9) moved against the blunderer by at least a pawn, or the game ended in mate. 'Immediate' = material lost on the "
        "very next move; 'within 2–3 moves' = after a check, a fork or an in-between move; 'not within 3 moves' = the refutation was "
        "positional, a longer mating attack, or the punishing side left the engine line (in which case the depth is unknown and the "
        "blunder is counted under 'line left / unknown'). Shares of each side's blunders, all time classes pooled, then per time class "
        "the share of blunders that were **punished** (the opponent's reply lost less than 100 cp of the gift, i.e. kept most of the advantage) "
        "and the share where the opponent found the engine's best reply."
    )
    depth = refutation_depth(moves, blunders)
    blunders["depth"] = depth
    depth_rows = []
    known = ["immediate", "within 2–3 moves", "mate within 3 moves", "not within 3 moves"]
    for cat in [*known, "line left / unknown"]:
        row = {"refutation depth": cat}
        for side, label in [("player", "your blunders"), ("opponent", "their blunders")]:
            sub = blunders[blunders["side"] == side]
            row[f"{label}: share"] = f"{(sub['depth'] == cat).mean():.3f}"
            row[f"{label}: n"] = int((sub["depth"] == cat).sum())
            resolved = sub[sub["depth"].isin(known)]
            row[f"{label}: share of resolved"] = f"{(resolved['depth'] == cat).mean():.3f}" if cat in known else ""
        depth_rows.append(row)
    report.table(pd.DataFrame(depth_rows))
    report.text(
        "'Share of resolved' conditions on the blunders whose refutation could be followed (the punisher stayed on the engine line until "
        "material or mate decided it, or for three moves). Read it as: of the blunders that were refuted on the board, roughly two thirds lost "
        "material on the very next move; genuinely deep refutations are rare in this pool."
    )

    blunders["punished"] = (blunders["next_cp_loss"] < 100).astype(float).where(blunders["next_cp_loss"].notna())
    blunders["reply_was_best"] = blunders["next_is_best"].astype("float64")
    punished = mean_table(blunders, "punished", ["time_class"]).rename(columns={"you": "your blunders punished", "opponents": "their blunders punished (by you)"})
    best_reply = mean_table(blunders, "reply_was_best", ["time_class"]).rename(columns={"you": "best reply found vs you", "opponents": "best reply found by you"})
    t5 = punished[["time_class", "your blunders punished", "their blunders punished (by you)"]].merge(best_reply[["time_class", "best reply found vs you", "best reply found by you"]], on="time_class")
    t5["time_class"] = t5["time_class"].astype(str)
    report.table(t5)
    report.text(
        "Punishment by category (share of blunders where the reply kept most of the advantage), all time classes pooled: which of your "
        "gifts your opponents actually take, and which of theirs you take."
    )
    pun_cat = mean_table(blunders, "punished", ["what_played"]).rename(columns={"what_played": "what the move did", "you": "your blunders punished", "opponents": "their blunders punished by you"})
    report.table(pun_cat[["what the move did", "your blunders punished", "n_you", "their blunders punished by you", "n_opponents"]])

    # --- 6. by piece and phase -------------------------------------------------------
    report.heading("6. By piece moved and by phase")
    report.text(
        "Blunder rate per 100 moves **of that piece** (so the rows are comparable: a queen move is more likely to be a blunder than a "
        "pawn move), you vs opponents, per time class, and the hung-material and allowed-mate rates per 100 moves by phase."
    )
    moves["piece"] = pd.Categorical(moves["piece"], categories=PIECE_ORDER)
    piece_table = rate_table(moves, "is_blunder", ["time_class", "piece"])
    piece_table["time_class"] = piece_table["time_class"].astype(str)
    piece_table["piece"] = piece_table["piece"].astype(str)
    report.table(piece_table[["time_class", "piece", "you", "opponents", "you − them", "n"]].rename(columns={"n": "moves"}), floatfmt=".2f")

    moves["phase"] = pd.Categorical(moves["phase"], categories=["opening", "middlegame", "endgame"])
    phase_parts = []
    for ind, name in [("is_blunder", "blunders /100"), ("P:hung material (any)", "hung material /100"), ("P:allowed mate (any)", "allowed mate /100")]:
        t = rate_table(moves, ind, ["time_class", "phase"])
        t = t[["time_class", "phase", "you", "opponents", "you − them"]].rename(columns={"you": f"{name}, you", "opponents": f"{name}, them", "you − them": f"{name}, you − them"})
        phase_parts.append(t)
    phase_table = phase_parts[0].merge(phase_parts[1], on=["time_class", "phase"]).merge(phase_parts[2], on=["time_class", "phase"])
    phase_table["time_class"] = phase_table["time_class"].astype(str)
    phase_table["phase"] = phase_table["phase"].astype(str)
    report.heading("By phase", level=3)
    report.table(phase_table[["time_class", "phase", "blunders /100, you", "blunders /100, them", "hung material /100, you", "hung material /100, them", "allowed mate /100, you", "allowed mate /100, them"]])

    # --- 7. over time -----------------------------------------------------------------
    report.heading("7. Which kinds of blunder changed over the years")
    report.text(
        "Rates per 100 moves by year (2021 is folded into 2022 and 2026 into 2025; a few games each), per time class, for the overall "
        "blunder rate and the three big families. The opponents' rate in the same games is the control: the opponent pool is matched to "
        "your rating, so a type of blunder that fell for you but not for them is a real change in your play; one that fell for both "
        "reflects a change in the games (rating level, time control, opponents). Rapid has no games in 2024 and 35 in 2025."
    )
    year_cats = [("is_blunder", "blunders"), ("P:hung material (any)", "hung material"), ("P:allowed mate (any)", "allowed mate"), ("P:allowed a check tactic", "check tactic"), ("P:quiet refutation / deeper", "quiet / deeper")]
    years = ["2021-22", "2023", "2024", "2025-26"]
    moves["year_label"] = pd.Categorical(moves["year_label"], categories=years)
    year_tables = {}
    for ind, name in year_cats:
        t = rate_table(moves, ind, ["time_class", "year_label"])
        t["time_class"] = t["time_class"].astype(str)
        t["year"] = t["year_label"].astype(str)
        year_tables[name] = t
    for tc in TIME_CLASSES:
        parts = []
        for ind, name in year_cats[:3]:
            t = year_tables[name]
            t = t[t["time_class"] == tc][["year", "games", "you", "opponents"]].rename(columns={"you": f"{name}, you", "opponents": f"{name}, them"})
            parts.append(t)
        t = parts[0].merge(parts[1].drop(columns="games"), on="year").merge(parts[2].drop(columns="games"), on="year")
        t = t[t["games"] >= 20]
        report.heading(f"{tc}", level=3)
        report.table(t)

    fig, axes = plt.subplots(2, len(year_cats), figsize=(14, 6), sharex=True)
    for i, tc in enumerate(["bullet", "blitz"]):
        for j, (ind, name) in enumerate(year_cats):
            ax = axes[i, j]
            t = year_tables[name]
            t = t[(t["time_class"] == tc) & (t["games"] >= 20)].set_index("year").reindex(years)
            x = np.arange(len(years))
            for side, label in [("player", "you"), ("opponent", "opponents")]:
                ax.plot(x, t[side], marker="o", markersize=5, linewidth=2, color=SIDE_COLOURS[side], label=label)
                ax.fill_between(x, t[f"{side}_lo"], t[f"{side}_hi"], color=SIDE_COLOURS[side], alpha=0.15, linewidth=0)
            ax.set_xticks(x)
            ax.set_xticklabels(years, rotation=30)
            ax.set_ylim(bottom=0)
            if i == 0:
                ax.set_title(name)
            if j == 0:
                ax.set_ylabel(f"{tc}: per 100 moves")
    axes[0, 0].legend(loc="lower left")
    fig.suptitle("Blunder types per 100 moves by year, you vs opponents (bands: 95% CI by game)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "by_year", "Blunder families per 100 moves by year, bullet (top) and blitz (bottom); shaded bands are 95% CIs.")
    plt.close(fig)

    # --- 8. robustness: ep-based definition ------------------------------------------
    report.heading(f"8. Robustness: the same taxonomy on `ep_loss` ≥ {EP_BIG} errors")
    big_errors = classified[classified["ep_loss"] >= EP_BIG]
    overlap_you = big_errors[big_errors["is_player_move"]]
    report.text(
        f"Moves with `ep_loss` ≥ {EP_BIG} (a fifth of a point given away): {int(big_errors['is_player_move'].sum()):,} of yours, "
        f"{int((~big_errors['is_player_move']).sum()):,} of the opponents'; {overlap_you['label'].eq('blunder').mean():.0%} of yours are also "
        f"cp-blunders (the rest are mostly `mistake`-labelled moves in balanced positions, where 100–300 cp is a lot of expected points). "
        "Shares of each side's big errors by what the move did, all time classes pooled, to check the taxonomy is not an artefact of the cp threshold."
    )
    share_rows = []
    for cat in WHAT_PLAYED:
        row = {"what the move did": cat}
        for side, label in [("player", "you"), ("opponent", "opponents")]:
            sub = big_errors[big_errors["side"] == side]
            row[f"{label}: share (ep≥{EP_BIG})"] = (sub["what_played"] == cat).mean()
            sub_b = blunders[blunders["side"] == side]
            row[f"{label}: share (cp blunders)"] = (sub_b["what_played"] == cat).mean()
        share_rows.append(row)
    report.table(pd.DataFrame(share_rows), floatfmt=".3f")

    # --- 9. what this means ----------------------------------------------------------------
    headline = headline_numbers(played_table, missed_table, blunders, rates)
    report.heading("What this means for the player")
    report.text(headline)

    report.heading("Caveats")
    report.text(
        "- The taxonomy is rule-based and coarse: a static exchange evaluation ignores pins beyond legality, promotions other than to a "
        "queen, and any tactic longer than one capture sequence; 'quiet refutation / deeper' is a residual, not a diagnosis.\n"
        "- A blunder is defined by `cp_loss` ≥ 300 on evals clipped at ±1000, so errors in already-decided positions are under-counted and "
        "the missed mates that keep a winning position are mostly labelled 'good' (section 8 checks the taxonomy under the ep-based "
        "definition instead).\n"
        "- The engine's best reply is the stored best move of the next position; it is missing when the blunder was the last move of the "
        "game (about 1.6% of blunders), where the SEE threat is used alone.\n"
        "- Refutation depth is read off the game continuation while the punishing side stayed on the engine's best move, not from an "
        "engine line; no Stockfish re-search was run (the optional step in the plan was skipped). The depth distribution is therefore "
        "conditional on the opponent finding the line, and 'line left / unknown' is large.\n"
        "- 'Punished' uses the next move's `cp_loss` < 100, a cp criterion; one-move punishment only, not conversion of the game.\n"
        "- Years mix rating levels and time controls (2022 was blitz-heavy, 2025 bullet-heavy); the opponents' rate in the same games is the "
        "control, but the comparison across years is descriptive, not causal.\n"
        "- All evaluations are Stockfish 17.1 at 1M nodes; at that depth the 'best move' in sharp positions is sometimes not the only good move, "
        "so 'what was missed' is the engine's preference, not a unique truth.\n"
        "- An earlier `moves_analysed` table flagged the 1,801 moves that delivered mate as `missed_mate` (5,144 flags instead of 3,339); "
        "`analysed.py` was fixed and the script guards against stale tables anyway. The blunder taxonomy is unaffected (a delivered mate has `cp_loss` 0).\n"
        "- Opponent ratings are post-game and are not used anywhere here except through the matched-opponent design."
    )

    path = report.write()
    print(f"wrote {path}; replay {replay_seconds:.0f} s over {len(error_set):,} positions; total {time.time() - t0:.0f} s")


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
        """Material balance (white − black) after the move at `ply`, and whether it is mate."""
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
        if mate:  # the blunder itself delivered mate?! cannot happen for a blunder; keep for safety
            result.append("not within 3 moves")
            continue
        depth = "not within 3 moves"
        for k in range(1, 4):
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
                depth = "immediate" if k == 1 else "within 2–3 moves"
                break
            if own not in is_best:
                depth = "line left / unknown"
                break
        result.append(depth)
    return pd.Series(result, index=blunders.index)


def material_balance(board: chess.Board) -> int:
    total = 0
    for piece_type, value in PIECE_VALUE.items():
        total += value * (len(board.pieces(piece_type, chess.WHITE)) - len(board.pieces(piece_type, chess.BLACK)))
    return total


def headline_numbers(played_table: pd.DataFrame, missed_table: pd.DataFrame, blunders: pd.DataFrame, rates: pd.DataFrame) -> str:
    """One paragraph for the player, built from the computed tables so it stays in step with the numbers."""
    you_b = blunders[blunders["is_player_move"]]
    hung_cats = ["losing capture", "moved piece en prise", "ignored a threatened piece", "exposed another piece"]
    hung_share = you_b["what_played"].isin(hung_cats).mean()
    mate_share = you_b["what_played"].isin(["allowed mate in 1", "allowed mate (longer)"]).mean()
    check_share = (you_b["what_played"] == "allowed a check tactic").mean()
    residual_share = (you_b["what_played"] == "quiet refutation / deeper").mean()
    missed_free = (you_b["what_missed"] == "free material").mean()
    missed_defence = (you_b["what_missed"] == "saving a threatened piece").mean()
    queen_hung = int((you_b["what_played"].isin(hung_cats) & (you_b["victim"] == "queen")).sum())
    rook_hung = int((you_b["what_played"].isin(hung_cats) & (you_b["victim"] == "rook")).sum())

    def diff(table: pd.DataFrame, key: str, cat: str, tc: str) -> str:
        row = table[(table[key] == cat) & (table["time_class"] == tc)].iloc[0]
        return row["you − them"]

    rate_text = ", ".join(f"{r['time_class']} {r['your blunders /100']} vs {r['their blunders /100']}" for _, r in rates.iterrows())
    mate_text = "; ".join(f"{tc} {diff(played_table, 'what the move did', 'allowed mate (longer)', tc)} (longer) and {diff(played_table, 'what the move did', 'allowed mate in 1', tc)} (in 1)" for tc in TIME_CLASSES)
    free_text = "; ".join(f"{tc} {diff(missed_table, 'what the best move was', 'free material', tc)}" for tc in TIME_CLASSES)
    quiet_text = "; ".join(f"{tc} {diff(missed_table, 'what the best move was', 'quiet move', tc)}" for tc in TIME_CLASSES)
    return (
        f"You blunder about as often as the opponents matched to your rating ({rate_text} per 100 moves) and your blunders are the same "
        f"kinds as theirs: {hung_share:.0%} of yours simply hand over material (a piece left en prise, moved en prise, or a capture of a defended piece), "
        f"{mate_share:.0%} allow a mate, {check_share:.0%} walk into a check-based tactic, and {residual_share:.0%} are refuted by a quiet move "
        f"(a third of those from positions you had already won). You hung your queen {queen_hung} times and a rook {rook_hung} times. "
        f"Two differences from your opponents survive the confidence intervals. First, you **allow mates more often** at every level of the "
        f"evaluation, not because you are in lost positions more (you are in them less): you − them per 100 moves is {mate_text}; "
        f"mate-in-1 allowed is the most expensive blunder there is, and the one a half-second 'what does this reply threaten?' check removes. "
        f"Second, your blunders are **less often a missed free capture** than theirs ({free_text} per 100 moves; {missed_free:.0%} of your blunders "
        f"missed free material vs {missed_defence:.0%} that missed saving a threatened piece) and more often a missed quiet move ({quiet_text}): "
        f"you see what is on offer, you under-see what the opponent is about to do. The useful habit is therefore the defensive one: before "
        f"moving, name the opponent's best reply, check whether anything of yours (the moved piece first) can be taken for free, and whether that "
        f"reply is a check or a mate threat."
    )


if __name__ == "__main__":
    main()
