"""Q1: where points are won and lost - your mistakes vs your opponents'.

Every move gives away some expected points (`ep_loss`: the calibrated
expected score before the move minus after it, from the mover's view). Per
game and per exact time control, this script computes:

1. the expected points each side gives away per game, you vs opponents in
   the same games (paired difference, Elo-equivalent), the score budget
   (score vs the model's expectation at move 1, residual) and a sanity check
   that the net difference predicts results;
2. how concentrated the damage is: the share of all expected points lost
   that comes from the costliest 1%, 5%, 10% ... of moves, and from moves
   losing at least 0.1 / 0.2 / 0.3;
3. how often a lost game was decided by one identifiable move of the loser
   (the decisive mistake), lost without one, or lost while the model still
   gave the loser chances; and what the decisive move looks like (size,
   phase, clock in seconds, beyond the increment and as a share of the base
   time), with the you-minus-opponents difference of the clock measures;
4. expected points given away by phase (per game and per move);
5. conversion: the actual score from positions the model rated winning,
   against the model's expected score there, you vs opponents; the
   threshold sensitivity; and whether the model is even-handed between the
   sides (observed win rate by model P(win));
6. saves: how often each side avoids losing a position the model rated
   lost, against expectation, and how (opponent flagged, on the board, draw);
7. losses that came while the loser still had chances, by how they ended.

Also writes the per-game table data/<user>/games_decomposed.parquet (one row
per standard live game, independent of grouping), which Q2, Q5, Q6, Q7 and
Q8 read: its content must not change unless its definition does.

Writes CSVs to presentation/data/01_decomposition/ and SVGs to
presentation/figures/01_decomposition/ (no prose).

Run: .venv/bin/python analysis/01_decomposition.py  (about 40 seconds)
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis.toolkit import (
    Results,
    add_elo,
    cluster_bootstrap_mean,
    compare_sides,
    load_data,
    opponent_rating_pre,
    ratio_bootstrap,
    repo_root,
)

SLUG = "01_decomposition"
PHASES = ["opening", "middlegame", "endgame"]
# "Winning" / "losing" model P(win) for conversion and saves: 80% is where a human calls a position won
# (about +3 in a level blitz game), and the model's observed win rate there is already above 90%.
WIN_T, LOSE_T = 0.8, 0.2
# Decisive mistake: the loser is "lost on the board" below LO after their last own move; "playable" means
# P(win) >= HI before a move. 10% / 20% keep the slide short enough to name one move (see _decisive).
DECISIVE_LO, DECISIVE_HI = 0.1, 0.2
DECISIVE_ALT = (0.15, 0.3)  # a stricter pair (lost < 15%, playable >= 30%), as a sensitivity check
# The costliest move of the final slide counts as the decisive blunder only if it lost at least this much: a
# twentieth of a game, well above the 0.03 an average move loses.
MIN_DECISIVE_EP = 0.05
# Error sizes for the tail tables: a tenth, a fifth and almost a third of a game (an average move loses 0.03).
TAIL = [0.1, 0.2, 0.3]
# Shares of the costliest moves for the concentration curve (1% of moves ~ one move in three games).
TOP_SHARES = [0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5]
TOP_SHARE_TICKS = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5]  # labelled on the log axis (20% and 30% would collide)
# A share conditional on cases (losses, winning positions, ...) needs this many cases: a share from 30 cases has
# a 95% CI of about +-0.18 at 50%, wide but still able to tell 20% from 80%.
MIN_CASES = 30
# A phase cell needs this many games in which the side played a move in that phase (same reasoning).
MIN_PHASE_GAMES = 30
# Thresholds for the conversion / save sensitivity table: from just above even to near-certain. The save
# threshold is the mirror image, 1 - t (rounded, so 1 - 0.8 is exactly LOSE_T and 5c matches 5_conversion).
SENSITIVITY_THRESHOLDS = [0.6, 0.7, 0.8, 0.9]
# Net expected points (opponents' lost minus yours) bins for the sanity check: a tenth of a game is the smallest
# swing worth separating; beyond +-0.5 a game is nearly always decided.
NET_EP_EDGES = [-10.0, -0.5, -0.2, -0.1, 0.0, 0.1, 0.2, 0.5, 10.0]
NET_EP_LABELS = ["< -0.5", "-0.5 to -0.2", "-0.2 to -0.1", "-0.1 to 0", "0 to 0.1", "0.1 to 0.2", "0.2 to 0.5", "> 0.5"]
# Model P(win) bins for the even-handedness check: deciles.
CALIBRATION_EDGES = np.round(np.linspace(0, 1, 11), 1)
# The conversion chart's y axis starts here: every non-thin score is above 80%, and a zero baseline would hide
# the gaps between the sides.
CONVERSION_AXIS_FLOOR = 0.5
# Elo-equivalents of per-game point differences are taken around an even score (pairings are rating-matched).
EVEN_SCORE = 0.5
# The Elo-equivalent of a score within this distance of 0 or 1 is unbounded (a CI end at a score of 1.0 maps to
# 900+ Elo in the small controls), so such Elo cells are left blank rather than printed as meaningless numbers.
ELO_SCORE_MARGIN = 0.001
# Segment shades in the stacked bars run from solid to this alpha: pale enough to separate four shades, dark
# enough that the palest still shows on the light surface.
MIN_SEGMENT_ALPHA = 0.2
N_BOOT = 500
N_BOOT_CURVE = 300  # resamples for the concentration curve (each one walks every move)
SEED = 1
SIDE_PREFIX = (("player", "my"), ("opponent", "opp"))
ALIVE_TERMINATIONS = ["timeout", "resigned", "checkmated", "abandoned"]
# How a save ended, from the saving side's result and the other side's result.
SAVE_KINDS = ["won on time", "won on the board", "drawn on time", "drawn by stalemate", "drawn otherwise"]
# The chart merges the rarer board draws (stalemate, repetition, agreement, ...) so four shades stay readable.
SAVE_CHART_KINDS = {"won on time": "won on time", "won on the board": "won on the board", "drawn on time": "drawn on time", "drawn by stalemate": "drawn on the board", "drawn otherwise": "drawn on the board"}


# --- per-game decomposition (writes games_decomposed.parquet; keep its content stable) ---------------


def _decisive(decided: pd.DataFrame, lo: float, hi: float) -> pd.DataFrame:
    """Decisive mistake per decided game, judged on the loser's own moves only.

    The loser's P(win) is read before and after each of the loser's own moves
    (mover's view, so terminal positions and the winner's moves add no noise).
    A game counts as lost on the board if the loser's P(win) after their last
    own move is below `lo`. The final slide starts at the last own move the
    loser made from a playable position (P(win) >= `hi` before the move; the
    first move if there never was one, e.g. against a much stronger opponent).
    The decisive mistake is the loser's costliest move (largest `ep_loss`) from
    that move to the end; it counts as a blunder if it lost at least
    MIN_DECISIVE_EP, otherwise the game was lost without a single identifiable
    error (`lost_without_blunder`). Games where the loser was still at or above
    `lo` after their last own move have no decisive mistake: flags,
    resignations and abandonments from positions the model still considered
    alive. (A plain "last crossing below 10%" rule was tried first: the model's
    P(win) decays gradually in bullet and a quarter of games were "decided" by
    the winner's routine recapture nudging the loser from 12% to 7%.)
    """
    lm = decided[decided["loser_moved"]].copy()
    last_own = lm.groupby("game_uuid").tail(1).set_index("game_uuid")
    first_ply = lm.groupby("game_uuid")["ply"].min()
    playable = lm[lm["p_win_before"] >= hi].groupby("game_uuid")["ply"].max()
    out = pd.DataFrame(index=last_own.index)
    out["loser_last_p_win"] = last_own["p_win_after"]
    lost = out["loser_last_p_win"] < lo
    out["slide_start_ply"] = playable.reindex(out.index).fillna(first_ply.reindex(out.index)).astype("Int64").where(lost)
    lm["slide_start"] = lm["game_uuid"].map(out["slide_start_ply"].astype("float64"))
    slide = lm[lm["ply"] >= lm["slide_start"]]
    cross = slide.loc[slide.groupby("game_uuid")["ep_loss"].idxmax()].set_index("game_uuid").reindex(out.index)
    out["decisive_found"] = lost & (cross["ep_loss"] >= MIN_DECISIVE_EP)
    out["lost_without_blunder"] = lost & ~out["decisive_found"]
    out["decisive_ply"] = cross["ply"].astype("Int64")
    out["decisive_is_last_playable"] = (cross["ply"] == out["slide_start_ply"]).astype("boolean").where(lost)
    out["decisive_by"] = cross["side"]
    out["decisive_ep_loss"] = cross["ep_loss"]
    out["decisive_cp_loss"] = cross["cp_loss"]
    out["decisive_label"] = cross["label"]
    out["decisive_phase"] = cross["phase"]
    out["decisive_p_win_before"] = cross["p_win_before"]
    out["decisive_p_win_after"] = cross["p_win_after"]
    out["decisive_clock_before"] = cross["clock_before_seconds"]
    return out


def with_player_view(moves: pd.DataFrame) -> pd.DataFrame:
    """Moves sorted by game and ply with the player's and the opponent's P(win) / expected points before and after."""
    m = moves.sort_values(["game_uuid", "ply"]).reset_index(drop=True)
    mine = m["is_player_move"].to_numpy()
    # Player's P(win) / expected points after every move: mover's p_win if the player moved, else the mover's p_loss.
    m["my_p_win_after"] = np.where(mine, m["p_win_after"], m["p_loss_after"])
    m["my_p_win_before"] = np.where(mine, m["p_win_before"], m["p_loss_before"])
    m["opp_p_win_after"] = np.where(mine, m["p_loss_after"], m["p_win_after"])
    m["opp_p_win_before"] = np.where(mine, m["p_loss_before"], m["p_win_before"])
    m["my_ep_after"] = np.where(mine, m["ep_after"], 1 - m["ep_after"])
    m["my_ep_before"] = np.where(mine, m["ep_before"], 1 - m["ep_before"])
    return m


def decompose_games(moves: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per game in `games`, keyed by uuid, in the order of `games`."""
    m = with_player_view(moves)
    m["ep_gain"] = (m["ep_after"] - m["ep_before"]).clip(lower=0)
    for t in TAIL:
        m[f"big{int(t * 100)}"] = m["ep_loss"] >= t
    m["is_blunder"] = m["label"] == "blunder"
    m["is_mistake"] = m["label"] == "mistake"
    m["is_inaccuracy"] = m["label"] == "inaccuracy"

    out = pd.DataFrame({"uuid": games["uuid"].to_numpy()}).set_index("uuid")
    g = m.groupby("game_uuid", sort=False)
    out["n_plies"] = g.size().astype("Int64")
    first, last = g.head(1).set_index("game_uuid"), g.tail(1).set_index("game_uuid")
    out["my_ep_start"] = first["my_ep_before"]
    out["my_ep_end"] = last["my_ep_after"]
    out["my_p_win_start"] = first["my_p_win_before"]
    out["my_p_win_end"] = last["my_p_win_after"]

    # Sums and counts by side (and by phase).
    agg = {"ep_loss": "sum", "cp_loss": "sum", "ep_gain": "sum", "is_blunder": "sum", "is_mistake": "sum", "is_inaccuracy": "sum", "ply": "size"}
    agg.update({f"big{int(t * 100)}": "sum" for t in TAIL})
    by_side = m.groupby(["game_uuid", "side"]).agg(agg).unstack("side")
    rename = {"ep_loss": "ep_lost", "cp_loss": "cp_lost", "ep_gain": "ep_gained", "is_blunder": "blunders", "is_mistake": "mistakes", "is_inaccuracy": "inaccuracies", "ply": "moves"}
    rename.update({f"big{int(t * 100)}": f"errors_ge_{int(t * 100)}" for t in TAIL})
    for column, name in rename.items():
        for side, prefix in SIDE_PREFIX:
            out[f"{prefix}_{name}"] = by_side.get((column, side))
    by_phase = m.groupby(["game_uuid", "side", "phase"], observed=True)["ep_loss"].sum().unstack(["side", "phase"])
    for phase in PHASES:
        for side, prefix in SIDE_PREFIX:
            out[f"{prefix}_ep_lost_{phase}"] = by_phase.get((side, phase))
    max_loss = m.groupby(["game_uuid", "side"])["ep_loss"].max().unstack("side")
    for side, prefix in SIDE_PREFIX:
        out[f"{prefix}_max_ep_loss"] = max_loss.get(side)
    count_columns = [c for c in out.columns if c.endswith(("_moves", "_blunders", "_mistakes", "_inaccuracies")) or "_errors_ge_" in c]
    out[count_columns] = out[count_columns].fillna(0).astype("Int64")
    sum_columns = [c for c in out.columns if "_ep_lost" in c or "_cp_lost" in c or "_ep_gained" in c]
    out[sum_columns] = out[sum_columns].astype("float64").fillna(0.0)
    out["net_ep"] = out["opp_ep_lost"] - out["my_ep_lost"]

    # Swings: each side's P(win) extremes over the game (positions after each move, plus the start).
    start = first.reset_index()[["game_uuid", "my_p_win_before", "opp_p_win_before"]].rename(columns={"my_p_win_before": "my_p_win_after", "opp_p_win_before": "opp_p_win_after"})
    track = pd.concat([m[["game_uuid", "my_p_win_after", "opp_p_win_after"]], start])
    extremes = track.groupby("game_uuid").agg(my_max=("my_p_win_after", "max"), my_min=("my_p_win_after", "min"), opp_max=("opp_p_win_after", "max"), opp_min=("opp_p_win_after", "min"))
    out["my_p_win_max"], out["my_p_win_min"] = extremes["my_max"], extremes["my_min"]
    out["opp_p_win_max"], out["opp_p_win_min"] = extremes["opp_max"], extremes["opp_min"]
    out["reached_winning"] = out["my_p_win_max"] >= WIN_T
    out["reached_losing"] = out["my_p_win_min"] <= LOSE_T
    out["opp_reached_winning"] = out["opp_p_win_max"] >= WIN_T
    out["opp_reached_losing"] = out["opp_p_win_min"] <= LOSE_T

    # Decisive mistake (decided games only), at the main threshold and the sensitivity one.
    outcome = games.set_index("uuid")["outcome"]
    m["outcome"] = m["game_uuid"].map(outcome)
    decided = m[m["outcome"].isin(["win", "loss"])].copy()
    decided["loser_moved"] = (decided["outcome"] == "loss") == decided["is_player_move"]
    main = _decisive(decided, DECISIVE_LO, DECISIVE_HI)
    alt = _decisive(decided, *DECISIVE_ALT)
    out = out.join(main)
    out["decisive_found"] = out["decisive_found"].astype("boolean")
    out["lost_without_blunder"] = out["lost_without_blunder"].astype("boolean")
    out["decisive_plies_from_end"] = (out["n_plies"] - out["decisive_ply"]).astype("Int64")
    out["decisive_strict_found"] = alt["decisive_found"].reindex(out.index).astype("boolean")
    out["decisive_strict_ply"] = alt["decisive_ply"].reindex(out.index).astype("Int64")
    out["decisive_strict_ep_loss"] = alt["decisive_ep_loss"].reindex(out.index)
    out["decisive_strict_phase"] = alt["decisive_phase"].reindex(out.index)
    return out.reset_index()


def first_reach(m: pd.DataFrame) -> pd.DataFrame:
    """Per game, each side's expected points at the first position where its P(win) reached >= WIN_T / <= LOSE_T.

    Uses the same positions as the reached_* flags (the start plus after each
    move), so the expected score belongs to exactly the games those flags select.
    """
    first = m.groupby("game_uuid", sort=False).head(1)
    start = pd.DataFrame({"game_uuid": first["game_uuid"].to_numpy(), "ply": 0, "my_p": first["my_p_win_before"].to_numpy(), "opp_p": first["opp_p_win_before"].to_numpy(), "my_ep": first["my_ep_before"].to_numpy()})
    after = pd.DataFrame({"game_uuid": m["game_uuid"].to_numpy(), "ply": m["ply"].to_numpy(), "my_p": m["my_p_win_after"].to_numpy(), "opp_p": m["opp_p_win_after"].to_numpy(), "my_ep": m["my_ep_after"].to_numpy()})
    track = pd.concat([start, after], ignore_index=True).sort_values(["game_uuid", "ply"], kind="stable")
    out = pd.DataFrame(index=pd.Index(track["game_uuid"].unique(), name="uuid"))
    out["my_ep_at_winning"] = track[track["my_p"] >= WIN_T].groupby("game_uuid")["my_ep"].first()
    out["opp_ep_at_winning"] = 1 - track[track["opp_p"] >= WIN_T].groupby("game_uuid")["my_ep"].first()
    out["my_ep_at_losing"] = track[track["my_p"] <= LOSE_T].groupby("game_uuid")["my_ep"].first()
    out["opp_ep_at_losing"] = 1 - track[track["opp_p"] <= LOSE_T].groupby("game_uuid")["my_ep"].first()
    return out.reset_index()


def write_table(table: pd.DataFrame, username: str) -> Path:
    path = repo_root() / "data" / username / "games_decomposed.parquet"
    table.to_parquet(path, index=False)
    return path


# --- game-level bootstrap helpers (each game is its own cluster) -------------------------------


def boot_rate(y, cond, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float, int]:
    """Mean of y over the games where cond holds, with a bootstrap CI over games; returns (est, lo, hi, n_cond)."""
    y, cond = np.asarray(y, dtype="float64"), np.asarray(cond, dtype="float64")
    y = np.where(cond > 0, y, 0.0)
    n = int(cond.sum())
    if n == 0:
        return float("nan"), float("nan"), float("nan"), 0
    est = float((y * cond).sum() / n)
    if n < 2:
        return est, float("nan"), float("nan"), n
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(y), size=(n_boot, len(y)))
    with np.errstate(invalid="ignore"):
        boot = (y[idx] * cond[idx]).sum(axis=1) / cond[idx].sum(axis=1)
    lo, hi = np.nanquantile(boot, [0.025, 0.975])
    return est, float(lo), float(hi), n


def boot_rate_diff(y1, c1, y2, c2, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """mean(y1 | c1) - mean(y2 | c2) with both computed on the same resampled games."""
    y1, c1, y2, c2 = (np.asarray(a, dtype="float64") for a in (y1, c1, y2, c2))
    y1, y2 = np.where(c1 > 0, y1, 0.0), np.where(c2 > 0, y2, 0.0)
    if c1.sum() < 2 or c2.sum() < 2:
        return float("nan"), float("nan"), float("nan")
    est = float((y1 * c1).sum() / c1.sum() - (y2 * c2).sum() / c2.sum())
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(y1), size=(n_boot, len(y1)))
    with np.errstate(invalid="ignore"):
        r1 = (y1[idx] * c1[idx]).sum(axis=1) / c1[idx].sum(axis=1)
        r2 = (y2[idx] * c2[idx]).sum(axis=1) / c2[idx].sum(axis=1)
    lo, hi = np.nanquantile(r1 - r2, [0.025, 0.975])
    return est, float(lo), float(hi)


def est_cols(prefix: str, values: tuple) -> dict:
    """{prefix: est, prefix_lo: lo, prefix_hi: hi} from (est, lo, hi, ...)."""
    return {prefix: values[0], f"{prefix}_lo": values[1], f"{prefix}_hi": values[2]}


def add_points(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    """Adds <prefix>_pp(_lo/_hi): a difference of two shares in percentage points (100 x the share difference)."""
    for c in ("", "_lo", "_hi"):
        frame[f"{prefix}_pp{c}"] = 100 * frame[f"{prefix}{c}"]
    return frame


def blank_unbounded_elo(frame: pd.DataFrame, expected: str, columns: list[str]) -> pd.DataFrame:
    """Blanks <column>_elo where expected + column is within ELO_SCORE_MARGIN of 0 or 1 (unbounded Elo)."""
    for column in columns:
        score = frame[expected] + frame[column]
        frame.loc[(score >= 1 - ELO_SCORE_MARGIN) | (score <= ELO_SCORE_MARGIN), f"{column}_elo"] = np.nan
    return frame


def mean_ci(values, n_boot: int = N_BOOT) -> tuple[float, float, float, int]:
    v = np.asarray(values, dtype="float64")
    ok = ~np.isnan(v)
    return boot_rate(np.where(ok, v, 0.0), ok.astype(float), n_boot=n_boot)


# --- grouping ----------------------------------------------------------------------------


def class_members(groups: list[str], tc_class: dict[str, str]) -> dict[str, list[str]]:
    """Time class -> its reported controls, in the order of `groups` (from the data's tc_class map)."""
    classes: dict[str, list[str]] = {}
    for g in groups:
        classes.setdefault(tc_class[g], []).append(g)
    return classes


def pooled_label(time_class: str) -> str:
    return f"all {time_class}"


def group_frames(frame: pd.DataFrame, groups: list[str], tc_class: dict[str, str] | None = None):
    """(label, rows) per reported time control; with `tc_class`, also "all <class>" for classes with several controls."""
    for g in groups:
        yield g, frame[frame["tc_group"] == g]
    if tc_class is not None:
        for c, members in class_members(groups, tc_class).items():
            if len(members) > 1:
                yield pooled_label(c), frame[frame["tc_group"].isin(members)]


def panel_order(groups: list[str], tc_class: dict[str, str]) -> tuple[list[str], set[str]]:
    """Controls in order, each class followed by its pooled label when there is one; plus the set of pooled labels."""
    out: list[str] = []
    pooled: set[str] = set()
    for c, members in class_members(groups, tc_class).items():
        out += members
        if len(members) > 1:
            out.append(pooled_label(c))
            pooled.add(pooled_label(c))
    return out, pooled


def first_of_class(groups: list[str], tc_class: dict[str, str]) -> list[str]:
    """The first reported control of each time class (the first panel of each row in charts.group_grid)."""
    return [members[0] for members in class_members(groups, tc_class).values()]


# --- charts ------------------------------------------------------------------------------


def rotate(ax, angle: int = 35) -> None:
    for tick in ax.get_xticklabels():
        tick.set_rotation(angle)
        tick.set_ha("right")


def stacked_sides(ax, table: pd.DataFrame, order: list[str], segments: list[str], value: str, n_col: str, total: pd.DataFrame | None = None, pooled: set[str] | None = None) -> None:
    """Two stacked bars per level of `order` (you, opponents), one segment per entry of `segments`.

    `table` is long: tc_group, side, segment, value. Side sets the colour (you
    blue, opponents grey); segments are shaded from solid to pale. Bars whose
    case count is below MIN_CASES are not drawn; a 'thin' label marks them.
    `total` (optional, a frame with tc_group, side, est, lo, hi) adds a CI
    whisker at height `est` (the full bar, or the bottom segment). Levels in
    `pooled` (the "all <class>" bars) are set apart by a dotted divider and an
    italic label, so they are not read as one more control.
    """
    import matplotlib.colors as mcolors

    alphas = np.linspace(1.0, MIN_SEGMENT_ALPHA, len(segments))
    width = 0.38
    for k, side in enumerate(("player", "opponent")):
        base = charts.SIDE_COLOURS[side]
        offset = (k - 0.5) * width
        for i, level in enumerate(order):
            part = table[(table["tc_group"] == level) & (table["side"] == side)]
            if part.empty:
                continue
            if int(part[n_col].iloc[0]) < MIN_CASES:
                ax.text(i + offset, 0.01, "thin", rotation=90, ha="center", va="bottom", fontsize=7, color=charts.INK_MUTED)
                continue
            bottom = 0.0
            for seg, alpha in zip(segments, alphas):
                v = float(part.loc[part["segment"] == seg, value].sum())
                colour = mcolors.to_rgba(base, alpha)
                ax.bar(i + offset, v, bottom=bottom, width=width * 0.92, color=colour, edgecolor=charts.SURFACE, linewidth=0.6)
                bottom += v
            if total is not None:
                t = total[(total["tc_group"] == level) & (total["side"] == side)]
                if not t.empty:
                    est, lo, hi = (float(t[c].iloc[0]) for c in ("est", "lo", "hi"))
                    ax.errorbar(i + offset, est, yerr=[[est - lo], [hi - est]], fmt="none", ecolor=charts.INK_SECONDARY, elinewidth=1)
    ax.set_xticks(np.arange(len(order)), order)
    ax.grid(axis="x", visible=False)
    pooled = pooled or set()
    for i, level in enumerate(order):
        if level in pooled:
            ax.axvline(i - 0.5, color=charts.INK_MUTED, linewidth=0.8, linestyle=":", zorder=0)
    for tick in ax.get_xticklabels():
        if tick.get_text() in pooled:
            tick.set_fontstyle("italic")


def stacked_legend(fig, segments: list[str], **kwargs) -> None:
    """Legend: the two sides by colour, the segments by shade (in neutral grey)."""
    import matplotlib.colors as mcolors
    from matplotlib.patches import Patch

    alphas = np.linspace(1.0, MIN_SEGMENT_ALPHA, len(segments))
    handles = [Patch(color=charts.PLAYER, label="you"), Patch(color=charts.OPPONENT, label="opponents")]
    handles += [Patch(color=mcolors.to_rgba(charts.INK_SECONDARY, a), label=s) for s, a in zip(segments, alphas)]
    fig.legend(handles=handles, ncol=len(handles), **kwargs)


def percent_axis(ax) -> None:
    from matplotlib.ticker import PercentFormatter

    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))


# --- 1. points per game ------------------------------------------------------------------------


def points_per_game(g: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows, budget, check = [], [], []
    for label, part in group_frames(g, groups):
        ones = np.ones(len(part))
        me, opp = part["my_ep_lost"].to_numpy(float), part["opp_ep_lost"].to_numpy(float)
        diff = boot_rate(me - opp, ones)
        row = {"tc_group": label, **est_cols("player", boot_rate(me, ones)), **est_cols("opponent", boot_rate(opp, ones)), **est_cols("diff", diff)}
        row["diff_per_100_games"] = 100 * diff[0]
        row["margin"], row["margin_lo"], row["margin_hi"] = -diff[0], -diff[2], -diff[1]
        row["expected"] = EVEN_SCORE
        row.update(est_cols("player_cp", boot_rate(part["my_cp_lost"].to_numpy(float), ones)))
        row.update(est_cols("opponent_cp", boot_rate(part["opp_cp_lost"].to_numpy(float), ones)))
        row["player_moves_per_game"] = float(part["my_moves"].astype(float).mean())
        row["opponent_moves_per_game"] = float(part["opp_moves"].astype(float).mean())
        row["player_total"] = float(me.sum())
        row["opponent_total"] = float(opp.sum())
        row["games"] = len(part)
        rows.append(row)

        score, start, end = part["my_score"].to_numpy(float), part["my_ep_start"].to_numpy(float), part["my_ep_end"].to_numpy(float)
        residual = score - (start - me + opp)
        b = {"tc_group": label, **est_cols("score", boot_rate(score, ones)), **est_cols("expected", boot_rate(start, ones))}
        b.update(est_cols("excess", boot_rate(score - start, ones)))
        b.update(est_cols("player_ep_lost", boot_rate(me, ones)))
        b.update(est_cols("opponent_ep_lost", boot_rate(opp, ones)))
        b.update(est_cols("residual", boot_rate(residual, ones)))
        b.update(est_cols("score_minus_ep_at_end", boot_rate(score - end, ones)))
        b["corr_net_ep_score"] = float(np.corrcoef(part["net_ep"].to_numpy(float), score)[0, 1])
        b["games"] = len(part)
        budget.append(b)

        binned = part.assign(bin=pd.cut(part["net_ep"], bins=NET_EP_EDGES, labels=NET_EP_LABELS, right=False))
        for lab in NET_EP_LABELS:
            cell = binned[binned["bin"] == lab]
            c = {"tc_group": label, "net_ep_bin": lab}
            ones_c = np.ones(len(cell))
            c.update(est_cols("score", boot_rate(cell["my_score"].to_numpy(float), ones_c)))
            c.update(est_cols("ep_at_move_1", boot_rate(cell["my_ep_start"].to_numpy(float), ones_c)))
            c.update(est_cols("ep_at_end", boot_rate(cell["my_ep_end"].to_numpy(float), ones_c)))
            c["games"] = len(cell)
            c["thin"] = len(cell) < MIN_CASES
            check.append(c)
    table = add_elo(pd.DataFrame(rows), "expected", ["margin", "margin_lo", "margin_hi"])
    table["thin"] = table["games"] < MIN_CASES
    budget = add_elo(pd.DataFrame(budget), "expected", ["excess", "excess_lo", "excess_hi"])
    budget["thin"] = budget["games"] < MIN_CASES
    return table, budget, pd.DataFrame(check)


def chart_points(table: pd.DataFrame, groups: list[str]):
    import matplotlib.pyplot as plt

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(9.5, 5.6), sharex=True, gridspec_kw={"height_ratios": [1.6, 1]})
    charts.side_bars(top, table, "tc_group", order=groups)
    top.set_ylabel("expected points given\naway per game")
    top.set_title("How many points does each side give away per game?", fontsize=11, color=charts.INK)
    top.legend(loc="upper left", ncol=2)
    charts.zero_line(bottom)
    charts.estimate(bottom, table, "tc_group", y="diff", lo="diff_lo", hi="diff_hi", order=groups, colour=charts.PLAYER)
    bottom.set_ylabel("you minus opponents\n(below 0: you give away less)")
    rotate(bottom)
    charts.footnote(fig, "Expected points = calibrated win probability + half the draw probability; each move's loss is summed per game. Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    return fig


# --- 2. concentration ------------------------------------------------------------------------


def _curve(e_desc: np.ndarray, weights: np.ndarray, shares: np.ndarray) -> np.ndarray:
    cw = np.concatenate([[0.0], np.cumsum(weights)])
    ce = np.concatenate([[0.0], np.cumsum(weights * e_desc)])
    return np.interp(shares, cw / cw[-1], ce / ce[-1])


def concentration(moves: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Share of all expected points lost that comes from the costliest x% of moves, per side, with a paired CI."""
    shares = np.asarray(TOP_SHARES)
    rows, tail = [], []
    for label, part in group_frames(moves, groups):
        part = part[part["ep_loss"].notna()]
        codes, inverse = np.unique(part["game_uuid"].to_numpy(), return_inverse=True)
        sides = {}
        for side in ("player", "opponent"):
            mask = (part["side"] == side).to_numpy()
            e, gi = part["ep_loss"].to_numpy(float)[mask], inverse[mask]
            order = np.argsort(-e, kind="stable")
            sides[side] = (e[order], gi[order])
        est = {s: _curve(e, np.ones(len(e)), shares) for s, (e, _) in sides.items()}
        rng = np.random.default_rng(SEED)
        draws = {s: np.empty((N_BOOT_CURVE, len(shares))) for s in sides}
        for b in range(N_BOOT_CURVE):
            w = np.bincount(rng.integers(0, len(codes), len(codes)), minlength=len(codes)).astype(float)
            for s, (e, gi) in sides.items():
                draws[s][b] = _curve(e, w[gi], shares)
        diff = draws["player"] - draws["opponent"]
        for k, q in enumerate(shares):
            row = {"tc_group": label, "top_share_of_moves": q}
            for s in sides:
                row[s] = est[s][k]
                row[f"{s}_lo"], row[f"{s}_hi"] = np.quantile(draws[s][:, k], [0.025, 0.975])
            row["diff"] = est["player"][k] - est["opponent"][k]
            row["diff_lo"], row["diff_hi"] = np.quantile(diff[:, k], [0.025, 0.975])
            row["games"] = len(codes)
            row["player_moves"] = len(sides["player"][0])
            row["opponent_moves"] = len(sides["opponent"][0])
            rows.append(row)

        for t in TAIL:
            frame = part.assign(big=(part["ep_loss"] >= t).astype(float), big_loss=np.where(part["ep_loss"] >= t, part["ep_loss"], 0.0))
            cmp = compare_sides(frame, "big", n_boot=N_BOOT).iloc[0]
            row = {"tc_group": label, "min_ep_loss": t}
            for s in ("player", "opponent", "diff"):
                row[f"move_share_{s}"], row[f"move_share_{s}_lo"], row[f"move_share_{s}_hi"] = cmp[s], cmp[f"{s}_lo"], cmp[f"{s}_hi"]
            row["per_100_moves_player"] = 100 * cmp["player"]
            row["per_100_moves_opponent"] = 100 * cmp["opponent"]
            for s in ("player", "opponent"):
                sub = frame[frame["side"] == s]
                d = ratio_bootstrap(sub["big_loss"], sub["ep_loss"], sub["game_uuid"], n_boot=N_BOOT, seed=SEED)
                row[f"damage_share_{s}"], row[f"damage_share_{s}_lo"], row[f"damage_share_{s}_hi"] = d
            row["games"] = int(cmp["games"])
            row["moves"] = int(cmp["n"])
            tail.append(row)
    table = pd.DataFrame(rows)
    table["thin"] = table["games"] < MIN_CASES
    tail = pd.DataFrame(tail)
    tail["thin"] = tail["games"] < MIN_CASES
    return table, tail


def chart_concentration(table: pd.DataFrame, groups: list[str], tc_class: dict[str, str]):
    fig, axes = charts.group_grid(groups, sharey=True, title="How much of the damage comes from the costliest moves?", panel=(2.9, 2.4))
    for g, ax in axes.items():
        part = table[(table["tc_group"] == g) & ~table["thin"]]
        if part.empty:
            charts.thin_note(ax)
            continue
        ax.plot([0.01, 0.5], [0.01, 0.5], color=charts.AXIS, linewidth=1, linestyle="--", zorder=1)
        charts.side_lines(ax, part, "top_share_of_moves", markers=True)
        ax.set_xscale("log")
        ax.set_xticks(TOP_SHARE_TICKS, [f"{100 * q:g}%" for q in TOP_SHARE_TICKS])
        ax.minorticks_off()
        percent_axis(ax)
        ax.set_ylim(0, 1)
    for g in first_of_class(groups, tc_class):
        axes[g].set_ylabel("share of all expected\npoints given away")
    for ax in axes.values():
        ax.set_xlabel("costliest share of moves")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Each side's moves sorted from costliest down; dashed line: damage spread evenly. Bands: 95% CI, resampling games.")
    fig.tight_layout()
    return fig


# --- 3. decisive mistake ------------------------------------------------------------------------


DECISIVE_CATEGORIES = {
    "decisive blunder": "decisive_found",
    "lost without a blunder": "lost_without_blunder",
    "lost while still alive": "alive",
    "decisive blunder (strict thresholds)": "decisive_strict_found",
}


# Clock measures of the decisive blunder compared between the sides (3c).
DECISIVE_CLOCK = {
    "clock_seconds": "seconds left before the decisive blunder",
    "clock_buffer_seconds": "seconds left beyond one increment",
    "clock_share_of_base": "seconds left as a share of the base time",
}


def decisive_tables(g: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    decided = g[g["outcome"].isin(["win", "loss"])].copy()
    for col in ("decisive_found", "lost_without_blunder", "decisive_strict_found"):
        decided[col] = decided[col].fillna(False).astype(bool)
    decided["alive"] = ~decided["decisive_found"] & ~decided["lost_without_blunder"]
    decided["on_board"] = ~decided["alive"]
    decided["clock_seconds"] = decided["decisive_clock_before"].astype(float)
    decided["clock_buffer_seconds"] = decided["clock_seconds"] - decided["increment_seconds"].astype(float)
    decided["clock_share_of_base"] = decided["clock_seconds"] / decided["base_seconds"].astype(float)
    rows, profile, clock = [], [], []
    for label, part in group_frames(decided, groups):
        lost, won = (part["outcome"] == "loss").to_numpy(float), (part["outcome"] == "win").to_numpy(float)
        for column, measure in DECISIVE_CLOCK.items():
            y = part[column].to_numpy(float)
            known = part["decisive_found"].to_numpy(float) * (~np.isnan(y)).astype(float)
            y = np.nan_to_num(y)
            p, o = boot_rate(y, lost * known), boot_rate(y, won * known)
            c = {"tc_group": label, "measure": column, "description": measure, **est_cols("player", p), **est_cols("opponent", o), **est_cols("diff", boot_rate_diff(y, lost * known, y, won * known))}
            c["player_games"], c["opponent_games"] = p[3], o[3]
            clock.append(c)
        for category, col in DECISIVE_CATEGORIES.items():
            y = part[col].to_numpy(float)
            p, o = boot_rate(y, lost), boot_rate(y, won)
            row = {"tc_group": label, "category": category, "base": "lost games", **est_cols("player", p), **est_cols("opponent", o), **est_cols("diff", boot_rate_diff(y, lost, y, won))}
            row["player_losses"], row["opponent_losses"] = p[3], o[3]
            rows.append(row)
        # Among games lost on the board: how often one move can be named.
        y = part["decisive_found"].to_numpy(float)
        ob = part["on_board"].to_numpy(float)
        p, o = boot_rate(y, lost * ob), boot_rate(y, won * ob)
        row = {"tc_group": label, "category": "decisive blunder", "base": "games lost on the board", **est_cols("player", p), **est_cols("opponent", o), **est_cols("diff", boot_rate_diff(y, lost * ob, y, won * ob))}
        row["player_losses"], row["opponent_losses"] = p[3], o[3]
        rows.append(row)

        found = part[part["decisive_found"]]
        for outcome, side in (("loss", "player"), ("win", "opponent")):
            f = found[found["outcome"] == outcome]
            r = {"tc_group": label, "side": side}
            r.update(est_cols("ep_loss", mean_ci(f["decisive_ep_loss"])))
            r.update(est_cols("cp_loss", mean_ci(f["decisive_cp_loss"])))
            r.update(est_cols("p_win_before", mean_ci(f["decisive_p_win_before"])))
            r.update(est_cols("p_win_after", mean_ci(f["decisive_p_win_after"])))
            r.update(est_cols("from_last_playable", mean_ci(f["decisive_is_last_playable"].astype(float))))
            r.update(est_cols("plies_from_end", mean_ci(f["decisive_plies_from_end"].astype(float))))
            r.update(est_cols("clock_seconds", mean_ci(f["decisive_clock_before"])))
            r.update(est_cols("clock_buffer_seconds", mean_ci(f["decisive_clock_before"] - f["increment_seconds"])))
            r.update(est_cols("clock_share_of_base", mean_ci(f["decisive_clock_before"] / f["base_seconds"])))
            for phase in PHASES:
                r.update(est_cols(f"share_{phase}", mean_ci((f["decisive_phase"] == phase).astype(float))))
            r["games"] = len(f)
            r["thin"] = len(f) < MIN_CASES
            profile.append(r)
    table = add_points(pd.DataFrame(rows), "diff")
    table["thin"] = (table["player_losses"] < MIN_CASES) | (table["opponent_losses"] < MIN_CASES)
    clock = pd.DataFrame(clock)
    clock["thin"] = (clock["player_games"] < MIN_CASES) | (clock["opponent_games"] < MIN_CASES)
    return table, pd.DataFrame(profile), clock


def chart_decisive(table: pd.DataFrame, groups: list[str]):
    segs = ["decisive blunder", "lost without a blunder", "lost while still alive"]
    part = table[(table["base"] == "lost games") & table["category"].isin(segs)]
    long = []
    for _, r in part.iterrows():
        long.append({"tc_group": r["tc_group"], "side": "player", "segment": r["category"], "value": r["player"], "n": r["player_losses"]})
        long.append({"tc_group": r["tc_group"], "side": "opponent", "segment": r["category"], "value": r["opponent"], "n": r["opponent_losses"]})
    long = pd.DataFrame(long)
    first = part[part["category"] == segs[0]]
    total = pd.concat([first.assign(side=side, est=first[side], lo=first[f"{side}_lo"], hi=first[f"{side}_hi"]) for side in ("player", "opponent")])
    fig, ax = charts.single("How often does one move decide a lost game?", size=(9.5, 3.8))
    stacked_sides(ax, long, groups, segs, "value", "n", total=total[["tc_group", "side", "est", "lo", "hi"]])
    percent_axis(ax)
    ax.set_ylim(0, 1)
    ax.set_ylabel("share of the side's lost games")
    rotate(ax)
    stacked_legend(fig, segs, loc="upper center", bbox_to_anchor=(0.5, 0.0), fontsize=8)
    charts.footnote(fig, "\n\nBlue: your losses; grey: your opponents' losses (your wins). Decisive blunder: the loser's costliest move after their last playable position (P(win) >= 20%) lost >= 0.05 expected points; still alive: the loser's P(win) was still >= 10% after their last move. Whisker: 95% CI of the decisive-blunder share, resampling games.")
    fig.tight_layout()
    return fig


# --- 4. phase ------------------------------------------------------------------------------


def phase_tables(g: pd.DataFrame, moves: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    played = moves.groupby(["game_uuid", "side", "phase"], observed=True).size().unstack(["side", "phase"]).notna()
    rows = []
    for label, part in group_frames(g, groups):
        ones = np.ones(len(part))
        total_me, total_opp = part["my_ep_lost"].sum(), part["opp_ep_lost"].sum()
        for phase in PHASES:
            me, opp = part[f"my_ep_lost_{phase}"].to_numpy(float), part[f"opp_ep_lost_{phase}"].to_numpy(float)
            row = {"tc_group": label, "phase": phase, **est_cols("player", boot_rate(me, ones)), **est_cols("opponent", boot_rate(opp, ones)), **est_cols("diff", boot_rate(me - opp, ones))}
            row["player_share_of_total"] = float(me.sum() / total_me)
            row["opponent_share_of_total"] = float(opp.sum() / total_opp)
            idx = part["uuid"]
            row["player_games_in_phase"] = int(played.get(("player", phase), pd.Series(dtype=bool)).reindex(idx).fillna(False).sum())
            row["opponent_games_in_phase"] = int(played.get(("opponent", phase), pd.Series(dtype=bool)).reindex(idx).fillna(False).sum())
            row["games"] = len(part)
            rows.append(row)
    per_game = pd.DataFrame(rows)
    per_game["thin"] = (per_game["player_games_in_phase"] < MIN_PHASE_GAMES) | (per_game["opponent_games_in_phase"] < MIN_PHASE_GAMES)

    frame = moves.assign(
        big=(moves["ep_loss"] >= TAIL[1]).astype(float),
        blunder_cp=(moves["label"] == "blunder").astype(float),
        phase_all="all",
    )
    measures = {"ep_loss": "expected points lost per move", "big": f"share of moves losing >= {TAIL[1]:g} expected points", "cp_loss": "centipawns lost per move", "blunder_cp": "share of moves losing >= 300 centipawns"}
    rows = []
    for label, part in group_frames(frame, groups):
        for column, measure in measures.items():
            t = pd.concat([compare_sides(part, column, by=["phase_all"], n_boot=N_BOOT).rename(columns={"phase_all": "phase"}), compare_sides(part, column, by=["phase"], n_boot=N_BOOT)], ignore_index=True)
            t.insert(0, "measure", measure)
            t.insert(0, "tc_group", label)
            rows.append(t)
    per_move = pd.concat(rows, ignore_index=True).rename(columns={"n": "moves"})
    per_move["phase"] = pd.Categorical(per_move["phase"].astype(str), ["all", *PHASES], ordered=True)
    per_move = per_move.sort_values(["measure", "phase"], kind="stable")
    per_move["_g"] = per_move["tc_group"].map({g: i for i, g in enumerate(groups)})
    per_move = per_move.sort_values(["_g", "measure", "phase"], kind="stable").drop(columns="_g")
    per_move["thin"] = per_move["games"] < MIN_PHASE_GAMES
    return per_game, per_move


def chart_phase(table: pd.DataFrame, groups: list[str], tc_class: dict[str, str]):
    fig, axes = charts.group_grid(groups, sharey=True, title="In which phase do you give away the most?", panel=(2.9, 2.4))
    for g, ax in axes.items():
        part = table[table["tc_group"] == g].copy()
        for side in ("player", "opponent"):
            for c in ("", "_lo", "_hi"):
                part.loc[part["thin"], f"{side}{c}"] = np.nan
        charts.side_bars(ax, part, "phase", order=PHASES)
    for g in first_of_class(groups, tc_class):
        axes[g].set_ylabel("expected points given\naway per game")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Sum over each side's moves in that phase (material heuristic), averaged over all games of the control (0 for games that never reached the phase). Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    return fig


# --- 5. conversion and 6. saves -------------------------------------------------------------


def outcome_arrays(part: pd.DataFrame) -> dict[str, np.ndarray]:
    score = part["my_score"].to_numpy(float)
    return {"player_win": (part["outcome"] == "win").to_numpy(float), "opponent_win": (part["outcome"] == "loss").to_numpy(float), "player_score": score, "opponent_score": 1 - score}


def conversion_tables(g: pd.DataFrame, groups: list[str], tc_class: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Conversion (reached >= WIN_T) and saves (reached <= LOSE_T) per side; plus you-minus-opponents differences."""
    rows, diffs = [], []
    specs = {
        "conversion": {"player": ("reached_winning", "my_ep_at_winning"), "opponent": ("opp_reached_winning", "opp_ep_at_winning")},
        "save": {"player": ("reached_losing", "my_ep_at_losing"), "opponent": ("opp_reached_losing", "opp_ep_at_losing")},
    }
    for label, part in group_frames(g, groups, tc_class):
        a = outcome_arrays(part)
        ones = np.ones(len(part))
        for kind, sides in specs.items():
            per_side = {}
            for side, (flag, ep_col) in sides.items():
                cond = part[flag].fillna(False).to_numpy(float)
                win = a[f"{side}_win"]
                other_win = a["opponent_win" if side == "player" else "player_win"]
                score = a[f"{side}_score"]
                expected = part[ep_col].fillna(0.0).to_numpy(float)
                rate_y = win if kind == "conversion" else 1 - other_win  # conversion: won; save: did not lose
                row = {"tc_group": label, "situation": kind, "side": side}
                row.update(est_cols("reached", boot_rate(cond, ones)))
                rate = boot_rate(rate_y, cond)
                row.update(est_cols("rate", rate))
                row.update(est_cols("score", boot_rate(score, cond)))
                row["expected"] = boot_rate(expected, cond)[0]
                row.update(est_cols("excess", boot_rate(score - expected, cond)))
                row["cases"] = rate[3]
                row["games"] = len(part)
                rows.append(row)
                per_side[side] = (rate_y, score, expected, cond)
            (py, ps, pe, pc), (oy, os_, oe, oc) = per_side["player"], per_side["opponent"]
            d = {"tc_group": label, "situation": kind}
            d.update(est_cols("rate_diff", boot_rate_diff(py, pc, oy, oc)))
            d.update(est_cols("score_diff", boot_rate_diff(ps, pc, os_, oc)))
            d.update(est_cols("excess_diff", boot_rate_diff(ps - pe, pc, os_ - oe, oc)))
            d["player_cases"], d["opponent_cases"] = int(pc.sum()), int(oc.sum())
            d["games"] = len(part)
            diffs.append(d)
    table = add_elo(pd.DataFrame(rows), "expected", ["excess", "excess_lo", "excess_hi"])
    table = blank_unbounded_elo(table, "expected", ["excess", "excess_lo", "excess_hi"])
    table["thin"] = table["cases"] < MIN_CASES
    diffs = add_points(pd.DataFrame(diffs), "rate_diff")
    diffs["thin"] = (diffs["player_cases"] < MIN_CASES) | (diffs["opponent_cases"] < MIN_CASES)
    return table, diffs


def threshold_sensitivity(g: pd.DataFrame, groups: list[str], tc_class: dict[str, str]) -> pd.DataFrame:
    rows = []
    for label, part in group_frames(g, groups, tc_class):
        a = outcome_arrays(part)
        for t in SENSITIVITY_THRESHOLDS:
            for side, prefix in SIDE_PREFIX:
                win = a[f"{side}_win"]
                other_win = a["opponent_win" if side == "player" else "player_win"]
                conv = boot_rate(win, (part[f"{prefix}_p_win_max"] >= t).to_numpy(float))
                save = boot_rate(1 - other_win, (part[f"{prefix}_p_win_min"] <= round(1 - t, 6)).to_numpy(float))
                rows.append({"tc_group": label, "threshold": t, "side": side, **est_cols("conversion", conv), "conversion_cases": conv[3], **est_cols("save", save), "save_cases": save[3], "games": len(part)})
    out = pd.DataFrame(rows)
    out["thin"] = (out["conversion_cases"] < MIN_CASES) | (out["save_cases"] < MIN_CASES)
    return out


def model_by_side(m: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Observed win rate of each side by the model's P(win) for that side after every move (even-handedness check)."""
    frame = pd.DataFrame(
        {
            "tc_group": m["tc_group"].to_numpy(),
            "game_uuid": m["game_uuid"].to_numpy(),
            "player_p": m["my_p_win_after"].to_numpy(),
            "opponent_p": m["opp_p_win_after"].to_numpy(),
            "player_won": (m["outcome"] == "win").to_numpy(float),
            "opponent_won": (m["outcome"] == "loss").to_numpy(float),
        }
    )
    rows = []
    labelled = [*group_frames(frame, groups), ("all games", frame)]
    for label, part in labelled:
        for lo, hi in zip(CALIBRATION_EDGES[:-1], CALIBRATION_EDGES[1:]):
            row = {"tc_group": label, "p_win_bin": f"{lo:.1f}-{hi:.1f}", "bin_mid": (lo + hi) / 2}
            for side in ("player", "opponent"):
                p = part[f"{side}_p"]
                cell = part[(p >= lo) & ((p < hi) if hi < 1 else (p <= hi))]
                est = cluster_bootstrap_mean(cell[f"{side}_won"], cell["game_uuid"], n_boot=N_BOOT, seed=SEED)
                row.update(est_cols(f"{side}_observed", est))
                row[f"{side}_positions"] = len(cell)
                row[f"{side}_games"] = int(cell["game_uuid"].nunique())
            rows.append(row)
    out = pd.DataFrame(rows)
    out["diff"] = out["player_observed"] - out["opponent_observed"]
    out["thin"] = (out["player_games"] < MIN_CASES) | (out["opponent_games"] < MIN_CASES)
    return out


def chart_conversion(table: pd.DataFrame, groups: list[str]):
    part = table[(table["situation"] == "conversion") & table["tc_group"].isin(groups)]
    wide = part.pivot(index="tc_group", columns="side", values=["score", "score_lo", "score_hi", "expected", "thin"])
    frame = pd.DataFrame({"tc_group": wide.index})
    for side in ("player", "opponent"):
        thin = wide[("thin", side)].astype(bool).to_numpy()
        for c in ("", "_lo", "_hi"):
            frame[f"{side}{c}"] = np.where(thin, np.nan, wide[(f"score{c}", side)].to_numpy(float))
        frame[f"{side}_expected"] = np.where(thin, np.nan, wide[("expected", side)].to_numpy(float))
    fig, ax = charts.single("How often do you convert a winning position?", size=(9.5, 3.6))
    charts.side_bars(ax, frame, "tc_group", order=groups)
    rank = {g: i for i, g in enumerate(groups)}
    for k, side in enumerate(("player", "opponent")):
        xs = frame["tc_group"].map(rank).to_numpy(float) + (k - 0.5) * 0.38
        ax.scatter(xs, frame[f"{side}_expected"], marker="_", s=180, color=charts.INK, linewidths=1.6, zorder=4, label="model's expected score" if k == 0 else None)
    for k, side in enumerate(("player", "opponent")):
        for i, g in enumerate(groups):
            if np.isnan(frame.loc[frame["tc_group"] == g, side]).all():
                ax.text(i + (k - 0.5) * 0.38, CONVERSION_AXIS_FLOOR + 0.01, "thin", rotation=90, ha="center", va="bottom", fontsize=7, color=charts.INK_MUTED)
    ax.set_ylim(CONVERSION_AXIS_FLOOR, 1.0)
    percent_axis(ax)
    ax.set_ylabel("score from positions\nrated >= 80% to win")
    rotate(ax)
    charts.legend(fig, loc="upper center", bbox_to_anchor=(0.5, 0.0))
    charts.footnote(fig, "\n\nScore (win 1, draw 0.5) in games where the side's model P(win) reached 80% at some point; black tick: the model's expected score at that first moment. Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    return fig


def how_saved(g: pd.DataFrame, groups: list[str], tc_class: dict[str, str]) -> pd.DataFrame:
    rows = []
    for label, part in group_frames(g, groups, tc_class):
        for side, flag, own, other in (("player", "reached_losing", "my_result", "opponent_result"), ("opponent", "opp_reached_losing", "opponent_result", "my_result")):
            cond = part[flag].fillna(False).to_numpy(float)
            r, o = part[own], part[other]
            kinds = {
                "won on time": (r == "win") & (o == "timeout"),
                "won on the board": (r == "win") & (o != "timeout"),
                "drawn on time": r == "timevsinsufficient",
                "drawn by stalemate": r == "stalemate",
                "drawn otherwise": r.isin(["repetition", "agreed", "insufficient", "50move"]),
            }
            saved = sum(k.to_numpy(float) for k in kinds.values()) * cond
            for kind in SAVE_KINDS:
                y = kinds[kind].to_numpy(float)
                est = boot_rate(y, cond)
                rows.append({"tc_group": label, "side": side, "how": kind, **est_cols("share_of_lost_positions", est), "share_of_saves": float((y * cond).sum() / saved.sum()) if saved.sum() else np.nan, "saves_of_this_kind": int((y * cond).sum()), "saves": int(saved.sum()), "cases": est[3]})
    out = pd.DataFrame(rows)
    out["thin"] = out["cases"] < MIN_CASES  # share_of_lost_positions rests on cases
    out["thin_share_of_saves"] = out["saves"] < MIN_CASES  # share_of_saves rests on the (often few) saves
    return out


def chart_saves(how: pd.DataFrame, saves: pd.DataFrame, groups: list[str], tc_class: dict[str, str]):
    order, pooled = panel_order(groups, tc_class)
    long = how.assign(segment=how["how"].map(SAVE_CHART_KINDS)).groupby(["tc_group", "side", "segment"], sort=False, as_index=False).agg(value=("share_of_lost_positions", "sum"), cases=("cases", "first"))
    segments = list(dict.fromkeys(SAVE_CHART_KINDS.values()))
    s = saves[saves["situation"] == "save"].rename(columns={"rate": "est", "rate_lo": "lo", "rate_hi": "hi"})
    fig, ax = charts.single("How often do you save a lost position, and how?", size=(10.5, 3.8))
    stacked_sides(ax, long, order, segments, "value", "cases", total=s, pooled=pooled)
    percent_axis(ax)
    ax.set_ylabel("share of positions rated\n<= 20% to win not lost")
    rotate(ax)
    stacked_legend(fig, segments, loc="upper center", bbox_to_anchor=(0.5, 0.0), fontsize=8)
    charts.footnote(fig, "\n\nGames in which the side's model P(win) fell to 20% or less at some point; bar height: share not lost (whisker: 95% CI, resampling games), split by how the game ended. 'all ...': the controls of a class pooled.")
    fig.tight_layout()
    return fig


# --- 7. lost while still alive -------------------------------------------------------------


def alive_tables(g: pd.DataFrame, groups: list[str], tc_class: dict[str, str]) -> pd.DataFrame:
    decided = g[g["outcome"].isin(["win", "loss"])].copy()
    decided["alive"] = ~decided["decisive_found"].fillna(False).astype(bool) & ~decided["lost_without_blunder"].fillna(False).astype(bool)
    rows = []
    for label, part in group_frames(decided, groups, tc_class):
        lost, won = (part["outcome"] == "loss").to_numpy(float), (part["outcome"] == "win").to_numpy(float)
        alive = part["alive"].to_numpy(float)
        # loser's termination: your result in your losses, the opponent's in your wins
        term = np.where(part["outcome"] == "loss", part["my_result"], part["opponent_result"])
        for t in ["any", *ALIVE_TERMINATIONS]:
            is_t = np.ones(len(part)) if t == "any" else (term == t).astype(float)
            y = alive * is_t
            p, o = boot_rate(y, lost), boot_rate(y, won)
            row = {"tc_group": label, "termination": t, **est_cols("player", p), **est_cols("opponent", o), **est_cols("diff", boot_rate_diff(y, lost, y, won))}
            pw, ow = boot_rate(alive, lost * is_t), boot_rate(alive, won * is_t)
            row.update(est_cols("player_within", pw))
            row.update(est_cols("opponent_within", ow))
            row["player_term_share"] = float((is_t * lost).sum() / lost.sum())
            row["opponent_term_share"] = float((is_t * won).sum() / won.sum())
            # the loser's P(win) after their last own move (missing when the loser never moved)
            pl = part["loser_last_p_win"].to_numpy(float)
            known = (~np.isnan(pl)).astype(float)
            row.update(est_cols("player_last_p_win", boot_rate(pl, lost * is_t * alive * known)))
            row.update(est_cols("opponent_last_p_win", boot_rate(pl, won * is_t * alive * known)))
            row["player_cases"], row["opponent_cases"] = pw[3], ow[3]
            row["player_losses"], row["opponent_losses"] = p[3], o[3]
            rows.append(row)
    out = add_points(pd.DataFrame(rows), "diff")
    out["thin"] = (out["player_cases"] < MIN_CASES) | (out["opponent_cases"] < MIN_CASES)
    return out


def chart_alive(table: pd.DataFrame, groups: list[str], tc_class: dict[str, str]):
    order, pooled = panel_order(groups, tc_class)
    long, total = [], []
    for _, r in table.iterrows():
        for side in ("player", "opponent"):
            if r["termination"] == "any":
                total.append({"tc_group": r["tc_group"], "side": side, "est": r[side], "lo": r[f"{side}_lo"], "hi": r[f"{side}_hi"]})
            else:
                long.append({"tc_group": r["tc_group"], "side": side, "segment": r["termination"], "value": r[side], "n": r[f"{side}_losses"]})
    fig, ax = charts.single("How many of your losses came while you still had chances?", size=(10.5, 3.8))
    stacked_sides(ax, pd.DataFrame(long), order, ALIVE_TERMINATIONS, "value", "n", total=pd.DataFrame(total), pooled=pooled)
    percent_axis(ax)
    ax.set_ylabel("share of the side's lost games\nended with P(win) >= 10%")
    rotate(ax)
    stacked_legend(fig, ["lost on time", "resigned", "checkmated", "abandoned"], loc="upper center", bbox_to_anchor=(0.5, 0.0), fontsize=8)
    charts.footnote(fig, "\n\nBlue: your losses; grey: your opponents' losses (your wins). Still alive: the loser's model P(win) after their own last move was 10% or more; split by how the game ended. Whisker: 95% CI, resampling games. 'all ...': the controls of a class pooled.")
    fig.tight_layout()
    return fig


# --- data counts ---------------------------------------------------------------------------


def data_counts(games: pd.DataFrame, g: pd.DataFrame, groups: list[str], noted: dict[str, int]) -> pd.DataFrame:
    rows = []
    gap = opponent_rating_pre(g) - g["my_rating_before"].astype(float)
    for label in groups:
        all_g = games[games["tc_group"] == label]
        part = g[g["tc_group"] == label]
        gp = gap[g["tc_group"] == label]
        rows.append(
            {
                "tc_group": label, "reported": True, "games": len(all_g), "games_with_moves": len(part),
                "wins": int((part["outcome"] == "win").sum()), "draws": int((part["outcome"] == "draw").sum()), "losses": int((part["outcome"] == "loss").sum()),
                "player_moves": int(part["my_moves"].sum()), "opponent_moves": int(part["opp_moves"].sum()),
                "median_abs_rating_gap": float(gp.abs().median()), "mean_rating_gap": float(gp.mean()),
            }  # fmt: skip
        )
    for label, n in noted.items():
        rows.append({"tc_group": label, "reported": False, "games": n})
    out = pd.DataFrame(rows)
    counts = ["games_with_moves", "wins", "draws", "losses", "player_moves", "opponent_moves"]
    out[counts] = out[counts].astype("Int64")
    return out


# --- main ----------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    games = data.games.copy()
    decomposed = decompose_games(data.moves, games)
    table_path = write_table(decomposed, data.username)
    print(f"wrote {table_path} ({len(decomposed):,} rows, {len(decomposed.columns)} columns)")

    groups, tc_class = data.tc_groups, data.tc_class
    moves = data.moves[data.moves["tc_group"].isin(groups)].copy()
    moves["tc_group"] = moves["tc_group"].astype(str)
    m = with_player_view(moves)
    reach = first_reach(m)
    games["tc_group"] = games["tc_group"].astype(str)
    g = games.merge(decomposed, on="uuid", how="left").merge(reach, on="uuid", how="left")
    g = g[g["n_plies"].notna() & g["tc_group"].isin(groups)].reset_index(drop=True)
    results = Results(SLUG)

    points, budget, check = points_per_game(g, groups)
    results.table("1_points_per_game", points, "Expected points each side gave away per game (sum of its moves' ep_loss), you (player) vs opponents in the same games; diff = you minus opponents (paired, CIs resample games); margin = opponents minus you with its Elo-equivalent around an even score; centipawns per game as the secondary measure.")
    results.table("1b_score_budget", budget, "Score vs the model's expected score at move 1 (expected), with Elo-equivalent; score ~ expected - your ep lost + their ep lost + residual (resignations, flags, end-of-game model error, clipping at 0); correlation of net ep with score.")
    results.table("1c_net_ep_check", check, "Sanity check: your score by net expected points (opponents' lost minus yours) per game, with the model's expected points at move 1 and after the last move.")
    results.figure(chart_points(points, groups), "1_points_per_game")
    print(f"1 done at {time.time() - t0:.0f} s")

    conc, tail = concentration(moves, groups)
    results.table("2_concentration", conc, "Share of all expected points a side gave away that came from its costliest x% of moves (top_share_of_moves), you vs opponents; diff = you minus opponents; CIs resample games.")
    results.table("2b_big_errors", tail, "Moves losing at least min_ep_loss expected points: share of moves (and per 100 moves) and share of all expected points lost that they carry (damage_share), you vs opponents; CIs resample games.")
    results.figure(chart_concentration(conc, groups, tc_class), "2_concentration")
    print(f"2 done at {time.time() - t0:.0f} s")

    decisive, profile, decisive_clock = decisive_tables(g, groups)
    results.table("3_decisive", decisive, "Decided games by how the loser lost: player = share of your losses, opponent = share of your opponents' losses (your wins); diff = you minus opponents (diff_pp in percentage points); base 'games lost on the board' restricts to games where the loser's P(win) after their last move was < 10%. CIs resample games.")
    results.table("3b_decisive_profile", profile, "What the decisive blunder looks like (games with one): its expected points and centipawns lost, the loser's P(win) before/after, share played from the last playable position, plies before the end, clock before it (seconds, beyond one increment, share of base) and phase shares; player = in your losses.")
    results.table("3c_decisive_clock", decisive_clock, "Clock before the decisive blunder (games with one), you (in your losses) vs opponents (in your wins), in seconds left, seconds beyond one increment and share of the base time; diff = you minus opponents, both computed on the same resampled games.")
    results.figure(chart_decisive(decisive, groups), "3_decisive")
    print(f"3 done at {time.time() - t0:.0f} s")

    per_game_phase, per_move_phase = phase_tables(g, moves, groups)
    results.table("4_phase", per_game_phase, "Expected points given away per game in each phase (0 for games that never reached it), you vs opponents; diff = you minus opponents (paired); share_of_total = the phase's share of all expected points the side gave away.")
    results.table("4b_phase_per_move", per_move_phase, "Per-move measures by phase (and 'all'), you vs opponents in the same games (compare_sides): expected points lost per move, share of moves losing >= 0.2, centipawns lost per move, share of moves losing >= 300 cp.")
    results.figure(chart_phase(per_game_phase, groups, tc_class), "4_phase")
    print(f"4 done at {time.time() - t0:.0f} s")

    conv, conv_diff = conversion_tables(g, groups, tc_class)
    results.table("5_conversion", conv, "Conversion (side's model P(win) reached >= 0.8: rate = share won) and saves (fell to <= 0.2: rate = share not lost), per side: actual score vs the model's expected score at the first such position (excess, with Elo-equivalent); reached = share of games. 'all <class>' rows pool the controls of a class.")
    results.table("5b_conversion_diff", conv_diff, "You minus opponents for the rates, scores and excess over the model in 5_conversion, computed on the same resampled games; rate_diff_pp = the rate difference in percentage points.")
    results.table("5c_threshold_sensitivity", threshold_sensitivity(g, groups, tc_class), "Conversion (P(win) reached >= threshold: share won) and save (fell to <= 1 - threshold: share not lost) rates for other thresholds, per side.")
    results.table("5d_model_by_side", model_by_side(m.merge(g[["uuid"]], left_on="game_uuid", right_on="uuid"), groups), "Even-handedness check of the P(win) model: observed win rate of each side by the model's P(win) for that side after every move (bin_mid = what a calibrated, side-blind model would give); CIs resample games.")
    results.figure(chart_conversion(conv, groups), "5_conversion")
    print(f"5 done at {time.time() - t0:.0f} s")

    how = how_saved(g, groups, tc_class)
    results.table("6_saves_how", how, "Games where a side's model P(win) fell to <= 0.2: share that ended in each kind of save (won on time = the other side flagged; drawn on time = timeout vs insufficient material), as a share of those games (with CI) and of all saves; thin = fewer than 30 such games, thin_share_of_saves = fewer than 30 saves. 'all <class>' rows pool the controls of a class.")
    results.figure(chart_saves(how, conv, groups, tc_class), "6_saves")
    print(f"6 done at {time.time() - t0:.0f} s")

    alive = alive_tables(g, groups, tc_class)
    results.table("7_lost_with_chances", alive, "Decided games the loser lost while the model still gave them P(win) >= 0.1 after their own last move, by the loser's termination ('any' = all): player/opponent = share of the side's losses (diff = you minus opponents, diff_pp in percentage points); *_within = share among losses with that termination; *_last_p_win = loser's P(win) after their last move in those games; *_term_share = share of losses with that termination. 'all <class>' rows pool controls.")
    results.figure(chart_alive(alive, groups, tc_class), "7_lost_with_chances")

    results.table("8_data", data_counts(games, g, groups, data.tc_noted), "Games per time control: reported controls with games, games with at least one move, results, moves per side and the rating gap to the opponent (pre-game proxy: opponent's post-game rating plus your change, minus your pre-game rating); thin controls are listed with reported = False.")
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
