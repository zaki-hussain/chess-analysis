"""Shared helpers for the analysis scripts under analysis/.

Conventions every analysis follows:
- load the data once with `load_data()`; it joins the per-game view onto
  the moves so filters like time class, rating band or session are always
  to hand;
- report bullet, blitz and rapid separately (`TIME_CLASSES`);
- every estimate carries a confidence interval, bootstrapped by *game*,
  because moves within a game are not independent;
- every number about the player is paired with the same number for the
  opponents in the same games (`compare_sides`);
- `ep_loss` is the main move-quality measure, `cp_loss` the secondary one;
- results are written with `Report` to reports/<nn>_<slug>.md.
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
    """

    moves: pd.DataFrame
    games: pd.DataFrame
    username: str

    @property
    def mine(self) -> pd.DataFrame:
        return self.moves[self.moves["is_player_move"]]

    @property
    def theirs(self) -> pd.DataFrame:
        return self.moves[~self.moves["is_player_move"]]


def load_data(username: str | None = None, include_daily: bool = False, rated_only: bool = False) -> Data:
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

    moves = load_analysed(username)
    moves = moves[moves["game_uuid"].isin(games["uuid"])]
    moves = moves.merge(games[["uuid", *GAME_COLUMNS]].rename(columns={"uuid": "game_uuid"}), on="game_uuid", how="left")
    moves["side"] = np.where(moves["is_player_move"], "player", "opponent")
    return Data(moves=moves.reset_index(drop=True), games=games, username=settings.username)


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


# --- reporting ---------------------------------------------------------------


def repo_root() -> Path:
    config = find_config()
    return config.parent if config else Path.cwd()


def format_table(frame: pd.DataFrame, floatfmt: str = ".3f", index: bool = False) -> str:
    """A DataFrame as a GitHub-flavoured markdown table."""
    frame = frame.reset_index() if index else frame
    columns = list(frame.columns)

    def cell(value) -> str:
        if isinstance(value, (float, np.floating)):
            return "" if np.isnan(value) else format(value, floatfmt)
        if value is pd.NA or value is None:
            return ""
        return str(value)

    lines = ["| " + " | ".join(map(str, columns)) + " |", "|" + "---|" * len(columns)]
    for row in frame.itertuples(index=False):
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def ci_column(frame: pd.DataFrame, mean: str, lo: str, hi: str, fmt: str = ".3f") -> pd.Series:
    """'0.031 [0.029, 0.033]' strings for compact tables."""
    return frame.apply(lambda r: f"{r[mean]:{fmt}} [{r[lo]:{fmt}}, {r[hi]:{fmt}}]" if pd.notna(r[lo]) else f"{r[mean]:{fmt}}", axis=1)


@dataclass
class Report:
    """Builds a markdown report with optional figures, written under reports/."""

    slug: str
    title: str
    parts: list[str] = field(default_factory=list)
    figures_dir: Path | None = None

    def heading(self, text: str, level: int = 2) -> "Report":
        self.parts.append(f"{'#' * level} {text}\n")
        return self

    def text(self, text: str) -> "Report":
        self.parts.append(text.strip() + "\n")
        return self

    def table(self, frame: pd.DataFrame, floatfmt: str = ".3f", index: bool = False) -> "Report":
        self.parts.append(format_table(frame, floatfmt, index) + "\n")
        return self

    def figure(self, fig, name: str, caption: str = "") -> "Report":
        """Save a matplotlib figure under reports/figures/<slug>/ and embed it."""
        directory = self.figures_dir or repo_root() / "reports" / "figures" / self.slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.png"
        fig.savefig(path, dpi=130, bbox_inches="tight")
        relative = Path("figures") / self.slug / path.name
        self.parts.append(f"![{caption or name}]({relative.as_posix()})\n" + (f"\n*{caption}*\n" if caption else ""))
        return self

    def render(self) -> str:
        return f"# {self.title}\n\n" + "\n".join(self.parts)

    def write(self, path: Path | None = None) -> Path:
        path = path or repo_root() / "reports" / f"{self.slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


def figure_style() -> None:
    """A quiet, consistent look for the report figures."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "figure.figsize": (7, 4),
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "axes.titleweight": "bold",
            "font.size": 10,
            "legend.frameon": False,
        }
    )


SIDE_COLOURS = {"player": "#1f6f8b", "opponent": "#c46a2d"}
