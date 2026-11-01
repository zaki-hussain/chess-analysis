"""Move quality from the mover's point of view, derived from the engine evaluations.

`moves_analysed.parquet` holds only derived columns, keyed by (game_uuid,
ply); `load_analysed()` joins them back onto the enriched moves. Forced
mates are mapped onto the centipawn scale (±MATE_CP minus plies to mate) so
every move has a numeric eval before and after; losses are computed on
evals clipped to ±CLIP_CP so a blunder in a lost position doesn't count for
thousands of centipawns.
"""

import numpy as np
import pandas as pd

from .engine import position_key

MATE_CP = 10_000
CLIP_CP = 1_000
# Thresholds on cp_loss, the usual chess.com/Lichess-style bands.
INACCURACY_CP = 50
MISTAKE_CP = 100
BLUNDER_CP = 300

ANALYSED_COLUMNS = [
    "game_uuid", "ply",
    "cp_before", "cp_after", "mate_before", "mate_after", "cp_loss",
    "ep_sf_before", "ep_sf_after", "ep_sf_loss",
    "best_move", "second_move", "best_cp", "second_cp", "criticality",
    "is_best_move", "is_second_move", "missed_mate", "allowed_mate", "label",
    "tb_wdl_before", "tb_wdl_after", "tb_dtz_before", "tb_dtz_after", "tb_error",
]  # fmt: skip


def white_cp(score_cp: pd.Series, mate_in: pd.Series, wdl_win: pd.Series, terminal: pd.Series) -> pd.Series:
    """Numeric eval from White's view with mates on the centipawn scale.

    mate_in > 0: White mates in N -> MATE_CP - N. mate_in < 0: -(MATE_CP - N).
    A terminal checkmate has mate_in 0; the WDL columns say who was mated.
    """
    mate = mate_in.astype("float64")
    # Stockfish reports a decisive result beyond its mate horizon as ~±20000 cp with no mate count.
    cp = score_cp.astype("float64").clip(-MATE_CP, MATE_CP)
    has_mate = mate.notna()
    cp[has_mate] = np.sign(mate[has_mate]) * (MATE_CP - mate[has_mate].abs())
    mated = has_mate & (mate == 0) & terminal.astype(bool)
    cp[mated] = np.where(wdl_win[mated].astype("float64") > 500, MATE_CP, -MATE_CP)
    return cp


def _expected_points_white(wdl_win: pd.Series, wdl_draw: pd.Series) -> pd.Series:
    return (wdl_win.astype("float64") + wdl_draw.astype("float64") / 2) / 1000


def analyse_moves(moves_enriched: pd.DataFrame, evals: pd.DataFrame, tablebase: pd.DataFrame | None = None) -> pd.DataFrame:
    """Derived move-quality columns for every move with evaluations before and after.

    With a tablebase table, small endgames also get the exact outcome from the
    mover's view before and after the move (tb_wdl: 2 win ... -2 loss) and
    `tb_error`: the move turned a win into a draw/loss or a draw into a loss.
    """
    ev = evals.set_index("position")
    cols = ["score_cp", "mate_in", "wdl_win", "wdl_draw", "terminal", "best_move", "second_move", "second_score_cp", "second_mate_in"]
    before = ev[cols].reindex(moves_enriched["fen_before"].map(position_key).values).reset_index(drop=True)
    after = ev[["score_cp", "mate_in", "wdl_win", "wdl_draw", "terminal"]].reindex(moves_enriched["fen_after"].map(position_key).values).reset_index(drop=True)
    m = moves_enriched.reset_index(drop=True)
    sign = pd.Series(np.where(m["color"] == "white", 1.0, -1.0))

    out = pd.DataFrame({"game_uuid": m["game_uuid"], "ply": m["ply"]})
    out["cp_before"] = white_cp(before["score_cp"], before["mate_in"], before["wdl_win"], before["terminal"]) * sign
    out["cp_after"] = white_cp(after["score_cp"], after["mate_in"], after["wdl_win"], after["terminal"]) * sign
    out["mate_before"] = (before["mate_in"].astype("float64") * sign).astype("Int64")
    out["mate_after"] = (after["mate_in"].astype("float64") * sign).astype("Int64")
    out["cp_loss"] = (out["cp_before"].clip(-CLIP_CP, CLIP_CP) - out["cp_after"].clip(-CLIP_CP, CLIP_CP)).clip(lower=0)

    ep_before_white = _expected_points_white(before["wdl_win"], before["wdl_draw"])
    ep_after_white = _expected_points_white(after["wdl_win"], after["wdl_draw"])
    out["ep_sf_before"] = np.where(sign > 0, ep_before_white, 1 - ep_before_white)
    out["ep_sf_after"] = np.where(sign > 0, ep_after_white, 1 - ep_after_white)
    out["ep_sf_loss"] = (out["ep_sf_before"] - out["ep_sf_after"]).clip(lower=0)

    out["best_move"] = before["best_move"]
    out["second_move"] = before["second_move"]
    out["best_cp"] = out["cp_before"].clip(-CLIP_CP, CLIP_CP)
    second_white = white_cp(before["second_score_cp"], before["second_mate_in"], before["wdl_win"], pd.Series(False, index=before.index))
    out["second_cp"] = (second_white * sign).clip(-CLIP_CP, CLIP_CP)
    out["criticality"] = (out["best_cp"] - out["second_cp"]).clip(lower=0)
    out["is_best_move"] = m["uci"] == out["best_move"]
    out["is_second_move"] = m["uci"] == out["second_move"]

    had_mate = out["mate_before"].fillna(0) > 0
    # mate_after == 0 (not NA) is a terminal position: the mover has just delivered checkmate.
    has_mate = out["mate_after"].fillna(-1) >= 0
    out["missed_mate"] = had_mate & ~has_mate
    opponent_had_mate = out["mate_before"].fillna(0) < 0
    opponent_has_mate = out["mate_after"].fillna(0) < 0
    out["allowed_mate"] = ~opponent_had_mate & opponent_has_mate

    bins = [-np.inf, INACCURACY_CP, MISTAKE_CP, BLUNDER_CP, np.inf]
    out["label"] = pd.cut(out["cp_loss"], bins=bins, right=False, labels=["good", "inaccuracy", "mistake", "blunder"]).astype(str)

    for column in ("tb_wdl_before", "tb_wdl_after", "tb_dtz_before", "tb_dtz_after"):
        out[column] = pd.Series(pd.NA, index=out.index, dtype="Int64")
    out["tb_error"] = False
    if tablebase is not None and len(tablebase):
        tb = tablebase.set_index("position")
        tb_before = tb.reindex(m["fen_before"].map(position_key).values).reset_index(drop=True)
        tb_after = tb.reindex(m["fen_after"].map(position_key).values).reset_index(drop=True)
        # Probes are from the side to move: before the move that's the mover, after it's the opponent.
        out["tb_wdl_before"] = tb_before["wdl"].astype("Int64")
        out["tb_wdl_after"] = (-tb_after["wdl"]).astype("Int64")
        out["tb_dtz_before"] = tb_before["dtz"].astype("Int64")
        out["tb_dtz_after"] = tb_after["dtz"].astype("Int64")
        outcome = lambda wdl: np.sign(wdl.astype("float64")) * (wdl.abs() == 2)  # win 1, draw 0 (cursed wins count as draws), loss -1
        both = out["tb_wdl_before"].notna() & out["tb_wdl_after"].notna()
        out.loc[both, "tb_error"] = (outcome(out.loc[both, "tb_wdl_after"]) < outcome(out.loc[both, "tb_wdl_before"])).to_numpy()
    return out[ANALYSED_COLUMNS]
