"""Q5: sessions - fatigue, warm-up and tilt.

Does play degrade over a session (fatigue), start poorly (warm-up), or
collapse after losses (tilt), and does session length relate to results?
Sessions are recomputed here from the standard live games with a
configurable gap (`session_features`, reused by Q6), the stopping rule
(you stop more often after a loss) is measured, and every curve by game
number is shown naively and corrected for that rule: non-last games only,
paired with game 1 of the same session, and within sessions of equal
length. Tilt = the next game's result-vs-expectation and move quality by
the previous result in the session, with the opponents' numbers from the
same games as the control.

Run: .venv/bin/python analysis/05_sessions.py
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

from chess_analysis.toolkit import (
    SIDE_COLOURS,
    TIME_CLASSES,
    Report,
    elo_expected_score,
    figure_style,
    load_data,
    repo_root,
)

SLUG = "05_sessions"
GAP_MINUTES = 30.0  # the project's session definition (config.session_gap_minutes)
GAP_CHECKS = (15.0, 30.0, 60.0)
N_BOOT = 500
SEED = 5
MIN_GAMES = 20  # cells resting on fewer games than this are blank
K_MAX = 10  # game numbers 10 and above are pooled as "10+"
LENGTH_BINS = [1, 2, 4, 7, 11, 100]
LENGTH_LABELS = ["1", "2-3", "4-6", "7-10", "11+"]
MINUTE_BINS = [0, 10, 20, 40, 60, 120, 10_000]
MINUTE_LABELS = ["0-10", "10-20", "20-40", "40-60", "60-120", "120+"]
CLASS_ORDER = {"bullet": 0, "blitz": 1, "rapid": 2}


# --- session features (reused by Q6) ----------------------------------------------------------


def session_features(games: pd.DataFrame, gap_minutes: float = GAP_MINUTES) -> pd.DataFrame:
    """Per-game session variables recomputed on `games` (any subset of the player's games), indexed like `games`.

    A new session starts when the gap between a game's start and the previous
    game's end exceeds `gap_minutes`. Columns: `session_id`, `game_in_session`
    (1-based), `session_games`, `minutes_into_session` (start minus the session's
    first start), `session_minutes` (whole session), `is_first_in_session`,
    `is_last_in_session`, `gap_before_minutes` (NaN for the first game), and the
    history inside the session before this game: `prev_outcome` (win/draw/loss,
    None for the first game), `prev_score`, `prev2_outcomes` (e.g. "LL", None
    before game 3), `loss_streak` / `win_streak` (consecutive losses/wins
    immediately before this game), `session_score_before`, `session_net_before`
    (wins minus losses so far), `session_rating_change_before` (summed
    `my_rating_change` of the earlier rated games), `prev_time_class`,
    `prev_blown_win` (previous game lost after your P(win) had reached >= 0.8;
    needs `reached_winning` on `games`, else NaN), `prev_loss_how`
    (`my_result` of the previous game if it was lost), `session_time_classes`
    (distinct classes in the session). Uses only the games it is given, so
    pass `load_data().games` (standard live games) for the analyses here.
    """
    ordered = games.sort_values(["start_datetime", "end_datetime"], kind="stable")
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
    out["gap_before_minutes"] = gap.where(~out["is_first_in_session"])

    score = ordered["outcome"].map({"win": 1.0, "draw": 0.5, "loss": 0.0})
    out["prev_outcome"] = ordered["outcome"].groupby(session).shift(1)
    out["prev_score"] = score.groupby(session).shift(1)
    letters = ordered["outcome"].map({"win": "W", "draw": "D", "loss": "L"})
    prev1, prev2 = letters.groupby(session).shift(1), letters.groupby(session).shift(2)
    out["prev2_outcomes"] = (prev2 + prev1).where(prev2.notna() & prev1.notna())
    out["loss_streak"] = _streak_before(ordered["outcome"] == "loss", session)
    out["win_streak"] = _streak_before(ordered["outcome"] == "win", session)
    out["session_score_before"] = score.groupby(session).cumsum() - score
    net = (ordered["outcome"] == "win").astype(int) - (ordered["outcome"] == "loss").astype(int)
    out["session_net_before"] = net.groupby(session).cumsum() - net
    change = pd.to_numeric(ordered["my_rating_change"], errors="coerce").fillna(0.0) * ordered["rated"].astype(float)
    out["session_rating_change_before"] = change.groupby(session).cumsum() - change
    out["prev_time_class"] = ordered["time_class"].groupby(session).shift(1)
    if "reached_winning" in ordered:
        blown = (ordered["outcome"] == "loss") & ordered["reached_winning"].fillna(False).astype(bool)
        out["prev_blown_win"] = blown.astype(float).groupby(session).shift(1)
    else:
        out["prev_blown_win"] = np.nan
    lost_how = ordered["my_result"].where(ordered["outcome"] == "loss")
    out["prev_loss_how"] = lost_how.groupby(session).shift(1)
    out["session_time_classes"] = ordered["time_class"].groupby(session).transform("nunique")
    return out.loc[games.index]


def _streak_before(flag: pd.Series, session: pd.Series) -> pd.Series:
    """Number of consecutive True values immediately before each row, within session (0 if the previous row is False)."""
    flag = flag.astype(bool)
    key = session.astype(str) + ":" + flag.astype(str)
    # Run id changes when the flag or the session changes; cumcount within the run is the run length so far.
    run = (key != key.shift(1)).cumsum()
    run_len = flag.groupby(run).cumcount() + 1
    streak = run_len.where(flag, 0)
    prev = streak.groupby(session).shift(1).fillna(0).astype(int)
    return prev


def opponent_rating_pre(frame: pd.DataFrame) -> pd.Series:
    """Approximate pre-game rating of the opponent (as in Q3): post-game rating plus your rating change in rated games."""
    change = pd.to_numeric(frame["my_rating_change"], errors="coerce").astype(float)
    change = np.where(frame["rated"].astype(bool), change, 0.0)
    return frame["opponent_rating_after"].astype(float) + change


def build_games(data, decomposed: pd.DataFrame, gap_minutes: float = GAP_MINUTES) -> pd.DataFrame:
    """Per-game analysis frame: toolkit games + Q1 decomposition + session features + per-game metrics."""
    games = data.games.merge(decomposed, on="uuid", how="left")
    games = games[games["n_plies"].fillna(0) > 0].copy()  # two games without moves have no quality metrics
    # Keep the stored (all-games) session position for comparison; the recomputed columns replace the rest.
    games = games.rename(columns={"game_in_session": "game_in_session_stored"}).drop(columns=["session", "session_games", "minutes_into_session"])
    games = games.join(session_features(games, gap_minutes))
    games["opponent_rating_pre"] = opponent_rating_pre(games)
    games["elo_expected"] = elo_expected_score(games["my_rating_before"], games["opponent_rating_pre"])
    games["excess_model"] = games["my_score"] - games["my_ep_start"]
    games["excess_elo"] = games["my_score"] - games["elo_expected"]
    games["lost_on_time"] = ((games["outcome"] == "loss") & (games["my_result"] == "timeout")).astype(float)
    games["resigned"] = (games["my_result"] == "resigned").astype(float)
    games["opp_lost_on_time"] = ((games["outcome"] == "win") & (games["opponent_result"] == "timeout")).astype(float)
    games["opp_resigned"] = (games["opponent_result"] == "resigned").astype(float)
    games["is_loss"] = (games["outcome"] == "loss").astype(float)
    games["my_ep_rate"] = games["my_ep_lost"] / games["my_moves"].replace(0, np.nan)
    games["opp_ep_rate"] = games["opp_ep_lost"] / games["opp_moves"].replace(0, np.nan)
    games["ep_rate_gap"] = games["my_ep_rate"] - games["opp_ep_rate"]  # per game: positive = you lost more per move than they did
    games["k"] = games["game_in_session"].clip(upper=K_MAX)
    games["k_label"] = games["k"].astype(str).where(games["k"] < K_MAX, f"{K_MAX}+")
    games["length_bin"] = pd.cut(games["session_games"], LENGTH_BINS, labels=LENGTH_LABELS, right=False)
    games["minute_bin"] = pd.cut(games["minutes_into_session"], MINUTE_BINS, labels=MINUTE_LABELS, right=False)
    return games


def add_move_metrics(games: pd.DataFrame, moves: pd.DataFrame) -> pd.DataFrame:
    """Per-game time use from the move rows: mean seconds per move for each side (vectorised groupby)."""
    spent = moves.groupby(["game_uuid", "is_player_move"])["time_spent_seconds"].mean().unstack()
    spent.columns = ["opp_sec_per_move", "my_sec_per_move"]
    return games.merge(spent, left_on="uuid", right_index=True, how="left")


# --- bootstrap over clusters from per-cluster sums ------------------------------------------------


def boot_sums(frame: pd.DataFrame, columns: Sequence[str], cluster: str, stat: Callable[[np.ndarray], np.ndarray], n_boot: int = N_BOOT, seed: int = SEED) -> tuple[float, float, float]:
    """Any statistic of per-cluster column sums, with a CI from resampling clusters (games or sessions).

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
    draws = np.empty(n_boot)
    for b in range(n_boot):
        w = np.bincount(rng.integers(0, len(sums), len(sums)), minlength=len(sums)).astype(float)
        draws[b] = stat((w @ sums)[None, :])[0]
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return est, float(lo), float(hi)


def _ratio(s: np.ndarray) -> np.ndarray:
    return s[:, 0] / np.maximum(s[:, 1], 1e-9)


def _ratio_diff(s: np.ndarray) -> np.ndarray:
    return s[:, 0] / np.maximum(s[:, 1], 1e-9) - s[:, 2] / np.maximum(s[:, 3], 1e-9)


# Per-game metrics: (name, numerator column, denominator column or None for a mean over games, scale)
METRICS = {
    "excess_model": ("score − model expectation", "excess_model", None, 1.0),
    "excess_elo": ("score − Elo expectation", "excess_elo", None, 1.0),
    "score": ("score", "my_score", None, 1.0),
    "my_ep": ("your ep lost / move", "my_ep_lost", "my_moves", 1.0),
    "opp_ep": ("their ep lost / move", "opp_ep_lost", "opp_moves", 1.0),
    "my_err20": ("your moves losing ≥0.2 ep / 100", "my_errors_ge_20", "my_moves", 100.0),
    "opp_err20": ("their moves losing ≥0.2 ep / 100", "opp_errors_ge_20", "opp_moves", 100.0),
    "my_cp": ("your cp lost / move", "my_cp_lost", "my_moves", 1.0),
    "opp_cp": ("their cp lost / move", "opp_cp_lost", "opp_moves", 1.0),
    "my_blunder": ("your blunders / 100 moves", "my_blunders", "my_moves", 100.0),
    "opp_blunder": ("their blunders / 100 moves", "opp_blunders", "opp_moves", 100.0),
    "ep_gap": ("your − their ep / move", ("my_ep_lost", "my_moves", "opp_ep_lost", "opp_moves"), "pairdiff", 1.0),
    "lost_on_time": ("you lost on time", "lost_on_time", None, 1.0),
    "resign_share": ("resignations / your losses", "resigned", "is_loss", 1.0),
    "timeout_share": ("flags / your losses", "lost_on_time", "is_loss", 1.0),
    "opp_lost_on_time": ("they lost on time", "opp_lost_on_time", None, 1.0),
    "resigned": ("you resigned", "resigned", None, 1.0),
    "my_sec": ("your seconds / move", "my_sec_per_move", None, 1.0),
    "opp_sec": ("their seconds / move", "opp_sec_per_move", None, 1.0),
    "my_ep_opening": ("your ep lost in the opening / game", "my_ep_lost_opening", None, 1.0),
    "opp_ep_opening": ("their ep lost in the opening / game", "opp_ep_lost_opening", None, 1.0),
}


def _metric_spec(name: str) -> tuple[list[str], Callable[[np.ndarray], np.ndarray], float]:
    """Columns to sum per cluster and the statistic of their sums for one metric."""
    _, num, den, scale = METRICS[name]
    if den == "pairdiff":  # difference of two move-weighted rates within the same games (you − opponents)
        return list(num), _ratio_diff, scale
    return [num, den or "_one"], _ratio, scale


def metric(frame: pd.DataFrame, name: str, cluster: str = "uuid", n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """One per-game metric of a set of games with a cluster-bootstrap CI (ratio of sums for per-move rates)."""
    cols, stat, scale = _metric_spec(name)
    est, lo, hi = boot_sums(frame.assign(_one=1.0), cols, cluster, stat, n_boot=n_boot)
    return est * scale, lo * scale, hi * scale


def metric_diff(a: pd.DataFrame, b: pd.DataFrame, name: str, cluster: str = "uuid", n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """metric(a) − metric(b) with both computed on the same resampled clusters (a and b may share clusters)."""
    cols, stat, scale = _metric_spec(name)
    m = len(cols)
    both = pd.concat([a.assign(_one=1.0, _a=1.0), b.assign(_one=1.0, _a=0.0)])
    for c in cols:
        both[f"{c}__a"] = both[c] * both["_a"]
        both[f"{c}__b"] = both[c] * (1 - both["_a"])
    all_cols = [f"{c}__a" for c in cols] + [f"{c}__b" for c in cols]
    est, lo, hi = boot_sums(both, all_cols, cluster, lambda s: stat(s[:, :m]) - stat(s[:, m:]), n_boot=n_boot)
    return est * scale, lo * scale, hi * scale


def paired_diff(pairs: pd.DataFrame, value_a: str, value_b: str, cluster: str, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """Mean of (a − b) over pairs, bootstrapped over clusters (sessions)."""
    part = pairs.assign(_d=pairs[value_a] - pairs[value_b], _one=1.0)
    return boot_sums(part, ["_d", "_one"], cluster, _ratio, n_boot=n_boot)


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    if pd.isna(est):
        return ""
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]" if pd.notna(lo) else f"{est:{f}}"


def fmt_metric(frame: pd.DataFrame, name: str, f: str = ".3f", cluster: str = "uuid", min_games: int = MIN_GAMES) -> str:
    if len(frame) < min_games:
        return ""
    return fmt(*metric(frame, name, cluster=cluster), f=f)


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def excludes_zero(lo: float, hi: float) -> bool:
    return pd.notna(lo) and (lo > 0 or hi < 0)


# --- cluster OLS (copied from analysis/03_time_pressure.py) ---------------------------------------


def cluster_ols(X: np.ndarray, y: np.ndarray, groups: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and bootstrap draws, resampling whole groups, from per-group sufficient statistics."""
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


# --- report sections ------------------------------------------------------------------------------


def section_overview(report: Report, games: pd.DataFrame) -> dict:
    report.heading("1. What a session looks like")
    report.text(
        f"A session is a run of games with no gap longer than {GAP_MINUTES:.0f} minutes between one game's end and the next game's start, "
        "recomputed here over the standard live games only (the stored `session` column also counts daily and variant games; "
        f"{int((games['game_in_session'] != games['game_in_session_stored']).sum())} games change their position). "
        "Each game is classified by its own time class; a session can mix classes."
    )
    sessions = games.groupby("session_id").agg(games=("uuid", "size"), minutes=("session_minutes", "first"), classes=("time_class", lambda s: "/".join(sorted(set(s), key=CLASS_ORDER.get))), main=("time_class", lambda s: s.mode().iloc[0]))
    rows = []
    for tc in ["all", *TIME_CLASSES]:
        s = sessions if tc == "all" else sessions[sessions["main"] == tc]
        g = games if tc == "all" else games[games["time_class"] == tc]
        rows.append({
            "sessions (by main class)": tc, "n sessions": len(s), "games": len(g),
            "median games / session": f"{s['games'].median():.0f}", "mean": f"{s['games'].mean():.1f}",
            "1 game": pct((s["games"] == 1).mean()), "≥5 games": pct((s["games"] >= 5).mean()), "≥10 games": pct((s["games"] >= 10).mean()),
            "median minutes": f"{s['minutes'].median():.0f}", "≥60 min": pct((s["minutes"] >= 60).mean()),
            "mixed classes": pct(s["classes"].str.contains("/").mean()),
        })  # fmt: skip
    report.table(pd.DataFrame(rows))
    mixed = (games["session_time_classes"] > 1).mean()
    report.text(
        f"{pct(mixed)} of games sit in a session that mixes time classes. The per-game view below always keeps the game's own time class; "
        "`game_in_session` counts every game of the session whatever its class."
    )
    return {"n_sessions": len(sessions), "mixed": mixed}


def section_stopping(report: Report, games: pd.DataFrame) -> dict:
    report.heading("2. The stopping rule: when do you quit?")
    report.text(
        "Whether a game is the last of its session, by its result. This is the selection that every naive curve by game number "
        "has to be corrected for: if sessions end on losses, late games are over-represented by losses and look like fatigue."
    )
    rows, out = [], {}
    for tc in ["all", *TIME_CLASSES]:
        g = games if tc == "all" else games[games["time_class"] == tc]
        row = {"time class": tc, "games": len(g)}
        for oc in ["win", "draw", "loss"]:
            part = g[g["outcome"] == oc]
            row[f"P(stop | {oc})"] = fmt_metric(part.assign(my_score=part["is_last_in_session"].astype(float)), "score", f=".3f")
        d = metric_diff(g[g["outcome"] == "loss"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), g[g["outcome"] == "win"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), "score")
        row["loss − win"] = fmt(*d)
        if tc == "all":
            out["stop_loss_minus_win"] = d
        # after two losses in a row vs two wins in a row (the game after the streak exists or the session ended)
        for label, mask in (("after LL", (g["outcome"] == "loss") & (g["loss_streak"] >= 1)), ("after WW", (g["outcome"] == "win") & (g["win_streak"] >= 1))):
            part = g[mask]
            row[f"P(stop | {label})"] = fmt_metric(part.assign(my_score=part["is_last_in_session"].astype(float)), "score", f=".3f")
        rows.append(row)
    report.table(pd.DataFrame(rows))
    # Hazard by game number
    haz, haz_est = [], {}
    k_labels = [str(k) for k in range(1, K_MAX)] + [f"{K_MAX}+"]
    for k_label in k_labels:
        g = games[games["k_label"] == k_label]
        row = {"game in session": k_label, "games": len(g)}
        for oc in ["win", "loss"]:
            part = g[g["outcome"] == oc]
            row[f"P(stop | {oc})"] = fmt_metric(part.assign(my_score=part["is_last_in_session"].astype(float)), "score", f=".2f")
            haz_est[(k_label, oc)] = part["is_last_in_session"].mean() if len(part) >= MIN_GAMES else np.nan
        haz.append(row)
    # Describe the shape of the hazard from the numbers themselves: range over game numbers, per result
    span = {oc: (np.nanmin([haz_est[(k, oc)] for k in k_labels]), np.nanmax([haz_est[(k, oc)] for k in k_labels])) for oc in ("win", "loss")}
    gap_by_k = [haz_est[(k, "loss")] - haz_est[(k, "win")] for k in k_labels]
    report.text(
        f"The same by game number (all time classes): the hazard of stopping is roughly flat for both results "
        f"(after a win it stays between {span['win'][0]:.2f} and {span['win'][1]:.2f}, highest at game {max(k_labels, key=lambda k: np.nan_to_num(haz_est[(k, 'win')], nan=-1))}; "
        f"after a loss between {span['loss'][0]:.2f} and {span['loss'][1]:.2f}), while the loss − win gap persists at every game number "
        f"(smallest {np.nanmin(gap_by_k):.2f}, largest {np.nanmax(gap_by_k):.2f})."
    )
    report.table(pd.DataFrame(haz))
    out["hazard"] = haz_est
    out["last_vs_rest"] = {name: metric_diff(games[games["is_last_in_session"]], games[~games["is_last_in_session"]], name) for name in ("score", "excess_model", "my_ep", "opp_ep")}
    lv = out["last_vs_rest"]
    report.text(
        f"Consequence: the last game of a session scores {fmt(*lv['score'])} less than the other games (score − model expectation: {fmt(*lv['excess_model'])}), "
        f"and you lose {fmt(*lv['my_ep'], f='.4f')} more expected points per move in it (opponents: {fmt(*lv['opp_ep'], f='.4f')}). "
        "Part of that is the rule itself (a loss makes you stop), so the sections below exclude last games or compare within equal-length sessions."
    )
    return out


def curve_table(games: pd.DataFrame, by: str, order: Sequence[str], names: Sequence[str], fmts: Sequence[str], min_games: int = MIN_GAMES) -> pd.DataFrame:
    rows = []
    for level in order:
        part = games[games[by].astype(str) == str(level)]
        row = {by: level, "games": len(part)}
        for name, f in zip(names, fmts):
            row[METRICS[name][0]] = fmt_metric(part, name, f=f, min_games=min_games)
        rows.append(row)
    return pd.DataFrame(rows)


def curve_values(games: pd.DataFrame, by: str, order: Sequence, name: str, min_games: int = MIN_GAMES) -> pd.DataFrame:
    rows = []
    for level in order:
        part = games[games[by].astype(str) == str(level)]
        if len(part) < min_games:
            continue
        est, lo, hi = metric(part, name)
        rows.append({by: str(level), "est": est, "lo": lo, "hi": hi, "games": len(part)})
    return pd.DataFrame(rows)


def paired_vs_game1(games: pd.DataFrame, value: str, k_groups: Sequence[tuple[str, int, int]]) -> pd.DataFrame:
    """Game k vs game 1 of the same session, non-last games only (the session reached k+1), paired per session."""
    first = games[games["game_in_session"] == 1].set_index("session_id")[value].rename("g1")
    later = games[(games["game_in_session"] >= 2) & ~games["is_last_in_session"]]
    rows = []
    for label, lo_k, hi_k in k_groups:
        part = later[(later["game_in_session"] >= lo_k) & (later["game_in_session"] <= hi_k)]
        per_session = part.groupby("session_id")[value].mean().rename("gk").to_frame().join(first, how="inner").dropna()
        if len(per_session) < MIN_GAMES:
            rows.append({"game k": label, "sessions": len(per_session), "est": np.nan, "lo": np.nan, "hi": np.nan})
            continue
        est, lo, hi = paired_diff(per_session.reset_index(), "gk", "g1", "session_id")
        rows.append({"game k": label, "sessions": len(per_session), "est": est, "lo": lo, "hi": hi})
    return pd.DataFrame(rows)


def per_game_rates(games: pd.DataFrame) -> pd.DataFrame:
    """Per-game versions of the per-move rates, for paired within-session comparisons."""
    g = games.copy()
    g["my_ep_rate"] = g["my_ep_lost"] / g["my_moves"].replace(0, np.nan)
    g["opp_ep_rate"] = g["opp_ep_lost"] / g["opp_moves"].replace(0, np.nan)
    g["my_err20_rate"] = 100 * g["my_errors_ge_20"] / g["my_moves"].replace(0, np.nan)
    g["opp_err20_rate"] = 100 * g["opp_errors_ge_20"] / g["opp_moves"].replace(0, np.nan)
    g["my_cp_rate"] = g["my_cp_lost"] / g["my_moves"].replace(0, np.nan)
    return g


K_GROUPS = [("2", 2, 2), ("3", 3, 3), ("4", 4, 4), ("5", 5, 5), ("6", 6, 6), ("7-9", 7, 9), ("10+", 10, 999)]
PAIR_VALUES = [("excess_model", "score − model exp.", ".3f"), ("my_ep_rate", "your ep / move", ".4f"), ("opp_ep_rate", "their ep / move", ".4f"), ("my_err20_rate", "your ≥0.2 errors / 100", ".2f"), ("my_cp_rate", "your cp / move", ".1f")]


def paired_table(rates: pd.DataFrame, k_groups, out: dict, key: str) -> pd.DataFrame:
    """Corrected-A table: one column per PAIR_VALUES metric, sessions from the first metric."""
    table = None
    for value, label, f in PAIR_VALUES:
        t = paired_vs_game1(rates, value, k_groups)
        out[(key, value)] = t
        col = t.set_index("game k").apply(lambda r: fmt(r["est"], r["lo"], r["hi"], f=f), axis=1).rename(label)
        table = t.set_index("game k")[["sessions"]].join(col) if table is None else table.join(col)
    return table.reset_index()


def section_fatigue(report: Report, games: pd.DataFrame) -> dict:
    import matplotlib.pyplot as plt

    report.heading("3. Fatigue and warm-up: play by game number in the session")
    report.text(
        "Three views of the same question. **Naive**: every game by its number in the session. **Corrected A**: game k compared with game 1 "
        "of the *same* session, using only sessions that went on to at least game k+1 (so neither game is the one you stopped after); "
        "the difference is paired per session with CIs bootstrapped over sessions. **Corrected B**: within sessions of exactly L games. "
        "Quality is expected points lost per move (`ep_loss`, ratio of sums with game-bootstrap CIs); results are score minus the "
        "calibration model's expectation at move 1 (pre-game ratings and time control). Opponents' numbers are from the same games."
    )
    k_order = [str(k) for k in range(1, K_MAX)] + [f"{K_MAX}+"]
    out = {}
    for tc in TIME_CLASSES:
        g = games[games["time_class"] == tc]
        report.heading(f"{tc}: naive curve", level=3)
        report.table(curve_table(g, "k_label", k_order, ["excess_model", "my_ep", "opp_ep", "my_err20", "my_cp"], [".3f", ".4f", ".4f", ".2f", ".1f"]).rename(columns={"k_label": "game in session"}))
        report.heading(f"{tc}: corrected A, game k minus game 1 of the same session (non-last games only)", level=3)
        rates = per_game_rates(g)
        report.table(paired_table(rates, K_GROUPS, out, tc))
    # Pooled across time classes (the time class of game k and of game 1 may differ in mixed sessions; both are matched by rating)
    report.heading("All time classes: corrected A", level=3)
    rates = per_game_rates(games)
    report.table(paired_table(rates, K_GROUPS, out, "all"))

    # Figure: naive vs corrected A for ep/move (both sides) and for excess score, per time class
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.5), sharex=True)
    for j, tc in enumerate(TIME_CLASSES):
        g = games[games["time_class"] == tc]
        ax = axes[0, j]
        base = {}
        for name, side in (("my_ep", "player"), ("opp_ep", "opponent")):
            naive = curve_values(g, "k_label", k_order, name)
            base[side] = naive.loc[naive["k_label"] == "1", "est"].iloc[0] if (naive["k_label"] == "1").any() else np.nan
            x = np.arange(len(naive))
            ax.plot(x, naive["est"] - base[side], ls="--", marker="o", mfc="white", color=SIDE_COLOURS[side], lw=1.5, label=f"{'you' if side == 'player' else 'opponents'}, naive")
        value = {"player": "my_ep_rate", "opponent": "opp_ep_rate"}
        for side in ("player", "opponent"):
            t = out[(tc, value[side])].dropna(subset=["est"])
            xs = [k_order.index(lbl) if lbl in k_order else k_order.index("8") for lbl in t["game k"]]
            ax.errorbar(xs, t["est"], yerr=[t["est"] - t["lo"], t["hi"] - t["est"]], fmt="s", color=SIDE_COLOURS[side], lw=2, capsize=2, label=f"{'you' if side == 'player' else 'opponents'}, corrected A")
        ax.axhline(0, color="black", lw=0.6)
        ax.set_title(tc)
        ax.set_xticks(range(len(k_order)))
        ax.set_xticklabels(k_order)
        if j == 0:
            ax.set_ylabel("ep lost / move, change vs game 1")
            ax.legend(fontsize=7, loc="upper left")
        ax = axes[1, j]
        naive = curve_values(g, "k_label", k_order, "excess_model")
        ax.plot(range(len(naive)), naive["est"], ls="--", marker="o", mfc="white", color=SIDE_COLOURS["player"], lw=1.5, label="naive, score − expectation")
        t = out[(tc, "excess_model")].dropna(subset=["est"])
        xs = [k_order.index(lbl) if lbl in k_order else k_order.index("8") for lbl in t["game k"]]
        ax.errorbar(xs, t["est"], yerr=[t["est"] - t["lo"], t["hi"] - t["est"]], fmt="s", color=SIDE_COLOURS["player"], lw=2, capsize=2, label="corrected A, game k − game 1")
        ax.axhline(0, color="black", lw=0.6)
        ax.set_xlabel("game in session")
        if j == 0:
            ax.set_ylabel("score − model expectation")
            ax.legend(fontsize=7, loc="upper left")
    fig.suptitle("Play by game number: naive (hollow, dashed) vs corrected for the stopping rule (filled squares, paired with game 1)", fontsize=10)
    fig.tight_layout()
    report.figure(fig, "fatigue_curves", "Top: expected points lost per move relative to game 1 (naive) and the paired game-k-minus-game-1 difference in sessions that reached k+1 (corrected A), you and opponents. Bottom: score minus the model's pre-game expectation, same two views. The corrected-A group 'games 7-9' is drawn at the x position of game 8. Error bars are 95% CIs over sessions.")
    plt.close(fig)
    return out


def section_equal_length(report: Report, games: pd.DataFrame) -> None:
    import matplotlib.pyplot as plt

    report.heading("Corrected B: within sessions of equal length", level=3)
    report.text(
        "Every game of a session of exactly L games, by position. All games of such sessions were played whatever their result, except that "
        "the last one is the one you stopped after; it is kept and marked so the stop effect is visible. Bullet dominates the long sessions."
    )
    rows = []
    for L in range(2, 9):
        part = games[games["session_games"] == L]
        if len(part) < MIN_GAMES * L:
            continue
        for pos in range(1, L + 1):
            p = part[part["game_in_session"] == pos]
            rows.append({"L": L, "pos": pos, "games": len(p), **{n: metric(p, n) for n in ("my_ep", "opp_ep", "ep_gap", "excess_model")}})
    table = pd.DataFrame(rows)
    show = table.copy()
    for n, f in (("my_ep", ".4f"), ("opp_ep", ".4f"), ("ep_gap", ".4f"), ("excess_model", ".3f")):
        show[METRICS[n][0]] = show[n].apply(lambda t: fmt(*t, f=f))
    show["position"] = show.apply(lambda r: f"{r['pos']}{' (last)' if r['pos'] == r['L'] else ''}", axis=1)
    report.table(show[["L", "position", "games", *[METRICS[n][0] for n in ("my_ep", "opp_ep", "ep_gap", "excess_model")]]].rename(columns={"L": "session length"}))
    report.text(
        "The opponents' curve follows yours position by position (the 'your − their' column stays near zero): whatever shape the within-session curve has "
        "is a property of the games (which time class, how long they run), not of you."
    )
    # Figure: small multiples
    Ls = sorted(table["L"].unique())
    fig, axes = plt.subplots(1, len(Ls), figsize=(1.9 * len(Ls) + 1, 3.4), sharey=True)
    for ax, L in zip(np.atleast_1d(axes), Ls):
        t = table[table["L"] == L]
        for n, side in (("my_ep", "player"), ("opp_ep", "opponent")):
            est = t[n].apply(lambda v: v[0])
            lo = t[n].apply(lambda v: v[1])
            hi = t[n].apply(lambda v: v[2])
            ax.errorbar(t["pos"], est, yerr=[est - lo, hi - est], marker="o", color=SIDE_COLOURS[side], lw=1.5, capsize=2, label="you" if side == "player" else "opponents")
        ax.set_title(f"L = {L} ({int(t['games'].iloc[0])} sessions)", fontsize=9)
        ax.set_xticks(range(1, L + 1))
        ax.set_xlabel("game")
    np.atleast_1d(axes)[0].set_ylabel("ep lost / move")
    np.atleast_1d(axes)[0].legend(fontsize=7)
    fig.suptitle("Expected points lost per move within sessions of exactly L games (all time classes); the last point is the game you stopped after", fontsize=9)
    fig.tight_layout()
    report.figure(fig, "equal_length_sessions", "Within sessions of exactly L games, ep lost per move by position, you (blue) and opponents (orange), 95% CIs over games.")
    plt.close(fig)


def section_minutes(report: Report, games: pd.DataFrame) -> None:
    report.heading("Minutes into the session", level=3)
    long_share = (games.groupby("session_id")["session_minutes"].first() >= 60).mean()
    report.text(
        "The same quality measures by minutes since the session's first game started. Naive: all games. Corrected: non-last games of sessions "
        f"that lasted at least 60 minutes in total (so every bin is drawn from the same sessions and no game is the one you stopped after); only {pct(long_share)} "
        "of sessions last that long, so the corrected columns are thin."
    )
    for tc in TIME_CLASSES:
        g = games[games["time_class"] == tc]
        long = g[(g["session_minutes"] >= 60) & ~g["is_last_in_session"]]
        naive = curve_table(g, "minute_bin", MINUTE_LABELS, ["excess_model", "my_ep", "opp_ep"], [".3f", ".4f", ".4f"]).rename(columns={"minute_bin": "minutes into session"})
        corr = curve_table(long, "minute_bin", MINUTE_LABELS, ["excess_model", "my_ep", "opp_ep"], [".3f", ".4f", ".4f"]).rename(columns={"minute_bin": "minutes into session"})
        corr.columns = [c if c in ("minutes into session",) else f"{c} (corrected)" for c in corr.columns]
        report.heading(tc, level=4)
        report.table(naive.merge(corr, on="minutes into session"))


def section_regression(report: Report, games: pd.DataFrame) -> dict:
    report.heading("A pooled regression, between and within sessions", level=3)
    report.text(
        "Per-game OLS on non-last games of multi-game sessions, CIs bootstrapped by session: outcome ~ log2(game number) + minutes/30 + previous game lost + "
        "previous game won + time class + year, so fatigue (game number, minutes) and tilt (previous result) are estimated together. Reference: previous game drawn or "
        "first game of the session, bullet, 2024. **Pooled** compares games across sessions as well as within them, so a long session that went well "
        "contributes many late, good games (the stopping rule again); **within** demeans every variable inside its session and so only uses how a game differs "
        "from the other games of the same session. 'your − their ep / move' is the per-game difference, the paired control for the pool and the kind of game."
    )
    sub = games[~games["is_last_in_session"] & (games["session_games"] >= 2)].copy()
    sub["prev_loss"] = (sub["prev_outcome"] == "loss").astype(float)
    sub["prev_win"] = (sub["prev_outcome"] == "win").astype(float)
    X = pd.DataFrame({
        "intercept": 1.0,
        "log2(game number)": np.log2(sub["game_in_session"].astype(float)),
        "minutes / 30": sub["minutes_into_session"] / 30,
        "previous game lost": sub["prev_loss"],
        "previous game won": sub["prev_win"],
        "blitz": (sub["time_class"] == "blitz").astype(float),
        "rapid": (sub["time_class"] == "rapid").astype(float),
    })  # fmt: skip
    for y in sorted(sub["year"].unique()):
        if y != 2024:
            X[f"year {y}"] = (sub["year"] == y).astype(float)
    outcomes = {"excess_model": (sub["excess_model"], ".3f"), "my_ep_rate": (sub["my_ep_rate"], ".4f"), "opp_ep_rate": (sub["opp_ep_rate"], ".4f"), "ep_rate_gap": (sub["ep_rate_gap"], ".4f"), "my_err20_rate": (100 * sub["my_errors_ge_20"] / sub["my_moves"], ".2f")}
    labels = {"excess_model": "score − model exp.", "my_ep_rate": "your ep / move", "opp_ep_rate": "their ep / move", "ep_rate_gap": "your − their ep / move", "my_err20_rate": "your ≥0.2 errors / 100"}
    groups = sub["session_id"].to_numpy()
    results = {}
    within_terms = ["log2(game number)", "minutes / 30"]  # the previous result is a lagged outcome: demeaning it in 2-10 game panels gives the Nickell bias, so it stays out
    for mode in ("pooled", "within"):
        rows = {}
        for name, (y, f) in outcomes.items():
            ok = y.notna().to_numpy()
            if mode == "pooled":
                Xm, ym = X.to_numpy(float)[ok], y.to_numpy(float)[ok]
                terms = list(X.columns)
            else:
                frame = X[within_terms].copy()
                frame["_y"] = y
                frame = frame[ok]
                frame = frame - frame.groupby(groups[ok]).transform("mean")
                Xm, ym = frame[within_terms].to_numpy(float), frame["_y"].to_numpy(float)
                terms = within_terms
            beta, draws = cluster_ols(Xm, ym, groups[ok], n_boot=N_BOOT)
            lo, hi = np.nanquantile(draws, [0.025, 0.975], axis=0)
            rows[labels[name]] = [fmt(b, l, h, f=f) for b, l, h in zip(beta, lo, hi)]
            results[(mode, name)] = dict(zip(terms, zip(beta, lo, hi)))
        table = pd.DataFrame({"term": terms, **rows})
        report.text(f"**{mode.capitalize()}**" + (" (year dummies not shown)" if mode == "pooled" else " (session-demeaned; intercept, time class and year drop out, and the previous-result terms are left out because a demeaned lagged outcome in panels of 2-10 games is badly biased)"))
        report.table(table[~table["term"].str.startswith("year")])
    report.text(f"{len(sub):,} games in {sub['session_id'].nunique():,} sessions.")
    return results


TILT_METRICS = [("excess_model", ".3f"), ("excess_elo", ".3f"), ("my_ep", ".4f"), ("opp_ep", ".4f"), ("ep_gap", ".4f"), ("my_err20", ".2f"), ("my_cp", ".1f"), ("lost_on_time", ".3f"), ("resign_share", ".3f"), ("my_sec", ".2f"), ("opp_sec", ".2f")]
TILT_SHORT = TILT_METRICS[:6] + [TILT_METRICS[7]]


def tilt_table(games: pd.DataFrame, by: str, levels: Sequence, metrics: Sequence[tuple[str, str]], contrast: tuple | None = None, min_games: int = MIN_GAMES) -> pd.DataFrame:
    rows = []
    for level in levels:
        part = games[games[by].astype(str) == str(level)]
        row = {by: str(level), "games": len(part)}
        for name, f in metrics:
            row[METRICS[name][0]] = fmt_metric(part, name, f=f, cluster="session_id", min_games=min_games)
        rows.append(row)
    if contrast is not None:
        a, b = contrast
        pa, pb = games[games[by].astype(str) == str(a)], games[games[by].astype(str) == str(b)]
        row = {by: f"{a} − {b}", "games": len(pa) + len(pb)}
        for name, f in metrics:
            row[METRICS[name][0]] = fmt(*metric_diff(pa, pb, name, cluster="session_id"), f=f) if min(len(pa), len(pb)) >= min_games else ""
        rows.append(row)
    return pd.DataFrame(rows)


def section_tilt(report: Report, games: pd.DataFrame) -> dict:
    import matplotlib.pyplot as plt

    report.heading("4. Tilt: the game after a loss")
    report.text(
        "Games 2+ of a session by the result of the previous game in the session. 'Score − model expectation' uses the calibration model's "
        "expectation at move 1 (it knows both pre-game ratings, so a lower rating after a loss and the slightly weaker opponent that follows "
        "are already priced in); 'score − Elo expectation' uses your pre-game rating and the opponent's approximate pre-game rating "
        "(post-game rating + your rating change). Opponents' error rates come from the same games. CIs are bootstrapped over sessions. "
        "The game after a loss exists only if you kept playing, so these are the games you chose to play on tilt, if tilt there is."
    )
    later = games[games["game_in_session"] >= 2]
    out = {}
    for tc in ["all", *TIME_CLASSES]:
        g = later if tc == "all" else later[later["time_class"] == tc]
        report.heading(f"{tc}: by the previous game's result", level=3)
        t = tilt_table(g, "prev_outcome", ["win", "draw", "loss"], TILT_METRICS, contrast=("loss", "win"))
        report.table(t.rename(columns={"prev_outcome": "previous game"}))
        out[tc] = {name: metric_diff(g[g["prev_outcome"] == "loss"], g[g["prev_outcome"] == "win"], name, cluster="session_id") for name, _ in TILT_METRICS}
        out[(tc, "n_loss")] = int((g["prev_outcome"] == "loss").sum())
        out[(tc, "n_win")] = int((g["prev_outcome"] == "win").sum())
    # Streaks
    report.heading("By the losing streak before the game (all time classes)", level=3)
    report.text("Consecutive losses immediately before the game, within the session (0 = the previous game was a win or a draw).")
    g = later.copy()
    g["streak"] = g["loss_streak"].clip(upper=3).astype(str).replace({"3": "3+"})
    report.table(tilt_table(g, "streak", ["0", "1", "2", "3+"], TILT_SHORT, contrast=("3+", "0")).rename(columns={"streak": "losses in a row before"}))
    out["streak_frame"] = g
    out["streak"] = {s: metric(g[g["streak"] == s], "excess_model", cluster="session_id") for s in ["0", "1", "2", "3+"]}
    out["streak_ep"] = {s: metric(g[g["streak"] == s], "my_ep", cluster="session_id") for s in ["0", "1", "2", "3+"]}
    # Previous two results: does the game before last matter as much as the last one?
    report.heading("By the previous two results (all time classes)", level=3)
    report.text(
        "Games 3+ of a session by the results of the two games before (draws dropped). If only the last result mattered (tilt proper), WL and LL would look alike "
        "and WW and LW would look alike; if form over several games is what shows, LL would be worse than WL."
    )
    g3 = g[g["prev2_outcomes"].isin(["WW", "LW", "WL", "LL"])]
    report.table(tilt_table(g3, "prev2_outcomes", ["WW", "LW", "WL", "LL"], TILT_SHORT, contrast=("LL", "WL")).rename(columns={"prev2_outcomes": "two games before (older first)"}))
    out["prev2"] = {lvl: metric(g3[g3["prev2_outcomes"] == lvl], "excess_model", cluster="session_id") for lvl in ["WW", "LW", "WL", "LL"]}
    out["prev2_contrast"] = metric_diff(g3[g3["prev2_outcomes"] == "LL"], g3[g3["prev2_outcomes"] == "WL"], "excess_model", cluster="session_id")
    out["streak_contrast"] = metric_diff(g[g["streak"] == "3+"], g[g["streak"] == "0"], "excess_model", cluster="session_id")
    out["streak_1_vs_0"] = metric_diff(g[g["streak"] == "1"], g[g["streak"] == "0"], "excess_model", cluster="session_id")
    out["streak_3_vs_1"] = metric_diff(g[g["streak"] == "3+"], g[g["streak"] == "1"], "excess_model", cluster="session_id")
    # Winning streaks
    g["wstreak"] = g["win_streak"].clip(upper=3).astype(str).replace({"3": "3+"})
    report.heading("By the winning streak before the game (all time classes)", level=3)
    report.table(tilt_table(g, "wstreak", ["0", "1", "2", "3+"], TILT_SHORT, contrast=("3+", "0")).rename(columns={"wstreak": "wins in a row before"}))
    # How the previous game was lost
    report.heading("By how the previous game was lost (all time classes)", level=3)
    report.text("Among games following a loss: whether that loss came after you had been winning (your P(win) reached ≥ 0.8: a blown win), and how it ended.")
    after_loss = later[later["prev_outcome"] == "loss"].copy()
    after_loss["prev_kind"] = np.where(after_loss["prev_blown_win"] == 1, "blown win", "ordinary loss")
    report.table(tilt_table(after_loss, "prev_kind", ["ordinary loss", "blown win"], TILT_SHORT, contrast=("blown win", "ordinary loss")).rename(columns={"prev_kind": "previous loss"}))
    out["blown"] = metric_diff(after_loss[after_loss["prev_kind"] == "blown win"], after_loss[after_loss["prev_kind"] == "ordinary loss"], "excess_model", cluster="session_id")
    out["blown_n"] = int((after_loss["prev_kind"] == "blown win").sum())
    how_levels = ["checkmated", "resigned", "timeout"]
    after_loss["prev_how"] = after_loss["prev_loss_how"].where(after_loss["prev_loss_how"].isin(how_levels), "other")
    report.table(tilt_table(after_loss, "prev_how", how_levels + ["other"], TILT_SHORT).rename(columns={"prev_how": "previous loss ended by"}))
    # Re-queue speed
    report.heading("How fast you start the next game", level=3)
    rows = []
    for tc in ["all", *TIME_CLASSES]:
        g2 = later if tc == "all" else later[later["time_class"] == tc]
        row = {"time class": tc}
        for oc in ["win", "draw", "loss"]:
            part = g2[g2["prev_outcome"] == oc]
            row[f"median gap after {oc} (s)"] = f"{60 * part['gap_before_minutes'].median():.0f}" if len(part) >= MIN_GAMES else ""
            row[f"started within 30 s after {oc}"] = fmt_metric(part.assign(my_score=(part["gap_before_minutes"] * 60 <= 30).astype(float)), "score", cluster="session_id", f=".2f")
        rows.append(row)
    report.table(pd.DataFrame(rows))
    # Figure: tilt
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    levels = ["0", "1", "2", "3+"]
    ax = axes[0]
    vals = [out["streak"][s] for s in levels]
    ax.errorbar(range(4), [v[0] for v in vals], yerr=[[v[0] - v[1] for v in vals], [v[2] - v[0] for v in vals]], marker="o", color=SIDE_COLOURS["player"], lw=2, capsize=3)
    ax.axhline(0, color="black", lw=0.6)
    ax.set_xticks(range(4))
    ax.set_xticklabels(levels)
    ax.set_xlabel("losses in a row before the game")
    ax.set_ylabel("score − model expectation")
    ax.set_title("Result of the next game")
    ax = axes[1]
    for side, key in (("player", "my_ep"), ("opponent", "opp_ep")):
        vals = [metric(g[g["streak"] == s], key, cluster="session_id") for s in levels]
        ax.errorbar(np.arange(4) + (0.05 if side == "opponent" else -0.05), [v[0] for v in vals], yerr=[[v[0] - v[1] for v in vals], [v[2] - v[0] for v in vals]], marker="o", color=SIDE_COLOURS[side], lw=2, capsize=3, label="you" if side == "player" else "opponents")
    ax.set_xticks(range(4))
    ax.set_xticklabels(levels)
    ax.set_xlabel("losses in a row before the game")
    ax.set_ylabel("ep lost / move")
    ax.set_title("Move quality in the next game")
    ax.legend(fontsize=8)
    fig.suptitle("Tilt: games 2+ of a session by the losing streak before them (all time classes, 95% CIs over sessions)", fontsize=10)
    fig.tight_layout()
    report.figure(fig, "tilt", "Left: score minus the model's pre-game expectation in the next game by the number of consecutive losses just before it. Right: expected points lost per move in that game, you and opponents.")
    plt.close(fig)
    return out


def section_length(report: Report, games: pd.DataFrame) -> dict:
    report.heading("5. Session length and results")
    report.text(
        "Games by the length of the session they belong to. Short sessions are short partly *because* they went badly (section 2), so this is "
        "descriptive; the 'excluding the last game' columns remove the direct stop effect."
    )
    rows = []
    for tc in ["all", *TIME_CLASSES]:
        g = games if tc == "all" else games[games["time_class"] == tc]
        for label in LENGTH_LABELS:
            part = g[g["length_bin"].astype(str) == label]
            non_last = part[~part["is_last_in_session"]]
            rows.append({
                "time class": tc, "session length": label, "games": len(part),
                "score − model exp.": fmt_metric(part, "excess_model"),
                "your ep / move": fmt_metric(part, "my_ep", f=".4f"), "their ep / move": fmt_metric(part, "opp_ep", f=".4f"),
                "score − model exp. (excl. last)": fmt_metric(non_last, "excess_model"),
                "your ep / move (excl. last)": fmt_metric(non_last, "my_ep", f=".4f"),
                "their ep / move (excl. last)": fmt_metric(non_last, "opp_ep", f=".4f"),
            })  # fmt: skip
    report.table(pd.DataFrame(rows))
    # Session-level: net rating change per session by length
    rated = games[games["rated"]]
    sess = rated.groupby("session_id").agg(n=("uuid", "size"), change=("my_rating_change", "sum"), score=("my_score", "mean"), expected=("my_ep_start", "mean"))
    sess["length_bin"] = pd.cut(sess["n"], LENGTH_BINS, labels=LENGTH_LABELS, right=False)
    rows = []
    for label in LENGTH_LABELS:
        part = sess[sess["length_bin"].astype(str) == label].reset_index().assign(_one=1.0)
        if len(part) < MIN_GAMES:
            continue
        chg = boot_sums(part, ["change", "_one"], "session_id", _ratio)
        pos = boot_sums(part.assign(pos=(part["change"] > 0).astype(float)), ["pos", "_one"], "session_id", _ratio)
        rows.append({"session length (rated games)": label, "sessions": len(part), "mean rating change per session": fmt(*chg, f=".1f"), "P(session gained rating)": fmt(*pos)})
    report.text(
        "Per session (rated games only): the summed rating change and the share of sessions that ended above where they started. Long sessions gain rating "
        "because you keep playing while winning and stop when losing (section 2), not because long sessions make you play better."
    )
    report.table(pd.DataFrame(rows))
    # Stop-after-N test: among non-last games, is play after game N worse than up to game N?
    report.heading("Is there a game number after which play is worse?", level=3)
    report.text(
        "For each N: non-last games numbered N+1 or more versus non-last games numbered 1..N in the *same* sessions (sessions that reached at least N+2 games), "
        "paired per session, all time classes. A 'stop after N' rule would need the later games to be clearly worse."
    )
    rates = per_game_rates(games)
    non_last = rates[~rates["is_last_in_session"]]
    rows = []
    out = {}
    for N in (2, 3, 4, 5, 6, 8, 10):
        early = non_last[non_last["game_in_session"] <= N].groupby("session_id")[["excess_model", "my_ep_rate", "opp_ep_rate"]].mean()
        late = non_last[non_last["game_in_session"] > N].groupby("session_id")[["excess_model", "my_ep_rate", "opp_ep_rate"]].mean()
        pairs = early.add_suffix("_early").join(late.add_suffix("_late"), how="inner").dropna().reset_index()
        row = {"N": N, "sessions": len(pairs), "games after N": int((non_last["game_in_session"] > N).sum())}
        for value, f in (("excess_model", ".3f"), ("my_ep_rate", ".4f"), ("opp_ep_rate", ".4f")):
            d = paired_diff(pairs, f"{value}_late", f"{value}_early", "session_id") if len(pairs) >= MIN_GAMES else (np.nan, np.nan, np.nan)
            row[f"{PAIR_LABEL[value]}: after N − up to N"] = fmt(*d, f=f)
            out[(N, value)] = d
        rows.append(row)
    report.table(pd.DataFrame(rows))
    return out


PAIR_LABEL = {"excess_model": "score − model exp.", "my_ep_rate": "your ep / move", "opp_ep_rate": "their ep / move"}


def section_cutoffs(report: Report, data, decomposed: pd.DataFrame, games30: pd.DataFrame) -> pd.DataFrame:
    report.heading("6. Does the session definition matter? 15 / 30 / 60-minute gaps")
    report.text(
        "The headline numbers recomputed with the session gap set to 15, 30 (the project default) and 60 minutes, all time classes. "
        "'Fatigue' is corrected A for games 5+ vs game 1 (your ep per move) and 'tilt' is the loss − win contrast in the game after."
    )
    rows = []
    for gap in GAP_CHECKS:
        g = games30 if gap == GAP_MINUTES else add_move_metrics(build_games(data, decomposed, gap), data.moves)
        rates = per_game_rates(g)
        fat = paired_vs_game1(rates, "my_ep_rate", [("5+", 5, 999)]).iloc[0]
        fat_opp = paired_vs_game1(rates, "opp_ep_rate", [("5+", 5, 999)]).iloc[0]
        fat_score = paired_vs_game1(rates, "excess_model", [("5+", 5, 999)]).iloc[0]
        later = g[g["game_in_session"] >= 2]
        loss, win = later[later["prev_outcome"] == "loss"], later[later["prev_outcome"] == "win"]
        rows.append({
            "gap (min)": int(gap), "sessions": int(g["session_id"].nunique()), "games in multi-game sessions": int((g["session_games"] >= 2).sum()),
            "median games / session": f"{g.groupby('session_id').size().median():.0f}",
            "fatigue: your ep/move, game 5+ − game 1": fmt(fat["est"], fat["lo"], fat["hi"], f=".4f"),
            "fatigue: their ep/move": fmt(fat_opp["est"], fat_opp["lo"], fat_opp["hi"], f=".4f"),
            "fatigue: score − exp.": fmt(fat_score["est"], fat_score["lo"], fat_score["hi"], f=".3f"),
            "tilt: score − exp., after loss − after win": fmt(*metric_diff(loss, win, "excess_model", cluster="session_id")),
            "tilt: your ep/move": fmt(*metric_diff(loss, win, "my_ep", cluster="session_id"), f=".4f"),
            "tilt: their ep/move": fmt(*metric_diff(loss, win, "opp_ep", cluster="session_id"), f=".4f"),
            "P(stop | loss) − P(stop | win)": fmt(*metric_diff(g[g["outcome"] == "loss"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), g[g["outcome"] == "win"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), "score")),
        })  # fmt: skip
    table = pd.DataFrame(rows)
    report.table(table)
    return table


# --- main -----------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    figure_style()
    data = load_data()
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    games = build_games(data, decomposed)
    games = add_move_metrics(games, data.moves)

    report = Report(SLUG, "Sessions: fatigue, warm-up and tilt")
    n_by_tc = games["time_class"].value_counts()
    report.text(
        f"Data: {len(games):,} standard live games with at least one move ({n_by_tc.get('bullet', 0):,} bullet, {n_by_tc.get('blitz', 0):,} blitz, "
        f"{n_by_tc.get('rapid', 0):,} rapid) in {games['session_id'].nunique():,} sessions ({GAP_MINUTES:.0f}-minute gap), "
        f"{len(data.moves):,} analysed moves, player {data.username}. Per-game quality comes from Q1's `games_decomposed.parquet` "
        "(`my_ep_lost`, `opp_ep_lost`, moves, error counts). Numbers are estimate [95% CI]; CIs are bootstrapped by game, or by session where games of the "
        f"same session are compared or conditioned on ({N_BOOT} resamples). `ep_loss` (calibrated expected points lost per move) is the main quality "
        "measure, `cp_loss` secondary. Every number about you is next to the opponents' number from the same games where the question allows it."
    )
    overview = section_overview(report, games)
    stopping = section_stopping(report, games)
    fatigue = section_fatigue(report, games)
    section_equal_length(report, games)
    section_minutes(report, games)
    regression = section_regression(report, games)
    tilt = section_tilt(report, games)
    length = section_length(report, games)
    cutoffs = section_cutoffs(report, data, decomposed, games)

    # --- summary paragraphs computed from the results ---
    sl = stopping["stop_loss_minus_win"]
    stop_loss = metric(games[games["outcome"] == "loss"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), "score")[0]
    stop_win = metric(games[games["outcome"] == "win"].assign(my_score=lambda x: x["is_last_in_session"].astype(float)), "score")[0]
    bullet = games[games["time_class"] == "bullet"]
    naive_b = curve_values(bullet, "k_label", ["1", "10+"], "my_ep").set_index("k_label")
    naive_bs = curve_values(bullet, "k_label", ["1", "10+"], "excess_model").set_index("k_label")
    fat_b = fatigue[("bullet", "my_ep_rate")].set_index("game k")
    fat_bo = fatigue[("bullet", "opp_ep_rate")].set_index("game k")
    fat_bs = fatigue[("bullet", "excess_model")].set_index("game k")
    fat_all = fatigue[("all", "my_ep_rate")].set_index("game k")
    worse_cells = [f"{tc} game {k}" for tc in TIME_CLASSES for k, r in fatigue[(tc, "my_ep_rate")].set_index("game k").iterrows() if excludes_zero(r["lo"], r["hi"]) and r["est"] > 0]
    k2 = fat_all.loc["2"]
    w_ep = regression[("within", "my_ep_rate")]
    w_gap = regression[("within", "ep_rate_gap")]
    w_score = regression[("within", "excess_model")]
    p_score = regression[("pooled", "excess_model")]
    tilt_all = tilt["all"]
    streak, streak_ep, prev2 = tilt["streak"], tilt["streak_ep"], tilt["prev2"]
    stopN = length[(3, "excess_model")]
    tilt_sig = excludes_zero(tilt_all["excess_model"][1], tilt_all["excess_model"][2])
    gap_sig = excludes_zero(tilt_all["ep_gap"][1], tilt_all["ep_gap"][2])
    prev2_c, streak_c = tilt["prev2_contrast"], tilt["streak_contrast"]
    prev2_sig = excludes_zero(prev2_c[1], prev2_c[2])
    streak_grows = excludes_zero(tilt["streak_3_vs_1"][1], tilt["streak_3_vs_1"][2])  # 3+ vs 1 loss: does the deficit deepen beyond the first loss?
    if prev2_sig:
        prev2_text = (f"and LL before a game ({fmt(*prev2['LL'])}) is worse than WL ({fmt(*prev2['WL'])}; LL − WL {fmt(*prev2_c)}), so the last result is not the whole story: "
                      "several-game form, or the session itself going badly, carries it. ")
    else:
        prev2_text = (f"but whether the game before last adds anything is unresolved: LL before a game ({fmt(*prev2['LL'])}) looks worse than WL ({fmt(*prev2['WL'])}), "
                      f"yet the LL − WL contrast is {fmt(*prev2_c)}, so the data cannot separate 'the last result' from 'several-game form'. ")
    if streak_grows:
        streak_text = f"the score deficit deepens with the losing streak (3+ vs 1 loss: {fmt(*tilt['streak_3_vs_1'])}; 3+ vs 0: {fmt(*streak_c)}) "
    else:
        streak_text = (f"the score deficit after 3+ losses versus none is {fmt(*streak_c)}, but the step from one loss to three or more "
                       f"(relative to none: {fmt(*tilt['streak_1_vs_0'])} after one loss, {fmt(*streak_c)} after 3+; 3+ vs 1 directly: {fmt(*tilt['streak_3_vs_1'])}) is within noise, "
                       "so the resolved part is 'after a loss vs after a win', not a dose-response in the streak length, ")

    report.heading("What this means for the player")
    report.text(
        f"The dominant session effect is the stopping rule: a loss ends the session {pct(stop_loss)} of the time, a win {pct(stop_win)} "
        f"(difference {fmt(*sl)}), so the last game of every session is a bad one by construction and any naive curve by game number is bent by it. "
        f"Fatigue: in bullet, where sessions are long, the naive curve goes from {naive_b.loc['1', 'est']:.4f} ep lost per move in game 1 to {naive_b.loc['10+', 'est']:.4f} in game 10+ "
        f"(score − expectation {naive_bs.loc['1', 'est']:+.3f} to {naive_bs.loc['10+', 'est']:+.3f}); paired with game 1 of the same session and excluding the game you stopped after, "
        f"game 10+ differs by {fmt(fat_b.loc['10+', 'est'], fat_b.loc['10+', 'lo'], fat_b.loc['10+', 'hi'], f='.4f')} ep per move (opponents {fmt(fat_bo.loc['10+', 'est'], fat_bo.loc['10+', 'lo'], fat_bo.loc['10+', 'hi'], f='.4f')}) "
        f"and {fmt(fat_bs.loc['10+', 'est'], fat_bs.loc['10+', 'lo'], fat_bs.loc['10+', 'hi'])} in score − expectation. "
        f"Within sessions, a doubling of the game number changes your ep per move by {fmt(*w_ep['log2(game number)'], f='.4f')} and your gap to the opponents by {fmt(*w_gap['log2(game number)'], f='.4f')}; "
        f"the pooled slope in score − expectation ({fmt(*p_score['log2(game number)'])} per doubling) shrinks to {fmt(*w_score['log2(game number)'])} within sessions, "
        "which is the signature of selection (long sessions are the ones that went well), not of late-session strength. "
        + (f"Corrected cells where you are clearly worse than in game 1: {', '.join(worse_cells)}. " if worse_cells else "No corrected cell in any time class shows you clearly worse than in game 1: no fatigue is measurable. ")
        + f"Warm-up: game 2 vs game 1 of the same session is {fmt(k2['est'], k2['lo'], k2['hi'], f='.4f')} ep per move, and the opponents' curve follows yours position by position, so there is no first-game effect of your own either. "
        f"Tilt is the one real within-session signal, and it is about results more than moves: after a loss the next game scores {fmt(*tilt_all['excess_model'])} relative to the model's expectation compared with after a win, "
        f"you lose {fmt(*tilt_all['my_ep'], f='.4f')} more ep per move but the opponents in those games also lose {fmt(*tilt_all['opp_ep'], f='.4f')} more, "
        + (f"and your gap to them widens by {fmt(*tilt_all['ep_gap'], f='.4f')}; " if gap_sig else f"so your gap to them barely moves ({fmt(*tilt_all['ep_gap'], f='.4f')}); ")
        + f"you resign a larger share of the losses that follow a loss ({fmt(*tilt_all['resign_share'])}). After three or more losses in a row score − expectation is {fmt(*streak['3+'])} against {fmt(*streak['0'])} after a win or draw, "
        + prev2_text
        + f"A fixed 'stop after N games' is not supported: play after game 3 versus games 1–3 of the same sessions differs by {fmt(*stopN)} in score − expectation with no change in move quality; "
        f"if a stopping rule is to be drawn from this it is about the previous result, not game numbers: "
        + streak_text
        + "while nothing grows with the game number, and you already stop more often after two losses than after one (section 2). "
        "The 15/30/60-minute session definitions give the same picture (section 6)."
    )
    report.heading("Caveats")
    report.text(
        "- You choose when to stop, and you stop after losses: every curve by game number is selected. The corrections (non-last games, pairing with game 1 "
        "of the same session, equal-length sessions) remove the direct effect of the last game but not a subtler one: continuing after game 8 is a stronger "
        "selection on going well than continuing after game 1, which biases late non-last games slightly in your favour. Treat small negative late-session "
        "differences as 'no fatigue', not as improvement.\n"
        "- Tilt is measured on the games you chose to play after a loss; games you would have lost had you played them are not observed, and a common cause "
        "(a bad day, a stronger pool at that hour) can produce autocorrelation that looks like tilt. The opponents' numbers in the same games are the control for the pool.\n"
        "- 'Score − model expectation' uses the calibration model's move-1 expectation, which includes pre-game ratings and time control; the Elo version uses "
        "an approximate pre-game opponent rating (post-game rating + your rating change in rated games). Both are flat near zero overall; the player beats the model slightly (Q1).\n"
        "- Sessions are defined by a clock gap over standard live games only; daily and variant games are ignored, so a few sessions differ from the stored `session` column. Sessions mixing time classes are kept; "
        "the per-game time class is the game's own.\n"
        "- Per-game quality is the sum of `ep_loss` over the side's moves divided by moves, from Q1; it inherits Q1's caveats (engine-perfect baseline, "
        "pooled P(win) model, clipping at zero).\n"
        "- Cells with fewer than 20 games (or 20 sessions for paired differences) are left blank; rapid late-session cells are small and mostly blank."
    )
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
