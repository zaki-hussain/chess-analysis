"""Question 3: time pressure. How the player's move quality degrades on a low
clock, compared with the opponents in the same games.

Writes reports/03_time_pressure.md and figures under reports/figures/03_time_pressure/.

The clock-bucket definitions and the binned-curve helpers at module level are
reused by analysis/04_clock_allocation.py:

    import importlib, sys
    sys.path.insert(0, "analysis")
    tp = importlib.import_module("03_time_pressure")
    moves = tp.add_clock_buckets(data.moves)
    curve = tp.bucket_curve(moves, "clock_bucket", "ep_loss", by=["time_class"])
"""

from __future__ import annotations

import time
from collections.abc import Sequence

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

SLUG = "03_time_pressure"
SEED = 3

# --- clock buckets (shared with Q4) --------------------------------------------

# Seconds left on the mover's clock before the move.
CLOCK_SECONDS_EDGES = [0, 5, 10, 20, 30, 60, 120, 300, np.inf]
CLOCK_SECONDS_LABELS = ["<5 s", "5-10 s", "10-20 s", "20-30 s", "30-60 s", "1-2 min", "2-5 min", "5+ min"]
# Coarse version used by the regression: "30+ s" is the reference level.
CLOCK_COARSE_EDGES = [0, 5, 10, 30, np.inf]
CLOCK_COARSE_LABELS = ["<5 s", "5-10 s", "10-30 s", "30+ s"]
# Clock left as a fraction of the base time (can exceed 1 with increments).
CLOCK_FRACTION_EDGES = [0, 0.05, 0.10, 0.20, 0.35, 0.50, 0.75, 1.0, np.inf]
CLOCK_FRACTION_LABELS = ["<5%", "5-10%", "10-20%", "20-35%", "35-50%", "50-75%", "75-100%", ">100%"]
# Mover's clock minus the opponent's clock, in seconds.
CLOCK_DIFF_EDGES = [-np.inf, -30, -10, -3, 3, 10, 30, np.inf]
CLOCK_DIFF_LABELS = ["behind 30+ s", "behind 10-30 s", "behind 3-10 s", "level (±3 s)", "ahead 3-10 s", "ahead 10-30 s", "ahead 30+ s"]
# "Low clock" headline threshold.
LOW_CLOCK_SECONDS = 10.0

BUCKETS = {
    "clock_bucket": ("clock_before_seconds", CLOCK_SECONDS_EDGES, CLOCK_SECONDS_LABELS),
    "clock_bucket_coarse": ("clock_before_seconds", CLOCK_COARSE_EDGES, CLOCK_COARSE_LABELS),
    "clock_fraction_bucket": ("clock_fraction", CLOCK_FRACTION_EDGES, CLOCK_FRACTION_LABELS),
    "clock_diff_bucket": ("clock_diff_seconds", CLOCK_DIFF_EDGES, CLOCK_DIFF_LABELS),
    "opponent_clock_bucket_coarse": ("opponent_clock_seconds", CLOCK_COARSE_EDGES, CLOCK_COARSE_LABELS),
}


def cut_bucket(values: pd.Series, edges: Sequence[float], labels: Sequence[str]) -> pd.Series:
    """Ordered categorical bucket of `values`; left-closed bins, lowest edge included."""
    return pd.cut(values, bins=list(edges), labels=list(labels), right=False, include_lowest=True)


def add_clock_buckets(moves: pd.DataFrame) -> pd.DataFrame:
    """Copy of `moves` with the clock bucket columns in BUCKETS plus `low_clock` (< LOW_CLOCK_SECONDS)."""
    frame = moves.copy()
    for name, (source, edges, labels) in BUCKETS.items():
        frame[name] = cut_bucket(frame[source], edges, labels)
    frame["low_clock"] = frame["clock_before_seconds"] < LOW_CLOCK_SECONDS
    frame["is_endgame"] = frame["phase"] == "endgame"
    frame["opponent_rating_pre"] = opponent_rating_pre(frame)
    return frame


def opponent_rating_pre(frame: pd.DataFrame) -> pd.Series:
    """Proxy for the opponent's rating *before* the game (the data only hold post-game ratings).

    In a rated game the opponent's rating change is approximately minus the
    player's (`my_rating_change`), so `opponent_rating_after + my_rating_change`
    undoes the result's effect on the opponent's rating; in an unrated game the
    rating does not move, so the post-game rating is the pre-game rating. NaN
    where the player's change is unknown (first rated game in a pool).
    """
    change = pd.to_numeric(frame["my_rating_change"], errors="coerce").astype(float)
    change = np.where(frame["rated"].astype(bool), change, 0.0)
    return frame["opponent_rating_after"].astype(float) + change


def ratio_bootstrap(numerator: pd.Series, denominator: pd.Series, groups: pd.Series, n_boot: int = 1000, seed: int = SEED) -> tuple[float, float, float]:
    """Ratio of two sums with a CI from resampling whole groups (games), via per-group sums."""
    codes, inverse = np.unique(groups.to_numpy(), return_inverse=True)
    num = np.bincount(inverse, weights=numerator.to_numpy(dtype=float), minlength=len(codes))
    den = np.bincount(inverse, weights=denominator.to_numpy(dtype=float), minlength=len(codes))
    rng = np.random.default_rng(seed)
    w = np.stack([np.bincount(rng.integers(0, len(codes), len(codes)), minlength=len(codes)) for _ in range(n_boot)]).astype(float)
    draws = (w @ num) / (w @ den)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(num.sum() / den.sum()), float(lo), float(hi)


def bucket_curve(moves: pd.DataFrame, bucket: str, column: str, by: Sequence[str] = (), n_boot: int = 500) -> pd.DataFrame:
    """Player vs opponents for `column` in each level of the `bucket` column (and each `by` group).

    Wraps `compare_sides`; adds `n_player`/`n_opponent` move counts and keeps
    the bucket order in `bucket_order` so curves can be plotted left to right.
    """
    frame = moves[moves[bucket].notna()]
    table = compare_sides(frame, column, by=[*by, bucket], n_boot=n_boot)
    counts = frame.groupby([*by, bucket, "side"], observed=True)[column].count().unstack("side").reindex(columns=["player", "opponent"]).reset_index()
    table = table.merge(counts.rename(columns={"player": "n_player", "opponent": "n_opponent"}), on=[*by, bucket], how="left")
    order = {label: i for i, label in enumerate(frame[bucket].cat.categories)}
    table["bucket_order"] = table[bucket].map(order).astype(int)
    table[bucket] = table[bucket].astype(str)
    return table.sort_values([*by, "bucket_order"]).reset_index(drop=True)


def plot_curves(table: pd.DataFrame, bucket: str, ylabel: str, title: str, classes: Sequence[str] = TIME_CLASSES, min_moves: int = 30):
    """One panel per time class: player and opponent curves with CI bands."""
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(classes), figsize=(4.2 * len(classes), 3.6), sharey=True)
    for ax, tc in zip(np.atleast_1d(axes), classes):
        part = table[table["time_class"] == tc]
        for side in ("player", "opponent"):
            ok = part[f"n_{side}"] >= min_moves
            x = part.loc[ok, "bucket_order"]
            ax.plot(x, part.loc[ok, side], marker="o", color=SIDE_COLOURS[side], label="you" if side == "player" else "opponents")
            ax.fill_between(x, part.loc[ok, f"{side}_lo"], part.loc[ok, f"{side}_hi"], color=SIDE_COLOURS[side], alpha=0.18)
        ax.set_xticks(part["bucket_order"])
        ax.set_xticklabels(part[bucket], rotation=45, ha="right", fontsize=8)
        ax.set_title(tc)
    np.atleast_1d(axes)[0].set_ylabel(ylabel)
    np.atleast_1d(axes)[0].legend(loc="upper right")
    fig.suptitle(title, fontweight="bold")
    fig.tight_layout()
    return fig


# --- game-clustered bootstrap OLS ----------------------------------------------


def cluster_ols(X: np.ndarray, y: np.ndarray, groups: np.ndarray, n_boot: int = 500, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and bootstrap draws, resampling whole groups (games).

    Uses per-group sufficient statistics (X'X and X'y summed within each
    group), so each bootstrap replicate is a weighted sum followed by one
    small solve: fast for hundreds of thousands of rows.
    """
    codes, inverse = np.unique(groups, return_inverse=True)
    n_groups, k = len(codes), X.shape[1]
    xtx = np.empty((n_groups, k, k))
    for i in range(k):
        for j in range(i, k):
            s = np.bincount(inverse, weights=X[:, i] * X[:, j], minlength=n_groups)
            xtx[:, i, j] = s
            xtx[:, j, i] = s
    xty = np.stack([np.bincount(inverse, weights=X[:, i] * y, minlength=n_groups) for i in range(k)], axis=1)

    def solve(A: np.ndarray, b: np.ndarray) -> np.ndarray:
        # A column absent from a resample (a dummy nobody in the resampled games has) makes A singular:
        # drop it for that replicate and report NaN for its coefficient.
        keep = np.diag(A) > 0
        out = np.full(k, np.nan)
        out[keep] = np.linalg.lstsq(A[np.ix_(keep, keep)], b[keep], rcond=None)[0]
        return out

    beta = solve(xtx.sum(0), xty.sum(0))
    rng = np.random.default_rng(seed)
    draws = np.empty((n_boot, k))
    for b in range(n_boot):
        w = np.bincount(rng.integers(0, n_groups, n_groups), minlength=n_groups).astype(float)
        draws[b] = solve(np.tensordot(w, xtx, axes=1), w @ xty)
    return beta, draws


def dummies(series: pd.Series, drop: str | None, prefix: str) -> pd.DataFrame:
    frame = pd.get_dummies(series.astype(str), prefix=prefix, prefix_sep="=", dtype=float)
    if drop is not None and f"{prefix}={drop}" in frame:
        frame = frame.drop(columns=f"{prefix}={drop}")
    return frame


def stakes_bucket(frame: pd.DataFrame, outcome: str) -> pd.Series:
    """Stakes of the position in the outcome's own currency: the gap between the best and second-best move.

    `ep_criticality` for ep_loss, `criticality` (centipawns) for cp_loss; NaN (one legal move) is its own level.
    """
    if outcome == "ep_loss":
        return pd.cut(frame["ep_criticality"].fillna(-1), [-2, -0.5, 1e-9, 0.01, 0.03, 0.10, np.inf], labels=["one move", "0", "<0.01", "<0.03", "<0.10", "0.10+"])
    return pd.cut(frame["criticality"].fillna(-1), [-2, -0.5, 1e-9, 10, 30, 100, 300, np.inf], labels=["one move", "0", "<10", "<30", "<100", "<300", "300+"])


def susceptibility_regression(moves: pd.DataFrame, outcome: str, n_boot: int = 400, stakes: bool = True) -> pd.DataFrame:
    """ep_loss (or cp_loss) ~ clock bucket x side + controls, for one time class / stratum.

    Returns, per non-reference clock bucket, the opponents' adjusted
    degradation relative to 30+ s, the player's adjusted degradation, and
    their difference (the player x bucket interaction: the susceptibility
    test), each with a game-clustered bootstrap CI. `stakes=False` drops the
    stakes control to show how much of the adjustment it carries.
    """
    frame = moves
    player = frame["is_player_move"].astype(float).rename("player")
    clock = dummies(frame["clock_bucket_coarse"], "30+ s", "clock")
    inter = clock.mul(player, axis=0).add_prefix("player*")
    controls = [
        dummies(frame["phase"], "endgame", "phase"),
        *([dummies(stakes_bucket(frame, outcome), "0", "crit")] if stakes else []),
        dummies(pd.cut(frame["ply"], [0, 20, 40, 60, 80, np.inf], labels=["<20", "20-40", "40-60", "60-80", "80+"]), "<20", "ply"),
        dummies(pd.cut(frame["pieces"], [-1, 2, 5, 9, 99], labels=["0-2", "3-5", "6-9", "10+"]), "10+", "pieces"),
        dummies(frame["opponent_clock_bucket_coarse"], "30+ s", "oppclock"),
    ]
    design = pd.concat([player, clock, inter, *controls], axis=1)
    design = design.loc[:, design.std(axis=0).fillna(0) > 0]  # drop constant columns (e.g. phase within a stratum)
    design.insert(0, "intercept", 1.0)
    beta, draws = cluster_ols(design.to_numpy(dtype=float), frame[outcome].to_numpy(dtype=float), frame["game_uuid"].to_numpy(), n_boot=n_boot)
    names = list(design.columns)
    rows = []
    for label in CLOCK_COARSE_LABELS[:-1]:
        c, pc = f"clock={label}", f"player*clock={label}"
        if c not in names or pc not in names:
            continue
        i, j = names.index(c), names.index(pc)
        opp, opp_d = beta[i], draws[:, i]
        extra, extra_d = beta[j], draws[:, j]
        you, you_d = opp + extra, opp_d + extra_d
        q = lambda d: np.nanquantile(d, [0.025, 0.975])  # noqa: E731
        rows.append(
            {
                "clock": label,
                "opponent": opp, "opponent_lo": q(opp_d)[0], "opponent_hi": q(opp_d)[1],
                "player": you, "player_lo": q(you_d)[0], "player_hi": q(you_d)[1],
                "diff": extra, "diff_lo": q(extra_d)[0], "diff_hi": q(extra_d)[1],
                "n_player": int(((frame["clock_bucket_coarse"] == label) & frame["is_player_move"]).sum()),
                "n_opponent": int(((frame["clock_bucket_coarse"] == label) & ~frame["is_player_move"]).sum()),
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


# --- rating equivalent ---------------------------------------------------------


def rating_equivalent(curve_moves: pd.DataFrame, target_moves: pd.DataFrame, column: str, n_boot: int = 1000, seed: int = SEED, rating_column: str = "opponent_rating_pre") -> dict:
    """Where the player's mean `column` over `target_moves` sits on the opponents' rating curve.

    The curve is a move-weighted linear fit of `column` on the opponents'
    pre-game rating proxy (`rating_column`, see `opponent_rating_pre`) over
    `curve_moves`. Both the curve and the target mean are re-estimated in
    each game-resampled replicate (games are shared between the two frames,
    so the resampling is joint). The equivalent is reported as NaN when the
    slope's CI covers zero (mapping not identified).
    """
    games = np.unique(np.concatenate([curve_moves["game_uuid"].to_numpy(), target_moves["game_uuid"].to_numpy()]))
    gi = {g: i for i, g in enumerate(games)}
    G = len(games)

    def per_game(frame: pd.DataFrame, weights: np.ndarray) -> np.ndarray:
        idx = frame["game_uuid"].map(gi).to_numpy()
        return np.bincount(idx, weights=weights, minlength=G)

    x = curve_moves[rating_column].astype(float).to_numpy()
    y = curve_moves[column].astype(float).to_numpy()
    stats = np.stack([per_game(curve_moves, np.ones_like(y)), per_game(curve_moves, x), per_game(curve_moves, y), per_game(curve_moves, x * y), per_game(curve_moves, x * x)], axis=1)
    t = target_moves[column].astype(float).to_numpy()
    target = np.stack([per_game(target_moves, np.ones_like(t)), per_game(target_moves, t)], axis=1)

    def estimate(w: np.ndarray) -> tuple[float, float, float, float]:
        n, sx, sy, sxy, sxx = w @ stats
        slope = (sxy - sx * sy / n) / (sxx - sx * sx / n)
        intercept = sy / n - slope * sx / n
        tn, ts = w @ target
        mean = ts / tn if tn > 0 else np.nan
        return slope, intercept, mean, (mean - intercept) / slope

    slope, intercept, mean, equiv = estimate(np.ones(G))
    rng = np.random.default_rng(seed)
    draws = np.array([estimate(np.bincount(rng.integers(0, G, G), minlength=G).astype(float)) for _ in range(n_boot)])
    s_lo, s_hi = np.nanquantile(draws[:, 0], [0.025, 0.975])
    identified = s_hi < 0 or s_lo > 0
    e_lo, e_hi = np.nanquantile(draws[:, 3], [0.025, 0.975]) if identified else (np.nan, np.nan)
    return {
        "mean": mean, "mean_lo": np.nanquantile(draws[:, 2], 0.025), "mean_hi": np.nanquantile(draws[:, 2], 0.975),
        "slope_per_100": slope * 100, "slope_lo": s_lo * 100, "slope_hi": s_hi * 100,
        "rating_equiv": equiv if identified else np.nan, "rating_lo": e_lo, "rating_hi": e_hi,
        "n_curve_moves": int(len(curve_moves)), "n_target_moves": int(len(target_moves)),
    }  # fmt: skip


# --- main ----------------------------------------------------------------------


MIN_MOVES = 30  # cells with fewer moves than this on either side are left out of the report tables


def clip_rating(value: float, lo: float = 0, hi: float = 2500) -> str:
    if pd.isna(value):
        return ""
    return f"{min(max(value, lo), hi):.0f}" + ("*" if value < lo or value > hi else "")


def compact(table: pd.DataFrame, bucket: str, fmt: str = ".4f", extra: Sequence[str] = (), min_moves: int = MIN_MOVES) -> pd.DataFrame:
    table = table[(table["n_player"].fillna(0) >= min_moves) & (table["n_opponent"].fillna(0) >= min_moves)]
    out = pd.DataFrame({bucket: table[bucket]})
    for col in extra:
        out[col] = table[col]
    out["you"] = ci_column(table, "player", "player_lo", "player_hi", fmt)
    out["opponents"] = ci_column(table, "opponent", "opponent_lo", "opponent_hi", fmt)
    out["you - opp"] = ci_column(table, "diff", "diff_lo", "diff_hi", fmt)
    out["moves you/opp"] = table["n_player"].fillna(0).astype(int).astype(str) + "/" + table["n_opponent"].fillna(0).astype(int).astype(str)
    return out


def main() -> None:
    t0 = time.time()
    figure_style()
    import matplotlib.pyplot as plt

    data = load_data()
    moves = add_clock_buckets(data.moves)
    moves = moves[moves["time_class"].isin(TIME_CLASSES)]
    games = data.games[data.games["time_class"].isin(TIME_CLASSES)]
    report = Report(SLUG, "Time pressure: how your play degrades on a low clock, vs others")
    report.text(
        f"{len(games):,} standard live games ({', '.join(f'{n:,} {tc}' for tc, n in games['time_class'].value_counts().reindex(TIME_CLASSES).items())}), "
        f"{len(moves):,} analysed moves ({moves['is_player_move'].sum():,} yours, {(~moves['is_player_move']).sum():,} opponents'). "
        "Every estimate is shown as `estimate [lo, hi]`, a 95% interval from a bootstrap that resamples whole games. "
        "`ep_loss` (calibrated expected points lost by a move) is the main quality measure, `cp_loss` the secondary one. "
        f"\"Low clock\" means under {LOW_CLOCK_SECONDS:.0f} s on the mover's clock before the move."
    )

    # 1. exposure --------------------------------------------------------------
    report.heading("1. How often each side is short of time")
    rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        row = {"time_class": tc}
        for thr, name in [(10, "<10 s"), (5, "<5 s")]:
            flag = (part["clock_before_seconds"] < thr).astype(float)
            for side, side_name in [(True, "you"), (False, "opp")]:
                sub = part[part["is_player_move"] == side]
                est, lo, hi = cluster_bootstrap_mean(flag[sub.index], sub["game_uuid"], n_boot=500)
                row[f"moves {name} {side_name}"] = f"{100 * est:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"
        ever = part.groupby(["game_uuid", "side"])["low_clock"].any().unstack("side").reset_index()
        for side, side_name in [("player", "you"), ("opponent", "opp")]:
            est, lo, hi = cluster_bootstrap_mean(ever[side].fillna(False).astype(float), ever["game_uuid"], n_boot=500)
            row[f"games {side_name} <10 s"] = f"{100 * est:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"
        rows.append(row)
    exposure = pd.DataFrame(rows)
    report.table(exposure)
    report.text("Share of each side's moves played with under 10 s (or 5 s) on the clock, and share of games (with at least one analysed move) in which the side ever dropped under 10 s before a move.")

    # 2. curves by seconds -----------------------------------------------------
    report.heading("2. Error vs seconds left on the clock")
    curve_s_ep = bucket_curve(moves, "clock_bucket", "ep_loss", by=["time_class"], n_boot=500)
    curve_s_cp = bucket_curve(moves, "clock_bucket", "cp_loss", by=["time_class"], n_boot=500)
    fig = plot_curves(curve_s_ep, "clock_bucket", "mean ep_loss per move", "Expected points lost per move vs clock left (cells with 30+ moves)")
    report.figure(fig, "ep_loss_vs_seconds", "Mean ep_loss per move by seconds left before the move; bands are game-clustered 95% CIs. You in blue, opponents in orange.")
    plt.close(fig)
    report.text(f"`ep_loss` per move (tables leave out cells with fewer than {MIN_MOVES} moves on either side):")
    report.table(compact(curve_s_ep, "clock_bucket", extra=["time_class"]))
    fig = plot_curves(curve_s_cp, "clock_bucket", "mean cp_loss per move", "Centipawns lost per move vs clock left")
    report.figure(fig, "cp_loss_vs_seconds", "Mean cp_loss per move by seconds left.")
    plt.close(fig)
    report.text("`cp_loss` per move (centipawns), same cells:")
    report.table(compact(curve_s_cp, "clock_bucket", fmt=".1f", extra=["time_class"]))
    report.text(
        "Raw curves. Note the confounding: moves made on a low clock are almost all late endgame moves in sharper positions "
        "(see section 5), so part of the rise is the position, not the clock. Section 6 adjusts for that."
    )

    # 3. curves by fraction of base ----------------------------------------------
    report.heading("3. Error vs clock left as a fraction of the base time")
    curve_f_ep = bucket_curve(moves, "clock_fraction_bucket", "ep_loss", by=["time_class"], n_boot=500)
    fig = plot_curves(curve_f_ep, "clock_fraction_bucket", "mean ep_loss per move", "Expected points lost per move vs fraction of base time left")
    report.figure(fig, "ep_loss_vs_fraction", "Mean ep_loss per move by clock left as a fraction of the base time (10% of base is 6 s in 1+0 bullet, 18 s in 3+2 blitz, 60 s in 10+0 rapid).")
    plt.close(fig)
    report.table(compact(curve_f_ep, "clock_fraction_bucket", extra=["time_class"]))

    # 4. relative to the opponent -------------------------------------------------
    report.heading("4. Error vs clock relative to the opponent")
    curve_d_ep = bucket_curve(moves, "clock_diff_bucket", "ep_loss", by=["time_class"], n_boot=500)
    fig = plot_curves(curve_d_ep, "clock_diff_bucket", "mean ep_loss per move", "Expected points lost per move vs clock lead over the opponent")
    report.figure(fig, "ep_loss_vs_clock_diff", "Mean ep_loss per move by the mover's clock minus the opponent's clock.")
    plt.close(fig)
    report.table(compact(curve_d_ep, "clock_diff_bucket", extra=["time_class"]))

    # 5. endgames vs earlier -------------------------------------------------------
    report.heading("5. Low clock is mostly an endgame problem: by phase")
    comp = moves.groupby(["time_class", "clock_bucket_coarse"], observed=True).agg(endgame_share=("is_endgame", "mean"), mean_ply=("ply", "mean"), mean_ep_criticality=("ep_criticality", "mean")).reset_index()
    comp["endgame_share"] = (100 * comp["endgame_share"]).round(0).astype(int).astype(str) + "%"
    comp["mean_ply"] = comp["mean_ply"].map(lambda v: f"{v:.1f}")
    report.text("What low-clock moves look like (both sides): share of endgame moves, mean ply and mean stakes (`ep_criticality`, the expected-points gap between the best and second-best move).")
    report.table(comp.rename(columns={"clock_bucket_coarse": "clock"}), floatfmt=".3f")
    moves["phase2"] = np.where(moves["is_endgame"], "endgame", "opening/middlegame")
    curve_phase = bucket_curve(moves, "clock_bucket_coarse", "ep_loss", by=["time_class", "phase2"], n_boot=500)
    report.text("`ep_loss` by clock bucket, endgame vs earlier phases:")
    report.table(compact(curve_phase, "clock_bucket_coarse", extra=["time_class", "phase2"]))

    # 6. regression ---------------------------------------------------------------
    report.heading("6. Susceptibility: do you degrade more than your opponents? (adjusted)")
    report.text(
        "Linear model per time class: `ep_loss ~ clock bucket × side + phase + stakes + ply bucket + "
        "material (pieces bucket) + opponent's clock bucket`, moves of both sides, CIs from a bootstrap over games. Stakes are the "
        "gap between the best and second-best move in the outcome's own currency (`ep_criticality` buckets for ep_loss, `criticality` "
        "in centipawns for cp_loss). "
        "Reference bucket is 30+ s. `opponents` is the opponents' extra ep_loss per move in that bucket relative to 30+ s after the controls; "
        "`you` the same for you; `you - opp` is the side × clock interaction, i.e. the susceptibility test "
        "(positive: you degrade more than opponents do under the same clock). Rows are also fitted within endgame and non-endgame moves only."
    )
    reg_rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for stratum, sub in [("all", part), ("endgame", part[part["is_endgame"]]), ("opening/middlegame", part[~part["is_endgame"]])]:
            for outcome in ["ep_loss", "cp_loss"]:
                table = susceptibility_regression(sub, outcome, n_boot=400)
                table.insert(0, "outcome", outcome)
                table.insert(0, "stratum", stratum)
                table.insert(0, "time_class", tc)
                reg_rows.append(table)
    reg = pd.concat(reg_rows, ignore_index=True)
    for outcome, fmt in [("ep_loss", ".4f"), ("cp_loss", ".1f")]:
        report.heading(f"Adjusted degradation in {outcome} relative to 30+ s", level=3)
        sel = reg[reg["outcome"] == outcome]
        report.table(compact(sel, "clock", fmt=fmt, extra=["time_class", "stratum"]))
    # the role of the stakes control
    nostakes = []
    for tc in ["bullet", "blitz"]:
        part = moves[moves["time_class"] == tc]
        for outcome in ["ep_loss", "cp_loss"]:
            table = susceptibility_regression(part, outcome, n_boot=400, stakes=False)
            table.insert(0, "outcome", outcome)
            table.insert(0, "time_class", tc)
            nostakes.append(table)
    nostakes = pd.concat(nostakes, ignore_index=True)
    report.heading("The same model without the stakes control", level=3)
    report.text(
        "Most of the raw rise in `ep_loss` on a low clock is carried by the stakes of the positions reached, not by the clock: "
        "dropping the stakes control (keeping phase, ply, material and the opponent's clock) brings the low-clock coefficients back up. "
        "Bullet and blitz, all phases."
    )
    for outcome, fmt in [("ep_loss", ".4f"), ("cp_loss", ".1f")]:
        report.text(f"`{outcome}`, without the stakes control:")
        report.table(compact(nostakes[nostakes["outcome"] == outcome], "clock", fmt=fmt, extra=["time_class"]))
    # susceptibility figure: per bucket, all strata, both outcomes
    fig, axes = plt.subplots(2, 3, figsize=(12.6, 6.6), sharey="row")
    for r, (outcome, ylabel) in enumerate([("ep_loss", "extra ep_loss per move vs 30+ s"), ("cp_loss", "extra cp_loss per move vs 30+ s")]):
        sel = reg[(reg["outcome"] == outcome) & (reg["stratum"] == "all")]
        for ax, tc in zip(axes[r], TIME_CLASSES):
            p = sel[(sel["time_class"] == tc) & (sel["n_player"] >= MIN_MOVES) & (sel["n_opponent"] >= MIN_MOVES)]
            x = np.arange(len(p))
            for k, (col, colour, label) in enumerate([("opponent", SIDE_COLOURS["opponent"], "opponents"), ("player", SIDE_COLOURS["player"], "you")]):
                ax.errorbar(x + (k - 0.5) * 0.18, p[col], yerr=[p[col] - p[f"{col}_lo"], p[f"{col}_hi"] - p[col]], fmt="o", color=colour, capsize=3, label=label)
            ax.axhline(0, color="grey", lw=0.8)
            ax.set_xticks(x)
            ax.set_xticklabels(p["clock"])
            if r == 0:
                ax.set_title(tc)
        axes[r][0].set_ylabel(ylabel)
    axes[0][0].legend(loc="upper right")
    fig.suptitle("Adjusted degradation by clock bucket (all phases; cells with 30+ moves)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "adjusted_degradation", "Adjusted extra ep_loss (top) and cp_loss (bottom) per move relative to 30+ s, after controlling for phase, stakes, ply, material and the opponent's clock; you vs opponents.")
    plt.close(fig)

    # 7. cost per game -----------------------------------------------------------------
    report.heading("7. What low-clock moves cost per game")
    low = moves.copy()
    low["ep_loss_low"] = np.where(low["low_clock"], low["ep_loss"], 0.0)
    per_game_low = low.groupby(["game_uuid", "time_class", "is_player_move"], observed=True)["ep_loss_low"].sum().reset_index()
    cost = compare_sides(per_game_low, "ep_loss_low", by=["time_class"], n_boot=1000)
    cost["n_player"] = cost["games"]
    cost["n_opponent"] = cost["games"]
    cost_table = compact(cost, "time_class", fmt=".4f").drop(columns="moves you/opp")
    cost_table["games"] = cost["games"].to_numpy()
    report.text(
        "Expected points lost per game on moves made with under 10 s (zero for games where the side never got that low), you vs opponents, "
        "all games of the class with at least one analysed move (two bullet/rapid games have none, hence 6,027 games here against 6,029 in the header)."
    )
    report.table(cost_table)
    share_rows = []
    for tc in TIME_CLASSES:
        part = moves[moves["time_class"] == tc]
        for side, name in [(True, "you"), (False, "opponents")]:
            sub = part[part["is_player_move"] == side]
            low_ep = sub["ep_loss"].where(sub["low_clock"], 0.0)
            blunder = (sub["ep_loss"] >= 0.1).astype(float)
            s_ep = ratio_bootstrap(low_ep, sub["ep_loss"], sub["game_uuid"], n_boot=1000)
            s_bl = ratio_bootstrap(blunder.where(sub["low_clock"], 0.0), blunder, sub["game_uuid"], n_boot=1000)
            share_rows.append({"time_class": tc, "side": name, "share of ep_loss under 10 s": f"{100 * s_ep[0]:.1f}% [{100 * s_ep[1]:.1f}, {100 * s_ep[2]:.1f}]", "share of blunders (ep_loss>=0.1) under 10 s": f"{100 * s_bl[0]:.1f}% [{100 * s_bl[1]:.1f}, {100 * s_bl[2]:.1f}]"})
    share = pd.DataFrame(share_rows)
    report.text("Share of each side's total `ep_loss` (and of its blunders) that occurred on moves under 10 s, with game-resampled CIs:")
    report.table(share)

    # 8. rating equivalent -------------------------------------------------------------
    report.heading("8. What rating your low-clock play resembles")
    report.text(
        "The calibrated `ep_loss` is built conditional on the players' ratings, so it is nearly flat across the rating range (its "
        "slope is reported below for completeness) and cannot map play to a rating. The mapping therefore uses `cp_loss` (and, as a "
        "second currency, the share of moves losing 300+ cp, the `blunder` label), which do fall with rating. Curve: opponents' moves in "
        "the class, mean per move vs the opponent's pre-game rating (move-weighted linear fit); the player's low-clock mean is read off it. "
        "The data hold only post-game ratings, so the opponent's pre-game rating is reconstructed as `opponent_rating_after + my_rating_change` "
        "in rated games (the two sides' rating changes are approximately equal and opposite) and as the post-game rating in unrated games "
        "(where ratings do not move); this removes the result's effect on the x variable (the convention is never to use post-game ratings as "
        "predictors), at the cost of a few points of noise where the two players' rating deviations differ. Games whose rating change is unknown "
        "(the first rated game in a pool) or whose opponent is rated under 100 are left out of the curve. Equivalents are shown only where the "
        "slope's CI excludes zero, and are clipped to [0, 2500] (marked with `*`); `ep_loss` rows are marked `not used` whatever their slope, since a "
        "slope of a few thousandths of a point per 100 rating points is calibration residue, not a rating signal. Rows: all moves, and endgame "
        "moves only for cp_loss (nearly all low-clock moves are endgame moves, so the endgame-only row would compare like with like, but opponents' "
        "endgame cp_loss turns out not to vary with rating in bullet and rapid, and in blitz the slope is so shallow that its equivalents span "
        f"hundreds of points). Cells with fewer than {MIN_MOVES} of your moves are left out."
    )
    moves["blunder"] = (moves["label"] == "blunder").astype(float)
    opp = moves[~moves["is_player_move"] & (moves["opponent_rating_pre"] >= 100)]
    mine = moves[moves["is_player_move"]]
    rows = []
    for tc in TIME_CLASSES:
        actual = games.loc[games["time_class"] == tc, "my_rating_before"].astype(float)
        for stratum in ["all", "endgame"]:
            o = opp[opp["time_class"] == tc]
            me = mine[mine["time_class"] == tc]
            if stratum == "endgame":
                o, me = o[o["is_endgame"]], me[me["is_endgame"]]
            for column, fmt in [("ep_loss", ".4f"), ("cp_loss", ".1f"), ("blunder", ".3f")]:
                if stratum == "endgame" and column != "cp_loss":
                    continue
                for target_name, target in [("<10 s", me[me["low_clock"]]), ("30+ s", me[me["clock_before_seconds"] >= 30]), ("all your moves", me)]:
                    if len(target) < MIN_MOVES:
                        continue
                    r = rating_equivalent(o, target, column, n_boot=600)
                    if column == "ep_loss":
                        equiv_text = "not used (flat by construction)"
                        r["rating_equiv"] = r["rating_lo"] = r["rating_hi"] = np.nan
                    elif pd.notna(r["rating_equiv"]):
                        equiv_text = f"{clip_rating(r['rating_equiv'])} [{clip_rating(r['rating_lo'])}, {clip_rating(r['rating_hi'])}]"
                    else:
                        equiv_text = "not identified"
                    rows.append({"time_class": tc, "stratum": stratum, "currency": column, "your moves": target_name, "your mean": f"{r['mean']:{fmt}} [{r['mean_lo']:{fmt}}, {r['mean_hi']:{fmt}}]", "curve slope per 100 pts": f"{r['slope_per_100']:{fmt}} [{r['slope_lo']:{fmt}}, {r['slope_hi']:{fmt}}]", "rating equivalent": equiv_text, "your actual mean rating": f"{actual.mean():.0f}", "moves": r["n_target_moves"], "_equiv": r["rating_equiv"], "_lo": r["rating_lo"], "_hi": r["rating_hi"]})
    equiv = pd.DataFrame(rows)
    for tc in TIME_CLASSES:
        report.text(f"**{tc}**")
        report.table(equiv[equiv["time_class"] == tc].drop(columns=["time_class", "_equiv", "_lo", "_hi"]))
    # figure: opponents' all-moves cp_loss curve with the player's low-clock level marked
    fig, axes = plt.subplots(1, 3, figsize=(12.6, 3.6), sharey=False)
    for ax, tc in zip(axes, TIME_CLASSES):
        o = opp[opp["time_class"] == tc].copy()
        o["band"] = (o["opponent_rating_pre"] // 100 * 100).astype(int)
        band = o.groupby("band").agg(cp=("cp_loss", "mean"), n=("cp_loss", "size"), games=("game_uuid", "nunique")).reset_index()
        band = band[band["games"] >= 20]
        ax.scatter(band["band"] + 50, band["cp"], s=np.clip(band["n"] / 100, 8, 200), color=SIDE_COLOURS["opponent"], alpha=0.7, label="opponents (all moves), by 100-pt band")
        x = o["opponent_rating_pre"].astype(float).to_numpy()
        y = o["cp_loss"].to_numpy()
        b = np.polyfit(x, y, 1)
        xs = np.linspace(x.min(), x.max(), 50)
        ax.plot(xs, np.polyval(b, xs), color=SIDE_COLOURS["opponent"], lw=1)
        me = mine[mine["time_class"] == tc]
        for target_name, ls in [("<10 s", "--"), ("30+ s", ":")]:
            sel = equiv[(equiv["time_class"] == tc) & (equiv["stratum"] == "all") & (equiv["currency"] == "cp_loss") & (equiv["your moves"] == target_name)]
            mask = me["low_clock"] if target_name == "<10 s" else me["clock_before_seconds"] >= 30
            if mask.sum() < MIN_MOVES:
                continue
            level = me.loc[mask, "cp_loss"].mean()
            ax.axhline(level, color=SIDE_COLOURS["player"], ls=ls, label=f"your cp_loss at {target_name}")
            if len(sel) and pd.notna(sel["_equiv"].iloc[0]):
                ax.plot([sel["_equiv"].iloc[0]], [level], marker="v", color=SIDE_COLOURS["player"], ms=8)
        ax.set_title(tc)
        ax.set_xlabel("opponent rating (pre-game proxy)")
    axes[0].set_ylabel("mean cp_loss per move")
    axes[0].legend(fontsize=7, loc="upper right")
    fig.suptitle("Rating curve of opponents' cp_loss, with your low-clock level", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "rating_equivalent", "Opponents' mean cp_loss by pre-game rating band (point size = moves) with the move-weighted linear fit; dashed/dotted lines are your cp_loss under 10 s and at 30+ s, triangles mark the rating equivalents where identified (the band means are noisy; the fit uses every move).")
    plt.close(fig)

    # 9. summary ----------------------------------------------------------------------------
    report.heading("What this means for the player")

    def pick(frame, **kw):
        sel = frame
        for k, v in kw.items():
            sel = sel[sel[k] == v]
        return sel.iloc[0]

    def ci(row, col, fmt):
        return f"{row[col]:{fmt}} [{row[col + '_lo']:{fmt}}, {row[col + '_hi']:{fmt}}]"

    b5 = pick(reg, time_class="bullet", stratum="all", outcome="ep_loss", clock="<5 s")
    b5cp = pick(reg, time_class="bullet", stratum="all", outcome="cp_loss", clock="<5 s")
    b10cp = pick(reg, time_class="bullet", stratum="all", outcome="cp_loss", clock="10-30 s")
    z5 = pick(reg, time_class="blitz", stratum="all", outcome="ep_loss", clock="<5 s")
    z10cp = pick(reg, time_class="blitz", stratum="all", outcome="cp_loss", clock="10-30 s")
    raw_b5 = pick(curve_s_ep, time_class="bullet", clock_bucket="<5 s")
    raw_b30 = pick(curve_s_ep, time_class="bullet", clock_bucket="30-60 s")
    cost_b = pick(cost, time_class="bullet")
    cost_z = pick(cost, time_class="blitz")
    map_lo = pick(equiv, time_class="bullet", stratum="all", currency="cp_loss", **{"your moves": "<10 s"})
    map_hi = pick(equiv, time_class="bullet", stratum="all", currency="cp_loss", **{"your moves": "30+ s"})
    report.text(
        f"Time pressure is a bullet phenomenon in these games: you play {exposure.loc[0, 'moves <10 s you']} of your bullet moves with under 10 s "
        f"(opponents {exposure.loc[0, 'moves <10 s opp']}) and drop under 10 s in {exposure.loc[0, 'games you <10 s']} of bullet games; in blitz it is "
        f"{exposure.loc[1, 'moves <10 s you']} of moves, in rapid almost never. Your raw bullet error rate under 5 s is {ci(raw_b5, 'player', '.4f')} expected "
        f"points per move against {ci(raw_b30, 'player', '.4f')} at 30-60 s, the same rise as your opponents show, and in expected points you are not more "
        f"susceptible than they are: the adjusted you-minus-opponents gap under 5 s is {ci(b5, 'diff', '+.4f')} in bullet and {ci(z5, 'diff', '+.4f')} in blitz. "
        f"Once the stakes of the position are held fixed, a low clock does not make either side lose more *expected points* per move (bullet, <5 s: you "
        f"{ci(b5, 'player', '+.4f')}, opponents {ci(b5, 'opponent', '+.4f')} relative to 30+ s), because the calibration already discounts a position whose owner "
        f"is about to flag; what does rise is the positional damage in centipawns, and there you degrade more than your opponents in bullet under 5 s "
        f"(you {ci(b5cp, 'player', '+.1f')} cp vs opponents {ci(b5cp, 'opponent', '+.1f')} cp, gap {ci(b5cp, 'diff', '+.1f')}), with a weaker hint of the same "
        f"in blitz with 10-30 s left (gap {ci(z10cp, 'diff', '+.1f')} cp) but not in bullet at 10-30 s ({ci(b10cp, 'diff', '+.1f')}). Moves under 10 s cost you {ci(cost_b, 'player', '.3f')} expected points per bullet "
        f"game ({ci(cost_b, 'opponent', '.3f')} for opponents) and {ci(cost_z, 'player', '.3f')} per blitz game ({ci(cost_z, 'opponent', '.3f')}), about one "
        f"seventh of all the expected points you give away in bullet. On the opponents' all-moves cp_loss curve your bullet play under 10 s resembles a "
        f"{map_lo['rating equivalent']} player and your play at 30+ s a {map_hi['rating equivalent']} player (actual mean rating {map_hi['your actual mean rating']}); "
        f"indicative only. The practical reading: your low-clock move quality is ordinary for the field, so the lever is exposure rather than composure: get "
        f"under 10 s less often, and convert the centipawn sloppiness in bullet scrambles (where you shed more material than your opponents do) into fewer "
        f"flagged or thrown endgames. Q4 (clock allocation) takes up where the seconds go."
    )

    report.heading("Caveats")
    report.text(
        "- Time pressure is confounded with position type: moves under 10 s are ~90% endgame moves, late in the game and at higher stakes. The raw curves (sections 2-4) carry that; section 6 adjusts for phase, stakes, ply, material and the opponent's clock, but the adjustment is linear and cannot remove residual selection (who gets low on the clock, and in which games, is not random).\n"
        "- `ep_loss` is calibrated with both clocks as features, so part of a low-clock move's `ep_loss` can reflect the clock situation after the move, not only the quality of the move, and a blunder made with 2 s left costs fewer expected points than the same blunder with 30 s because the game was already likely to be decided by the clock. The expected points lost *by getting into* time pressure are attributed to the earlier moves that spent the time (Q4), not to the low-clock moves. `cp_loss` is clock-free and is reported alongside.\n"
        "- The stakes control (`ep_criticality` / `criticality` buckets) is strong: it absorbs the sharper positions that time pressure produces. Section 6 shows the model with and without it; the truth about \"the clock's own effect\" lies between the two.\n"
        "- Rapid has very few moves under 10 s (tens, not thousands); its low-clock cells and regression rows are wide and should not be over-read. The fraction-of-base curves (section 3) are the better view for rapid.\n"
        "- The rating mapping uses a reconstructed pre-game rating for opponents (`opponent_rating_after + my_rating_change` in rated games; exact only if both players' rating changes were equal and opposite, which Glicko guarantees only when their rating deviations match, so provisional-era games add noise), a linear fit over a 200-1300 range where the `cp_loss`-rating slope is shallow (around -3 to -4 cp per 100 points), and compares the player's low-clock moves (nearly all endgame moves) to opponents' moves at all clocks and phases; restricted to endgame moves the opponents' curve has no slope in bullet and rapid and a barely identified one in blitz, so the like-for-like mapping is not usable. Equivalents are indicative; their intervals are wide by construction (a small slope in the denominator). The calibrated `ep_loss` cannot be mapped to a rating.\n"
        "- Ratings under ~400 in early blitz games are provisional-era ratings and add noise to the curve.\n"
        "- Everything is per move; a side that plays more moves under time pressure has more chances to lose points even at the same per-move rate (section 7 gives the per-game totals)."
    )

    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
