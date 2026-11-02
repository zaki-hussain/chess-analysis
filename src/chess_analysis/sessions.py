"""Session machinery shared by the session (Q5) and time-of-day (Q6) analyses.

A *session* is a run of games with no pause longer than the configured gap
(`config.toml: session_gap_minutes`) between one game's end and the next
game's start. Sessions are recomputed here over the standard live games
(`load_data().games` with at least one move), so daily and variant games
never split or join a sitting.

What lives here:
- `session_features`: per-game session variables (session id, game number,
  first/last flags, gaps, the results earlier in the session, streaks);
- `build_games`: the per-game analysis frame (toolkit games + Q1's
  per-game decomposition + session features + per-game metrics), including
  each session's *main control* (the time control with most games in it);
- `METRICS` and `metric` / `metric_diff` / `paired_diff`: per-game metrics
  as ratios of sums with CIs from resampling whole clusters (games or
  sessions) via per-cluster sums (`boot_sums`), so a cell of thousands of
  games bootstraps in milliseconds;
- `cell_stats` / `score_columns`: the tidy columns every table uses (score,
  model expectation, score minus expectation with CI and Elo-equivalents,
  move quality for both sides);
- `subset` / `by_level` / `thin_cell`: the cells of a table (a time-control
  group or pool x a level of some column) with counts and a `thin` flag.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from .toolkit import OUTCOME_SCORE, elo_expected_score, excess_to_elo, opponent_rating_pre

N_BOOT = 500  # resamples per CI: the CI ends of a score move by under 0.005 between seeds at this size
SEED = 5
ALL = "all games"  # pooled row/panel over every reported control (thin controls excluded)
# A cell resting on fewer games than this has a score CI wider than about +-0.18 (a fifth of the whole
# win-to-loss range): it is computed and kept, but flagged thin and not drawn.
MIN_CELL_GAMES = 30
# Every CI resamples whole sessions: with fewer sessions than this the bootstrap has too few clusters to give
# a trustworthy interval, so a cell is also thin under this many sessions.
MIN_SESSIONS = 30


# --- session features -----------------------------------------------------------------------------


def session_features(games: pd.DataFrame, gap_minutes: float) -> pd.DataFrame:
    """Per-game session variables recomputed on `games`, indexed like `games`.

    A new session starts when the pause between a game's start and the
    previous game's end exceeds `gap_minutes`. Columns:
    `session_id`; `game_in_session` (1-based, counting every game of the
    session whatever its control); `session_games`; `minutes_into_session`
    (start minus the session's first start); `session_minutes` (first start to
    last end); `is_first_in_session`, `is_last_in_session`;
    `gap_before_minutes` (pause since the previous game of the same session,
    NaN for a first game); `break_before_minutes` (pause since the previous
    game of any session, NaN for the very first game, floored at 0);
    `prev_outcome` (previous game in the session: win/draw/loss, None for a
    first game); `prev_any_outcome` (previous game overall); `prev2_outcomes`
    (e.g. "LW", older first, None before game 3); `loss_streak` / `win_streak`
    (consecutive losses / wins immediately before the game, within the
    session).
    """
    ordered = games.sort_values(["start_datetime", "end_datetime", "uuid"], kind="stable")
    gap = (ordered["start_datetime"] - ordered["end_datetime"].shift(1)).dt.total_seconds() / 60
    session = (gap.isna() | (gap > gap_minutes)).cumsum()
    out = pd.DataFrame(index=ordered.index)
    out["session_id"] = session.to_numpy()
    grp = out.groupby("session_id")
    out["game_in_session"] = grp.cumcount() + 1
    out["session_games"] = grp["session_id"].transform("size")
    first_start = ordered["start_datetime"].groupby(session).transform("min")
    last_end = ordered["end_datetime"].groupby(session).transform("max")
    out["minutes_into_session"] = (ordered["start_datetime"] - first_start).dt.total_seconds() / 60
    out["session_minutes"] = (last_end - first_start).dt.total_seconds() / 60
    out["is_first_in_session"] = out["game_in_session"] == 1
    out["is_last_in_session"] = out["game_in_session"] == out["session_games"]
    out["gap_before_minutes"] = gap.clip(lower=0).where(~out["is_first_in_session"])
    out["break_before_minutes"] = gap.clip(lower=0)

    outcome = ordered["outcome"]
    out["prev_outcome"] = outcome.groupby(session).shift(1)
    out["prev_any_outcome"] = outcome.shift(1)
    letters = outcome.map({"win": "W", "draw": "D", "loss": "L"})
    prev1, prev2 = letters.groupby(session).shift(1), letters.groupby(session).shift(2)
    out["prev2_outcomes"] = (prev2 + prev1).where(prev2.notna() & prev1.notna())
    out["loss_streak"] = streak_before(outcome == "loss", session)
    out["win_streak"] = streak_before(outcome == "win", session)
    return out.loc[games.index]


def streak_before(flag: pd.Series, session: pd.Series) -> pd.Series:
    """Consecutive True values immediately before each row, within its session (0 if the previous row is False)."""
    flag = flag.astype(bool)
    key = session.astype(str) + ":" + flag.astype(str)
    run = (key != key.shift(1)).cumsum()  # a new run whenever the flag or the session changes
    run_len = flag.groupby(run).cumcount() + 1
    streak = run_len.where(flag, 0)
    return streak.groupby(session).shift(1).fillna(0).astype(int)


def session_main_control(games: pd.DataFrame) -> pd.Series:
    """Each session's main control: the `tc_group` with most games in it; a tie goes to the control played first. Indexed by session_id."""
    frame = games.assign(_tc=games["tc_group"].astype(str))
    counts = frame.groupby(["session_id", "_tc"]).agg(n=("uuid", "size"), first=("start_datetime", "min")).reset_index()
    counts = counts.sort_values(["session_id", "n", "first"], ascending=[True, False, True], kind="stable")
    return counts.drop_duplicates("session_id").set_index("session_id")["_tc"].rename("session_main_tc")


# --- the per-game frame ----------------------------------------------------------------------------


def build_games(data, decomposed: pd.DataFrame, gap_minutes: float) -> pd.DataFrame:
    """Per-game analysis frame: `data.games` + Q1's per-game decomposition + session features + metrics.

    Games without moves (no quality metrics) are dropped. Sessions are
    computed over every remaining standard live game, including the thin
    controls in `data.tc_noted`: a 0.5+0 game still takes up part of a
    sitting. Analyses then keep the rows whose own `tc_group` is reported.
    """
    games = data.games.merge(decomposed, on="uuid", how="left")
    games = games[games["n_plies"].fillna(0) > 0].copy()
    games = games.drop(columns=["session", "game_in_session", "session_games", "minutes_into_session"], errors="ignore")
    games = games.join(session_features(games, gap_minutes))
    main = session_main_control(games)
    games["session_main_tc"] = games["session_id"].map(main)
    games["session_controls"] = games.groupby("session_id")["tc_group"].transform(lambda s: s.astype(str).nunique())
    games["opponent_rating_pre"] = opponent_rating_pre(games)
    games["elo_expected"] = elo_expected_score(games["my_rating_before"], games["opponent_rating_pre"])
    games["excess_model"] = games["my_score"] - games["my_ep_start"]
    games["excess_rating"] = games["my_score"] - games["elo_expected"]
    games["is_loss"] = (games["outcome"] == "loss").astype(float)
    games["opp_is_loss"] = (games["outcome"] == "win").astype(float)
    games["lost_on_time"] = ((games["outcome"] == "loss") & (games["my_result"] == "timeout")).astype(float)
    games["opp_lost_on_time"] = ((games["outcome"] == "win") & (games["opponent_result"] == "timeout")).astype(float)
    games["resigned"] = (games["my_result"] == "resigned").astype(float)
    games["is_last"] = games["is_last_in_session"].astype(float)
    games["my_ep_rate"] = games["my_ep_lost"] / games["my_moves"].replace(0, np.nan)
    games["opp_ep_rate"] = games["opp_ep_lost"] / games["opp_moves"].replace(0, np.nan)
    games["ep_rate_gap"] = games["my_ep_rate"] - games["opp_ep_rate"]  # per game: positive = you lost more per move than they did
    spent = data.moves.groupby(["game_uuid", "is_player_move"])["time_spent_seconds"].mean().unstack()
    spent = spent.rename(columns={True: "my_sec_per_move", False: "opp_sec_per_move"})
    games = games.merge(spent, left_on="uuid", right_index=True, how="left")
    games["tc_group"] = games["tc_group"].astype(str)
    return games.reset_index(drop=True)


# --- bootstrap over clusters from per-cluster sums ---------------------------------------------------


def boot_sums(frame: pd.DataFrame, columns: Sequence[str], cluster: str, stat: Callable[[np.ndarray], np.ndarray], n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Any statistic of per-cluster column sums, with a 95% CI from resampling clusters.

    `stat` receives an array of shape (draws, len(columns)) holding the summed
    columns of each resample and returns one value per draw.
    """
    part = frame[[cluster, *columns]].dropna(subset=list(columns))
    if part.empty:
        return float("nan"), float("nan"), float("nan")
    sums = part.groupby(cluster)[list(columns)].sum().to_numpy(dtype=float)
    est = float(stat(sums.sum(axis=0, keepdims=True))[0])
    if len(sums) < 2:
        return est, float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    weights = np.stack([np.bincount(rng.integers(0, len(sums), len(sums)), minlength=len(sums)) for _ in range(n_boot)]).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = stat(weights @ sums)
    if np.isnan(draws).all():
        return est, float("nan"), float("nan")
    lo, hi = np.nanquantile(draws, [0.025, 0.975])
    return est, float(lo), float(hi)


def ratio(s: np.ndarray) -> np.ndarray:
    """Sum of column 0 over sum of column 1 (NaN when the denominator is 0)."""
    return s[:, 0] / np.where(s[:, 1] > 0, s[:, 1], np.nan)


def ratio_diff(s: np.ndarray) -> np.ndarray:
    """(col 0 / col 1) - (col 2 / col 3): the difference of two ratios of sums in the same resample."""
    return ratio(s[:, :2]) - ratio(s[:, 2:])


# Per-game metrics: name -> (label, numerator column, denominator column or None for a mean over games, scale).
# A tuple numerator with denominator "pairdiff" is a difference of two move-weighted rates in the same games.
METRICS: dict[str, tuple[str, object, object, float]] = {
    "score": ("score", "my_score", None, 1.0),
    "expected": ("model expectation", "my_ep_start", None, 1.0),
    "excess": ("score minus model expectation", "excess_model", None, 1.0),
    "excess_rating": ("score minus Elo-formula expectation", "excess_rating", None, 1.0),
    "my_ep": ("your expected points lost per move", "my_ep_lost", "my_moves", 1.0),
    "opp_ep": ("opponents' expected points lost per move", "opp_ep_lost", "opp_moves", 1.0),
    "ep_gap": ("your minus opponents' expected points lost per move", ("my_ep_lost", "my_moves", "opp_ep_lost", "opp_moves"), "pairdiff", 1.0),
    "my_cp": ("your centipawns lost per move", "my_cp_lost", "my_moves", 1.0),
    "opp_cp": ("opponents' centipawns lost per move", "opp_cp_lost", "opp_moves", 1.0),
    "lost_on_time": ("share of games you lost on time", "lost_on_time", None, 1.0),
    "timeout_share": ("share of your losses that were on time", "lost_on_time", "is_loss", 1.0),
    "opp_timeout_share": ("share of opponents' losses that were on time", "opp_lost_on_time", "opp_is_loss", 1.0),
    "resign_share": ("share of your losses you resigned", "resigned", "is_loss", 1.0),
    "stop": ("share of games after which you stopped", "is_last", None, 1.0),
    "my_sec": ("your seconds per move", "my_sec_per_move", None, 1.0),
    "opp_sec": ("opponents' seconds per move", "opp_sec_per_move", None, 1.0),
}


def _metric_spec(name: str) -> tuple[list[str], Callable[[np.ndarray], np.ndarray], float]:
    _, num, den, scale = METRICS[name]
    if den == "pairdiff":
        return list(num), ratio_diff, scale
    return [num, den or "_one"], ratio, scale


def metric(frame: pd.DataFrame, name: str, cluster: str = "session_id", n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """One per-game metric of a set of games with a cluster-bootstrap CI (ratio of sums for per-move rates)."""
    cols, stat, scale = _metric_spec(name)
    est, lo, hi = boot_sums(frame.assign(_one=1.0), cols, cluster, stat, n_boot=n_boot, seed=seed)
    return est * scale, lo * scale, hi * scale


def metric_diff(a: pd.DataFrame, b: pd.DataFrame, name: str, cluster: str = "session_id", n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """metric(a) - metric(b), both computed on the same resampled clusters (a and b may share clusters)."""
    cols, stat, scale = _metric_spec(name)
    m = len(cols)
    both = pd.concat([a.assign(_one=1.0, _a=1.0), b.assign(_one=1.0, _a=0.0)])
    for c in cols:
        both[f"{c}__a"] = both[c] * both["_a"]
        both[f"{c}__b"] = both[c] * (1 - both["_a"])
    all_cols = [f"{c}__a" for c in cols] + [f"{c}__b" for c in cols]
    est, lo, hi = boot_sums(both, all_cols, cluster, lambda s: stat(s[:, :m]) - stat(s[:, m:]), n_boot=n_boot, seed=seed)
    return est * scale, lo * scale, hi * scale


def paired_diff(pairs: pd.DataFrame, value_a: str, value_b: str, cluster: str, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Mean of (a - b) over pairs (one row per pair), bootstrapped over clusters."""
    part = pairs.assign(_d=pairs[value_a] - pairs[value_b], _one=1.0)
    return boot_sums(part, ["_d", "_one"], cluster, ratio, n_boot=n_boot, seed=seed)


# --- tidy table helpers ---------------------------------------------------------------------------------


def with_elo(row: dict, expected: str = "expected", prefix: str = "excess") -> dict:
    """Adds `<prefix>_elo`, `_elo_lo`, `_elo_hi`: the Elo-equivalent of the excess at the given expected score."""
    exp = row.get(expected, np.nan)
    for suffix in ("", "_lo", "_hi"):
        value = row.get(f"{prefix}{suffix}", np.nan)
        row[f"{prefix}_elo{suffix}"] = float(excess_to_elo(exp, value)) if pd.notna(exp) and pd.notna(value) else np.nan
    return row


def cell_stats(part: pd.DataFrame, names: Iterable[str], cluster: str = "session_id", n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """{name, name_lo, name_hi} for each metric, in order.

    `excess` (score minus model expectation) comes with `score` and
    `expected` (point estimates) before it and its Elo-equivalents
    (`excess_elo`, `_lo`, `_hi`, at the cell's expected score) right after it.
    `excess_rating` (score minus the Elo-formula expectation) likewise gets
    `elo_expected` before it and `excess_rating_elo`, `_lo`, `_hi` after it.
    """
    out: dict = {}
    for name in names:
        if name == "excess":
            out["score"] = float(part["my_score"].mean()) if len(part) else np.nan
            out["expected"] = float(part["my_ep_start"].mean()) if len(part) else np.nan
        if name == "excess_rating":
            out["elo_expected"] = float(part["elo_expected"].mean()) if len(part) else np.nan
        est, lo, hi = metric(part, name, cluster=cluster, n_boot=n_boot, seed=seed) if len(part) else (np.nan, np.nan, np.nan)
        out[name], out[f"{name}_lo"], out[f"{name}_hi"] = est, lo, hi
        if name == "excess":
            with_elo(out)
        if name == "excess_rating":
            with_elo(out, expected="elo_expected", prefix="excess_rating")
    return out


def diff_stats(a: pd.DataFrame, b: pd.DataFrame, names: Iterable[str], cluster: str = "session_id", n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """{name_diff, _lo, _hi} for each metric (a minus b); `excess` also gets a's expected score and the Elo-equivalents."""
    out: dict = {}
    for name in names:
        if name == "excess":
            out["expected"] = float(a["my_ep_start"].mean()) if len(a) else np.nan
        est, lo, hi = metric_diff(a, b, name, cluster=cluster, n_boot=n_boot, seed=seed) if len(a) and len(b) else (np.nan, np.nan, np.nan)
        out[f"{name}_diff"], out[f"{name}_diff_lo"], out[f"{name}_diff_hi"] = est, lo, hi
        if name == "excess":
            with_elo(out, prefix="excess_diff")
    return out


def outcome_score(outcome: pd.Series) -> pd.Series:
    """win/draw/loss -> 1/0.5/0."""
    return outcome.map(OUTCOME_SCORE)


# --- table cells -------------------------------------------------------------------------------------


def groups_with_all(data) -> list[str]:
    """The reported time-control groups, then the `ALL` pool."""
    return [*data.tc_groups, ALL]


def subset(games: pd.DataFrame, group: str) -> pd.DataFrame:
    """The games of a group: `ALL`, a pooled time class ("all bullet") or one time-control group."""
    if group == ALL:
        return games
    if group.startswith("all "):
        return games[games["time_class"] == group[4:]]
    return games[games["tc_group"] == group]


def counts(part: pd.DataFrame, cluster: str = "session_id") -> dict:
    """{games, sessions} of a cell."""
    return {"games": int(len(part)), "sessions": int(part[cluster].nunique())}


def thin_cell(part: pd.DataFrame, min_games: int = MIN_CELL_GAMES, cluster: str = "session_id") -> bool:
    """Too few games, or too few sessions (`MIN_SESSIONS`) for the session bootstrap."""
    return len(part) < min_games or part[cluster].nunique() < MIN_SESSIONS


def by_level(games: pd.DataFrame, groups: Sequence[str], column: str, levels: Sequence, names: Iterable[str], label: str = "tc_group", min_games: int = MIN_CELL_GAMES, cluster: str = "session_id", seed: int = SEED) -> pd.DataFrame:
    """`cell_stats` per (group, level of `column`), tidy: one row per cell with its counts and `thin` flag; the group goes in column `label`."""
    rows = []
    for group in groups:
        part_g = subset(games, group)
        for level in levels:
            part = part_g[part_g[column] == level]
            rows.append({label: group, column: level, **cell_stats(part, names, cluster=cluster, seed=seed), **counts(part, cluster), "thin": thin_cell(part, min_games, cluster)})
    return pd.DataFrame(rows)
