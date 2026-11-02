"""Clock machinery shared by the time-pressure (Q3) and clock-allocation (Q4) analyses.

- clock buckets in plain seconds left and in *clock buffer* (seconds beyond
  one increment, `clock_buffer_seconds` from `toolkit.load_data`), plus the
  clock as a share of the base time; `add_clock_buckets` adds them all;
- `bucket_curve`: you vs opponents for a column in every bucket, per
  time-control group, with move counts and a `thin` flag;
- `group_frames`: each reported time-control group, plus a pooled frame per
  time class for results whose per-control cells are mostly thin;
- `stakes_bucket` and `susceptibility_regression`: the game-clustered OLS
  of move quality on clock bucket x side with position controls;
- `rating_equivalent`: where a mean sits on the opponents' rating curve.

Plain seconds is what a player sees; the buffer is what matters with an
increment (at 3 s in 1+1 you still get a second back every move, at 3 s in
1+0 you do not). Both are always reported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from .toolkit import cluster_ols, compare_sides, dummies, opponent_rating_pre

# --- buckets ----------------------------------------------------------------------

# Seconds left before the move (or seconds of buffer). Under 5 s is a pure scramble in every control; 10 s is
# the classic "low clock" line; 30 s still leaves a few seconds a move for a bullet ending; 60/120/300 s split
# the comfortable range so blitz and rapid have their own low end. The lowest edge is -inf because the buffer
# goes slightly negative (a move started with less than one increment on the clock).
CLOCK_SECONDS_EDGES = [-np.inf, 5, 10, 20, 30, 60, 120, 300, np.inf]
CLOCK_SECONDS_LABELS = ["<5 s", "5-10 s", "10-20 s", "20-30 s", "30-60 s", "1-2 min", "2-5 min", "5+ min"]
# Coarse version for regressions: "30+ s" is the reference level (comfortable in bullet, normal in blitz).
CLOCK_COARSE_EDGES = [-np.inf, 5, 10, 30, np.inf]
CLOCK_COARSE_LABELS = ["<5 s", "5-10 s", "10-30 s", "30+ s"]
CLOCK_REFERENCE = "30+ s"
# Where the reference level starts, in seconds (or seconds of buffer): "comfortable" in the rating-equivalent targets.
COMFORT_SECONDS = float(CLOCK_COARSE_EDGES[-2])
# Clock left as a share of the base time (above 1 with increments): comparable across controls.
CLOCK_FRACTION_EDGES = [-np.inf, 0.05, 0.10, 0.20, 0.35, 0.50, 0.75, 1.0, np.inf]
CLOCK_FRACTION_LABELS = ["<5%", "5-10%", "10-20%", "20-35%", "35-50%", "50-75%", "75-100%", ">100%"]
# The headline "low clock": under 10 s before the move (in seconds left, or in buffer).
LOW_CLOCK_SECONDS = 10.0
# The two ways of reading the clock, with the mover's and the opponent's column.
CLOCK_MEASURES = {
    "seconds left": ("clock_before_seconds", "opponent_clock_seconds"),
    "buffer beyond increment": ("clock_buffer_seconds", "opponent_clock_buffer_seconds"),
}
# Bucket columns that `add_clock_buckets` creates for each measure: (fine bucket, coarse bucket, opponent coarse).
MEASURE_COLUMNS = {
    "seconds left": ("clock_bucket", "clock_coarse", "opponent_clock_coarse"),
    "buffer beyond increment": ("buffer_bucket", "buffer_coarse", "opponent_buffer_coarse"),
}

# A you-vs-opponents cell needs this many moves on each side before its mean is drawn or read: with fewer, a
# single blunder (ep_loss ~0.3-0.5) moves the mean by more than the differences being looked for.
MIN_CELL_MOVES = 30
# Position controls for the regressions. Ply bands of 20 plies (10 moves) follow the game's arc; piece counts
# split king-and-pawn / minor-piece / middlegame material (non-pawn, non-king pieces, both sides).
PLY_EDGES = [0, 20, 40, 60, 80, np.inf]
PLY_LABELS = ["<20", "20-40", "40-60", "60-80", "80+"]
PIECES_EDGES = [-1, 2, 5, 9, 99]
PIECES_LABELS = ["0-2", "3-5", "6-9", "10+"]
# Stakes in the outcome's own currency: the gap between the best and second-best move. Expected points:
# 0.01/0.03/0.10 separate trivial, small, real and decisive choices; centipawns: 10/30/100/300 are the usual
# "equal / slight / clear / decisive" steps. "one move" (only one legal move) is its own level.
EP_STAKES_EDGES = [-2, -0.5, 1e-9, 0.01, 0.03, 0.10, np.inf]
EP_STAKES_LABELS = ["one move", "0", "<0.01", "<0.03", "<0.10", "0.10+"]
CP_STAKES_EDGES = [-2, -0.5, 1e-9, 10, 30, 100, 300, np.inf]
CP_STAKES_LABELS = ["one move", "0", "<10", "<30", "<100", "<300", "300+"]


def cut_bucket(values: pd.Series, edges: Sequence[float], labels: Sequence[str]) -> pd.Series:
    """Ordered categorical bucket of `values`; left-closed bins ([a, b)), lowest edge included."""
    return pd.cut(values, bins=list(edges), labels=list(labels), right=False, include_lowest=True)


def add_clock_buckets(moves: pd.DataFrame) -> pd.DataFrame:
    """Copy of `moves` with the clock buckets of both measures, the share-of-base bucket and helper flags.

    Adds `clock_bucket` / `buffer_bucket` (fine), `clock_coarse` / `buffer_coarse` and the opponent's coarse
    buckets, `fraction_bucket`, `low_clock` / `low_buffer` (< LOW_CLOCK_SECONDS), `is_endgame`, `phase2`
    and `opponent_rating_pre`.
    """
    frame = moves.copy()
    for measure, (mover, opponent) in CLOCK_MEASURES.items():
        fine, coarse, opp_coarse = MEASURE_COLUMNS[measure]
        frame[fine] = cut_bucket(frame[mover], CLOCK_SECONDS_EDGES, CLOCK_SECONDS_LABELS)
        frame[coarse] = cut_bucket(frame[mover], CLOCK_COARSE_EDGES, CLOCK_COARSE_LABELS)
        frame[opp_coarse] = cut_bucket(frame[opponent], CLOCK_COARSE_EDGES, CLOCK_COARSE_LABELS)
    frame["fraction_bucket"] = cut_bucket(frame["clock_fraction"], CLOCK_FRACTION_EDGES, CLOCK_FRACTION_LABELS)
    frame["low_clock"] = frame["clock_before_seconds"] < LOW_CLOCK_SECONDS
    frame["low_buffer"] = frame["clock_buffer_seconds"] < LOW_CLOCK_SECONDS
    frame["is_endgame"] = frame["phase"] == "endgame"
    frame["phase2"] = np.where(frame["is_endgame"], "endgame", "opening/middlegame")
    frame["opponent_rating_pre"] = opponent_rating_pre(frame)
    return frame


def group_frames(moves: pd.DataFrame, groups: Sequence[str], pooled: bool = False, tc_class: Mapping[str, str] | None = None) -> list[tuple[str, pd.DataFrame]]:
    """(label, moves) for each time-control group, in order; with `pooled`, then "all <class>" for every time
    class that has two or more reported groups (the pooled frame holds only the reported groups).

    `tc_class` maps group -> time class (`toolkit.Data.tc_class`); without it the class is the label's first word.
    """
    out = [(g, moves[moves["tc_group"] == g]) for g in groups]
    if pooled:
        classes: dict[str, list[str]] = {}
        for g in groups:
            classes.setdefault(tc_class[g] if tc_class is not None else g.split(" ", 1)[0], []).append(g)
        for c, members in classes.items():
            if len(members) >= 2:
                out.append((f"all {c}", moves[moves["tc_group"].isin(members)]))
    return out


def bucket_curve(moves: pd.DataFrame, bucket: str, column: str, groups: Sequence[str], n_boot: int = 500, min_moves: int = MIN_CELL_MOVES, pooled: bool = False, tc_class: Mapping[str, str] | None = None) -> pd.DataFrame:
    """You vs opponents for `column` in each level of `bucket`, per time-control group.

    Tidy: tc_group, bucket, player/opponent/diff with _lo/_hi, games, n_player, n_opponent, thin (fewer than
    `min_moves` moves on either side). Levels keep their category order; empty levels are left out.
    """
    levels = list(moves[bucket].cat.categories)
    parts = []
    for label, frame in group_frames(moves, groups, pooled=pooled, tc_class=tc_class):
        frame = frame[frame[bucket].notna() & frame[column].notna()]
        if frame.empty:
            continue
        table = compare_sides(frame, column, by=[bucket], n_boot=n_boot)
        counts = frame.groupby([bucket, "side"], observed=True)[column].count().unstack("side").reindex(columns=["player", "opponent"]).fillna(0).astype(int)
        table[bucket] = table[bucket].astype(str)
        table = table.merge(counts.rename(columns={"player": "n_player", "opponent": "n_opponent"}).rename(index=str).reset_index(), on=bucket, how="left")
        table.insert(0, "tc_group", label)
        order = {str(v): i for i, v in enumerate(levels)}
        parts.append(table.sort_values(bucket, key=lambda s: s.map(order), kind="stable"))
    out = pd.concat(parts, ignore_index=True)
    out["n_player"] = out["n_player"].fillna(0).astype(int)
    out["n_opponent"] = out["n_opponent"].fillna(0).astype(int)
    out["thin"] = (out["n_player"] < min_moves) | (out["n_opponent"] < min_moves)
    cols = ["tc_group", bucket, "player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi", "games", "n_player", "n_opponent", "thin"]
    return out[cols].reset_index(drop=True)


# --- the susceptibility regression ------------------------------------------------------


def stakes_bucket(frame: pd.DataFrame, outcome: str) -> pd.Series:
    """Stakes of the position in the outcome's own currency (see EP_STAKES_EDGES / CP_STAKES_EDGES)."""
    if outcome == "cp_loss":
        return pd.cut(frame["criticality"].fillna(-1), CP_STAKES_EDGES, labels=CP_STAKES_LABELS)
    return pd.cut(frame["ep_criticality"].fillna(-1), EP_STAKES_EDGES, labels=EP_STAKES_LABELS)


def position_controls(frame: pd.DataFrame, outcome: str, stakes: bool = True) -> list[pd.DataFrame]:
    """Dummy blocks for phase, stakes (optional), ply band and piece count."""
    return [
        dummies(frame["phase"], "endgame", "phase"),
        *([dummies(stakes_bucket(frame, outcome), "0", "crit")] if stakes else []),
        dummies(pd.cut(frame["ply"], PLY_EDGES, labels=PLY_LABELS), PLY_LABELS[0], "ply"),
        dummies(pd.cut(frame["pieces"], PIECES_EDGES, labels=PIECES_LABELS), PIECES_LABELS[-1], "pieces"),
    ]


def interaction_ols(frame: pd.DataFrame, outcome: str, factor: pd.Series, labels: Sequence[str], reference: str, controls: Sequence[pd.DataFrame], n_boot: int, seed: int) -> pd.DataFrame:
    """outcome ~ factor x side + controls, game-clustered; per non-reference level of `factor`:
    the opponents' adjusted change vs the reference, yours, and the difference (the side x level interaction)."""
    keep = factor.notna() & frame[outcome].notna()  # moves without a clock reading or a quality value drop out
    frame, factor, controls = frame[keep], factor[keep].astype(str), [c[keep] for c in controls]
    player = frame["is_player_move"].astype(float).rename("player")
    levels = dummies(factor, reference, "level")
    inter = levels.mul(player, axis=0).add_prefix("player*")
    design = pd.concat([player, levels, inter, *controls], axis=1)
    design = design.loc[:, design.std(axis=0).fillna(0) > 0]  # drop constant columns (e.g. phase within a stratum)
    design.insert(0, "intercept", 1.0)
    beta, draws = cluster_ols(design.to_numpy(dtype=float), frame[outcome].to_numpy(dtype=float), frame["game_uuid"].to_numpy(), n_boot=n_boot, seed=seed)
    names = list(design.columns)
    rows = []
    for label in labels:
        if label == reference:
            continue
        c, pc = f"level={label}", f"player*level={label}"
        n_player = int(((factor == label) & frame["is_player_move"]).sum())
        n_opponent = int(((factor == label) & ~frame["is_player_move"]).sum())
        row = {"level": label}
        if c in names and pc in names:
            i, j = names.index(c), names.index(pc)
            for name, est, d in (("player", beta[i] + beta[j], draws[:, i] + draws[:, j]), ("opponent", beta[i], draws[:, i]), ("diff", beta[j], draws[:, j])):
                lo, hi = np.nanquantile(d, [0.025, 0.975])
                row.update({name: est, f"{name}_lo": lo, f"{name}_hi": hi})
        else:
            for name in ("player", "opponent", "diff"):
                row.update({name: np.nan, f"{name}_lo": np.nan, f"{name}_hi": np.nan})
        row.update({"n_player": n_player, "n_opponent": n_opponent})
        rows.append(row)
    return pd.DataFrame(rows)


def susceptibility_regression(frame: pd.DataFrame, outcome: str, measure: str = "seconds left", stakes: bool = True, n_boot: int = 400, seed: int = 0) -> pd.DataFrame:
    """ep_loss (or cp_loss) ~ clock bucket x side + phase + stakes + ply + pieces + opponent's clock bucket.

    `measure` picks plain seconds or the buffer (for both clocks). In a frame that spans several
    time controls (a pooled class), the control itself is added as a fixed effect. Returns one row per
    non-reference coarse bucket: `opponent` (opponents' adjusted change vs 30+ s), `player` (yours) and
    `diff` (you minus opponents: the susceptibility test), each with a game-clustered CI.
    """
    _, coarse, opp_coarse = MEASURE_COLUMNS[measure]
    controls = [*position_controls(frame, outcome, stakes), dummies(frame[opp_coarse], CLOCK_REFERENCE, "oppclock")]
    if frame["tc_group"].astype(str).nunique() > 1:
        controls.append(dummies(frame["tc_group"].astype(str), None, "tc").iloc[:, 1:])
    out = interaction_ols(frame, outcome, frame[coarse], CLOCK_COARSE_LABELS, CLOCK_REFERENCE, controls, n_boot=n_boot, seed=seed)
    return out.rename(columns={"level": "clock"})


# --- rating equivalent ------------------------------------------------------------------


def rating_equivalent(curve_moves: pd.DataFrame, target_moves: pd.DataFrame, column: str, n_boot: int = 1000, seed: int = 0, rating_column: str = "opponent_rating_pre") -> dict:
    """Where the mean `column` of `target_moves` sits on the opponents' rating curve.

    The curve is a move-weighted linear fit of `column` on the opponents' pre-game rating proxy over
    `curve_moves`. Curve and target mean are re-estimated in each game-resampled replicate (jointly: the
    two frames share games). The equivalent is NaN when the slope's CI covers zero (not identified).
    """
    nan = {"mean": np.nan, "mean_lo": np.nan, "mean_hi": np.nan, "slope_per_100": np.nan, "slope_lo": np.nan, "slope_hi": np.nan, "rating_equiv": np.nan, "rating_lo": np.nan, "rating_hi": np.nan}
    if curve_moves.empty or target_moves.empty:
        return {**nan, "n_curve_moves": len(curve_moves), "n_target_moves": len(target_moves)}
    games = np.unique(np.concatenate([curve_moves["game_uuid"].to_numpy(), target_moves["game_uuid"].to_numpy()]))
    gi = pd.Series(np.arange(len(games)), index=games)
    n_games = len(games)

    def sums(frame: pd.DataFrame, weights: np.ndarray) -> np.ndarray:
        return np.bincount(gi.reindex(frame["game_uuid"]).to_numpy(), weights=weights, minlength=n_games)

    x = curve_moves[rating_column].astype(float).to_numpy()
    y = curve_moves[column].astype(float).to_numpy()
    stats = np.stack([sums(curve_moves, np.ones_like(y)), sums(curve_moves, x), sums(curve_moves, y), sums(curve_moves, x * y), sums(curve_moves, x * x)], axis=1)
    t = target_moves[column].astype(float).to_numpy()
    target = np.stack([sums(target_moves, np.ones_like(t)), sums(target_moves, t)], axis=1)

    def estimate(w: np.ndarray) -> tuple[float, float, float, float]:
        n, sx, sy, sxy, sxx = w @ stats
        with np.errstate(invalid="ignore", divide="ignore"):
            slope = (sxy - sx * sy / n) / (sxx - sx * sx / n)
            intercept = sy / n - slope * sx / n
            tn, ts = w @ target
            mean = ts / tn if tn > 0 else np.nan
            return slope, intercept, mean, (mean - intercept) / slope

    slope, intercept, mean, equiv = estimate(np.ones(n_games))
    rng = np.random.default_rng(seed)
    draws = np.array([estimate(np.bincount(rng.integers(0, n_games, n_games), minlength=n_games).astype(float)) for _ in range(n_boot)])
    s_lo, s_hi = np.nanquantile(draws[:, 0], [0.025, 0.975])
    identified = s_hi < 0 or s_lo > 0
    e_lo, e_hi = np.nanquantile(draws[:, 3], [0.025, 0.975]) if identified else (np.nan, np.nan)
    m_lo, m_hi = np.nanquantile(draws[:, 2], [0.025, 0.975])
    return {
        "mean": mean, "mean_lo": m_lo, "mean_hi": m_hi,
        "slope_per_100": slope * 100, "slope_lo": s_lo * 100, "slope_hi": s_hi * 100,
        "rating_equiv": equiv if identified else np.nan, "rating_lo": e_lo, "rating_hi": e_hi,
        "n_curve_moves": int(len(curve_moves)), "n_target_moves": int(len(target_moves)),
    }  # fmt: skip
