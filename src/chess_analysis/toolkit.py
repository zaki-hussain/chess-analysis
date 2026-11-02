"""Shared helpers for the analysis scripts under analysis/.

Conventions every analysis follows:
- load the data once with `load_data()`; it joins the per-game view onto
  the moves so filters like time control, rating band or session are always
  to hand;
- report each exact time control separately (`data.tc_groups`, e.g.
  "bullet 1+0", "bullet 1+1"): a control is its own group when it has at
  least `MIN_GROUP_GAMES` games, and the rest are listed in
  `data.tc_noted` but not analysed;
- every estimate carries a confidence interval, bootstrapped by *game*,
  because moves within a game are not independent;
- every number about the player is paired with the same number for the
  opponents in the same games (`compare_sides`);
- `ep_loss` is the main move-quality measure, `cp_loss` the secondary one;
- results are data: `Results` writes every table as CSV under
  presentation/data/<slug>/ and every chart as SVG under
  presentation/figures/<slug>/ (styled by `chess_analysis.charts`). Scripts
  write no prose: the reading lives in presentation/README.md.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import find_config, load_settings
from .dataset import load_analysed, load_my_games

TIME_CLASSES = ["bullet", "blitz", "rapid"]
# A time control with fewer standard live games than this is too thin to estimate anything with a usable
# CI (a 95% CI on a score from 50 games is about +-0.13); it is listed in `Data.tc_noted` instead.
MIN_GROUP_GAMES = 50
OUTCOME_SCORE = {"win": 1.0, "draw": 0.5, "loss": 0.0}
# Columns from my_games worth having on every move row.
GAME_COLUMNS = [
    "rated", "my_color", "my_rating_before", "my_rating_after", "my_rating_change", "outcome", "my_result",
    "opponent", "opponent_rating_after", "opponent_result", "start_datetime", "end_datetime", "local_hour",
    "weekday", "session", "game_in_session", "session_games", "minutes_into_session",
    "days_since_last_in_pool", "pool_games_last_30d", "opening_eco", "opening_name", "book_ply", "termination",
]  # fmt: skip


@dataclass
class Data:
    """Everything an analysis needs, loaded once.

    `moves`: one row per move of every standard live game (daily games are
    dropped by default because they have no clocks), with the game-level
    columns joined on. `games`: one row per game from the player's side.
    `tc_groups`: the time-control groups to report, in order (bullet before
    blitz before rapid, then by base time and increment). `tc_noted`: the
    time controls too thin to report, with their game counts.
    """

    moves: pd.DataFrame
    games: pd.DataFrame
    username: str
    tc_groups: list[str] = field(default_factory=list)
    tc_noted: dict[str, int] = field(default_factory=dict)

    @property
    def mine(self) -> pd.DataFrame:
        return self.moves[self.moves["is_player_move"]]

    @property
    def theirs(self) -> pd.DataFrame:
        return self.moves[~self.moves["is_player_move"]]

    @property
    def tc_class(self) -> dict[str, str]:
        """Time class of each reported time-control group."""
        return {g: g.split(" ", 1)[0] for g in self.tc_groups}


def _minutes(seconds: float) -> str:
    minutes = float(seconds) / 60
    return f"{minutes:g}"


def tc_label(base_seconds: float, increment_seconds: float) -> str:
    """'1+0', '1+1', '0.5+0', '10+0': base in minutes + increment in seconds, as chess.com writes them."""
    return f"{_minutes(base_seconds)}+{float(increment_seconds):g}"


def tc_group(time_class: pd.Series, base_seconds: pd.Series, increment_seconds: pd.Series) -> pd.Series:
    """'bullet 1+0'-style labels, one per row."""
    labels = [f"{c} {tc_label(b, i)}" if pd.notna(b) and pd.notna(i) else pd.NA for c, b, i in zip(time_class, base_seconds, increment_seconds)]
    return pd.Series(labels, index=time_class.index, dtype="object")


def tc_group_order(games: pd.DataFrame) -> list[str]:
    """Every time-control group present, bullet -> blitz -> rapid (-> daily), then by base time and increment."""
    class_rank = {c: i for i, c in enumerate([*TIME_CLASSES, "daily"])}
    keys = games[["time_class", "base_seconds", "increment_seconds", "tc_group"]].dropna().drop_duplicates("tc_group")
    keys = keys.assign(_rank=keys["time_class"].map(class_rank).fillna(99))
    return list(keys.sort_values(["_rank", "base_seconds", "increment_seconds"])["tc_group"])


def load_data(username: str | None = None, include_daily: bool = False, rated_only: bool = False, min_group_games: int = MIN_GROUP_GAMES) -> Data:
    settings = load_settings(username)
    games = load_my_games(username)
    games = games[games["rules"] == "chess"]
    if not include_daily:
        games = games[games["time_class"] != "daily"]
    if rated_only:
        games = games[games["rated"]]
    games = games.reset_index(drop=True)
    games["my_score"] = games["outcome"].map(OUTCOME_SCORE)
    games["year"] = games["start_datetime"].dt.year
    games["tc_group"] = tc_group(games["time_class"], games["base_seconds"], games["increment_seconds"])
    order = tc_group_order(games)
    counts = games["tc_group"].value_counts()
    tc_groups = [g for g in order if counts.get(g, 0) >= min_group_games]
    tc_noted = {g: int(counts.get(g, 0)) for g in order if g not in tc_groups}
    games["tc_group"] = pd.Categorical(games["tc_group"], categories=order, ordered=True)

    moves = load_analysed(username)
    moves = moves[moves["game_uuid"].isin(games["uuid"])]
    moves = moves.merge(games[["uuid", *GAME_COLUMNS, "tc_group"]].rename(columns={"uuid": "game_uuid"}), on="game_uuid", how="left")
    moves["side"] = np.where(moves["is_player_move"], "player", "opponent")
    # Seconds on the clock beyond one increment: what is left if every remaining move were played at
    # increment pace. Equal to the clock in games without increment; 0 means living on the increment.
    if "clock_before_seconds" in moves:
        moves["clock_buffer_seconds"] = moves["clock_before_seconds"] - moves["increment_seconds"]
        moves["opponent_clock_buffer_seconds"] = moves["opponent_clock_seconds"] - moves["increment_seconds"]
    return Data(moves=moves.reset_index(drop=True), games=games, username=settings.username, tc_groups=tc_groups, tc_noted=tc_noted)


# --- statistics ---------------------------------------------------------------


def cluster_bootstrap_mean(values: pd.Series, groups: pd.Series, n_boot: int = 1000, seed: int = 0, ci: float = 0.95) -> tuple[float, float, float]:
    """Mean of `values` with a confidence interval from resampling whole groups (games).

    Fast path for the mean: resample per-group sums and counts rather than rows.
    """
    frame = pd.DataFrame({"v": values.astype("float64"), "g": groups.to_numpy()}).dropna()
    if frame.empty:
        return float("nan"), float("nan"), float("nan")
    by_group = frame.groupby("g")["v"].agg(["sum", "count"])
    sums, counts = by_group["sum"].to_numpy(), by_group["count"].to_numpy()
    estimate = sums.sum() / counts.sum()
    if len(by_group) < 2:
        return float(estimate), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(by_group), size=(n_boot, len(by_group)))
    boot = sums[idx].sum(axis=1) / counts[idx].sum(axis=1)
    lo, hi = np.quantile(boot, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return float(estimate), float(lo), float(hi)


def cluster_bootstrap(frame: pd.DataFrame, groups: str, stat: Callable[[pd.DataFrame], float], n_boot: int = 500, seed: int = 0, ci: float = 0.95) -> tuple[float, float, float]:
    """Any statistic of a frame with a CI from resampling whole groups. Slower; use for ratios, differences, etc."""
    estimate = float(stat(frame))
    keys = frame[groups].to_numpy()
    unique, inverse = np.unique(keys, return_inverse=True)
    if len(unique) < 2:
        return estimate, float("nan"), float("nan")
    members = [np.flatnonzero(inverse == i) for i in range(len(unique))]
    rng = np.random.default_rng(seed)
    boot = []
    for _ in range(n_boot):
        picks = rng.integers(0, len(unique), size=len(unique))
        rows = np.concatenate([members[p] for p in picks])
        boot.append(float(stat(frame.iloc[rows])))
    lo, hi = np.quantile(boot, [(1 - ci) / 2, 1 - (1 - ci) / 2])
    return estimate, float(lo), float(hi)


def mean_with_ci(frame: pd.DataFrame, column: str, by: Sequence[str] = (), groups: str = "game_uuid", n_boot: int = 1000) -> pd.DataFrame:
    """Mean of `column` (with CI and counts) for each combination of `by`."""
    rows = []
    for key, part in (frame.groupby(list(by), observed=True) if by else [((), frame)]):
        est, lo, hi = cluster_bootstrap_mean(part[column], part[groups], n_boot=n_boot)
        key = key if isinstance(key, tuple) else (key,)
        rows.append({**dict(zip(by, key)), "mean": est, "lo": lo, "hi": hi, "n": int(part[column].notna().sum()), "games": int(part[groups].nunique())})
    return pd.DataFrame(rows)


def compare_sides(moves: pd.DataFrame, column: str, by: Sequence[str] = (), n_boot: int = 1000) -> pd.DataFrame:
    """Player vs opponents for `column`, per `by` group, with CIs and the CI of the difference.

    The difference is bootstrapped by game, so it compares both sides within
    the same resampled games.
    """
    rows = []
    for key, part in (moves.groupby(list(by), observed=True) if by else [((), moves)]):
        key = key if isinstance(key, tuple) else (key,)
        mine = part[part["is_player_move"]]
        theirs = part[~part["is_player_move"]]
        p_est, p_lo, p_hi = cluster_bootstrap_mean(mine[column], mine["game_uuid"], n_boot=n_boot)
        o_est, o_lo, o_hi = cluster_bootstrap_mean(theirs[column], theirs["game_uuid"], n_boot=n_boot)
        d_est, d_lo, d_hi = _bootstrap_difference(part, column, n_boot=n_boot)
        rows.append(
            {
                **dict(zip(by, key)),
                "player": p_est, "player_lo": p_lo, "player_hi": p_hi,
                "opponent": o_est, "opponent_lo": o_lo, "opponent_hi": o_hi,
                "diff": d_est, "diff_lo": d_lo, "diff_hi": d_hi,
                "games": int(part["game_uuid"].nunique()), "n": int(part[column].notna().sum()),
            }  # fmt: skip
        )
    return pd.DataFrame(rows)


def _bootstrap_difference(part: pd.DataFrame, column: str, n_boot: int, seed: int = 0) -> tuple[float, float, float]:
    frame = pd.DataFrame({"v": part[column].astype("float64"), "g": part["game_uuid"].to_numpy(), "p": part["is_player_move"].to_numpy()}).dropna()
    agg = frame.groupby(["g", "p"])["v"].agg(["sum", "count"]).unstack("p").fillna(0)
    if agg.empty or True not in agg["sum"].columns or False not in agg["sum"].columns:
        return float("nan"), float("nan"), float("nan")
    ps, pc = agg["sum"][True].to_numpy(), agg["count"][True].to_numpy()
    os_, oc = agg["sum"][False].to_numpy(), agg["count"][False].to_numpy()
    est = ps.sum() / max(pc.sum(), 1) - os_.sum() / max(oc.sum(), 1)
    if len(agg) < 2:
        return float(est), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(agg), size=(n_boot, len(agg)))
    boot = ps[idx].sum(axis=1) / np.maximum(pc[idx].sum(axis=1), 1) - os_[idx].sum(axis=1) / np.maximum(oc[idx].sum(axis=1), 1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(est), float(lo), float(hi)


def boot_mean(values, n_boot: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    """Mean of one value per game (NaNs dropped) with a 95% CI from resampling the values (games)."""
    v = np.asarray(values, dtype="float64")
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return np.nan, np.nan, np.nan
    if len(v) < 2:
        return float(v.mean()), np.nan, np.nan
    rng = np.random.default_rng(seed)
    boot = v[rng.integers(0, len(v), size=(n_boot, len(v)))].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return float(v.mean()), float(lo), float(hi)


def boot_mean_diff(a, b, n_boot: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    """mean(a) - mean(b) for two disjoint sets of games (one value per game, NaNs dropped), each resampled independently; 95% CI."""
    a, b = (np.asarray(x, dtype="float64") for x in (a, b))
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    if len(a) < 2 or len(b) < 2:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    ba = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    bb = b[rng.integers(0, len(b), size=(n_boot, len(b)))].mean(axis=1)
    lo, hi = np.quantile(ba - bb, [0.025, 0.975])
    return float(a.mean() - b.mean()), float(lo), float(hi)


def ci_cols(name: str, values: Sequence[float]) -> dict:
    """{name: estimate, name_lo: lower, name_hi: upper} from (estimate, lower, upper, ...) (anything after the third is ignored)."""
    return {name: values[0], f"{name}_lo": values[1], f"{name}_hi": values[2]}


def binned(moves: pd.DataFrame, x: str, bins: Sequence[float], column: str, by: Sequence[str] = (), labels: Sequence[str] | None = None, n_boot: int = 500) -> pd.DataFrame:
    """`compare_sides` for `column` within bins of `x` (plus any extra `by` columns)."""
    frame = moves.copy()
    frame["bin"] = pd.cut(frame[x], bins=list(bins), labels=labels, include_lowest=True, right=False)
    frame = frame[frame["bin"].notna()]
    return compare_sides(frame, column, by=[*by, "bin"], n_boot=n_boot)


def elo_expected_score(rating: pd.Series, opponent_rating: pd.Series) -> pd.Series:
    """Standard Elo expectation of the player's score."""
    return 1 / (1 + 10 ** ((opponent_rating.astype("float64") - rating.astype("float64")) / 400))


def rating_band(rating: pd.Series, width: int = 100) -> pd.Series:
    """Rating rounded down to a band, e.g. 1050 -> 1000 (as an Int64 for grouping)."""
    return (rating.astype("float64") // width * width).astype("Int64")


def per_game(moves: pd.DataFrame, column: str, agg: str | Callable = "mean", side: str = "player") -> pd.Series:
    """Aggregate `column` per game for one side ("player", "opponent" or "both"); indexed by game_uuid."""
    part = moves if side == "both" else moves[moves["side"] == side]
    return part.groupby("game_uuid")[column].agg(agg)


# --- Elo-equivalents ------------------------------------------------------------


def elo_from_score(score) -> np.ndarray:
    """Elo difference that predicts `score` under the standard Elo curve (scores clipped to 0.001-0.999)."""
    s = np.clip(np.asarray(score, dtype="float64"), 0.001, 0.999)
    return 400 * np.log10(s / (1 - s))


def excess_to_elo(expected, excess) -> np.ndarray:
    """Elo-equivalent of scoring `excess` above an expected score of `expected`.

    elo(expected + excess) - elo(expected): the rating gap that would turn the
    expected score into the actual one. Apply it to an estimate and to each CI
    end separately (it is monotone in `excess`, so the CI maps exactly).
    """
    expected = np.asarray(expected, dtype="float64")
    return elo_from_score(expected + np.asarray(excess, dtype="float64")) - elo_from_score(expected)


def add_elo(frame: pd.DataFrame, expected: str, columns: Sequence[str], suffix: str = "_elo") -> pd.DataFrame:
    """Adds `<column>_elo` for each score-excess column (estimate and CI ends) given the expected-score column."""
    out = frame.copy()
    for column in columns:
        out[f"{column}{suffix}"] = excess_to_elo(out[expected], out[column])
    return out


# --- shared model helpers -----------------------------------------------------------


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


def ratio_bootstrap(numerator: pd.Series, denominator: pd.Series, groups: pd.Series, n_boot: int = 1000, seed: int = 0) -> tuple[float, float, float]:
    """Ratio of two sums with a CI from resampling whole groups (games), via per-group sums."""
    codes, inverse = np.unique(groups.to_numpy(), return_inverse=True)
    num = np.bincount(inverse, weights=numerator.to_numpy(dtype=float), minlength=len(codes))
    den = np.bincount(inverse, weights=denominator.to_numpy(dtype=float), minlength=len(codes))
    if len(codes) < 2:
        return float(num.sum() / den.sum()) if den.sum() else float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    w = np.stack([np.bincount(rng.integers(0, len(codes), len(codes)), minlength=len(codes)) for _ in range(n_boot)]).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        draws = (w @ num) / (w @ den)
    lo, hi = np.nanquantile(draws, [0.025, 0.975])
    return float(num.sum() / den.sum()), float(lo), float(hi)


def cluster_ols(X: np.ndarray, y: np.ndarray, groups: np.ndarray, n_boot: int = 500, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and bootstrap draws, resampling whole groups (games or sessions).

    Uses per-group sufficient statistics (X'X and X'y summed within each
    group), so each bootstrap replicate is a weighted sum followed by one
    small solve: fast for hundreds of thousands of rows. A column absent from
    a resample makes its coefficient NaN for that replicate.
    """
    codes, inverse = np.unique(groups, return_inverse=True)
    n_groups, k = len(codes), X.shape[1]
    xtx = np.empty((n_groups, k, k))
    for i in range(k):
        for j in range(i, k):
            col = np.bincount(inverse, weights=X[:, i] * X[:, j], minlength=n_groups)
            xtx[:, i, j] = col
            xtx[:, j, i] = col
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


def dummies(series: pd.Series, drop: str | None, prefix: str) -> pd.DataFrame:
    """One-hot columns named '<prefix>=<level>', without the reference level `drop`."""
    frame = pd.get_dummies(series.astype(str), prefix=prefix, prefix_sep="=", dtype=float)
    if drop is not None and f"{prefix}={drop}" in frame:
        frame = frame.drop(columns=f"{prefix}={drop}")
    return frame


# --- output: CSV tables and SVG charts -------------------------------------------------


def repo_root() -> Path:
    config = find_config()
    return config.parent if config else Path.cwd()


CSV_DECIMALS = 6


@dataclass
class Results:
    """Writes one question's tables (CSV) and charts (SVG) under presentation/.

    `Results("06_time_of_day")` clears presentation/data/06_time_of_day/ and
    presentation/figures/06_time_of_day/ (so renamed outputs never linger),
    then `table(name, frame, description)` writes data/<slug>/<name>.csv and
    `figure(fig, name)` writes figures/<slug>/<name>.svg. `finish()` writes
    data/<slug>/_index.csv: every table with its description, rows and
    columns. Floats are rounded to `CSV_DECIMALS` places so reruns are
    byte-identical.
    """

    slug: str
    root: Path | None = None
    tables: list[dict] = field(default_factory=list)
    figures: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        base = self.root or repo_root() / "presentation"
        self.data_dir = base / "data" / self.slug
        self.figures_dir = base / "figures" / self.slug
        for directory in (self.data_dir, self.figures_dir):
            if directory.exists():
                for old in directory.iterdir():
                    if old.is_file():
                        old.unlink()
            directory.mkdir(parents=True, exist_ok=True)

    def table(self, name: str, frame: pd.DataFrame, description: str) -> Path:
        out = frame.reset_index(drop=True).copy()
        for column in out.columns:
            if pd.api.types.is_float_dtype(out[column]):
                out[column] = out[column].round(CSV_DECIMALS)
            elif isinstance(out[column].dtype, pd.CategoricalDtype):
                out[column] = out[column].astype(str)
        path = self.data_dir / f"{name}.csv"
        out.to_csv(path, index=False, lineterminator="\n")
        self.tables.append({"table": f"{name}.csv", "description": description, "rows": len(out), "columns": " ".join(map(str, out.columns))})
        return path

    def figure(self, fig, name: str) -> Path:
        import matplotlib.pyplot as plt

        path = self.figures_dir / f"{name}.svg"
        fig.savefig(path, format="svg", bbox_inches="tight", metadata={"Date": None})
        plt.close(fig)
        self.figures.append(path.name)
        return path

    def finish(self) -> Path:
        index = pd.DataFrame(self.tables, columns=["table", "description", "rows", "columns"])
        path = self.data_dir / "_index.csv"
        index.to_csv(path, index=False, lineterminator="\n")
        print(f"{self.slug}: {len(self.tables)} tables in {self.data_dir}, {len(self.figures)} charts in {self.figures_dir}")
        return path
