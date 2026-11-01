"""Question 4: clock allocation and losses on time.

Where extra seconds buy accuracy and where they do not; what the seconds
themselves cost in expected points; whether the player spends time in the
right places (long thinks on positions where nothing is at stake, snap moves
where everything is); and what the losses on time cost in expected points,
valued at the position on the board when the clock ran out.

Writes reports/04_clock_allocation.md and figures under
reports/figures/04_clock_allocation/.

Reuses the clock buckets, the game-clustered OLS and the stakes buckets of
analysis/03_time_pressure.py (imported as a module; importing runs nothing),
and refits F6's calibrated win model (deterministic) to value positions
under counterfactual clocks.
"""

from __future__ import annotations

import importlib
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    ci_column,
    cluster_bootstrap_mean,
    figure_style,
    load_data,
    mean_with_ci,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
tp = importlib.import_module("03_time_pressure")

SLUG = "04_clock_allocation"
SEED = 4
N_BOOT = 400
MIN_MOVES = tp.MIN_MOVES  # cells with fewer moves than this on either side are hidden
MIN_GAMES = 20

# Seconds spent on a move, bucketed per time class (the same position in the list means a different range in each class).
TIME_SPENT_EDGES = {
    "bullet": [0, 0.5, 1, 2, 4, 8, np.inf],
    "blitz": [0, 1, 2, 5, 10, 20, np.inf],
    "rapid": [0, 2, 5, 10, 20, 40, np.inf],
}
TIME_SPENT_LABELS = {
    "bullet": ["<0.5 s", "0.5-1 s", "1-2 s", "2-4 s", "4-8 s", "8+ s"],
    "blitz": ["<1 s", "1-2 s", "2-5 s", "5-10 s", "10-20 s", "20+ s"],
    "rapid": ["<2 s", "2-5 s", "5-10 s", "10-20 s", "20-40 s", "40+ s"],
}
# Stakes of the position in expected points: the gap between the best and the second-best move.
# "none" means the two best moves are worth the same (or there is only one legal move).
STAKES_EDGES = [-1, 1e-9, 0.03, 0.10, np.inf]
STAKES_LABELS = ["none", "low (<0.03)", "medium (0.03-0.10)", "high (0.10+)"]
PHASES = ["opening", "middlegame", "endgame"]
CHECK_MOVES = [10, 20, 30]  # move numbers at which the clock situation is read off
LONG_THINK_QUANTILE = 0.90  # a "long think" is above this quantile of all moves of the class (both sides)
SNAP_QUANTILE = 0.25  # a "snap move" is below this quantile
PREMOVE_SECONDS = 0.1  # chess.com charges 0.1 s for a pre-move


# --- helpers -------------------------------------------------------------------


def add_buckets(moves: pd.DataFrame) -> pd.DataFrame:
    """Q3's clock buckets plus the time-spent bucket (per class), the stakes band and the mover's result."""
    frame = tp.add_clock_buckets(moves)
    frame["time_bucket"] = pd.Series(pd.NA, index=frame.index, dtype="object")
    frame["time_order"] = np.nan
    for tc in TIME_CLASSES:
        mask = frame["time_class"] == tc
        cut = tp.cut_bucket(frame.loc[mask, "time_spent_seconds"], TIME_SPENT_EDGES[tc], TIME_SPENT_LABELS[tc])
        frame.loc[mask, "time_bucket"] = cut.astype(str)
        frame.loc[mask, "time_order"] = cut.cat.codes.astype(float)
    frame["stakes"] = tp.cut_bucket(frame["ep_criticality"].fillna(0.0), STAKES_EDGES, STAKES_LABELS)
    frame["one_move"] = frame["criticality"].isna()
    frame["premove"] = frame["time_spent_seconds"] <= PREMOVE_SECONDS
    frame["phase2"] = np.where(frame["phase"] == "endgame", "endgame", "opening/middlegame")
    frame["mover_result"] = np.where(frame["is_player_move"], frame["my_result"], frame["opponent_result"])
    frame["other_result"] = np.where(frame["is_player_move"], frame["opponent_result"], frame["my_result"])
    return frame


def with_time_categorical(part: pd.DataFrame, tc: str) -> pd.DataFrame:
    part = part.copy()
    part["time_bucket"] = pd.Categorical(part["time_bucket"], categories=TIME_SPENT_LABELS[tc], ordered=True)
    return part


def game_bootstrap(groups: Sequence[np.ndarray], weights: Sequence[np.ndarray], func: Callable[..., float], n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Any function of per-game sums, with a CI from resampling whole games.

    Each (groups[i], weights[i]) pair is summed within game; `func` receives
    the total of each sum (one float per pair) and returns the statistic. All
    pairs are resampled jointly, so paired quantities share their games.
    """
    games = np.unique(np.concatenate([np.asarray(g) for g in groups]))
    index = {g: i for i, g in enumerate(games)}
    per_game = []
    for g, w in zip(groups, weights):
        idx = pd.Series(np.asarray(g)).map(index).to_numpy()
        per_game.append(np.bincount(idx, weights=np.asarray(w, dtype=float), minlength=len(games)))
    stacked = np.stack(per_game, axis=1)
    estimate = float(func(*stacked.sum(axis=0)))
    if len(games) < 2:
        return estimate, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    draws = np.empty(n_boot)
    for b in range(n_boot):
        w = np.bincount(rng.integers(0, len(games), len(games)), minlength=len(games)).astype(float)
        draws[b] = func(*(w @ stacked))
    lo, hi = np.nanquantile(draws, [0.025, 0.975])
    return estimate, float(lo), float(hi)


def _two_frames(a: pd.DataFrame, b: pd.DataFrame, column: str) -> tuple[list, list]:
    groups = [a["game_uuid"].to_numpy(), a["game_uuid"].to_numpy(), b["game_uuid"].to_numpy(), b["game_uuid"].to_numpy()]
    weights = [a[column].to_numpy(), np.ones(len(a)), b[column].to_numpy(), np.ones(len(b))]
    return groups, weights


def mean_diff(a: pd.DataFrame, b: pd.DataFrame, column: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """mean(a[column]) - mean(b[column]), games resampled jointly."""
    return game_bootstrap(*_two_frames(a, b, column), lambda sa, na, sb, nb: sa / na - sb / nb if na > 0 and nb > 0 else np.nan, n_boot=n_boot)


def mean_ratio(a: pd.DataFrame, b: pd.DataFrame, column: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """mean(a[column]) / mean(b[column]), games resampled jointly."""
    return game_bootstrap(*_two_frames(a, b, column), lambda sa, na, sb, nb: (sa / na) / (sb / nb) if na > 0 and nb > 0 and sb > 0 else np.nan, n_boot=n_boot)


def share(frame: pd.DataFrame, numerator: pd.Series, denominator: pd.Series | None = None, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Share of `denominator` rows (all rows if None) that satisfy `numerator`, game-resampled."""
    den = pd.Series(True, index=frame.index) if denominator is None else denominator.astype(bool)
    return tp.ratio_bootstrap((numerator.astype(bool) & den).astype(float), den.astype(float), frame["game_uuid"], n_boot=n_boot, seed=SEED)


def fmt_ci(est: float, lo: float, hi: float, fmt: str = ".3f") -> str:
    if pd.isna(est):
        return ""
    if pd.isna(lo):
        return f"{est:{fmt}}"
    return f"{est:{fmt}} [{lo:{fmt}}, {hi:{fmt}}]"


def pct(est: float, lo: float, hi: float) -> str:
    if pd.isna(est):
        return ""
    return f"{100 * est:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"


def side_frames(frame: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [("you", frame[frame["is_player_move"]]), ("opponents", frame[~frame["is_player_move"]])]


def iid_ci(values: pd.Series, n_boot: int = 1000) -> tuple[float, float, float]:
    """Mean with a bootstrap CI where each row is a game."""
    return cluster_bootstrap_mean(values, pd.Series(np.arange(len(values)), index=values.index), n_boot=n_boot, seed=SEED)


def pick(table: pd.DataFrame, **where) -> pd.Series:
    m = pd.Series(True, index=table.index)
    for k, v in where.items():
        m &= table[k] == v
    return table[m].iloc[0]


# --- the calibrated model under counterfactual clocks -------------------------------


def fit_win_model():
    """Refit F6's calibrated win model (same features, same seed: deterministic) and return it with the pre-game ratings."""
    from chess_analysis import calibrate
    from chess_analysis.dataset import load_analysed, load_games, load_my_games

    ratings = calibrate.pre_game_ratings(load_my_games())
    X, y, _ = calibrate.training_frame(load_analysed(), load_games(), ratings)
    return calibrate.WinModel().fit(X, y), ratings


def add_clock_charge(moves: pd.DataFrame, model, ratings: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Price the seconds a move took: `clock_charge` and the unfloored change `ep_change`.

    `ep_after` is the calibrated expected points of the position after the
    move with the mover's clock *after* the move. Re-predicting the same
    position with the mover's clock as if the move had taken no time gives
    `ep_after_free`; `clock_charge = ep_after_free - ep_after` is the expected
    points the seconds themselves cost in the model. It is *not* an additive
    part of `ep_loss`: `ep_before` already prices in the time a typical move
    takes (the unfloored `ep_change = ep_before - ep_after` averages about
    zero), and `ep_loss` is the positive part of that change. Returns the
    frame with both columns and the largest gap between the refit's
    predictions and the stored `ep_after` (a check that the refit is exact).
    """
    from chess_analysis import calibrate

    after = calibrate.after_move_frame(moves, ratings)  # the side to move after the move: features from their view
    check = 1 - model.predict(after)["ep"].to_numpy()
    max_diff = float(np.nanmax(np.abs(check - moves["ep_after"].to_numpy())))
    free = after.copy()
    free["opponent_clock"] = (moves["clock_before_seconds"] + moves["increment_seconds"]).to_numpy()  # the mover's clock had the move taken no time
    free["clock_diff"] = free["clock"] - free["opponent_clock"]
    ep_after_free = 1 - model.predict(free)["ep"].to_numpy()
    out = moves.copy()
    out["clock_charge"] = ep_after_free - out["ep_after"].to_numpy()
    out["ep_change"] = out["ep_before"].to_numpy() - out["ep_after"].to_numpy()
    return out, max_diff


# --- time-value regression ------------------------------------------------------


def time_value_regression(frame: pd.DataFrame, tc: str, outcome: str, n_boot: int = N_BOOT) -> pd.DataFrame:
    """outcome ~ time-spent bucket x side + stakes + phase + ply + material + both clocks + legal moves + check + eval, one time class.

    Reference bucket is the fastest. Returns, per other bucket, the opponents'
    adjusted change in `outcome` relative to a snap move, the player's, and
    the difference (side x time interaction), each with a game-clustered CI.
    """
    labels = TIME_SPENT_LABELS[tc]
    player = frame["is_player_move"].astype(float).rename("player")
    tbucket = tp.dummies(frame["time_bucket"], labels[0], "time")
    inter = tbucket.mul(player, axis=0).add_prefix("player*")
    stakes_outcome = "cp_loss" if outcome == "cp_loss" else "ep_loss"
    controls = [
        tp.dummies(frame["phase"], "endgame", "phase"),
        tp.dummies(tp.stakes_bucket(frame, stakes_outcome), "0", "crit"),
        tp.dummies(pd.cut(frame["ply"], [0, 20, 40, 60, 80, np.inf], labels=["<20", "20-40", "40-60", "60-80", "80+"]), "<20", "ply"),
        tp.dummies(pd.cut(frame["pieces"], [-1, 2, 5, 9, 99], labels=["0-2", "3-5", "6-9", "10+"]), "10+", "pieces"),
        tp.dummies(frame["clock_bucket_coarse"], "30+ s", "clock"),
        tp.dummies(frame["opponent_clock_bucket_coarse"], "30+ s", "oppclock"),
        tp.dummies(pd.cut(frame["legal_moves"], [0, 1, 5, 15, 30, 999], labels=["1", "2-5", "6-15", "16-30", "31+"]), "16-30", "legal"),
        frame["in_check"].astype(float).rename("in_check"),
        tp.dummies(pd.cut(frame["cp_before"].abs(), [-1, 50, 150, 300, 600, 1e9], labels=["<50", "<150", "<300", "<600", "600+"]), "<50", "abs_eval"),
    ]
    design = pd.concat([player, tbucket, inter, *controls], axis=1)
    design = design.loc[:, design.std(axis=0).fillna(0) > 0]
    design.insert(0, "intercept", 1.0)
    beta, draws = tp.cluster_ols(design.to_numpy(dtype=float), frame[outcome].to_numpy(dtype=float), frame["game_uuid"].to_numpy(), n_boot=n_boot, seed=SEED)
    names = list(design.columns)
    rows = []
    for label in labels[1:]:
        c, pc = f"time={label}", f"player*time={label}"
        if c not in names or pc not in names:
            continue
        i, j = names.index(c), names.index(pc)
        opp, opp_d = beta[i], draws[:, i]
        you, you_d = beta[i] + beta[j], draws[:, i] + draws[:, j]
        q = lambda d: np.nanquantile(d, [0.025, 0.975])  # noqa: E731
        rows.append(
            {
                "time spent": label, "time_class": tc,
                "player": you, "player_lo": q(you_d)[0], "player_hi": q(you_d)[1],
                "opponent": opp, "opponent_lo": q(opp_d)[0], "opponent_hi": q(opp_d)[1],
                "diff": beta[j], "diff_lo": q(draws[:, j])[0], "diff_hi": q(draws[:, j])[1],
                "n_player": int(((frame["time_bucket"] == label) & frame["is_player_move"]).sum()),
                "n_opponent": int(((frame["time_bucket"] == label) & ~frame["is_player_move"]).sum()),
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


# --- losses on time -------------------------------------------------------------


def flag_positions(moves: pd.DataFrame, games: pd.DataFrame) -> pd.DataFrame:
    """One row per game decided or drawn by the clock, valued from the flagged side's view.

    The "moment you flagged" is the position after the last recorded move: in
    every timeout the last move in the PGN is the winner's (asserted in
    main), so the flagged side was to move in that position when the clock
    ran out. Columns: `flagged` ("player"/"opponent"), `kind` ("loss on time"
    / "flag draw"), `actual` (the flagged side's score), and the flagged
    side's expected points as played and board-only; `flag_valuations` adds
    the level-clock value.
    """
    last = moves.sort_values(["game_uuid", "ply"]).groupby("game_uuid").tail(1).set_index("game_uuid")
    g = games.set_index("uuid")
    rows = []
    for uuid, row in g.iterrows():
        if uuid not in last.index:
            continue
        if row["my_result"] == "timeout":
            flagged, kind, actual = "player", "loss on time", 0.0
        elif row["opponent_result"] == "timeout":
            flagged, kind, actual = "opponent", "loss on time", 0.0
        elif row["my_result"] == "timevsinsufficient":
            # The side that did not make the last recorded move was on move when the clock ran out.
            flagged, kind, actual = ("opponent" if last.loc[uuid, "is_player_move"] else "player"), "flag draw", 0.5
        else:
            continue
        rows.append({"uuid": uuid, "time_class": row["time_class"], "flagged": flagged, "kind": kind, "actual": actual})
    out = pd.DataFrame(rows).set_index("uuid")
    lm = last.loc[out.index]
    out["last_move_by_flagged"] = np.where(out["flagged"] == "player", lm["is_player_move"], ~lm["is_player_move"])
    out["last_ply"] = lm["ply"].to_numpy()
    out["phase"] = lm["phase"].to_numpy()
    # From the flagged side's view: the last mover's "after" values flipped.
    out["ep_as_played"] = (1 - lm["ep_after"]).to_numpy()
    out["ep_board"] = (1 - lm["ep_sf_after"]).to_numpy()
    out["winner_clock"] = lm["clock_seconds"].to_numpy()  # the last mover's clock after the move
    return out


def flag_valuations(flags: pd.DataFrame, moves: pd.DataFrame, model, ratings: pd.DataFrame) -> pd.DataFrame:
    """Add the level-clock counterfactual: the calibrated model on the same board with the flagged side's clock set equal to the winner's."""
    from chess_analysis import calibrate

    key = moves.set_index(["game_uuid", "ply"])
    sub = key.loc[list(zip(flags.index, flags["last_ply"]))].reset_index()
    after = calibrate.after_move_frame(sub, ratings)  # the flagged side's view (they are to move)
    level = after.copy()
    level["clock"] = level["opponent_clock"]
    level["clock_fraction"] = level["clock"] / level["base_seconds"]
    level["clock_diff"] = 0.0
    flags = flags.copy()
    flags["ep_level"] = model.predict(level)["ep"].to_numpy()
    return flags


# --- main ----------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    figure_style()
    import matplotlib.pyplot as plt

    data = load_data()
    moves = add_buckets(data.moves)
    moves = moves[moves["time_class"].isin(TIME_CLASSES)]
    games = data.games[data.games["time_class"].isin(TIME_CLASSES)].copy()
    model, ratings = fit_win_model()
    moves, refit_gap = add_clock_charge(moves, model, ratings)
    print(f"model refit done at {time.time() - t0:.0f} s; reproduces stored ep_after to within {refit_gap:.4f}")
    n_games = games["time_class"].value_counts().reindex(TIME_CLASSES)
    report = Report(SLUG, "Clock allocation and losses on time")
    report.text(
        f"{len(games):,} standard live games ({', '.join(f'{n:,} {tc}' for tc, n in n_games.items())}), "
        f"{len(moves):,} analysed moves ({moves['is_player_move'].sum():,} yours, {(~moves['is_player_move']).sum():,} opponents'), "
        f"{moves['game_uuid'].nunique():,} games with at least one analysed move. "
        "Every estimate is `estimate [lo, hi]`, a 95% interval from a bootstrap that resamples whole games. "
        "`ep_loss` (calibrated expected points lost by a move) is the main quality measure, `cp_loss` the secondary one. "
        "Time spent on a move is the mover's clock before the move minus the clock after it plus the increment; buckets of seconds spent are "
        "defined per time class (bullet " + ", ".join(TIME_SPENT_LABELS["bullet"]) + "; blitz " + ", ".join(TIME_SPENT_LABELS["blitz"]) + "; rapid " + ", ".join(TIME_SPENT_LABELS["rapid"]) + "). "
        "\"Stakes\" of a position is `ep_criticality`, the expected-points gap between the best and the second-best move: "
        "none (the two best moves are equally good, or only one legal move), low (<0.03), medium (0.03-0.10), high (0.10+). "
        f"F6's calibrated win model is refitted inside this script (deterministic; it reproduces the stored `ep_after` of every move to within {refit_gap:.4f}) "
        "so that positions can be re-valued under counterfactual clocks (sections 2c and 5)."
    )

    # ----- 1. where the seconds go -------------------------------------------------
    report.heading("1. Where the seconds go")
    report.text("Seconds spent per move by phase, you vs opponents in the same games:")
    per_phase = tp.bucket_curve(moves.assign(phase=pd.Categorical(moves["phase"], PHASES, ordered=True)), "phase", "time_spent_seconds", by=["time_class"], n_boot=N_BOOT)
    per_phase = per_phase.set_index("time_class").loc[TIME_CLASSES].reset_index()
    report.table(tp.compact(per_phase, "phase", ".2f", extra=["time_class"]))

    report.text("Share of each side's total thinking time that goes to each phase, and the total seconds used per game:")
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for side, frame in side_frames(part):
            row = {"time_class": tc, "side": side}
            for phase in PHASES:
                e, lo, hi = tp.ratio_bootstrap((frame["phase"] == phase) * frame["time_spent_seconds"], frame["time_spent_seconds"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)
                row[phase] = pct(e, lo, hi)
            total = frame.groupby("game_uuid")["time_spent_seconds"].sum()
            row["seconds used per game"] = fmt_ci(*iid_ci(total), ".0f")
            rows.append(row)
    report.table(pd.DataFrame(rows))

    report.text(
        "The clock situation at fixed points of the game, read off your own moves: your clock minus the opponent's (seconds, positive = you are ahead) "
        f"and the share of games in which you are behind or ahead by more than 3 s, at move {', '.join(map(str, CHECK_MOVES))}. Only games that reached the move count."
    )
    rows = []
    mine = moves[moves["is_player_move"]]
    for tc in TIME_CLASSES:
        for mv in CHECK_MOVES:
            part = mine[(mine["time_class"] == tc) & (mine["move_number"] == mv)]
            if len(part) < MIN_GAMES:
                continue
            rows.append(
                {
                    "time_class": tc, "move": mv, "games": len(part),
                    "your clock - opponent's (s)": fmt_ci(*iid_ci(part["clock_diff_seconds"]), ".1f"),
                    "behind by 3+ s": pct(*iid_ci((part["clock_diff_seconds"] < -3).astype(float))),
                    "ahead by 3+ s": pct(*iid_ci((part["clock_diff_seconds"] > 3).astype(float))),
                }  # fmt: skip
            )
    clock_at = pd.DataFrame(rows)
    report.table(clock_at)

    fig, axes = plt.subplots(1, 3, figsize=(12.6, 3.6))
    for ax, tc in zip(axes, TIME_CLASSES):
        part = moves[moves["time_class"] == tc]
        for side_key, side_name in (("player", "you"), ("opponent", "opponents")):
            s = part[part["side"] == side_key].groupby("move_number")["clock_before_seconds"].agg(["mean", "count"])
            s = s[s["count"] >= MIN_GAMES]
            ax.plot(s.index, s["mean"], color=SIDE_COLOURS[side_key], label=side_name)
        ax.set_title(tc)
        ax.set_xlabel("move number")
    axes[0].set_ylabel("mean seconds left before the move")
    axes[0].legend()
    fig.suptitle("Pace: mean clock left by move number (games still running)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "clock_by_move", "Mean clock left before each move, you vs opponents, over the games that reached that move (at least 20 games per point).")
    plt.close(fig)
    print(f"section 1 done at {time.time() - t0:.0f} s")

    # ----- 2. does time buy accuracy? ------------------------------------------------
    report.heading("2. Does spending time buy accuracy?")
    report.text(
        "Error per move by the seconds spent on it, you vs opponents. Two things make these curves rise with time: harder positions get more time (selection), "
        "and `ep_loss` charges a move for the clock it consumes (the position after the move is valued with the clock *after* the move; section 2c measures that charge). "
        "`cp_loss` is clock-free and measures board accuracy only. The clean evidence that time matters is Q3's clock-pressure result (board accuracy falls when a side is *forced* to move fast); "
        "what the time-spent curves can show is how your fast and slow moves compare with your opponents' fast and slow moves."
    )
    curves = {}
    for outcome in ("ep_loss", "cp_loss"):
        parts = []
        for tc in TIME_CLASSES:
            part = with_time_categorical(moves[moves["time_class"] == tc], tc)
            table = tp.bucket_curve(part, "time_bucket", outcome, n_boot=N_BOOT)
            table["time_class"] = tc
            parts.append(table)
        curves[outcome] = pd.concat(parts, ignore_index=True)
    fig, axes = plt.subplots(2, 3, figsize=(12.6, 6.4))
    for r, (outcome, ylabel) in enumerate((("ep_loss", "ep_loss per move"), ("cp_loss", "cp_loss per move"))):
        for ax, tc in zip(axes[r], TIME_CLASSES):
            part = curves[outcome][curves[outcome]["time_class"] == tc]
            for side_key, side_name in (("player", "you"), ("opponent", "opponents")):
                ok = part[f"n_{side_key}"] >= MIN_MOVES
                ax.plot(part.loc[ok, "bucket_order"], part.loc[ok, side_key], marker="o", color=SIDE_COLOURS[side_key], label=side_name)
                ax.fill_between(part.loc[ok, "bucket_order"], part.loc[ok, f"{side_key}_lo"], part.loc[ok, f"{side_key}_hi"], color=SIDE_COLOURS[side_key], alpha=0.18)
            ax.set_xticks(part["bucket_order"])
            ax.set_xticklabels(part["time_bucket"], rotation=45, ha="right", fontsize=8)
            if r == 0:
                ax.set_title(tc)
        axes[r][0].set_ylabel(ylabel)
    axes[0][0].legend(loc="upper left")
    fig.suptitle("Error per move by seconds spent on the move (raw)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "error_vs_time_spent", "Mean ep_loss (top) and cp_loss (bottom) per move by seconds spent on the move; bands are game-clustered 95% CIs.")
    plt.close(fig)
    report.text("`ep_loss` per move by seconds spent (cells with fewer than 30 moves on either side are left out):")
    report.table(tp.compact(curves["ep_loss"], "time_bucket", ".4f", extra=["time_class"]))
    report.text("`cp_loss` per move by seconds spent:")
    report.table(tp.compact(curves["cp_loss"], "time_bucket", ".1f", extra=["time_class"]))

    # Within stakes bands.
    report.heading("2a. Within stakes bands", level=3)
    report.text("The same `ep_loss` curves within stakes bands. The table shows the medium and high bands, where there is something to find.")
    parts = []
    for tc in TIME_CLASSES:
        part = with_time_categorical(moves[moves["time_class"] == tc], tc)
        table = tp.bucket_curve(part, "time_bucket", "ep_loss", by=["stakes"], n_boot=300)
        table["time_class"] = tc
        parts.append(table)
    by_stakes = pd.concat(parts, ignore_index=True)
    by_stakes["stakes"] = by_stakes["stakes"].astype(str)
    fig, axes = plt.subplots(1, 3, figsize=(12.6, 4.2), sharey=False)
    stakes_colours = {"none": "#9aa5ad", "low (<0.03)": "#6c9fb8", "medium (0.03-0.10)": "#2c6e91", "high (0.10+)": "#143d57"}
    for ax, tc in zip(axes, TIME_CLASSES):
        part = by_stakes[by_stakes["time_class"] == tc]
        for stakes in STAKES_LABELS:
            p = part[part["stakes"] == stakes]
            ok = (p["n_player"] >= MIN_MOVES) & (p["n_opponent"] >= MIN_MOVES)
            ax.plot(p.loc[ok, "bucket_order"], p.loc[ok, "player"], marker="o", color=stakes_colours[stakes], label=f"you, {stakes}")
            ax.plot(p.loc[ok, "bucket_order"], p.loc[ok, "opponent"], marker="o", linestyle="--", color=stakes_colours[stakes], alpha=0.7, label=f"opponents, {stakes}")
        ax.set_xticks(sorted(part["bucket_order"].unique()))
        ax.set_xticklabels(TIME_SPENT_LABELS[tc], rotation=45, ha="right", fontsize=8)
        ax.set_title(tc)
    axes[0].set_ylabel("ep_loss per move")
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=4, fontsize=8, bbox_to_anchor=(0.5, -0.02))
    fig.suptitle("ep_loss by seconds spent, within stakes bands (solid: you, dashed: opponents)", fontweight="bold")
    fig.tight_layout(rect=(0, 0.08, 1, 1))
    report.figure(fig, "error_vs_time_by_stakes", "ep_loss per move by seconds spent, one line per stakes band; solid lines you, dashed opponents. Points with fewer than 30 moves on either side are omitted.")
    plt.close(fig)
    report.table(tp.compact(by_stakes[by_stakes["stakes"].isin(["medium (0.03-0.10)", "high (0.10+)"])], "time_bucket", ".4f", extra=["time_class", "stakes"]))
    print(f"section 2 curves done at {time.time() - t0:.0f} s")

    # Adjusted regression.
    report.heading("2b. Adjusted: your error-vs-time gradient against your opponents'", level=3)
    report.text(
        "Linear model per time class: `outcome ~ seconds-spent bucket x side + stakes (in the outcome's currency) + phase + ply + material + mover's and opponent's clock bucket + number of legal moves + in check + |eval| bucket`, "
        "all moves of both sides, CIs from a bootstrap over games. The reference is the fastest bucket, so each coefficient is the extra error on moves that got that much time rather than a snap, holding those position features fixed. "
        "Positive coefficients for both sides mean the controls do not capture difficulty: time still goes to the moves that turn out hard, so **the coefficients are not the causal value of a second**. "
        "The informative column is `you - opp` (the side x time interaction): it compares the two sides' gradients under the same controls and says whether your long thinks end better or worse than your opponents' long thinks, relative to each side's snap moves. "
        "The `ep_loss` gradient also contains the clock charge of section 2c (steeper in bullet for that reason); the `cp_loss` gradient is board accuracy only."
    )
    adjusted = {}
    for outcome, fmt in (("ep_loss", ".4f"), ("cp_loss", ".1f")):
        parts = []
        for tc in TIME_CLASSES:
            part = moves[moves["time_class"] == tc]
            parts.append(time_value_regression(part, tc, outcome, n_boot=300))
        adjusted[outcome] = pd.concat(parts, ignore_index=True)
        report.text(f"Adjusted extra `{outcome}` relative to the fastest bucket:")
        report.table(tp.compact(adjusted[outcome], "time spent", fmt, extra=["time_class"]))
        print(f"regression {outcome} done at {time.time() - t0:.0f} s")

    # Clock charge.
    report.heading("2c. What the seconds themselves cost: the clock charge", level=3)
    mean_change = moves.groupby("time_class")["ep_change"].mean().reindex(TIME_CLASSES)
    report.text(
        "`ep_loss` values the position after a move with the mover's clock after the move. Re-valuing the same position with the clock the mover would have had if the move had taken no time gives the **clock charge**: "
        "the expected points the seconds themselves cost, as the calibrated model prices them (it has learnt from these games how much being behind on the clock lowers the score). "
        "The charge is not an additive slice of `ep_loss`: the model's value of a position before the move already assumes the mover will spend a typical amount of time on it, so the unfloored change `ep_before - ep_after` averages close to zero per move "
        f"({', '.join(f'{tc} {mean_change[tc]:+.4f}' for tc in TIME_CLASSES)}) and `ep_loss` is its positive part. What the charge shows is how much of a move's `ep_loss` can come from the clock alone, and how the price of a second depends on the time class and on the time left. "
        "Mean clock charge per move by seconds spent, you vs opponents:"
    )
    parts = []
    for tc in TIME_CLASSES:
        part = with_time_categorical(moves[moves["time_class"] == tc], tc)
        table = tp.bucket_curve(part, "time_bucket", "clock_charge", n_boot=300)
        table["time_class"] = tc
        parts.append(table)
    charge_curve = pd.concat(parts, ignore_index=True)
    report.table(tp.compact(charge_curve, "time_bucket", ".4f", extra=["time_class"]))
    report.text("The same charge depends on how much time is left: mean clock charge per move (both sides pooled) by the mover's clock before the move and the seconds spent, bullet and blitz. Blank: fewer than 30 moves.")
    for tc in ("bullet", "blitz"):
        part = with_time_categorical(moves[moves["time_class"] == tc], tc)
        grid = mean_with_ci(part, "clock_charge", by=["clock_bucket_coarse", "time_bucket"], n_boot=300)
        grid["cell"] = np.where(grid["n"] >= MIN_MOVES, ci_column(grid, "mean", "lo", "hi", ".4f"), "")
        wide = grid.pivot(index="clock_bucket_coarse", columns="time_bucket", values="cell").reindex(index=tp.CLOCK_COARSE_LABELS, columns=TIME_SPENT_LABELS[tc]).fillna("")
        wide.index.name = f"{tc}: clock before"
        report.table(wide, index=True)
    report.text(
        "Per game: the clock charge each side pays over a whole game (the sum over its moves), and the paired difference. In bullet a side's whole clock is worth about a point, and both sides spend it, "
        "so the totals are similar and largely cancel in the game's outcome; the difference between the sides is the price of one side's slower pace."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        row = {"time_class": tc}
        totals = {}
        for side, frame in side_frames(part):
            totals[side] = frame.groupby("game_uuid")["clock_charge"].sum()
            row[f"clock charge per game, {side}"] = fmt_ci(*iid_ci(totals[side]), ".3f")
        p, o = part[part["is_player_move"]], part[~part["is_player_move"]]
        n_g = part["game_uuid"].nunique()
        d = game_bootstrap([p["game_uuid"].to_numpy(), o["game_uuid"].to_numpy()], [p["clock_charge"].to_numpy(), o["clock_charge"].to_numpy()], lambda sp, so: (sp - so) / n_g)
        row["you - opp"] = fmt_ci(*d, ".3f")
        row["ep_loss per game, you / opp"] = f"{p.groupby('game_uuid')['ep_loss'].sum().mean():.3f} / {o.groupby('game_uuid')['ep_loss'].sum().mean():.3f}"
        rows.append(row)
    charge_per_game = pd.DataFrame(rows)
    report.table(charge_per_game)
    print(f"section 2c done at {time.time() - t0:.0f} s")

    # ----- 3. the where-to-spend-time map ----------------------------------------------
    report.heading("3. The where-to-spend-time map: phase x stakes")
    median_time = {tc: float(moves.loc[moves["time_class"] == tc, "time_spent_seconds"].median()) for tc in TIME_CLASSES}
    report.text(
        "For each phase x stakes cell: the seconds you and your opponents spend per move, and the raw difference in error between the slow and the quick moves of that cell "
        f"(slow = at or above the class's median seconds per move: {', '.join(f'{tc} {median_time[tc]:.1f} s' for tc in TIME_CLASSES)}; quick = below). "
        "Within a cell the slow moves are still the harder ones, so `slow - quick` is positive almost everywhere and is not the benefit of time; read it side by side: where your gap is larger than the opponents', your slow moves in that kind of position end relatively worse than theirs. "
        "`ep` is `ep_loss`, `cp` is `cp_loss`. Cells with fewer than 30 moves in any group are blank."
    )
    map_rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for phase in PHASES:
            for stakes in STAKES_LABELS:
                cell = part[(part["phase"] == phase) & (part["stakes"] == stakes)]
                row = {"time_class": tc, "phase": phase, "stakes": stakes}
                for side, frame in side_frames(cell):
                    row[f"seconds {side}"] = fmt_ci(*cluster_bootstrap_mean(frame["time_spent_seconds"], frame["game_uuid"], n_boot=300, seed=SEED), ".2f") if len(frame) >= MIN_MOVES else ""
                    slow, quick = frame[frame["time_spent_seconds"] >= median_time[tc]], frame[frame["time_spent_seconds"] < median_time[tc]]
                    ok = len(slow) >= MIN_MOVES and len(quick) >= MIN_MOVES
                    row[f"ep slow-quick {side}"] = fmt_ci(*mean_diff(slow, quick, "ep_loss", n_boot=300), ".4f") if ok else ""
                    row[f"cp slow-quick {side}"] = fmt_ci(*mean_diff(slow, quick, "cp_loss", n_boot=300), ".1f") if ok else ""
                    row[f"n {side}"] = len(frame)
                map_rows.append(row)
    time_map = pd.DataFrame(map_rows)
    for tc in TIME_CLASSES:
        report.text(f"**{tc}**")
        part = time_map[time_map["time_class"] == tc].drop(columns=["time_class"])
        part["moves you/opp"] = part["n you"].astype(str) + "/" + part["n opponents"].astype(str)
        report.table(part.drop(columns=["n you", "n opponents"]))

    report.heading("3a. The price of having no time, by position type", level=3)
    report.text(
        "A cleaner view of where seconds matter: the extra error on moves made with under 10 s on the clock compared with moves made with 30+ s, in the same phase x stakes cell, you vs opponents (bullet and blitz; rapid has too few low-clock moves). "
        "Here the shortage of time is imposed by the game rather than chosen move by move, so this is closer to what a second is worth in each kind of position. "
        "`cp_loss` is the clock-free currency; `ep_loss` is shown second because, as Q3 found, the calibration discounts positional damage when the mover is about to flag. "
        "Where a cell is clearly positive, moves of that kind suffer when the clock is short; where it straddles zero or is negative, the shortage of time is not what separates those moves from the same kind of move at 30+ s (the 30+ s moves of a bullet endgame are the early, material-rich endgames). Blank: fewer than 30 moves in either group."
    )
    rows = []
    for tc in ("bullet", "blitz"):
        part = moves[moves["time_class"] == tc]
        for phase2 in ("opening/middlegame", "endgame"):
            for stakes in STAKES_LABELS:
                cell = part[(part["phase2"] == phase2) & (part["stakes"] == stakes)]
                row = {"time_class": tc, "phase": phase2, "stakes": stakes}
                for side, frame in side_frames(cell):
                    low, high = frame[frame["clock_before_seconds"] < tp.LOW_CLOCK_SECONDS], frame[frame["clock_before_seconds"] >= 30]
                    ok = len(low) >= MIN_MOVES and len(high) >= MIN_MOVES
                    row[f"cp <10 s - 30+ s {side}"] = fmt_ci(*mean_diff(low, high, "cp_loss", n_boot=300), ".1f") if ok else ""
                    row[f"ep <10 s - 30+ s {side}"] = fmt_ci(*mean_diff(low, high, "ep_loss", n_boot=300), ".4f") if ok else ""
                    row[f"n {side}"] = f"{len(low)}/{len(high)}"
                rows.append(row)
    price_map = pd.DataFrame(rows)
    price_map["moves <10 s/30+ s you; opp"] = price_map["n you"] + "; " + price_map["n opponents"]
    price_map = price_map.drop(columns=["n you", "n opponents"])
    price_map = price_map[price_map.filter(like="cp <10").ne("").any(axis=1)]
    report.table(price_map)
    print(f"section 3 done at {time.time() - t0:.0f} s")

    # ----- 4. allocation: do the seconds follow the stakes? ------------------------------
    report.heading("4. Allocation: do the seconds follow the stakes?")
    report.text("Seconds spent per move by stakes band, you vs opponents, and the allocation ratio: seconds on high-stakes positions divided by seconds on no-stakes positions (1.0 = the same time whatever the stakes).")
    alloc = tp.bucket_curve(moves, "stakes", "time_spent_seconds", by=["time_class"], n_boot=N_BOOT)
    alloc = alloc.set_index("time_class").loc[TIME_CLASSES].reset_index()
    report.table(tp.compact(alloc, "stakes", ".2f", extra=["time_class"]))
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        row = {"time_class": tc}
        for side, frame in side_frames(part):
            high, none = frame[frame["stakes"] == "high (0.10+)"], frame[frame["stakes"] == "none"]
            row[f"ratio {side}"] = fmt_ci(*mean_ratio(high, none, "time_spent_seconds"), ".2f")
        groups, weights = [], []
        for _, frame in side_frames(part):
            for sel in ("high (0.10+)", "none"):
                f = frame[frame["stakes"] == sel]
                groups += [f["game_uuid"].to_numpy(), f["game_uuid"].to_numpy()]
                weights += [f["time_spent_seconds"].to_numpy(), np.ones(len(f))]
        d = game_bootstrap(groups, weights, lambda ph, nh, pn, nn, oh, noh, on, non: (ph / nh) / (pn / nn) - (oh / noh) / (on / non))
        row["ratio you - opp"] = fmt_ci(*d, ".2f")
        rows.append(row)
    alloc_ratio = pd.DataFrame(rows)
    report.table(alloc_ratio)

    fig, axes = plt.subplots(1, 3, figsize=(12.6, 3.6))
    for ax, tc in zip(axes, TIME_CLASSES):
        part = alloc[alloc["time_class"] == tc]
        x = np.arange(len(part))
        for k, (side_key, side_name) in enumerate((("player", "you"), ("opponent", "opponents"))):
            ax.bar(x + (k - 0.5) * 0.38, part[side_key], width=0.36, color=SIDE_COLOURS[side_key], label=side_name, yerr=[part[side_key] - part[f"{side_key}_lo"], part[f"{side_key}_hi"] - part[side_key]], capsize=2)
        ax.set_xticks(x)
        ax.set_xticklabels(part["stakes"], rotation=30, ha="right", fontsize=8)
        ax.set_title(tc)
    axes[0].set_ylabel("mean seconds per move")
    axes[0].legend()
    fig.suptitle("Seconds spent per move by the stakes of the position", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "time_by_stakes", "Mean seconds spent per move by stakes band (ep_criticality), you vs opponents; error bars are game-clustered 95% CIs.")
    plt.close(fig)

    thresholds = {tc: (float(moves.loc[moves["time_class"] == tc, "time_spent_seconds"].quantile(LONG_THINK_QUANTILE)), float(moves.loc[moves["time_class"] == tc, "time_spent_seconds"].quantile(SNAP_QUANTILE))) for tc in TIME_CLASSES}
    report.text(
        "Misallocation, two kinds. A **long think** is a move above the class's 90th percentile of seconds spent (both sides pooled: "
        + ", ".join(f"{tc} {thresholds[tc][0]:.1f} s" for tc in TIME_CLASSES)
        + "); a **snap move** is below the 25th percentile ("
        + ", ".join(f"{tc} {thresholds[tc][1]:.1f} s" for tc in TIME_CLASSES)
        + "). Wasted thinks: long thinks on positions with nothing at stake, and the share of all thinking time that goes to such positions. "
        "Rushed decisions: high-stakes positions (0.10+ expected points between the best and second-best move) played as snap moves, and the error on those snapped moves against the other high-stakes moves."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        long_t, snap_t = thresholds[tc]
        for side, frame in side_frames(part):
            is_long = frame["time_spent_seconds"] >= long_t
            is_snap = frame["time_spent_seconds"] < snap_t
            none = frame["stakes"] == "none"
            high = frame["stakes"] == "high (0.10+)"
            n_g = frame["game_uuid"].nunique()
            snapped, considered = frame[high & is_snap], frame[high & ~is_snap]
            rows.append(
                {
                    "time_class": tc, "side": side,
                    "long thinks per game": f"{is_long.sum() / n_g:.2f}",
                    "long thinks on no-stakes positions": pct(*share(frame, none, is_long)),
                    "share of thinking time on no-stakes positions": pct(*tp.ratio_bootstrap(none * frame["time_spent_seconds"], frame["time_spent_seconds"], frame["game_uuid"], n_boot=N_BOOT, seed=SEED)),
                    "high-stakes positions snapped": pct(*share(frame, is_snap, high)),
                    "ep_loss snapped high-stakes": fmt_ci(*cluster_bootstrap_mean(snapped["ep_loss"], snapped["game_uuid"], n_boot=N_BOOT, seed=SEED), ".3f") if len(snapped) >= MIN_MOVES else "",
                    "ep_loss other high-stakes": fmt_ci(*cluster_bootstrap_mean(considered["ep_loss"], considered["game_uuid"], n_boot=N_BOOT, seed=SEED), ".3f") if len(considered) >= MIN_MOVES else "",
                }  # fmt: skip
            )
    misalloc = pd.DataFrame(rows)
    report.table(misalloc)

    report.text(f"Pre-moves (moves charged {PREMOVE_SECONDS:.1f} s or less): how often each side pre-moves and what those moves cost.")
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for side, frame in side_frames(part):
            pm = frame[frame["premove"]]
            rows.append(
                {
                    "time_class": tc, "side": side,
                    "share of moves": pct(*share(frame, frame["premove"])),
                    "ep_loss on pre-moves": fmt_ci(*cluster_bootstrap_mean(pm["ep_loss"], pm["game_uuid"], n_boot=N_BOOT, seed=SEED), ".4f"),
                    "cp_loss on pre-moves": fmt_ci(*cluster_bootstrap_mean(pm["cp_loss"], pm["game_uuid"], n_boot=N_BOOT, seed=SEED), ".1f"),
                    "share of pre-moves on no-stakes positions": pct(*share(frame, frame["stakes"] == "none", frame["premove"])),
                    "moves": len(pm),
                }  # fmt: skip
            )
    premoves = pd.DataFrame(rows)
    report.table(premoves)

    report.text("Forced moves (one legal move): seconds spent on them per move and per game. Time spent here buys nothing.")
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for side, frame in side_frames(part):
            forced = frame[frame["one_move"]]
            per_game = forced.groupby("game_uuid")["time_spent_seconds"].sum().reindex(frame["game_uuid"].unique(), fill_value=0.0)
            rows.append({"time_class": tc, "side": side, "forced moves": len(forced), "seconds per forced move": fmt_ci(*cluster_bootstrap_mean(forced["time_spent_seconds"], forced["game_uuid"], n_boot=N_BOOT, seed=SEED), ".2f"), "seconds per game": fmt_ci(*iid_ci(per_game), ".2f")})
    report.table(pd.DataFrame(rows))
    print(f"section 4 done at {time.time() - t0:.0f} s")

    # ----- 5. losses on time ----------------------------------------------------------
    report.heading("5. Losses on time: what the clock cost")
    flags = flag_positions(moves, games)
    assert not flags["last_move_by_flagged"].any(), "a timeout game ends with the flagged side's own move"
    flags = flag_valuations(flags, moves, model, ratings)
    counts = []
    for tc in TIME_CLASSES:
        g = games[games["time_class"] == tc]
        f = flags[flags["time_class"] == tc]
        my_losses, opp_losses = (g["outcome"] == "loss").sum(), (g["outcome"] == "win").sum()
        counts.append(
            {
                "time_class": tc, "games": len(g),
                "your losses on time": int(((f["flagged"] == "player") & (f["kind"] == "loss on time")).sum()),
                "share of your losses": f"{((f['flagged'] == 'player') & (f['kind'] == 'loss on time')).sum() / my_losses:.1%}",
                "opponents' losses on time": int(((f["flagged"] == "opponent") & (f["kind"] == "loss on time")).sum()),
                "share of their losses": f"{((f['flagged'] == 'opponent') & (f['kind'] == 'loss on time')).sum() / opp_losses:.1%}",
                "flag draws (you / they flagged)": f"{((f['flagged'] == 'player') & (f['kind'] == 'flag draw')).sum()} / {((f['flagged'] == 'opponent') & (f['kind'] == 'flag draw')).sum()}",
            }  # fmt: skip
        )
    counts = pd.DataFrame(counts)
    report.table(counts)
    report.text(
        f"The position valued is the one on the board when the clock ran out: after the winner's last recorded move, with the flagged side to move "
        f"(in all {len(flags):,} games decided or drawn by the clock the last recorded move was the other side's, so this is unambiguous; a flag draw is a timeout against insufficient mating material). "
        "Three valuations of that position from the flagged side's view, all in expected points (1 = certain win):\n\n"
        "- **as played**: the calibrated model with the clocks as they were. The model knows the mover has (almost) no time, so this is low by construction and is a floor;\n"
        "- **level clocks**: the same model on the same board, with the flagged side's clock set equal to the winner's. This is what the position was worth had the clock not decided it, and is the headline;\n"
        "- **board only**: Stockfish's own win/draw/loss model of the eval, with no clocks, ratings or phase (over-confident at the extremes), a ceiling.\n\n"
        "Points lost to the clock = valuation minus the actual score (0 for a loss on time, 0.5 for a flag draw), summed over the flagged games."
    )
    rows = []
    for tc in TIME_CLASSES:
        for flagged, side in (("player", "you"), ("opponent", "opponents")):
            f = flags[(flags["time_class"] == tc) & (flags["flagged"] == flagged)]
            if len(f) < MIN_GAMES:
                continue
            row = {"time_class": tc, "flagged": side, "games": len(f)}
            for col, name in (("ep_as_played", "as played"), ("ep_level", "level clocks"), ("ep_board", "board only")):
                cost = f[col] - f["actual"]
                e, lo, hi = iid_ci(cost)
                row[f"{name}: per flagged game"] = fmt_ci(e, lo, hi, ".3f")
                row[f"{name}: total points"] = fmt_ci(e * len(f), lo * len(f), hi * len(f), ".1f")
            rows.append(row)
    valuation = pd.DataFrame(rows)
    report.table(valuation)

    report.text(
        "Spread over every game of the class (zero in games that did not end on the clock), the level-clock valuation gives the expected points per game that the clock took from each side; "
        "next to it, for scale, the expected points per game each side lost on moves played with under 10 s (Q3's measure of what low-clock *moves* cost, recomputed here)."
    )
    rows = []
    for tc in TIME_CLASSES:
        g_ids = games.loc[games["time_class"] == tc, "uuid"]
        part = moves[moves["time_class"] == tc]
        row = {"time_class": tc, "games": len(g_ids)}
        for flagged, side in (("player", "you"), ("opponent", "opponents")):
            f = flags[(flags["time_class"] == tc) & (flags["flagged"] == flagged)]
            cost = (f["ep_level"] - f["actual"]).reindex(g_ids, fill_value=0.0)
            row[f"clock cost per game, {side}"] = fmt_ci(*iid_ci(cost), ".4f")
        for side, frame in side_frames(part):
            low = frame[frame["low_clock"]].groupby("game_uuid")["ep_loss"].sum().reindex(g_ids, fill_value=0.0)
            row[f"<10 s move cost per game, {side}"] = fmt_ci(*iid_ci(low), ".4f")
        rows.append(row)
    per_game_cost = pd.DataFrame(rows)
    report.table(per_game_cost)

    report.text(
        "What the board looked like when the clock ran out (level-clock valuation): the share of flagged games in which the flagged side was winning (expected points >= 0.8), level (0.2-0.8) or lost (<= 0.2) on the board, "
        "and how much time the winner had left."
    )
    rows = []
    for tc in TIME_CLASSES:
        for flagged, side in (("player", "you"), ("opponent", "opponents")):
            f = flags[(flags["time_class"] == tc) & (flags["flagged"] == flagged)]
            if len(f) < MIN_GAMES:
                continue
            fake = f.assign(game_uuid=f.index)
            rows.append(
                {
                    "time_class": tc, "flagged": side, "games": len(f),
                    "winning on the board": pct(*share(fake, f["ep_level"] >= 0.8)),
                    "level": pct(*share(fake, (f["ep_level"] > 0.2) & (f["ep_level"] < 0.8))),
                    "lost on the board": pct(*share(fake, f["ep_level"] <= 0.2)),
                    "points lost from winning boards": f"{(f.loc[f['ep_level'] >= 0.8, 'ep_level'] - f.loc[f['ep_level'] >= 0.8, 'actual']).sum():.1f}",
                    "winner's clock: median s": f"{f['winner_clock'].median():.1f}",
                    "winner had 30+ s": pct(*share(fake, f["winner_clock"] >= 30)),
                    "endgame": pct(*share(fake, f["phase"] == "endgame")),
                }  # fmt: skip
            )
    board_state = pd.DataFrame(rows)
    report.table(board_state)

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, tc in zip(axes, ["bullet", "blitz"]):
        for flagged, side_key, side_name in (("player", "player", "you"), ("opponent", "opponent", "opponents")):
            f = flags[(flags["time_class"] == tc) & (flags["flagged"] == flagged)]
            ax.hist(f["ep_level"], bins=np.linspace(0, 1, 21), histtype="step", linewidth=1.8, color=SIDE_COLOURS[side_key], label=f"{side_name} flagged (n={len(f)})", density=True)
        ax.set_title(tc)
        ax.set_xlabel("expected points of the flagged side, level clocks")
        ax.legend(fontsize=8)
    axes[0].set_ylabel("density")
    fig.suptitle("What the flagged side's position was worth when the clock ran out", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "flag_value_distribution", "Distribution of the level-clock expected points of the flagged side at the moment the clock ran out, games you flagged vs games the opponent flagged.")
    plt.close(fig)

    report.text(
        f"Was the flag decided early? Each side's clock before its move {', '.join(map(str, CHECK_MOVES))}, in games that side lost on time against games it won on time and all other games that reached that move (seconds, mean with CI). "
        "A gap that is already there at move 10 means the time was spent in the opening, not in the scramble."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for side, frame in side_frames(part):
            frame = frame.assign(end=np.select([frame["mover_result"] == "timeout", frame["other_result"] == "timeout"], ["lost on time", "won on time"], "other"))
            for mv in CHECK_MOVES:
                at = frame[frame["move_number"] == mv]
                row = {"time_class": tc, "side": side, "move": mv}
                for end in ("lost on time", "won on time", "other"):
                    s = at.loc[at["end"] == end, "clock_before_seconds"]
                    row[end] = fmt_ci(*iid_ci(s), ".1f") + f" (n={len(s)})" if len(s) >= MIN_GAMES else ""
                rows.append(row)
    pace = pd.DataFrame(rows)
    report.table(pace[pace["time_class"].isin(["bullet", "blitz"])])

    rows = []
    for tc in TIME_CLASSES:
        f = flags[flags["time_class"] == tc]
        lost = (f.loc[f["flagged"] == "player", "ep_level"] - f.loc[f["flagged"] == "player", "actual"]).sum()
        gained = (f.loc[f["flagged"] == "opponent", "ep_level"] - f.loc[f["flagged"] == "opponent", "actual"]).sum()
        rows.append({"time_class": tc, "points you lost by flagging": f"{lost:.1f}", "points opponents lost by flagging": f"{gained:.1f}", "net to you": f"{gained - lost:+.1f}", "per 100 games": f"{100 * (gained - lost) / (games['time_class'] == tc).sum():+.1f}"})
    net = pd.DataFrame(rows)
    report.text("The clock's net balance in your games (level-clock valuation): expected points you lost by flagging against expected points your opponents handed you by flagging.")
    report.table(net)
    print(f"section 5 done at {time.time() - t0:.0f} s")

    # ----- what this means --------------------------------------------------------
    def c(row: pd.Series, side: str, fmt: str) -> str:
        return fmt_ci(row[side], row[f"{side}_lo"], row[f"{side}_hi"], fmt)

    def strip_n(text: str) -> str:
        return text.split(" (n=")[0]

    b_phase = per_phase[per_phase["time_class"] == "bullet"].set_index("phase")
    bl_phase = per_phase[per_phase["time_class"] == "blitz"].set_index("phase")
    cp_curve_b = curves["cp_loss"][curves["cp_loss"]["time_class"] == "bullet"].set_index("time_bucket")
    cp_curve_bl = curves["cp_loss"][curves["cp_loss"]["time_class"] == "blitz"].set_index("time_bucket")
    adj_cp_b = adjusted["cp_loss"][adjusted["cp_loss"]["time_class"] == "bullet"].set_index("time spent")
    adj_cp_bl = adjusted["cp_loss"][adjusted["cp_loss"]["time_class"] == "blitz"].set_index("time spent")
    charge_b = charge_curve[charge_curve["time_class"] == "bullet"].set_index("time_bucket")
    charge_bl = charge_curve[charge_curve["time_class"] == "blitz"].set_index("time_bucket")
    opp_losses_col = "opponents' losses on time"
    diff_col = "your clock - opponent's (s)"
    cnt_b, cnt_bl, cnt_r = pick(counts, time_class="bullet"), pick(counts, time_class="blitz"), pick(counts, time_class="rapid")
    val_b_you, val_b_opp = pick(valuation, time_class="bullet", flagged="you"), pick(valuation, time_class="bullet", flagged="opponents")
    val_bl_you = pick(valuation, time_class="blitz", flagged="you")
    cost_b = pick(per_game_cost, time_class="bullet")
    bs_b_you, bs_b_opp = pick(board_state, time_class="bullet", flagged="you"), pick(board_state, time_class="bullet", flagged="opponents")
    bs_bl_you = pick(board_state, time_class="blitz", flagged="you")
    ratio_b, ratio_bl = pick(alloc_ratio, time_class="bullet"), pick(alloc_ratio, time_class="blitz")
    mis_b_you, mis_b_opp = pick(misalloc, time_class="bullet", side="you"), pick(misalloc, time_class="bullet", side="opponents")
    mis_bl_you, mis_bl_opp = pick(misalloc, time_class="blitz", side="you"), pick(misalloc, time_class="blitz", side="opponents")
    pace_b_10, pace_b_20 = pick(pace, time_class="bullet", side="you", move=10), pick(pace, time_class="bullet", side="you", move=20)
    pace_bl_20 = pick(pace, time_class="blitz", side="you", move=20)
    clock_bl_20 = pick(clock_at, time_class="blitz", move=20)
    chg_b, chg_bl = pick(charge_per_game, time_class="bullet"), pick(charge_per_game, time_class="blitz")
    net_b = pick(net, time_class="bullet")
    fast_b, slow_b = TIME_SPENT_LABELS["bullet"][0], TIME_SPENT_LABELS["bullet"][-1]
    fast_bl, slow_bl = TIME_SPENT_LABELS["blitz"][0], TIME_SPENT_LABELS["blitz"][-1]

    report.heading("What this means for the player")
    report.text(
        f"You lost {cnt_b['your losses on time']} bullet and {cnt_bl['your losses on time']} blitz games on time ({cnt_b['share of your losses']} and {cnt_bl['share of your losses']} of your losses there); opponents lost {cnt_b[opp_losses_col]} bullet and {cnt_bl[opp_losses_col]} blitz games on time to you. "
        f"Valued at the board when the clock ran out, with the clocks levelled, a bullet flag cost you {val_b_you['level clocks: per flagged game']} expected points (theirs cost them {val_b_opp['level clocks: per flagged game']}): {val_b_you['level clocks: total points']} points in bullet and {val_bl_you['level clocks: total points']} in blitz, "
        f"{cost_b['clock cost per game, you']} per bullet game against the {cost_b['<10 s move cost per game, you']} you lose on all moves played under 10 s; the clock is a net {net_b['net to you']} points to you in bullet only because opponents flag more often. "
        f"The flags are a pace problem, not a scramble problem: in blitz the opponent still had 30+ s in {bs_bl_you['winner had 30+ s']} of the games you flagged, and the gap is there early (bullet clock at move 20: {strip_n(pace_b_20['lost on time'])} s in games you went on to flag vs {strip_n(pace_b_20['other'])} s in the others; "
        f"blitz: {strip_n(pace_bl_20['lost on time'])} vs {strip_n(pace_bl_20['other'])} s, and across all blitz games you are {clock_bl_20[diff_col]} s relative to the opponent at move 20). "
        f"The time goes early: {c(b_phase.loc['opening'], 'player', '.2f')} s per bullet opening move against the opponents' {c(b_phase.loc['opening'], 'opponent', '.2f')} s, blitz {c(bl_phase.loc['opening'], 'player', '.2f')} vs {c(bl_phase.loc['opening'], 'opponent', '.2f')} in the opening and {c(bl_phase.loc['middlegame'], 'player', '.2f')} vs {c(bl_phase.loc['middlegame'], 'opponent', '.2f')} in the middlegame, "
        f"which in the model's own price of time costs you {chg_bl['you - opp']} expected points per blitz game more than your opponents pay (bullet {chg_b['you - opp']}). "
        f"In bullet your time also follows the stakes less than theirs ({ratio_b['ratio you']} times as long on a high-stakes position as on a no-stakes one, opponents {ratio_b['ratio opponents']}) and you snap {mis_b_you['high-stakes positions snapped']} of your high-stakes positions (opponents {mis_b_opp['high-stakes positions snapped']}); in blitz it is the other way round ({mis_bl_you['high-stakes positions snapped']} vs {mis_bl_opp['high-stakes positions snapped']}). "
        f"The time-spent data cannot show seconds buying accuracy directly (for both sides the moves that got more time were worse, because time goes to hard positions), but your fast moves are cleaner than your opponents' ({c(cp_curve_b.loc[fast_b], 'player', '.1f')} vs {c(cp_curve_b.loc[fast_b], 'opponent', '.1f')} cp on {fast_b} bullet moves) "
        f"while your longest thinks end worse than theirs ({c(cp_curve_b.loc[slow_b], 'player', '.1f')} vs {c(cp_curve_b.loc[slow_b], 'opponent', '.1f')} cp at {slow_b} in bullet, {c(cp_curve_bl.loc[slow_bl], 'player', '.1f')} vs {c(cp_curve_bl.loc[slow_bl], 'opponent', '.1f')} at {slow_bl} in blitz; adjusted gradient steeper by {c(adj_cp_b.loc[slow_b], 'diff', '.1f')} and {c(adj_cp_bl.loc[slow_bl], 'diff', '.1f')} cp). "
        "Directionally: play the opening and the positions where nothing is at stake at your snap pace, cap the long thinks (they are your least productive seconds), and keep the time for the high-stakes moments and for mating quickly in won endings; "
        "in bullet the flag itself costs you nearly as much per game as every under-10-second move put together."
    )

    report.heading("Caveats")
    report.text(
        "- Time spent is confounded with difficulty: harder positions get more time, so raw error rises with seconds spent, and even within a stakes band and with the controls of 2b the slower moves are the harder ones. "
        "None of the time-spent coefficients is the causal value of a second; the you-vs-opponents comparisons within the same cells, and the clock-imposed comparison of 3a, are the robust parts.\n"
        "- `ep_loss` charges a move for the clock it consumes (the position after the move is valued with the clock after the move), so in expected points a long think can \"lose\" points even when it finds the best move; `cp_loss` is clock-free. "
        "Section 2c prices that charge with the refitted calibrated model; it is the model's price of time, learnt from these games, so it inherits the model's limits (gradient boosting, not monotone in the clocks; small negative charges occur). "
        "It is not an additive component of `ep_loss`: the value of a position before a move already assumes typical time use, so the unfloored change in expected points averages about zero per move and `ep_loss` is its positive part; subtracting the charge from `ep_loss` would mechanically favour long thinks, which is why no such \"board-only ep_loss\" is reported.\n"
        "- \"Stakes\" (`ep_criticality`) is the gap between the engine's best and second-best move. \"None\" means those two are equally good, not that every move is safe; a position with two good moves and twenty blunders counts as no-stakes. It is an engine-side measure of where thinking could pay, not what the player could see.\n"
        "- Long-think and snap thresholds are percentiles of the class (both sides pooled), so \"misallocation\" rates are relative to how the field in these games plays, not to an absolute standard. \"Optimal\" allocation is directional, not a policy.\n"
        "- The level-clock valuation sets the flagged side's clock equal to the winner's; in a bullet scramble that is often a few seconds for both, so it values the position as a fair scramble, not as a position with ample time. "
        "The calibrated model is under-confident at the extremes (Q1), so winning positions are valued below their true worth and the totals are, if anything, low; the Stockfish board-only valuation brackets them from above.\n"
        "- The winner's remaining time is read after their last move. Lag is not observable: some flags with a few tenths of a second left are lag, not thinking.\n"
        f"- Rapid has {cnt_r['your losses on time']} losses on time by you and {cnt_r[opp_losses_col]} by opponents; its clock-cost rows are hidden or wide and should not be read. Rapid also has too few low-clock moves for section 3a.\n"
        "- The clock-at-move-N comparisons condition on the game reaching that move and on how it ended; they describe the games that flagged, they do not estimate what spending less time would have done.\n"
        "- Abandoned games (a side disconnected) are not counted as losses on time; the 'other' category of section 5 includes them. The plan's count of 935 timeouts also includes daily and variant games, which are outside this analysis."
    )

    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
