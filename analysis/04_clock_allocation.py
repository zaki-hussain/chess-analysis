"""Question 4: clock allocation and losses on time. How your clock compares
with the opponent's through the game, where your seconds go (phase, stakes),
whether longer thinks produce better moves, how many games you lose on time
and from what positions, and whether a flag can be seen coming early; per
exact time control, you vs opponents in the same games.

Computes, for every reported time control (pooled by time class too where
the per-control counts are thin, i.e. the flag tables):
1. both clocks by move number, and the clock gap at moves 10/20/30;
2. seconds per move by phase and the share of thinking time per phase;
3. seconds per move by stakes (the expected-points gap between the best and
   second-best move), the high/no-stakes allocation ratio, long thinks on
   nothing, snapped high-stakes moves, pre-moves and forced moves;
4. move quality by seconds spent (buckets relative to the control's
   per-move budget), the adjusted you-vs-opponents gradient, and the clock
   charge (what the seconds themselves cost in the calibrated model);
5. losses on time: counts, the board when the flag fell (valued with the
   clocks levelled by the refitted calibrated model, as played, and by
   Stockfish's board-only model), expected points lost to flags per game;
6. each side's clock at moves 10/20/30 in games it went on to lose on time,
   win on time, or neither.

The calibrated win model is refitted here (deterministic: same features,
same seed) to value positions under counterfactual clocks.

Writes CSVs to presentation/data/04_clock_allocation/ and SVGs to
presentation/figures/04_clock_allocation/ (no prose). Shared clock machinery
lives in chess_analysis.clocks.

Run: .venv/bin/python analysis/04_clock_allocation.py  (about 2-4 minutes, 1-1.5 of them the model refit; 230 s measured with other jobs running)
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

from chess_analysis import charts, clocks
from chess_analysis.toolkit import MIN_GROUP_GAMES, Results, cluster_bootstrap_mean, dummies, excess_to_elo, load_data, ratio_bootstrap

SLUG = "04_clock_allocation"
SEED = 4
N_BOOT = 400  # resamples for every CI (games resampled), except the two below
N_BOOT_CURVE = 200  # the clock-by-move curves: ~100 move numbers x 9 controls; 200 is plenty for a drawn band
N_BOOT_THINK_REG = 300  # the think-time regression: each replicate is a full solve with ~40 dummy columns

# A point of the clock-by-move-number curve needs this many games on each side: below 20 the mean clock
# of a move number is driven by a handful of long games.
MIN_POINT_GAMES = 20
# Move numbers at which the clock situation is read off: end of the opening (10), middlegame (20), and the
# start of most endings in fast games (30).
CHECK_MOVES = [10, 20, 30]
# The move shown in the flag-coming chart: by move 20 most games are still running in every control.
FLAG_CHECK_MOVE = 20
# "Behind on the clock": by more than this many seconds (what a bullet player sees) ...
CLOCK_GAP_SECONDS = 3.0
# ... or by more than this share of the base time (3 s in 1+0, 9 s in 3+0, 30 s in 10+0), comparable across controls.
CLOCK_GAP_SHARE_OF_BASE = 0.05
# Seconds-spent buckets are multiples of the control's per-move budget, (base + EXPECTED_MOVES x increment)
# / EXPECTED_MOVES: chess.com estimates a game's length as base + 40 x increment, i.e. 40 moves each.
EXPECTED_MOVES = 40
# 1/4 budget is a snap move, 1/2-1 a normal one, 2-4x a long think, 4x+ a very long one.
THINK_MULTIPLE_EDGES = [0, 0.25, 0.5, 1, 2, 4, np.inf]
THINK_MULTIPLE_LABELS = ["<0.25x", "0.25-0.5x", "0.5-1x", "1-2x", "2-4x", "4x+"]
# Stakes of a position: the expected-points gap between the best and second-best move. Below 0.03 a wrong
# choice costs little; 0.10+ (a tenth of a game) is a decision that matters. "none": the two best moves
# are worth the same, or there is only one legal move.
STAKES_EDGES = [-1, 1e-9, 0.03, 0.10, np.inf]
STAKES_LABELS = ["none", "low", "medium", "high"]
PHASES = ["opening", "middlegame", "endgame"]
# A long think is above this quantile of the control's seconds per move (both sides pooled); a snap move
# below SNAP_QUANTILE: relative to how the field in these games plays the control.
LONG_THINK_QUANTILE = 0.90
SNAP_QUANTILE = 0.25
# chess.com charges 0.1 s for a pre-move.
PREMOVE_SECONDS = 0.1
# Extra controls for the think-time regression: legal-move bands (forced / few / normal / many) and the
# absolute eval (level / slight / clear / winning / decided, in centipawns).
LEGAL_EDGES = [0, 1, 5, 15, 30, 999]
LEGAL_LABELS = ["1", "2-5", "6-15", "16-30", "31+"]
LEGAL_REFERENCE = LEGAL_LABELS[3]  # 16-30 legal moves: the typical middlegame position
ABS_EVAL_EDGES = [-1, 50, 150, 300, 600, 1e9]
ABS_EVAL_LABELS = ["<50", "50-150", "150-300", "300-600", "600+"]
# Board state when the flag fell (level-clock expected points of the flagged side): 0.8+ is winning, 0.2 or
# less is lost; in between the game was still open.
WINNING_EP = 0.8
LOST_EP = 0.2
# Winner's clock "comfortable": 30 s or more after the last move (not a mutual scramble).
WINNER_COMFORT_SECONDS = 30.0
# A flag-valuation row needs this many flagged games: a share from fewer than 20 games has a CI of +-0.2.
MIN_FLAG_GAMES = 20
# Elo-equivalents of per-game point differences are taken around an even score.
EVEN_SCORE = 0.5


# --- helpers -------------------------------------------------------------------


def by_group(frame: pd.DataFrame, order: Sequence[str]) -> pd.DataFrame:
    """Rows in time-control order (pooled "all <class>" rows last), otherwise as they came."""
    rank = {g: i for i, g in enumerate(order)}
    return frame.sort_values("tc_group", key=lambda s: s.map(rank).fillna(len(rank)), kind="stable").reset_index(drop=True)


def iid_ci(values: pd.Series, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Mean with a bootstrap CI where each row is one game."""
    values = pd.Series(values, dtype=float).reset_index(drop=True)
    if values.empty:
        return np.nan, np.nan, np.nan
    return cluster_bootstrap_mean(values, pd.Series(np.arange(len(values))), n_boot=n_boot, seed=SEED)


def game_bootstrap(groups: Sequence[np.ndarray], weights: Sequence[np.ndarray], func: Callable[..., float], n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Any function of per-game sums, with a CI from resampling whole games jointly across all the sums."""
    games = np.unique(np.concatenate([np.asarray(g) for g in groups]))
    index = pd.Series(np.arange(len(games)), index=games)
    stacked = np.stack([np.bincount(index.reindex(np.asarray(g)).to_numpy(), weights=np.asarray(w, dtype=float), minlength=len(games)) for g, w in zip(groups, weights)], axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        estimate = float(func(*stacked.sum(axis=0)))
        if len(games) < 2:
            return estimate, np.nan, np.nan
        rng = np.random.default_rng(SEED)
        draws = np.array([func(*(np.bincount(rng.integers(0, len(games), len(games)), minlength=len(games)).astype(float) @ stacked)) for _ in range(n_boot)])
    lo, hi = np.nanquantile(draws, [0.025, 0.975])
    return estimate, float(lo), float(hi)


def share(frame: pd.DataFrame, numerator: pd.Series, denominator: pd.Series | None = None) -> tuple[float, float, float]:
    """Share of `denominator` rows (all rows if None) that satisfy `numerator`, games resampled."""
    den = pd.Series(True, index=frame.index) if denominator is None else denominator.astype(bool)
    if not den.any():
        return np.nan, np.nan, np.nan
    return ratio_bootstrap((numerator.astype(bool) & den).astype(float), den.astype(float), frame["game_uuid"], n_boot=N_BOOT, seed=SEED)


def ci_cols(name: str, values: tuple[float, float, float]) -> dict:
    return {name: values[0], f"{name}_lo": values[1], f"{name}_hi": values[2]}


def sides(frame: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [("player", frame[frame["is_player_move"]]), ("opponent", frame[~frame["is_player_move"]])]


def thin_panel(ax, text: str) -> None:
    """charts.thin_note for a panel in a shared-axis grid (thin_note clears the shared ticks of every panel)."""
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
    ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
    ax.grid(False)


def rotate(ax) -> None:
    for tick in ax.get_xticklabels():
        tick.set_rotation(45)
        tick.set_ha("right")


def budget_seconds(base: float, increment: float) -> float:
    return (base + EXPECTED_MOVES * increment) / EXPECTED_MOVES


def seconds_label(lo: float, hi: float) -> str:
    if lo == 0:
        return f"<{hi:.2g} s"
    if np.isinf(hi):
        return f"{lo:.2g}+ s"
    return f"{lo:.2g}-{hi:.2g} s"


def add_columns(moves: pd.DataFrame, games: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Clock buckets, the think bucket (multiples of the per-move budget), stakes and helper flags; plus
    the per-group think-bucket edges in seconds."""
    frame = clocks.add_clock_buckets(moves)
    budget = frame["base_seconds"].astype(float).add(EXPECTED_MOVES * frame["increment_seconds"].astype(float)) / EXPECTED_MOVES
    frame["think_bucket"] = clocks.cut_bucket(frame["time_spent_seconds"] / budget, THINK_MULTIPLE_EDGES, THINK_MULTIPLE_LABELS)
    frame["stakes"] = clocks.cut_bucket(frame["ep_criticality"].fillna(0.0), STAKES_EDGES, STAKES_LABELS)
    frame["one_move"] = frame["criticality"].isna()
    frame["premove"] = frame["time_spent_seconds"] <= PREMOVE_SECONDS
    frame["mover_result"] = np.where(frame["is_player_move"], frame["my_result"], frame["opponent_result"])
    frame["other_result"] = np.where(frame["is_player_move"], frame["opponent_result"], frame["my_result"])
    controls = games.groupby("tc_group")[["base_seconds", "increment_seconds"]].first()
    rows = []
    for g, (base, inc) in controls.iterrows():
        b = budget_seconds(base, inc)
        for k, label in enumerate(THINK_MULTIPLE_LABELS):
            lo, hi = THINK_MULTIPLE_EDGES[k] * b, THINK_MULTIPLE_EDGES[k + 1] * b
            rows.append({"tc_group": g, "think_bucket": label, "seconds_from": lo, "seconds_to": hi, "seconds_label": seconds_label(lo, hi), "budget_seconds": b})
    return frame, pd.DataFrame(rows)


# --- the calibrated model under counterfactual clocks -------------------------------


def fit_win_model():
    """Refit the calibrated win model (same features, same seed: deterministic) and return it with the pre-game ratings."""
    from chess_analysis import calibrate
    from chess_analysis.dataset import load_analysed, load_games, load_my_games

    ratings = calibrate.pre_game_ratings(load_my_games())
    X, y, _ = calibrate.training_frame(load_analysed(), load_games(), ratings)
    return calibrate.WinModel().fit(X, y), ratings


def add_clock_charge(moves: pd.DataFrame, model, ratings: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """`clock_charge`: expected points the seconds a move took cost in the model, and `ep_change` (unfloored).

    Re-predicts the position after the move with the mover's clock as if the move had taken no time
    (`ep_after_free`); `clock_charge = ep_after_free - ep_after`. Not an additive part of `ep_loss`:
    `ep_before` already prices in typical time use (the unfloored change averages about zero). Also returns
    the largest gap between the refit and the stored `ep_after` (a check that the refit is exact).
    """
    from chess_analysis import calibrate

    after = calibrate.after_move_frame(moves, ratings)
    check = 1 - model.predict(after)["ep"].to_numpy()
    max_diff = float(np.nanmax(np.abs(check - moves["ep_after"].to_numpy())))
    free = after.copy()
    free["opponent_clock"] = (moves["clock_before_seconds"] + moves["increment_seconds"]).to_numpy()
    free["clock_diff"] = free["clock"] - free["opponent_clock"]
    ep_after_free = 1 - model.predict(free)["ep"].to_numpy()
    out = moves.copy()
    out["clock_charge"] = ep_after_free - out["ep_after"].to_numpy()
    out["ep_change"] = out["ep_before"].to_numpy() - out["ep_after"].to_numpy()
    return out, max_diff


# --- losses on time -------------------------------------------------------------


def flag_positions(moves: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per game decided or drawn by the clock, valued from the flagged side's view.

    The position valued is the one after the last recorded move: in every timeout that move is the
    winner's (asserted in main), so the flagged side was to move when the clock ran out. A flag draw is a
    timeout against insufficient mating material.
    """
    last = moves.sort_values(["game_uuid", "ply"]).groupby("game_uuid").tail(1).set_index("game_uuid")
    g = games.set_index("uuid")
    g = g[g.index.isin(last.index)]
    flagged = pd.Series(pd.NA, index=g.index, dtype="object")
    flagged[g["my_result"] == "timeout"] = "player"
    flagged[g["opponent_result"] == "timeout"] = "opponent"
    draw = g["my_result"] == "timevsinsufficient"
    flagged[draw] = np.where(last.loc[g.index[draw], "is_player_move"], "opponent", "player")
    out = pd.DataFrame({"tc_group": g["tc_group"], "flagged": flagged, "kind": np.where(draw, "flag draw", "loss on time"), "actual": np.where(draw, 0.5, 0.0)})
    out = out[out["flagged"].notna()]
    lm = last.loc[out.index]
    out["last_move_by_flagged"] = np.where(out["flagged"] == "player", lm["is_player_move"], ~lm["is_player_move"])
    out["last_ply"] = lm["ply"].to_numpy()
    out["phase"] = lm["phase"].to_numpy()
    out["ep_as_played"] = (1 - lm["ep_after"]).to_numpy()
    out["ep_board"] = (1 - lm["ep_sf_after"]).to_numpy()
    out["winner_clock"] = lm["clock_seconds"].to_numpy()
    out["base_seconds"] = lm["base_seconds"].to_numpy()
    return out


def flag_valuations(flags: pd.DataFrame, moves: pd.DataFrame, model, ratings: pd.DataFrame) -> pd.DataFrame:
    """Adds `ep_level`: the calibrated model on the same board with the flagged side's clock set equal to the winner's."""
    from chess_analysis import calibrate

    key = moves.set_index(["game_uuid", "ply"])
    sub = key.loc[list(zip(flags.index, flags["last_ply"]))].reset_index()
    level = calibrate.after_move_frame(sub, ratings)
    level["clock"] = level["opponent_clock"]
    level["clock_fraction"] = level["clock"] / level["base_seconds"]
    level["clock_diff"] = 0.0
    out = flags.copy()
    out["ep_level"] = model.predict(level)["ep"].to_numpy()
    return out


# --- main ----------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    groups = data.tc_groups
    games = data.games[data.games["tc_group"].isin(groups)].copy()
    games["tc_group"] = games["tc_group"].astype(str)
    moves = data.moves[data.moves["tc_group"].isin(groups)].copy()
    moves["tc_group"] = moves["tc_group"].astype(str)
    moves, think_edges = add_columns(moves, games)
    model, ratings = fit_win_model()
    moves, refit_gap = add_clock_charge(moves, model, ratings)
    print(f"model refit done at {time.time() - t0:.0f} s; reproduces stored ep_after to within {refit_gap:.2e}")
    pooled = [label for label, _ in clocks.group_frames(games, groups, pooled=True, tc_class=data.tc_class) if label.startswith("all ")]
    results = Results(SLUG)

    # 1. both clocks through the game ---------------------------------------------------------
    clock_curve = clocks.bucket_curve(moves.assign(move=pd.Categorical(moves["move_number"], sorted(moves["move_number"].unique()), ordered=True)), "move", "clock_before_seconds", groups, n_boot=N_BOOT_CURVE, min_moves=MIN_POINT_GAMES)
    clock_curve["move"] = clock_curve["move"].astype(int)
    clock_curve = clock_curve.rename(columns={"n_player": "games_player", "n_opponent": "games_opponent"})
    results.table("1_clock_by_move", clock_curve, f"Mean seconds left before each move, by move number, you vs opponents (diff = you minus opponents), over the games still running at that move; thin = fewer than {MIN_POINT_GAMES} games on a side.")
    fig, axes = charts.group_grid(groups, sharey=False, title="Your clock and your opponents' clock through the game")
    for g, ax in axes.items():
        part = clock_curve[(clock_curve["tc_group"] == g) & ~clock_curve["thin"]]
        charts.side_lines(ax, part, "move", markers=False)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("move number")
    axes[groups[0]].set_ylabel("seconds left before the move")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, f"Mean over the games still running at that move (at least {MIN_POINT_GAMES} on a side). Bands: 95% CI, resampling games. Each panel has its own scale.")
    fig.tight_layout()
    results.figure(fig, "1_clock_by_move")

    rows = []
    mine = moves[moves["is_player_move"]]
    for g, part in clocks.group_frames(mine, groups):
        base = float(part["base_seconds"].iloc[0])
        for mv in CHECK_MOVES:
            at = part[part["move_number"] == mv]
            gap = at["clock_diff_seconds"]
            rows.append(
                {
                    "tc_group": g, "move": mv,
                    **ci_cols("clock_gap_seconds", iid_ci(gap)),
                    **ci_cols("clock_gap_share_of_base", iid_ci(gap / base)),
                    **ci_cols(f"behind_{CLOCK_GAP_SECONDS:g}s", iid_ci(gap < -CLOCK_GAP_SECONDS)),
                    **ci_cols(f"ahead_{CLOCK_GAP_SECONDS:g}s", iid_ci(gap > CLOCK_GAP_SECONDS)),
                    **ci_cols(f"behind_{100 * CLOCK_GAP_SHARE_OF_BASE:g}pct_base", iid_ci(gap < -CLOCK_GAP_SHARE_OF_BASE * base)),
                    **ci_cols(f"ahead_{100 * CLOCK_GAP_SHARE_OF_BASE:g}pct_base", iid_ci(gap > CLOCK_GAP_SHARE_OF_BASE * base)),
                    "games": len(at), "thin": len(at) < MIN_POINT_GAMES,
                }  # fmt: skip
            )
    results.table("1b_clock_gap", pd.DataFrame(rows), f"Your clock minus the opponent's before your move 10/20/30 (seconds and share of base), and the share of games in which you are behind or ahead by more than {CLOCK_GAP_SECONDS:g} s or {100 * CLOCK_GAP_SHARE_OF_BASE:g}% of base; games that reached the move; CIs by game.")
    print(f"1 done at {time.time() - t0:.0f} s")

    # 2. where the time goes: phase -----------------------------------------------------------
    by_phase = clocks.bucket_curve(moves.assign(phase=pd.Categorical(moves["phase"], PHASES, ordered=True)), "phase", "time_spent_seconds", groups, n_boot=N_BOOT)
    results.table("2_time_by_phase", by_phase, "Seconds spent per move by phase, you vs opponents (diff = you minus opponents); CIs by game.")
    fig, axes = charts.group_grid(groups, sharey="row", title="Seconds per move by phase of the game")
    for g, ax in axes.items():
        charts.side_bars(ax, by_phase[(by_phase["tc_group"] == g) & ~by_phase["thin"]], "phase", order=PHASES)
    axes[groups[0]].set_ylabel("seconds per move")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Mean seconds spent per move (clock before minus clock after plus increment). Rows share a scale within a time class. Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    results.figure(fig, "2_time_by_phase")
    rows = []
    for g, part in clocks.group_frames(moves, groups):
        for side, frame in sides(part):
            row = {"tc_group": g, "side": side}
            for phase in PHASES:
                row.update(ci_cols(f"share_{phase}", ratio_bootstrap((frame["phase"] == phase) * frame["time_spent_seconds"], frame["time_spent_seconds"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)))
            total = frame.groupby("game_uuid")["time_spent_seconds"].sum()
            row.update(ci_cols("seconds_per_game", iid_ci(total)))
            row.update({"base_seconds": float(frame["base_seconds"].iloc[0]), "games": int(frame["game_uuid"].nunique()), "moves": len(frame)})
            row["thin"] = row["games"] < MIN_GROUP_GAMES
            rows.append(row)
    results.table("2b_time_share_by_phase", pd.DataFrame(rows), "Share of each side's total thinking time spent in each phase, and seconds used per game; CIs by game.")
    print(f"2 done at {time.time() - t0:.0f} s")

    # 3. time by stakes ---------------------------------------------------------------------------
    by_stakes = clocks.bucket_curve(moves, "stakes", "time_spent_seconds", groups, n_boot=N_BOOT)
    results.table("3_time_by_stakes", by_stakes, "Seconds spent per move by the stakes of the position (expected points between the best and second-best move: none 0, low <0.03, medium 0.03-0.10, high 0.10+), you vs opponents; CIs by game.")
    fig, axes = charts.group_grid(groups, sharey="row", title="Seconds per move by what is at stake in the position")
    for g, ax in axes.items():
        charts.side_bars(ax, by_stakes[(by_stakes["tc_group"] == g) & ~by_stakes["thin"]], "stakes", order=STAKES_LABELS)
    axes[groups[0]].set_ylabel("seconds per move")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Stakes = expected points between the engine's best and second-best move: none 0 (or one legal move), low <0.03, medium 0.03-0.10, high 0.10+. Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    results.figure(fig, "3_time_by_stakes")

    rows = []
    for g, part in clocks.group_frames(moves, groups):
        row = {"tc_group": g}
        groups_w, weights = [], []
        for side, frame in sides(part):
            high, none = frame[frame["stakes"] == "high"], frame[frame["stakes"] == "none"]
            gw = [high["game_uuid"].to_numpy(), high["game_uuid"].to_numpy(), none["game_uuid"].to_numpy(), none["game_uuid"].to_numpy()]
            ww = [high["time_spent_seconds"].to_numpy(), np.ones(len(high)), none["time_spent_seconds"].to_numpy(), np.ones(len(none))]
            row.update(ci_cols(f"ratio_{side}", game_bootstrap(gw, ww, lambda sh, nh, sn, nn: (sh / nh) / (sn / nn))))
            row[f"high_moves_{side}"], row[f"none_moves_{side}"] = len(high), len(none)
            groups_w += gw
            weights += ww
        row.update(ci_cols("ratio_diff", game_bootstrap(groups_w, weights, lambda a, b, c, d, e, f, h, i: (a / b) / (c / d) - (e / f) / (h / i))))
        row["games"] = int(part["game_uuid"].nunique())
        row["thin"] = min(row["high_moves_player"], row["high_moves_opponent"], row["none_moves_player"], row["none_moves_opponent"]) < clocks.MIN_CELL_MOVES
        rows.append(row)
    results.table("3b_allocation_ratio", pd.DataFrame(rows), "Allocation ratio: seconds per move on high-stakes positions divided by seconds per move on no-stakes positions (1 = same time whatever the stakes), you, opponents and you minus opponents; CIs by game.")

    rows = []
    for g, part in clocks.group_frames(moves, groups):
        long_t = float(part["time_spent_seconds"].quantile(LONG_THINK_QUANTILE))
        snap_t = float(part["time_spent_seconds"].quantile(SNAP_QUANTILE))
        for side, frame in sides(part):
            is_long, is_snap = frame["time_spent_seconds"] >= long_t, frame["time_spent_seconds"] < snap_t
            none, high = frame["stakes"] == "none", frame["stakes"] == "high"
            snapped, considered = frame[high & is_snap], frame[high & ~is_snap]
            n_g = frame["game_uuid"].nunique()
            rows.append(
                {
                    "tc_group": g, "side": side, "long_think_seconds": long_t, "snap_seconds": snap_t,
                    "long_thinks_per_game": is_long.sum() / n_g,
                    **ci_cols("long_thinks_on_no_stakes", share(frame, none, is_long)),
                    **ci_cols("thinking_time_on_no_stakes", ratio_bootstrap(none * frame["time_spent_seconds"], frame["time_spent_seconds"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("high_stakes_snapped", share(frame, is_snap, high)),
                    **ci_cols("ep_loss_snapped_high", cluster_bootstrap_mean(snapped["ep_loss"], snapped["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("ep_loss_other_high", cluster_bootstrap_mean(considered["ep_loss"], considered["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    "games": int(n_g), "long_thinks": int(is_long.sum()), "high_stakes_moves": int(high.sum()), "snapped_high": len(snapped),
                    "thin": len(snapped) < clocks.MIN_CELL_MOVES,
                }  # fmt: skip
            )
    results.table("3c_misallocation", pd.DataFrame(rows), f"Long thinks (at or above the control's {100 * LONG_THINK_QUANTILE:g}th percentile of seconds per move, both sides) and snap moves (below the {100 * SNAP_QUANTILE:g}th): long thinks per game and the share on no-stakes positions, share of thinking time on no-stakes positions, share of high-stakes positions snapped and the expected points lost on them vs other high-stakes moves; CIs by game.")

    rows = []
    for g, part in clocks.group_frames(moves, groups):
        for side, frame in sides(part):
            pm = frame[frame["premove"]]
            forced = frame[frame["one_move"]]
            forced_per_game = forced.groupby("game_uuid")["time_spent_seconds"].sum().reindex(frame["game_uuid"].unique(), fill_value=0.0)
            rows.append(
                {
                    "tc_group": g, "side": side,
                    **ci_cols("premove_share", share(frame, frame["premove"])),
                    **ci_cols("ep_loss_premoves", cluster_bootstrap_mean(pm["ep_loss"], pm["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("ep_loss_all_moves", cluster_bootstrap_mean(frame["ep_loss"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("cp_loss_premoves", cluster_bootstrap_mean(pm["cp_loss"], pm["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("cp_loss_all_moves", cluster_bootstrap_mean(frame["cp_loss"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("premoves_on_no_stakes", share(frame, frame["stakes"] == "none", frame["premove"])),
                    **ci_cols("seconds_per_forced_move", cluster_bootstrap_mean(forced["time_spent_seconds"], forced["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    **ci_cols("forced_seconds_per_game", iid_ci(forced_per_game)),
                    "premoves": len(pm), "forced_moves": len(forced), "moves": len(frame), "games": int(frame["game_uuid"].nunique()),
                    "thin": len(pm) < clocks.MIN_CELL_MOVES,
                }  # fmt: skip
            )
    results.table("3d_premoves_and_forced", pd.DataFrame(rows), f"Pre-moves (charged {PREMOVE_SECONDS:g} s or less): share of moves, expected points and centipawns (clock-free) lost on them vs all moves, share on no-stakes positions; forced moves (one legal move): seconds per move and per game; CIs by game.")
    print(f"3 done at {time.time() - t0:.0f} s")

    # 4. longer thinks and move quality -----------------------------------------------------------
    labels = think_edges.set_index(["tc_group", "think_bucket"])
    curves = {}
    for outcome in ("ep_loss", "cp_loss"):
        t = clocks.bucket_curve(moves, "think_bucket", outcome, groups, n_boot=N_BOOT)
        t = t.merge(think_edges[["tc_group", "think_bucket", "seconds_label", "seconds_from", "seconds_to"]], on=["tc_group", "think_bucket"], how="left")
        cols = ["tc_group", "think_bucket", "seconds_label", "seconds_from", "seconds_to"]
        curves[outcome] = t[cols + [c for c in t.columns if c not in cols]]
    results.table("4_quality_by_think", curves["ep_loss"], f"Expected points lost per move by seconds spent on it, you vs opponents; buckets are multiples of the control's per-move budget (base + {EXPECTED_MOVES} x increment) / {EXPECTED_MOVES} with their seconds; CIs by game.")
    results.table("4b_cp_loss_by_think", curves["cp_loss"], "Centipawns lost per move by seconds spent on it (clock-free currency), you vs opponents; CIs by game.")
    fig, axes = charts.group_grid(groups, sharey=True, title="Do longer thinks produce better moves?", panel=(2.9, 2.5))
    for g, ax in axes.items():
        part = curves["cp_loss"][(curves["cp_loss"]["tc_group"] == g) & ~curves["cp_loss"]["thin"]]
        if part.empty:
            thin_panel(ax, "too few moves")
            continue
        charts.side_lines(ax, part, "think_bucket", order=THINK_MULTIPLE_LABELS)
        ax.set_xticks(range(len(THINK_MULTIPLE_LABELS)), [labels.loc[(g, b), "seconds_label"] for b in THINK_MULTIPLE_LABELS])
        rotate(ax)
    axes[groups[0]].set_ylabel("centipawns lost per move")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, f"Seconds spent on the move, in buckets of 1/4, 1/2, 1, 2 and 4 times the control's per-move budget ((base + {EXPECTED_MOVES} x increment) / {EXPECTED_MOVES}). Centipawns: the clock-free measure. Bands: 95% CI, resampling games; buckets under 30 moves a side left out.")
    fig.tight_layout()
    results.figure(fig, "4_quality_by_think")

    rows = []
    for g, part in clocks.group_frames(moves, groups):
        for outcome in ("ep_loss", "cp_loss"):
            controls = [
                *clocks.position_controls(part, outcome),
                dummies(part["clock_coarse"], clocks.CLOCK_REFERENCE, "clock"),
                dummies(part["opponent_clock_coarse"], clocks.CLOCK_REFERENCE, "oppclock"),
                dummies(pd.cut(part["legal_moves"], LEGAL_EDGES, labels=LEGAL_LABELS), LEGAL_REFERENCE, "legal"),
                part["in_check"].astype(float).rename("in_check"),
                dummies(pd.cut(part["cp_before"].abs(), ABS_EVAL_EDGES, labels=ABS_EVAL_LABELS), ABS_EVAL_LABELS[0], "abs_eval"),
            ]
            t = clocks.interaction_ols(part, outcome, part["think_bucket"], THINK_MULTIPLE_LABELS, THINK_MULTIPLE_LABELS[0], controls, n_boot=N_BOOT_THINK_REG, seed=SEED)
            t.insert(0, "outcome", outcome)
            t.insert(0, "tc_group", g)
            rows.append(t.rename(columns={"level": "think_bucket"}))
    adjusted = pd.concat(rows, ignore_index=True).merge(think_edges[["tc_group", "think_bucket", "seconds_label"]], on=["tc_group", "think_bucket"], how="left")
    adjusted["thin"] = (adjusted[["n_player", "n_opponent"]].min(axis=1) < clocks.MIN_CELL_MOVES) | adjusted["diff"].isna()
    results.table("4c_adjusted_think_gradient", adjusted, "Adjusted extra error per move relative to a snap move (<0.25x budget), from outcome ~ think bucket x side + phase + stakes + ply + pieces + both clocks + legal moves + in check + |eval|: opponents, you, and you minus opponents (diff); not the causal value of a second (time goes to hard positions); CIs by game.")
    rows = []
    for g, part in clocks.group_frames(moves, groups):
        p, o = part[part["is_player_move"]], part[~part["is_player_move"]]
        n_g = part["game_uuid"].nunique()
        row = {"tc_group": g}
        for side, frame in (("player", p), ("opponent", o)):
            totals = frame.groupby("game_uuid")["clock_charge"].sum().reindex(part["game_uuid"].unique(), fill_value=0.0)
            row.update(ci_cols(f"clock_charge_per_game_{side}", iid_ci(totals)))
            row[f"ep_loss_per_game_{side}"] = frame.groupby("game_uuid")["ep_loss"].sum().reindex(part["game_uuid"].unique(), fill_value=0.0).mean()
            row[f"mean_ep_change_{side}"] = frame["ep_change"].mean()
        row.update(ci_cols("clock_charge_diff", game_bootstrap([p["game_uuid"].to_numpy(), o["game_uuid"].to_numpy(), part["game_uuid"].drop_duplicates().to_numpy()], [p["clock_charge"].to_numpy(), o["clock_charge"].to_numpy(), np.ones(n_g)], lambda sp, so, n: (sp - so) / n)))
        row["games"] = int(n_g)
        row["thin"] = n_g < MIN_GROUP_GAMES
        rows.append(row)
    results.table("4d_clock_charge", pd.DataFrame(rows), f"Clock charge: expected points the seconds themselves cost per game in the refitted calibrated model (position after the move re-valued as if the move had taken no time; refit matches stored values to within {refit_gap:.1e}), you, opponents, you minus opponents; mean unfloored change in expected points per move for scale; CIs by game.")
    print(f"4 done at {time.time() - t0:.0f} s")

    # 5. losses on time ---------------------------------------------------------------------------
    flags = flag_positions(moves, games)
    assert not flags["last_move_by_flagged"].any(), "a timeout game ends with the flagged side's own move"
    flags = flag_valuations(flags, moves, model, ratings)
    flag_sets = clocks.group_frames(flags, groups, pooled=True, tc_class=data.tc_class)
    game_sets = dict(clocks.group_frames(games, groups, pooled=True, tc_class=data.tc_class))

    rows = []
    for g, f in flag_sets:
        gm = game_sets[g]
        your_losses, their_losses = int((gm["outcome"] == "loss").sum()), int((gm["outcome"] == "win").sum())
        mine_lot = int(((f["flagged"] == "player") & (f["kind"] == "loss on time")).sum())
        theirs_lot = int(((f["flagged"] == "opponent") & (f["kind"] == "loss on time")).sum())
        rows.append(
            {
                "tc_group": g, "games": len(gm), "your_losses": your_losses, "your_losses_on_time": mine_lot,
                "share_of_your_losses": mine_lot / your_losses if your_losses else np.nan,
                "your_losses_on_time_per_100_games": 100 * mine_lot / len(gm),
                "opponent_losses": their_losses, "opponent_losses_on_time": theirs_lot,
                "share_of_opponent_losses": theirs_lot / their_losses if their_losses else np.nan,
                "opponent_losses_on_time_per_100_games": 100 * theirs_lot / len(gm),
                "flag_draws_you_flagged": int(((f["flagged"] == "player") & (f["kind"] == "flag draw")).sum()),
                "flag_draws_opponent_flagged": int(((f["flagged"] == "opponent") & (f["kind"] == "flag draw")).sum()),
                "thin": min(mine_lot, theirs_lot) < MIN_FLAG_GAMES,
            }  # fmt: skip
        )
    results.table("5b_flag_counts", pd.DataFrame(rows), "Games lost on time by you and by your opponents, as a share of each side's losses and per 100 games, and flag draws (timeout vs insufficient material), per control and pooled by class.")

    def iid_share(mask: pd.Series) -> tuple[float, float, float]:
        return iid_ci(mask.astype(float))

    rows, val_rows = [], []
    for g, f in flag_sets:
        for flagged in ("player", "opponent"):
            ff = f[f["flagged"] == flagged]
            n = len(ff)
            rows.append(
                {
                    "tc_group": g, "flagged": flagged,
                    **ci_cols("winning", iid_share(ff["ep_level"] >= WINNING_EP)),
                    **ci_cols("level", iid_share((ff["ep_level"] > LOST_EP) & (ff["ep_level"] < WINNING_EP))),
                    **ci_cols("lost", iid_share(ff["ep_level"] <= LOST_EP)),
                    **ci_cols("mean_ep_level", iid_ci(ff["ep_level"])),
                    "points_lost_from_winning_boards": float((ff.loc[ff["ep_level"] >= WINNING_EP, "ep_level"] - ff.loc[ff["ep_level"] >= WINNING_EP, "actual"]).sum()),
                    "winner_clock_median_seconds": float(ff["winner_clock"].median()) if n else np.nan,
                    "winner_clock_median_share_of_base": float((ff["winner_clock"] / ff["base_seconds"]).median()) if n else np.nan,
                    **ci_cols(f"winner_had_{WINNER_COMFORT_SECONDS:g}s", iid_share(ff["winner_clock"] >= WINNER_COMFORT_SECONDS)),
                    **ci_cols("endgame", iid_share(ff["phase"] == "endgame")),
                    "flagged_games": n, "thin": n < MIN_FLAG_GAMES,
                }  # fmt: skip
            )
            val = {"tc_group": g, "flagged": flagged}
            for col, name in (("ep_as_played", "as_played"), ("ep_level", "level_clocks"), ("ep_board", "board_only")):
                e, lo, hi = iid_ci(ff[col] - ff["actual"])
                val.update({f"{name}_per_flag": e, f"{name}_per_flag_lo": lo, f"{name}_per_flag_hi": hi, f"{name}_total": e * n, f"{name}_total_lo": lo * n, f"{name}_total_hi": hi * n})
            val.update({"flagged_games": n, "thin": n < MIN_FLAG_GAMES})
            val_rows.append(val)
    board = pd.DataFrame(rows)
    results.table("5_flag_board_state", board, f"The board when the flag fell, valued for the flagged side with the clocks levelled (refitted calibrated model): share of flagged games winning (>= {WINNING_EP}), level, or lost (<= {LOST_EP}); the winner's clock after the last move; per control and pooled by class; CIs by game.")
    results.table("5c_flag_valuation", pd.DataFrame(val_rows), "Expected points lost to the clock per flagged game and in total: the flagged side's value of the final position minus its actual score (0, or 0.5 for a flag draw), valued as played (floor), with level clocks (headline) and by Stockfish's board-only model (ceiling); CIs by game.")

    rows = []
    for g, gm in game_sets.items():
        ids = gm["uuid"]
        f = flags[flags.index.isin(ids)]
        part = moves[moves["game_uuid"].isin(ids)]
        cost = {}
        for flagged in ("player", "opponent"):
            ff = f[f["flagged"] == flagged]
            cost[flagged] = (ff["ep_level"] - ff["actual"]).reindex(ids, fill_value=0.0)
        row = {"tc_group": g, "games": len(ids)}
        row.update(ci_cols("flag_cost_per_game_player", iid_ci(cost["player"])))
        row.update(ci_cols("flag_cost_per_game_opponent", iid_ci(cost["opponent"])))
        net = iid_ci(cost["opponent"].to_numpy() - cost["player"].to_numpy())
        row.update(ci_cols("net_to_you_per_game", net))
        row.update({"net_to_you_per_100_games": 100 * net[0], "net_to_you_per_100_games_lo": 100 * net[1], "net_to_you_per_100_games_hi": 100 * net[2]})
        row["expected_score"] = EVEN_SCORE
        for k, name in enumerate(("net_to_you_elo", "net_to_you_elo_lo", "net_to_you_elo_hi")):
            row[name] = float(excess_to_elo(EVEN_SCORE, net[k]))
        for side, frame in sides(part):
            low = frame[frame["low_clock"]].groupby("game_uuid")["ep_loss"].sum().reindex(ids, fill_value=0.0)
            row.update(ci_cols(f"low_clock_move_cost_per_game_{side}", iid_ci(low)))
        row["thin"] = min(len(f[f["flagged"] == "player"]), len(f[f["flagged"] == "opponent"])) < MIN_FLAG_GAMES
        rows.append(row)
    results.table("5d_clock_cost_per_game", by_group(pd.DataFrame(rows), [*groups, *pooled]), "Expected points per game the clock took from each side (level-clock valuation of flagged games, 0 in other games), the net to you (opponents' flag cost minus yours) per game, per 100 games and as an Elo-equivalent around an even score, and for scale the expected points each side lost on moves made under 10 s; CIs by game.")

    panels = [*groups, *pooled]
    states = ["winning", "level", "lost"]
    fig, axes = charts.group_grid(panels, sharey=True, title="When you lose on time, what was the position like?", panel=(2.6, 2.3))
    for g, ax in axes.items():
        part = board[board["tc_group"] == g]
        if part["thin"].all():
            thin_panel(ax, "too few flags")
            continue
        wide = pd.DataFrame({"state": states})
        for flagged in ("player", "opponent"):
            row = part[part["flagged"] == flagged].iloc[0]
            ok = not row["thin"]
            for suffix in ("", "_lo", "_hi"):
                wide[f"{flagged}{suffix}"] = [row[f"{s}{suffix}"] if ok else np.nan for s in states]
        charts.side_bars(ax, wide, "state", order=states)
        ax.yaxis.set_major_formatter(_percent())
    axes[panels[0]].set_ylabel("share of flagged games")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, f"Blue: games you lost on time; grey: games your opponents lost on time. The flagged side's position when the clock ran out, valued with the clocks levelled: winning (>= {WINNING_EP} expected points), level, lost (<= {LOST_EP}). Bottom row: controls of a class pooled. Sides with fewer than {MIN_FLAG_GAMES} flags not drawn. Whiskers: 95% CI.")
    fig.tight_layout()
    results.figure(fig, "5_flag_board_state")
    print(f"5 done at {time.time() - t0:.0f} s")

    # 6. can you see a flag coming? ----------------------------------------------------------------
    rows = []
    ends = ["lost on time", "won on time", "other"]
    for g, part in clocks.group_frames(moves, groups, pooled=True, tc_class=data.tc_class):
        for side, frame in sides(part):
            end = np.select([frame["mover_result"] == "timeout", frame["other_result"] == "timeout"], ends[:2], ends[2])
            frame = frame.assign(end=end)
            for mv in CHECK_MOVES:
                at = frame[frame["move_number"] == mv]
                for e in ends:
                    s = at.loc[at["end"] == e]
                    rows.append(
                        {
                            "tc_group": g, "side": side, "move": mv, "game_end": e,
                            **ci_cols("clock_seconds", iid_ci(s["clock_before_seconds"])),
                            **ci_cols("clock_share_of_base", iid_ci(s["clock_fraction"])),
                            "games": len(s), "thin": len(s) < MIN_FLAG_GAMES,
                        }  # fmt: skip
                    )
    pace = pd.DataFrame(rows)
    results.table("6_clock_before_flag", pace, f"Each side's clock before its move 10/20/30 in games that side went on to lose on time, win on time, or neither (seconds and share of base); games that reached the move; thin = fewer than {MIN_FLAG_GAMES} games; CIs by game.")
    fig, axes = charts.group_grid(panels, sharey=False, title=f"Can you see a flag coming? Clock before move {FLAG_CHECK_MOVE}")
    for g, ax in axes.items():
        part = pace[(pace["tc_group"] == g) & (pace["move"] == FLAG_CHECK_MOVE)]
        wide = pd.DataFrame({"game_end": ends})
        for side in ("player", "opponent"):
            p = part[part["side"] == side].set_index("game_end").reindex(ends)
            for suffix in ("", "_lo", "_hi"):
                wide[f"{side}{suffix}"] = np.where(p["thin"].to_numpy(dtype=bool), np.nan, p[f"clock_seconds{suffix}"].to_numpy(dtype=float))
        if wide[["player", "opponent"]].isna().all().all():
            charts.thin_note(ax, "too few flags")
            continue
        charts.side_bars(ax, wide, "game_end", order=ends)
        ax.set_ylim(bottom=0)
        rotate(ax)
    axes[groups[0]].set_ylabel("seconds left")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, f"Blue: your clock before your move {FLAG_CHECK_MOVE} in games you went on to lose on time, win on time, or neither; grey: the same for your opponents. Bars from fewer than {MIN_FLAG_GAMES} games left out. Bottom row: controls of a class pooled. Each panel has its own scale. Whiskers: 95% CI.")
    fig.tight_layout()
    results.figure(fig, "6_clock_before_flag")

    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


def _percent():
    from matplotlib.ticker import PercentFormatter

    return PercentFormatter(xmax=1, decimals=0)


if __name__ == "__main__":
    main()
