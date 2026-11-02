"""Q2: Rating trajectory, what changed as you improved (plus Q11: performance vs opponent rating and upsets).

Question: how did your rating move, and as it rose, what changed in your play
compared with the opponents you were matched with? And how do you score
against stronger and weaker players, and how do upsets happen?

Computes, per exact time control (`data.tc_groups`):
- the rating trajectory per rating pool (pools are per time class on
  chess.com, so this part is per class: bullet, blitz, rapid), by year, with
  first / last / low / peak milestones and the time controls played each year;
- per-game metrics from Q1's decomposition (`games_decomposed.parquet`) plus
  clock and phase metrics computed here, for you and for the opponents in the
  same games: by 100-point band of your pre-game rating, by low / mid / high
  third of your games by rating, and the change from the low to the high third
  (yours, the field's and the difference, all on the same resampled games);
  the same change without games played after a break (rating reliability);
- a regression of your pre-game rating on your per-game habits (bootstrap
  clustered by calendar month);
- your score vs the Elo expectation by rating-difference bin (rated games),
  with Elo-equivalents, pooled by time class as well; checks: split by your
  rating (median of each bin's games) and by new ground vs recovery from an
  earlier peak, unrated games (exact opponent rating), rating change per
  win / loss;
- how upset wins and losses ended, next to ordinary wins and losses.

Every estimate has a 95% CI from a bootstrap over games (both sides on the same
resampled games, so differences are paired).

Run: .venv/bin/python analysis/02_trajectory.py  (about 20 seconds). Writes CSVs to
presentation/data/02_trajectory/ and SVGs to presentation/figures/02_trajectory/.
"""

from __future__ import annotations

import time
import zlib
from collections.abc import Iterator

import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis.clocks import LOW_CLOCK_SECONDS  # "under 10 seconds": the plain-seconds low-clock line readers think in
from chess_analysis.toolkit import (
    TIME_CLASSES,
    Results,
    add_elo,
    cluster_ols,
    dummies,
    elo_expected_score,
    load_data,
    opponent_rating_pre,
    rating_band,
    repo_root,
)

SLUG = "02_trajectory"
SEED = 2
N_BOOT = 400  # game-bootstrap resamples per cell: the tables have hundreds of cells, and 400 pins a 95% CI end to about +-5% of its width
MIN_CELL_GAMES = 30  # a cell with fewer games is flagged thin: a score from 30 games has a 95% CI of about +-0.18, too wide to compare cells
MIN_CELL_CASES = 20  # conditional rates (share of losses on time, conversion, swindles) need this many cases per side, for the same reason
MIN_REGRESSION_GAMES = 200  # the rating regression has ~13 standardised habits plus year dummies: below ~15 games per parameter it is noise
MIN_REGRESSION_MONTHS = 20  # its bootstrap resamples calendar months; with fewer clusters the CI ends are unreliable
MIN_HABIT_GAMES = MIN_CELL_CASES  # a habit that departs from its usual value in fewer games (e.g. moves under 10 s in rapid) is left out: its coefficient explodes in resamples
BAND = 100  # rating-band width: a 100-point band is roughly a dozen net wins at chess.com's ~8 points per game
BREAK_DAYS = 7  # a gap of more than a week in a rating pool makes the rating a weaker measure of current strength
LOW_CLOCK_BASE_FRACTION = 0.1  # the same idea relative to the time control: under 10% of the base time (6 s in 1+0, 60 s in 10+0)
LOW_ALIVE = 0.1  # Q1's "lost while still alive": the loser's last calibrated P(win) was at least 10%
UPSET_GAP = 100  # an upset: a win against an opponent at least 100 points stronger, or a loss to one at least 100 weaker (Elo: 36% / 64% expected)
EVEN_GAP = 25  # "ordinary" wins and losses for comparison: opponents within 25 points (Elo expectation 46-54%)
# Rating difference (opponent minus you, pre-game) bins: symmetric, narrow near 0 where most games are (the pairing is tight).
RATING_DIFF_EDGES = [-np.inf, -100, -50, -25, 0, 25, 50, 100, np.inf]
RATING_DIFF_LABELS = ["<= -100", "-100..-50", "-50..-25", "-25..0", "0..25", "25..50", "50..100", "> 100"]
EVEN_BIN = "0..25"  # reference bin of the rating-change check
# Unrated games are paired loosely (median gap ~100 points), so they get coarser bins.
UNRATED_EDGES = [-np.inf, -100, 0, 100, np.inf]
UNRATED_LABELS = ["<= -100", "-100..0", "0..100", "> 100"]
LEVEL_SPLIT_QUANTILE = 0.5  # the level check splits each gap bin's own games at their median pre-game rating in the class: equal halves, so neither is thin by construction
PEAK_GAP = BAND  # the climb-vs-recovery check: "near your peak so far" = within one band (100 points) of your highest earlier rating in the pool, else "recovering"
LINE_GAP_DAYS = 30  # the rating line is broken where a pool had no games for more than a month, so a pause does not look like continuous play
THIRDS = ["low", "mid", "high"]
PHASES = ["opening", "middlegame", "endgame"]
ENDINGS = ["timeout", "resigned", "checkmated", "abandoned"]  # every way a game can be lost on chess.com (the other results are draws)
UPSET_CATEGORIES = ["upset wins", "ordinary wins", "upset losses", "ordinary losses"]
ALL_GAMES = "all games"


# ---------------------------------------------------------------------------------------
# bootstrap through weight matrices: every quantity of a cell is computed on the same
# resampled games, so you, the opponents and their difference are paired
# ---------------------------------------------------------------------------------------


def boot_weights(part: pd.DataFrame, n_boot: int = N_BOOT) -> np.ndarray:
    """Multinomial resampling weights for the games of `part`: shape (n_boot, games).

    Seeded by the set of games itself, so the same games always get the same
    resamples: a control that is its whole time class ('rapid 10+0' and
    'all rapid') gets identical CIs in both rows, and reruns are identical.
    """
    n = len(part)
    rng = np.random.default_rng([SEED, zlib.crc32("".join(sorted(part["uuid"])).encode())])
    return rng.multinomial(n, np.full(n, 1.0 / n), size=n_boot).astype(float)


def ratio_draws(num: np.ndarray, den: np.ndarray, w: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return (w @ num) / (w @ den)


def summary(est: float, draws: np.ndarray) -> tuple[float, float, float]:
    if np.isnan(est) or np.all(np.isnan(draws)):
        return float(est), float("nan"), float("nan")
    lo, hi = np.nanquantile(draws, [0.025, 0.975])
    return float(est), float(lo), float(hi)


def ratio_ci(num, den, w: np.ndarray) -> tuple[float, float, float]:
    num, den = np.nan_to_num(np.asarray(num, dtype=float)), np.nan_to_num(np.asarray(den, dtype=float))
    if den.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    return summary(num.sum() / den.sum(), ratio_draws(num, den, w))


def mean_ci(values, w: np.ndarray) -> tuple[float, float, float]:
    values = np.asarray(values, dtype=float)
    return ratio_ci(values, np.ones_like(values), w)


# ---------------------------------------------------------------------------------------
# per-game table
# ---------------------------------------------------------------------------------------


def build_games(data) -> pd.DataFrame:
    """Q1's decomposition joined onto the games, plus clock, phase-count and result metrics per side."""
    games = data.games[data.games["tc_group"].isin(data.tc_groups)]
    games = games.merge(pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet"), on="uuid", how="left")
    games = games[games["n_plies"].notna() & games["my_rating_before"].notna()].copy()  # games without a move; the first game in each pool has no pre-game rating
    zero = (games["my_rating_before"] <= 0) | (games["opponent_rating_after"] <= 0)
    games = games[~zero].copy()  # a rating of 0 is a placeholder, not a rating
    moves = data.moves[data.moves["game_uuid"].isin(games["uuid"])]

    counts = moves.groupby(["game_uuid", "side", "phase"], observed=True).size().unstack(["side", "phase"], fill_value=0)
    per_side = (
        moves.assign(
            low_clock=(moves["clock_before_seconds"] < LOW_CLOCK_SECONDS).astype(float),
            low_buffer=(moves["clock_buffer_seconds"] < LOW_CLOCK_SECONDS).astype(float),
            low_base=(moves["clock_before_seconds"] < LOW_CLOCK_BASE_FRACTION * moves["base_seconds"]).astype(float),
            spent_frac=moves["time_spent_seconds"] / moves["base_seconds"],
        )
        .groupby(["game_uuid", "side"])
        .agg(time_spent=("time_spent_seconds", "sum"), spent_frac=("spent_frac", "sum"), low_clock=("low_clock", "sum"), low_buffer=("low_buffer", "sum"), low_base=("low_base", "sum"))
        .unstack("side")
    )
    last = moves.sort_values("ply").groupby(["game_uuid", "side"]).tail(1).set_index(["game_uuid", "side"])
    end_frac = (last["clock_seconds"] / last["base_seconds"]).unstack("side")

    for side, prefix in (("player", "my"), ("opponent", "opp")):
        for phase in PHASES:
            col = (side, phase)
            games[f"{prefix}_moves_{phase}"] = games["uuid"].map(counts[col] if col in counts else pd.Series(dtype=float)).fillna(0).astype(float)
        for name in ("time_spent", "spent_frac", "low_clock", "low_buffer", "low_base"):
            games[f"{prefix}_{name}"] = games["uuid"].map(per_side[(name, side)]).astype(float)
        games[f"{prefix}_end_clock_frac"] = games["uuid"].map(end_frac[side]).astype(float)

    for col in games.columns:
        if str(games[col].dtype) in ("Int64", "boolean"):
            games[col] = games[col].astype(float)

    games["tc_group"] = games["tc_group"].astype(str)
    games["band"] = rating_band(games["my_rating_before"], BAND).astype(float)
    games["opp_rating_pre"] = opponent_rating_pre(games)
    games["rating_diff"] = games["opp_rating_pre"] - games["my_rating_before"].astype(float)
    games["elo_expected"] = elo_expected_score(games["my_rating_before"], games["opp_rating_pre"])
    games["diff_bin"] = pd.cut(games["rating_diff"], RATING_DIFF_EDGES, labels=RATING_DIFF_LABELS, right=True)
    games["after_break"] = games["days_since_last_in_pool"] > BREAK_DAYS
    games["month"] = games["start_datetime"].dt.strftime("%Y-%m")
    games["win"] = (games["outcome"] == "win").astype(float)
    games["loss"] = (games["outcome"] == "loss").astype(float)
    games["draw"] = (games["outcome"] == "draw").astype(float)
    games["my_losses"] = games["loss"]
    games["opp_losses"] = games["win"]
    games["my_flag_loss"] = (games["my_result"] == "timeout").astype(float)
    games["opp_flag_loss"] = (games["opponent_result"] == "timeout").astype(float)
    games["my_lost_alive"] = ((games["loss"] == 1) & (games["loser_last_p_win"] >= LOW_ALIVE)).astype(float)
    games["opp_lost_alive"] = ((games["win"] == 1) & (games["loser_last_p_win"] >= LOW_ALIVE)).astype(float)
    games["my_reached_winning"] = games["reached_winning"]
    games["my_reached_losing"] = games["reached_losing"]
    games["my_converted"] = games["reached_winning"] * games["win"]
    games["opp_converted"] = games["opp_reached_winning"] * games["loss"]
    games["my_swindled"] = games["reached_losing"] * (1 - games["loss"])
    games["opp_swindled"] = games["opp_reached_losing"] * (1 - games["win"])
    games["my_spoiled"] = games["reached_winning"] * (1 - games["win"])
    games["one"] = 1.0
    games["thirds"] = thirds(games)
    games = games.sort_values(["start_datetime", "uuid"]).reset_index(drop=True)
    games["points_below_peak"] = games.groupby("time_class")["my_rating_before"].cummax() - games["my_rating_before"]  # 0 at a new high in the pool
    return games


def thirds(games: pd.DataFrame) -> pd.Series:
    """Low / mid / high third of games by pre-game rating, within each time-control group."""
    out = pd.Series(index=games.index, dtype=object)
    for _, part in games.groupby("tc_group"):
        ranks = part["my_rating_before"].rank(method="first")
        out.loc[part.index] = pd.qcut(ranks, 3, labels=THIRDS).astype(str)
    return out


# ---------------------------------------------------------------------------------------
# metric registry: every metric is a ratio of per-game sums, per side (my_ / opp_)
# ---------------------------------------------------------------------------------------


def metric(key: str, label: str, num: str, den: str, unit: str, scale: float = 1.0, better: str = "lower", game_level: bool = False) -> dict:
    """`unit` is what the denominator counts: 'moves', 'games' or 'cases' (conditional rates, thin below MIN_CELL_CASES)."""
    return {"key": key, "label": label, "num": num, "den": den, "unit": unit, "scale": scale, "better": better, "game_level": game_level}


QUALITY = [
    metric("ep_move", "expected points lost per move", "{p}_ep_lost", "{p}_moves", "moves"),
    metric("cp_move", "centipawns lost per move", "{p}_cp_lost", "{p}_moves", "moves"),
    metric("err20", "moves losing >= 0.2 expected points, per 100 moves", "{p}_errors_ge_20", "{p}_moves", "moves", 100),
    metric("blunders", "blunders (>= 300 centipawns), per 100 moves", "{p}_blunders", "{p}_moves", "moves", 100),
    metric("max_ep", "largest single expected-points loss in the game", "{p}_max_ep_loss", "one", "games"),
    metric("ep_opening", "opening: expected points lost per move", "{p}_ep_lost_opening", "{p}_moves_opening", "moves"),
    metric("ep_middlegame", "middlegame: expected points lost per move", "{p}_ep_lost_middlegame", "{p}_moves_middlegame", "moves"),
    metric("ep_endgame", "endgame: expected points lost per move", "{p}_ep_lost_endgame", "{p}_moves_endgame", "moves"),
]
SPEED = [
    metric("time_move", "seconds spent per move", "{p}_time_spent", "{p}_moves", "moves", better="n/a"),
    metric("spent_frac", "share of the base time spent per move", "{p}_spent_frac", "{p}_moves", "moves", better="n/a"),
    metric("end_clock", "clock left after own last move, share of base time", "{p}_end_clock_frac", "one", "games", better="higher"),
    metric("low_clock", f"share of own moves played with under {LOW_CLOCK_SECONDS:g} s on the clock", "{p}_low_clock", "{p}_moves", "moves"),
    metric("low_buffer", f"share of own moves played with under {LOW_CLOCK_SECONDS:g} s beyond one increment", "{p}_low_buffer", "{p}_moves", "moves"),
    metric("low_base", f"share of own moves played with under {LOW_CLOCK_BASE_FRACTION:.0%} of the base time on the clock", "{p}_low_base", "{p}_moves", "moves"),
]
RESULTS = [
    metric("flag_loss", "share of losses on time", "{p}_flag_loss", "{p}_losses", "cases"),
    metric("lost_alive", f"share of losses while still alive (loser's P(win) >= {LOW_ALIVE:g})", "{p}_lost_alive", "{p}_losses", "cases"),
    metric("reached_win", "share of games reaching P(win) >= 0.8", "{p}_reached_winning", "one", "games", better="higher"),
    metric("conversion", "conversion: share of games won after reaching P(win) >= 0.8", "{p}_converted", "{p}_reached_winning", "cases", better="higher"),
    metric("swindle", "swindles: share of games not lost after falling to P(win) <= 0.2", "{p}_swindled", "{p}_reached_losing", "cases", better="higher"),
]
SHAPE = [
    metric("plies", "game length (plies)", "n_plies", "one", "games", better="n/a", game_level=True),
    metric("book_ply", "book depth (plies in a named opening line)", "book_ply", "one", "games", better="higher", game_level=True),
]
METRICS = [*QUALITY, *SPEED, *RESULTS, *SHAPE]
METRIC_BY_KEY = {m["key"]: m for m in METRICS}


def side_arrays(frame: pd.DataFrame, m: dict, prefix: str) -> tuple[np.ndarray, np.ndarray]:
    """Numerator and denominator per game; a game with a missing value drops out of the metric."""
    num = frame[m["num"].format(p=prefix)].to_numpy(dtype=float) * m["scale"]
    den = frame[m["den"].format(p=prefix)].to_numpy(dtype=float)
    missing = np.isnan(num) | np.isnan(den)
    return np.where(missing, 0.0, num), np.where(missing, 0.0, den)


def is_thin(games: int, n_player: float, n_opponent: float, m: dict) -> bool:
    if games < MIN_CELL_GAMES:
        return True
    if m["unit"] == "cases":
        return min(n_player, n_opponent if not m["game_level"] else n_player) < MIN_CELL_CASES
    return False


def cell(frame: pd.DataFrame, m: dict, w: np.ndarray) -> dict:
    """You, the opponents and the paired difference for one metric on one set of games."""
    my = side_arrays(frame, m, "my")
    out = {}
    out["player"], out["player_lo"], out["player_hi"] = ratio_ci(*my, w)
    if m["game_level"]:
        out.update(opponent=np.nan, opponent_lo=np.nan, opponent_hi=np.nan, diff=np.nan, diff_lo=np.nan, diff_hi=np.nan)
        n_opp = np.nan
    else:
        op = side_arrays(frame, m, "opp")
        out["opponent"], out["opponent_lo"], out["opponent_hi"] = ratio_ci(*op, w)
        if my[1].sum() and op[1].sum():
            est = my[0].sum() / my[1].sum() - op[0].sum() / op[1].sum()
            out["diff"], out["diff_lo"], out["diff_hi"] = summary(est, ratio_draws(*my, w) - ratio_draws(*op, w))
        else:
            out.update(diff=np.nan, diff_lo=np.nan, diff_hi=np.nan)
        n_opp = float(op[1].sum())
    out["games"] = len(frame)
    out["n_player"] = float(my[1].sum())
    out["n_opponent"] = n_opp
    out["thin"] = is_thin(len(frame), out["n_player"], n_opp, m)
    return out


def metric_columns(m: dict) -> dict:
    return {"metric": m["key"], "label": m["label"], "unit": m["unit"], "better": m["better"]}


# ---------------------------------------------------------------------------------------
# 2.1 the trajectory (per rating pool = time class)
# ---------------------------------------------------------------------------------------


def trajectory_tables(games: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    classes = [c for c in TIME_CLASSES if c in set(games["time_class"])]
    year_rows = []
    for c in classes:
        part_c = games[games["time_class"] == c]
        for year, part in part_c.groupby("year"):
            w = boot_weights(part)
            score = mean_ci(part["my_score"], w)
            excess = mean_ci(part["my_score"] - part["elo_expected"], w)
            main = part["tc_group"].value_counts().sort_index().sort_values(ascending=False, kind="stable")
            year_rows.append({
                "time_class": c, "year": int(year),
                "rating_mean": part["my_rating_before"].mean(), "rating_min": part["my_rating_before"].min(), "rating_max": part["my_rating_before"].max(),
                "rating_first": part["my_rating_before"].iloc[0], "rating_last": part["my_rating_before"].iloc[-1],
                "score": score[0], "score_lo": score[1], "score_hi": score[2],
                "expected": part["elo_expected"].mean(), "excess": excess[0], "excess_lo": excess[1], "excess_hi": excess[2],
                "main_tc_group": main.index[0], "main_tc_share": main.iloc[0] / len(part),
                "games": len(part), "rated_share": part["rated"].mean(), "games_after_break": int(part["after_break"].sum()),
                "thin": len(part) < MIN_CELL_GAMES,
            })  # fmt: skip
    by_year = add_elo(pd.DataFrame(year_rows), "expected", ["excess", "excess_lo", "excess_hi"])

    milestone_rows = []
    for c in classes:
        part = games[games["time_class"] == c]
        picks = {"first": part.index[0], "last": part.index[-1], "low": part["my_rating_before"].idxmin(), "peak": part["my_rating_before"].idxmax()}
        for event, i in picks.items():
            milestone_rows.append({"time_class": c, "event": event, "rating": part.loc[i, "my_rating_before"], "month": part.loc[i, "month"], "games_in_pool": len(part)})
    milestones = pd.DataFrame(milestone_rows)
    gain = milestones.pivot(index="time_class", columns="event", values="rating")
    milestones["change_first_to_last"] = milestones["time_class"].map(gain["last"] - gain["first"])
    milestones["fall_from_peak"] = milestones["time_class"].map(gain["peak"] - gain["last"])
    milestones["thin"] = milestones["games_in_pool"] < MIN_CELL_GAMES

    counts = games.groupby(["tc_group", "time_class", "year"]).size().rename("games").reset_index()
    class_year = games.groupby(["time_class", "year"]).size()
    counts["share_of_class_year"] = [n / class_year[(c, y)] for n, c, y in zip(counts["games"], counts["time_class"], counts["year"])]
    counts = counts[["tc_group", "time_class", "year", "games", "share_of_class_year"]]
    counts["thin"] = counts["games"] < MIN_CELL_GAMES
    return by_year, milestones, counts


def chart_trajectory(games: pd.DataFrame, by_year: pd.DataFrame):
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    classes = list(dict.fromkeys(by_year["time_class"]))
    fig, axes = plt.subplots(1, len(classes), figsize=(4.2 * len(classes), 3.4), sharey=True, squeeze=False)
    for ax, c in zip(axes[0], classes):
        part = games[games["time_class"] == c]
        x, y = part["start_datetime"].to_numpy(), part["my_rating_before"].to_numpy(dtype=float)
        pause = np.flatnonzero(np.diff(x) > np.timedelta64(LINE_GAP_DAYS, "D")) + 1
        x = np.insert(x, pause, x[pause - 1] + (x[pause] - x[pause - 1]) / 2)  # a NaN point inside each pause breaks the line
        y = np.insert(y, pause, np.nan)
        ax.plot(x, y, color=charts.PLAYER, lw=0.7, alpha=0.55, label="your pre-game rating, game by game")
        yr = by_year[(by_year["time_class"] == c) & ~by_year["thin"]]
        mid = pd.to_datetime(yr["year"].astype(str) + "-07-01")
        ax.errorbar(mid, yr["rating_mean"], yerr=[yr["rating_mean"] - yr["rating_min"], yr["rating_max"] - yr["rating_mean"]], fmt="o", color=charts.INK, ecolor=charts.INK_MUTED, elinewidth=1, markersize=5, label="yearly mean (bar: range)", zorder=4)
        for x, y in zip(mid, yr["rating_mean"]):
            ax.annotate(f"{y:.0f}", (x, y), xytext=(5, 4), textcoords="offset points", fontsize=7.5, color=charts.INK_SECONDARY)
        ax.set_title(f"{c} ({len(part):,} games)")
        ax.xaxis.set_major_locator(mdates.YearLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axes[0][0].set_ylabel("rating before the game")
    charts.legend(fig, loc="upper center", bbox_to_anchor=(0.5, 1.08))
    fig.tight_layout()
    charts.footnote(fig, f"Rating pools are per time class on chess.com, so all controls of a class share one line. The line breaks where the pool had no games for over {LINE_GAP_DAYS} days. Years with fewer than {MIN_CELL_GAMES} games have no marker.")
    return fig


# ---------------------------------------------------------------------------------------
# 2.2-2.4 metrics by rating band, by third, and the change from the low to the high third
# ---------------------------------------------------------------------------------------


def by_level(games: pd.DataFrame, groups: list[str], level: str, metrics: list[dict]) -> pd.DataFrame:
    """Long table: tc_group x level (band or third) x metric -> you / opponents / difference with CIs."""
    rows = []
    for g in groups:
        part_g = games[games["tc_group"] == g]
        for key, part in part_g.groupby(level, sort=True):
            w = boot_weights(part)
            for m in metrics:
                rows.append({"tc_group": g, **metric_columns(m), level: key, "rating_min": part["my_rating_before"].min(), "rating_max": part["my_rating_before"].max(), **cell(part, m, w)})
    out = pd.DataFrame(rows)
    out[level] = pd.Categorical(out[level], THIRDS, ordered=True) if level == "thirds" else out[level].astype(int)
    rank = {**{g: i for i, g in enumerate(groups)}, **{m["key"]: i for i, m in enumerate(METRICS)}}
    out = out.sort_values(["tc_group", "metric", level], key=lambda s: s.map(rank) if s.name in ("tc_group", "metric") else s, kind="stable")
    return out.rename(columns={"thirds": "third"}).reset_index(drop=True)


def change_table(games: pd.DataFrame, groups: list[str], metrics: list[dict]) -> pd.DataFrame:
    """High third minus low third of your games by rating: your change, the field's (opponents in the same games), and the difference."""
    rows = []
    for g in groups:
        part = games[games["tc_group"] == g]
        lo_part, hi_part = part[part["thirds"] == "low"], part[part["thirds"] == "high"]
        if lo_part.empty or hi_part.empty:
            continue
        w_lo = boot_weights(lo_part)
        w_hi = boot_weights(hi_part)
        for m in metrics:
            row = {
                "tc_group": g, **metric_columns(m),
                "rating_low_min": lo_part["my_rating_before"].min(), "rating_low_max": lo_part["my_rating_before"].max(),
                "rating_high_min": hi_part["my_rating_before"].min(), "rating_high_max": hi_part["my_rating_before"].max(),
            }  # fmt: skip
            sides = ("my",) if m["game_level"] else ("my", "opp")
            draws = {}
            counts = []
            for prefix, name in zip(sides, ("player", "opponent")):
                a_lo, a_hi = side_arrays(lo_part, m, prefix), side_arrays(hi_part, m, prefix)
                low = a_lo[0].sum() / a_lo[1].sum() if a_lo[1].sum() else np.nan
                high = a_hi[0].sum() / a_hi[1].sum() if a_hi[1].sum() else np.nan
                draws[name] = ratio_draws(*a_hi, w_hi) - ratio_draws(*a_lo, w_lo)
                row[f"{name}_low"], row[f"{name}_high"] = low, high
                row[f"{name}_change"], row[f"{name}_change_lo"], row[f"{name}_change_hi"] = summary(high - low, draws[name])
                counts += [a_lo[1].sum(), a_hi[1].sum()]
            if m["game_level"]:
                for k in ("opponent_low", "opponent_high", "opponent_change", "opponent_change_lo", "opponent_change_hi", "diff_change", "diff_change_lo", "diff_change_hi"):
                    row[k] = np.nan
            else:
                row["diff_change"], row["diff_change_lo"], row["diff_change_hi"] = summary(row["player_change"] - row["opponent_change"], draws["player"] - draws["opponent"])
            row["games_low"], row["games_high"] = len(lo_part), len(hi_part)
            row["n_min"] = float(min(counts))
            row["thin"] = min(len(lo_part), len(hi_part)) < MIN_CELL_GAMES or (m["unit"] == "cases" and row["n_min"] < MIN_CELL_CASES)
            rows.append(row)
    return pd.DataFrame(rows)


def breaks_sensitivity(games: pd.DataFrame, groups: list[str], change: pd.DataFrame) -> pd.DataFrame:
    """The low-to-high change recomputed without games played after a break in the pool."""
    kept = games[~games["after_break"]].copy()
    kept["thirds"] = thirds(kept)
    alt = change_table(kept, groups, METRICS)
    cols = ["tc_group", "metric", "label", "player_change", "player_change_lo", "player_change_hi", "diff_change", "diff_change_lo", "diff_change_hi", "games_low", "games_high", "thin"]
    merged = change[cols].merge(alt[cols], on=["tc_group", "metric", "label"], suffixes=("_all", "_no_breaks"))
    merged["games_after_break"] = merged["tc_group"].map(games[games["after_break"]].groupby("tc_group").size()).fillna(0).astype(int)
    excl = lambda lo, hi: (lo > 0) | (hi < 0)  # noqa: E731
    merged["player_change_excludes_zero_all"] = excl(merged["player_change_lo_all"], merged["player_change_hi_all"])
    merged["player_change_excludes_zero_no_breaks"] = excl(merged["player_change_lo_no_breaks"], merged["player_change_hi_no_breaks"])
    merged["same_sign"] = np.sign(merged["player_change_all"]) == np.sign(merged["player_change_no_breaks"])
    return merged


def chart_band_lines(band: pd.DataFrame, groups: list[str], key: str, ylabel: str, title: str, sharey) -> object:
    fig, axes = charts.group_grid(groups, sharey=sharey, title=title)
    for g, ax in axes.items():
        part = band[(band["tc_group"] == g) & (band["metric"] == key)].copy()
        if (~part["thin"]).sum() < 2:
            charts.thin_note(ax, "too few games per band")
            continue
        for col in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"):
            part.loc[part["thin"], col] = np.nan
        grid = np.arange(part["band"].min(), part["band"].max() + BAND, BAND)
        part = part.set_index("band").reindex(grid).rename_axis("band").reset_index()  # a thin or empty band is NaN: the line breaks there
        part["x"] = part["band"] + BAND / 2
        charts.side_lines(ax, part, "x", markers=True)
        for side in ("player", "opponent"):  # a band with no drawn neighbour has no CI shading: give it whiskers instead
            v = part[side].notna().to_numpy()
            alone = v & ~np.r_[False, v[:-1]] & ~np.r_[v[1:], False]
            iso = part[alone]
            ax.errorbar(iso["x"], iso[side], yerr=[iso[side] - iso[f"{side}_lo"], iso[f"{side}_hi"] - iso[side]], fmt="none", ecolor=charts.SIDE_COLOURS[side], elinewidth=1.2, alpha=0.6)
        ax.set_xlabel("your rating before the game", fontsize=7.5)
    for g in groups:
        if axes[g].get_subplotspec().colspan.start == 0:
            axes[g].set_ylabel(ylabel)
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    charts.footnote(fig, f"Bands of {BAND} rating points; bands with fewer than {MIN_CELL_GAMES} games are not drawn (the line breaks). Shaded: 95% CI, bootstrapped by game.")
    return fig


def chart_thirds_bars(thirds_table: pd.DataFrame, groups: list[str], key: str, ylabel: str, title: str) -> object:
    fig, axes = charts.group_grid(groups, sharey=True, title=title)
    for g, ax in axes.items():
        part = thirds_table[(thirds_table["tc_group"] == g) & (thirds_table["metric"] == key)].copy()
        if part.empty or part["thin"].all():
            charts.thin_note(ax)
            continue
        for col in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"):
            part.loc[part["thin"], col] = np.nan
        charts.side_bars(ax, part, "third", order=THIRDS)
        shown = part.set_index("third").reindex(THIRDS)
        for k, side in enumerate(("player", "opponent")):
            for i, v in enumerate(shown[side].to_numpy(dtype=float)):
                if v == 0:  # a zero-height bar draws nothing: say it is zero, not missing
                    ax.text(i + (k - 0.5) * 0.38, 0, "0", ha="center", va="bottom", fontsize=7.5, color=charts.SIDE_COLOURS[side])
        if part["thin"].any():
            ax.text(0.98, 0.98, "thin thirds left out", transform=ax.transAxes, ha="right", va="top", fontsize=7, color=charts.INK_MUTED)
    for g in groups:
        if axes[g].get_subplotspec().colspan.start == 0:
            axes[g].set_ylabel(ylabel)
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    charts.footnote(fig, "Thirds of your games in each control by your rating before the game (low / mid / high). Whiskers: 95% CI, bootstrapped by game.")
    return fig


# ---------------------------------------------------------------------------------------
# rating regression (which habits go with a higher rating)
# ---------------------------------------------------------------------------------------

REGRESSION_KEYS = ["cp_move", "err20", "ep_opening", "ep_middlegame", "ep_endgame", "max_ep", "my_spoiled", "my_swindled", "spent_frac", "end_clock", "low_buffer", "plies", "book_ply"]
REGRESSION_LABELS = {
    **{k: METRIC_BY_KEY[k]["label"] for k in REGRESSION_KEYS if k in METRIC_BY_KEY},
    "my_spoiled": "spoiled a winning position (reached P(win) >= 0.8, did not win)",
    "my_swindled": "swindled (fell to P(win) <= 0.2, did not lose)",
}


def rating_regression(games: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Pre-game rating on your standardised per-game habits, per control; bootstrap clustered by calendar month."""
    rows = []
    for gi, g in enumerate(groups):
        part = games[games["tc_group"] == g]
        feats = {}
        for key in REGRESSION_KEYS:
            if key in METRIC_BY_KEY:
                num, den = side_arrays(part, METRIC_BY_KEY[key], "my")
                with np.errstate(invalid="ignore", divide="ignore"):
                    feats[key] = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
            else:
                feats[key] = part[key].to_numpy(dtype=float)
        F = pd.DataFrame(feats, index=part.index)
        F = F.fillna(F.mean())
        departs = {k: int((F[k] != F[k].mode().iloc[0]).sum()) for k in F.columns}
        used = [k for k in F.columns if departs[k] >= MIN_HABIT_GAMES]
        sd = F.std()
        Z = (F[used] - F[used].mean()) / sd[used]
        y = part["my_rating_before"].to_numpy(dtype=float)
        clusters = part["month"].to_numpy()
        for with_year in (False, True):
            X = pd.concat([Z, pd.Series(np.log1p(part["days_since_last_in_pool"].fillna(0).to_numpy(dtype=float)), index=part.index, name="log_days")], axis=1)
            if with_year:
                years = part["year"].astype(int)
                X = pd.concat([X, dummies(years, str(years.min()), "year")], axis=1)
            X.insert(0, "const", 1.0)
            Xa = X.to_numpy(dtype=float)
            beta, draws = cluster_ols(Xa, y, clusters, n_boot=N_BOOT, seed=SEED + gi)
            fitted = Xa @ np.nan_to_num(beta)
            r2 = 1 - ((y - fitted) ** 2).sum() / ((y - y.mean()) ** 2).sum()
            months = len(np.unique(clusters))
            column = {name: i for i, name in enumerate(X.columns)}
            for name in REGRESSION_KEYS:
                i = column.get(name)
                est, lo, hi = (np.nan, np.nan, np.nan) if i is None else summary(beta[i], draws[:, i])
                rows.append({
                    "tc_group": g, "habit": name, "label": REGRESSION_LABELS[name], "with_year_dummies": with_year,
                    "rating_points_per_sd": est, "rating_points_per_sd_lo": lo, "rating_points_per_sd_hi": hi,
                    "habit_sd": sd[name], "games_departing_from_usual": departs[name], "left_out_rare": i is None,
                    "r2": r2, "games": len(part), "months": months,
                    "thin": len(part) < MIN_REGRESSION_GAMES or months < MIN_REGRESSION_MONTHS,
                })  # fmt: skip
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------
# 2.5 score vs Elo expectation by rating gap
# ---------------------------------------------------------------------------------------


def scopes(games: pd.DataFrame, groups: list[str], pooled_only: bool = False, all_label: str = ALL_GAMES) -> Iterator[tuple[str, pd.DataFrame]]:
    """Each time-control group, then each time class pooled ('all bullet'), then everything."""
    if not pooled_only:
        for g in groups:
            yield g, games[games["tc_group"] == g]
    for c in TIME_CLASSES:
        part = games[games["time_class"] == c]
        if len(part):
            yield f"all {c}", part
    yield all_label, games


def score_row(part: pd.DataFrame) -> dict:
    w = boot_weights(part)
    score = mean_ci(part["my_score"], w)
    excess = mean_ci(part["my_score"] - part["elo_expected"], w)
    return {
        "mean_rating_diff": part["rating_diff"].mean(),
        "score": score[0], "score_lo": score[1], "score_hi": score[2],
        "expected": part["elo_expected"].mean(),
        "excess": excess[0], "excess_lo": excess[1], "excess_hi": excess[2],
        "wins": int(part["win"].sum()), "draws": int(part["draw"].sum()), "losses": int(part["loss"].sum()),
        "games": len(part), "thin": len(part) < MIN_CELL_GAMES,
    }  # fmt: skip


def score_by_gap(rated: pd.DataFrame, groups: list[str], pooled_only: bool, bin_col: str = "diff_bin", all_label: str = "all rated") -> pd.DataFrame:
    rows = []
    for scope, part_s in scopes(rated, groups, pooled_only, all_label):
        for key, part in part_s.groupby(bin_col, observed=True):
            rows.append({"tc_group": scope, "rating_gap": str(key), **score_row(part)})
    out = add_elo(pd.DataFrame(rows), "expected", ["excess", "excess_lo", "excess_hi"])
    first = ["tc_group", "rating_gap", "mean_rating_diff", "score", "score_lo", "score_hi", "expected", "excess", "excess_lo", "excess_hi", "excess_elo", "excess_lo_elo", "excess_hi_elo"]
    return out[first + [c for c in out.columns if c not in first]]


def level_split(rated: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Score vs expectation by gap, each gap bin split two ways (pooled by class): is the excess only a feature of a low rating, or of climbing back after a fall?

    - by rating: at the median pre-game rating of that bin's own games in the class, so both halves have the same number of games;
    - by distance from your peak so far in the pool: within PEAK_GAP of it (new ground) or further below (recovering).
    """
    rated = rated.copy()
    split_rating = rated.groupby(["time_class", "diff_bin"], observed=True)["my_rating_before"].transform(lambda s: s.quantile(LEVEL_SPLIT_QUANTILE))
    rated["by_rating"] = np.where(rated["my_rating_before"] < split_rating, "lower half of your rating", "upper half of your rating")
    rated["by_peak"] = np.where(rated["points_below_peak"] > PEAK_GAP, f"more than {PEAK_GAP} below your peak so far", f"within {PEAK_GAP} of your peak so far")
    rows = []
    for split, col in (("rating, at the median of the bin's games", "by_rating"), ("distance from your peak so far", "by_peak")):
        for scope, part_s in scopes(rated, groups, pooled_only=True, all_label="all rated"):
            for key, part_b in part_s.groupby("diff_bin", observed=True):
                for half, part in part_b.groupby(col, sort=True):
                    rows.append({
                        "tc_group": scope, "split": split, "half": half, "rating_gap": str(key),
                        "rating_min": part["my_rating_before"].min(), "rating_max": part["my_rating_before"].max(),
                        "mean_points_below_peak": part["points_below_peak"].mean(), **score_row(part),
                    })  # fmt: skip
    out = add_elo(pd.DataFrame(rows), "expected", ["excess", "excess_lo", "excess_hi"])
    first = ["tc_group", "split", "half", "rating_gap", "rating_min", "rating_max", "mean_points_below_peak", "mean_rating_diff", "score", "score_lo", "score_hi", "expected", "excess", "excess_lo", "excess_hi", "excess_elo", "excess_lo_elo", "excess_hi_elo"]
    return out[first + [c for c in out.columns if c not in first]]


def rating_change_check(rated: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Does your rating change per win / loss scale with the gap as Elo says? (Checks the opponent-rating reconstruction.)"""
    rows = []
    for scope, part_s in scopes(rated, groups, pooled_only=True, all_label="all rated"):
        change = pd.to_numeric(part_s["my_rating_change"], errors="coerce").astype(float)
        even = part_s[part_s["diff_bin"] == EVEN_BIN]
        if even.empty:
            continue
        even_win = change[(part_s["diff_bin"] == EVEN_BIN) & (part_s["win"] == 1)].mean()
        even_loss = change[(part_s["diff_bin"] == EVEN_BIN) & (part_s["loss"] == 1)].mean()
        e_even = even["elo_expected"].mean()
        for key, part in part_s.groupby("diff_bin", observed=True):
            ch = change.loc[part.index]
            win, loss = ch[part["win"] == 1].mean(), ch[part["loss"] == 1].mean()
            e = part["elo_expected"].mean()
            rows.append({
                "tc_group": scope, "rating_gap": str(key), "expected": e,
                "mean_change_per_win": win, "mean_change_per_loss": loss,
                "win_ratio_to_even": win / even_win, "elo_win_ratio_to_even": (1 - e) / (1 - e_even),
                "loss_ratio_to_even": loss / even_loss, "elo_loss_ratio_to_even": e / e_even,
                "wins": int(part["win"].sum()), "losses": int(part["loss"].sum()), "games": len(part),
                "thin": min(part["win"].sum(), part["loss"].sum()) < MIN_CELL_CASES,
            })  # fmt: skip
    return pd.DataFrame(rows)


def play_by_gap(rated: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Expected points and centipawns lost per move, you vs opponents, by rating gap."""
    rows = []
    for scope, part_s in scopes(rated, groups, all_label="all rated"):
        for key, part in part_s.groupby("diff_bin", observed=True):
            w = boot_weights(part)
            for m in (METRIC_BY_KEY["ep_move"], METRIC_BY_KEY["cp_move"], METRIC_BY_KEY["err20"]):
                rows.append({"tc_group": scope, **metric_columns(m), "rating_gap": str(key), **cell(part, m, w)})
    return pd.DataFrame(rows)


def chart_score_vs_gap(table: pd.DataFrame, panels: list[str], title: str, grid: bool):
    import matplotlib.pyplot as plt

    if grid:
        fig, axes = charts.group_grid(panels, panel=(3.3, 2.7), sharey=True, title=title)
    else:
        fig, row = plt.subplots(1, len(panels), figsize=(3.3 * len(panels), 3.0), sharey=True, squeeze=False)
        axes = dict(zip(panels, row[0]))
        for p, ax in axes.items():
            ax.set_title(p)
        fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    pos = {lab: i for i, lab in enumerate(RATING_DIFF_LABELS)}
    for p, ax in axes.items():
        part = table[(table["tc_group"] == p) & ~table["thin"]]
        if len(part) < 2:
            charts.thin_note(ax)
            continue
        x = part["rating_gap"].map(pos).to_numpy(dtype=float)
        ax.plot(x, part["expected"], color=charts.OPPONENT, ls="--", marker="s", ms=3.5, lw=1.2, label="Elo-expected score")
        ax.errorbar(x, part["score"], yerr=[part["score"] - part["score_lo"], part["score_hi"] - part["score"]], fmt="o", color=charts.PLAYER, ecolor=charts.PLAYER, elinewidth=1.4, ms=5, label="your score (95% CI)")
        ax.set_xticks(range(len(RATING_DIFF_LABELS)), RATING_DIFF_LABELS, rotation=45, ha="right", fontsize=7)
        ax.set_xlim(-0.5, len(RATING_DIFF_LABELS) - 0.5)
        ax.set_ylim(0, 1)
    for p, ax in axes.items():
        if ax.get_subplotspec().colspan.start == 0:
            ax.set_ylabel("score")
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout()
    charts.footnote(fig, f"x: opponent's rating minus yours before the game (positive = stronger opponent). Rated games; bins with fewer than {MIN_CELL_GAMES} games are not drawn.")
    return fig


# ---------------------------------------------------------------------------------------
# 2.6 upsets
# ---------------------------------------------------------------------------------------


def upset_category(games: pd.DataFrame) -> pd.Series:
    cat = pd.Series(pd.NA, index=games.index, dtype="object")
    diff = games["rating_diff"]
    cat[(games["win"] == 1) & (diff >= UPSET_GAP)] = "upset wins"
    cat[(games["win"] == 1) & (diff.abs() <= EVEN_GAP)] = "ordinary wins"
    cat[(games["loss"] == 1) & (diff <= -UPSET_GAP)] = "upset losses"
    cat[(games["loss"] == 1) & (diff.abs() <= EVEN_GAP)] = "ordinary losses"
    return cat


def upsets_table(games: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    frame = games[games["rating_diff"].notna()].copy()
    frame["category"] = upset_category(frame)
    frame = frame[frame["category"].notna()]
    frame["loser_result"] = np.where(frame["win"] == 1, frame["opponent_result"], frame["my_result"])
    frame["loser_alive"] = np.where(frame["win"] == 1, frame["opp_lost_alive"], frame["my_lost_alive"])
    rows = []
    for scope, part_s in scopes(frame, groups):
        for cat in UPSET_CATEGORIES:
            part = part_s[part_s["category"] == cat]
            row = {"tc_group": scope, "category": cat, "games": len(part), "rated_share": part["rated"].mean() if len(part) else np.nan, "mean_rating_diff": part["rating_diff"].mean() if len(part) else np.nan}
            if part.empty:
                rows.append({**row, "thin": True})
                continue
            w = boot_weights(part)
            for ending in ENDINGS:
                hit = part["loser_result"] == ending
                row[f"loser_{ending}"], row[f"loser_{ending}_lo"], row[f"loser_{ending}_hi"] = mean_ci(hit.astype(float), w)
            row["loser_still_alive"], row["loser_still_alive_lo"], row["loser_still_alive_hi"] = mean_ci(part["loser_alive"], w)
            row["decisive_blunder_found"], row["decisive_blunder_found_lo"], row["decisive_blunder_found_hi"] = mean_ci(part["decisive_found"].fillna(0), w)
            for m in (METRIC_BY_KEY["ep_move"], METRIC_BY_KEY["cp_move"]):
                c = cell(part, m, w)
                for k in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"):
                    row[f"{m['key']}_{k}"] = c[k]
            row["plies"], row["plies_lo"], row["plies_hi"] = mean_ci(part["n_plies"], w)
            row["thin"] = len(part) < MIN_CELL_GAMES
            rows.append(row)
    return pd.DataFrame(rows)


def chart_upsets(table: pd.DataFrame, panels: list[str]):
    import matplotlib.pyplot as plt

    colours = dict(zip(ENDINGS, [charts.SERIES[1], charts.SERIES[0], charts.SERIES[2], charts.SERIES[3]]))
    fig, row = plt.subplots(1, len(panels), figsize=(3.3 * len(panels), 2.9), sharey=True, squeeze=False)
    for ax, p in zip(row[0], panels):
        part = table[table["tc_group"] == p].set_index("category").reindex(UPSET_CATEGORIES)
        y = np.arange(len(UPSET_CATEGORIES))
        thin = part["thin"].fillna(True).to_numpy(dtype=bool)
        shares = [np.where(thin, 0.0, part[f"loser_{ending}"].fillna(0).to_numpy(dtype=float)) for ending in ENDINGS]
        charts.stacked_bars(ax, y, shares, [colours[e] for e in ENDINGS], labels=[f"loser: {e}" for e in ENDINGS], width=0.7, horizontal=True)
        for yi, (n, thin) in enumerate(zip(part["games"].fillna(0).astype(int), part["thin"].fillna(True))):
            ax.text(1.02, yi, f"{n}" if not thin else f"{n}: too few", va="center", fontsize=7, color=charts.INK_MUTED if thin else charts.INK_SECONDARY, transform=ax.get_yaxis_transform())
        ax.set_yticks(y, UPSET_CATEGORIES)
        ax.set_xlim(0, 1)
        ax.set_title(p)
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("share of games", fontsize=7.5)
    row[0][0].invert_yaxis()  # once: the panels share the y axis
    charts.legend(fig, loc="upper center", bbox_to_anchor=(0.5, 1.12))
    fig.tight_layout()
    charts.footnote(fig, f"Upset: a win against an opponent at least {UPSET_GAP} points stronger, or a loss to one at least {UPSET_GAP} weaker; ordinary: opponent within {EVEN_GAP} points. Numbers on the right: games.")
    return fig


# ---------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    groups = list(data.tc_groups)
    games = build_games(data)
    results = Results(SLUG)

    # 2.1 trajectory
    by_year, milestones, tc_by_year = trajectory_tables(games)
    order = {g: i for i, g in enumerate(groups)}
    tc_by_year = tc_by_year.assign(_o=tc_by_year["tc_group"].map(order)).sort_values(["_o", "year"]).drop(columns="_o")
    results.table("1_rating_by_year", by_year, "Per rating pool (time class) and year: your pre-game rating (mean, range, first, last), score with CI, Elo-expected score and score above it (with Elo-equivalent), the year's main time control, games, rated share, games after a break.")
    results.figure(chart_trajectory(games, by_year), "1_rating_by_year")
    results.table("1b_rating_milestones", milestones, "Per rating pool: first, last, lowest and peak pre-game rating with the month, the change first to last and the fall from the peak.")
    results.table("1c_games_by_year", tc_by_year, "Games per time-control group and year, and their share of the time class that year (the controls behind each pool's rating line).")

    # 2.2-2.4 metrics by band, by third, change low -> high third
    band = by_level(games, groups, "band", METRICS)
    third = by_level(games, groups, "thirds", METRICS)
    change = change_table(games, groups, METRICS)
    keys = lambda ms: [m["key"] for m in ms]  # noqa: E731
    pick = lambda frame, ms: frame[frame["metric"].isin(keys(ms))].reset_index(drop=True)  # noqa: E731

    results.table("2_quality_by_band", pick(band, QUALITY), f"Move quality by {BAND}-point band of your pre-game rating, per time control: you, opponents in the same games, and the paired difference (CIs bootstrapped by game).")
    results.figure(chart_band_lines(band, groups, "cp_move", "centipawns lost per move", "Centipawns lost per move by your rating: you vs your opponents", True), "2_quality_by_band")
    results.table("2b_quality_by_third", pick(third, QUALITY), "Move quality in the low / mid / high third of your games by pre-game rating, per time control: you, opponents, paired difference.")
    results.table("2c_quality_change", pick(change, QUALITY), "Move quality, high third minus low third of your games by rating: your change, the field's change (opponents in the same games) and your change minus the field's, CIs bootstrapped by game.")
    results.table("2d_change_without_breaks", breaks_sensitivity(games, groups, change), f"Reliability check: every low-to-high change recomputed without games played after more than {BREAK_DAYS} days away from the pool.")

    results.table("3_speed_by_band", pick(band, SPEED), f"Clock use by {BAND}-point band of your pre-game rating, per time control: seconds and share of base per move, clock left at the end, share of moves under {LOW_CLOCK_SECONDS:g} s (plain and beyond one increment) and under {LOW_CLOCK_BASE_FRACTION:.0%} of base.")
    results.figure(chart_band_lines(band, groups, "time_move", "seconds per move", "Seconds spent per move by your rating: you vs your opponents", "row"), "3_speed_by_band")
    results.table("3b_speed_by_third", pick(third, SPEED), "Clock use in the low / mid / high third of your games by pre-game rating, per time control.")
    results.table("3c_speed_change", pick(change, SPEED), "Clock use, high third minus low third of your games by rating: your change, the field's and the difference.")
    results.table("3d_rating_regression", rating_regression(games, groups), f"Rating points associated with one standard deviation of each of your per-game habits, holding the others and log(days since the last game in the pool) fixed; with and without year dummies; bootstrap clustered by calendar month. Thin below {MIN_REGRESSION_GAMES} games.")

    results.table("4_time_losses", pick(third, RESULTS), "How games end, low / mid / high third of your games by rating, per time control: share of losses on time, losses while still alive, games reaching a winning position, conversion, swindles; you vs opponents.")
    results.figure(chart_thirds_bars(third, groups, "flag_loss", "share of losses on time", "Share of your losses that were on time, by your rating: you vs your opponents"), "4_time_losses")
    results.table("4b_time_losses_change", pick(change, RESULTS), "How games end, high third minus low third of your games by rating: your change, the field's and the difference.")
    results.table("4c_by_band_other", pick(band, [*RESULTS, *SHAPE]), f"Results and game shape by {BAND}-point band of your pre-game rating, per time control.")
    results.table("4d_change_shape", pick(change, SHAPE), "Game length and book depth, high third minus low third of your games by rating.")

    # 2.5 score vs expectation by rating gap (rated games)
    rated = games[games["rated"].astype(bool) & games["rating_diff"].notna()]
    unrated = games[~games["rated"].astype(bool) & games["rating_diff"].notna()].copy()
    unrated["coarse_bin"] = pd.cut(unrated["rating_diff"], UNRATED_EDGES, labels=UNRATED_LABELS, right=True)
    per_group = score_by_gap(rated, groups, pooled_only=False)
    is_pooled = ~per_group["tc_group"].isin(groups)
    results.table("5_score_vs_gap", per_group[~is_pooled], "Rated games per time control, by rating gap (opponent minus you, pre-game): your score with CI, the Elo-expected score, score above expectation with CI and its Elo-equivalent; wins, draws, losses.")
    results.figure(chart_score_vs_gap(per_group, groups, "Your score vs the Elo expectation, by how much stronger the opponent was", grid=True), "5_score_vs_gap")
    pooled_panels = [p for p in per_group.loc[is_pooled, "tc_group"].drop_duplicates()]
    results.table("5b_score_vs_gap_pooled", per_group[is_pooled], "The same, pooled by time class ('all bullet' ...) and over all rated games, because the outer gap bins are thin in every control but 1+0.")
    results.figure(chart_score_vs_gap(per_group, pooled_panels, "Your score vs the Elo expectation, pooled by time class", grid=False), "5b_score_vs_gap_pooled")
    total = pd.DataFrame([{"tc_group": s, **score_row(p)} for s, p in scopes(rated, groups, all_label="all rated")])
    results.table("5c_score_overall", add_elo(total, "expected", ["excess", "excess_lo", "excess_hi"]), "All rated games per time control and pooled: score, Elo-expected score, score above expectation and its Elo-equivalent.")
    results.table("5d_level_split", level_split(rated, groups), f"Check: score vs expectation by rating gap, each bin split in two, pooled by time class: at the median rating of the bin's own games, and by whether you were within {PEAK_GAP} points of your peak so far in the pool or further below it (is over-performance against stronger players only a feature of a low rating, or of recovering after a fall?).")
    results.table("5e_unrated", score_by_gap(unrated, groups, pooled_only=True, bin_col="coarse_bin", all_label="all unrated"), "Check: unrated games, where the opponent's rating is exact (no reconstruction), coarse gap bins; pooled by time class.")
    results.table("5f_rating_change_check", rating_change_check(rated, groups), f"Check on the opponent-rating reconstruction: your mean rating change per win and per loss by gap, as a ratio to the even bin ({EVEN_BIN}), next to the ratio Elo predicts.")
    results.table("5g_play_by_gap", play_by_gap(rated, groups), "Expected points and centipawns lost per move and big errors per 100 moves, you vs opponents, by rating gap; per time control and pooled.")

    # 2.6 upsets
    ups = upsets_table(games, groups)
    results.table("6_upsets", ups, f"How upset wins (opponent >= {UPSET_GAP} points stronger) and upset losses (opponent >= {UPSET_GAP} weaker) ended, next to ordinary wins and losses (within {EVEN_GAP} points): how the loser lost, still alive, decisive blunder found, both sides' errors per move, length. Per control and pooled; rated and unrated games.")
    results.figure(chart_upsets(ups, [ALL_GAMES, *[f"all {c}" for c in TIME_CLASSES if f"all {c}" in set(ups["tc_group"])]]), "6_upsets")

    # counts for the method notes
    wide = rated[rated["rating_diff"] > UPSET_GAP]
    wide_years = wide["year"].astype(int).value_counts().sort_index()
    busiest = int(wide_years.idxmax())
    info = pd.DataFrame([
        {"item": "games analysed", "value": len(games)},
        {"item": "rated games", "value": len(rated)},
        {"item": "unrated games", "value": len(unrated)},
        {"item": "games after a break", "value": int(games["after_break"].sum())},
        {"item": "median absolute rating gap, rated", "value": float(rated["rating_diff"].abs().median())},
        {"item": "share of rated games within 50 points", "value": float((rated["rating_diff"].abs() <= 50).mean())},
        {"item": "median absolute rating gap, unrated", "value": float(unrated["rating_diff"].abs().median())},
        {"item": f"rated games against opponents more than {UPSET_GAP} stronger", "value": len(wide)},
        {"item": f"of which in {busiest} (the year with the most)", "value": int(wide_years.loc[busiest])},
        *[{"item": f"excluded control {g} (games)", "value": n} for g, n in data.tc_noted.items()],
    ])  # fmt: skip
    results.table("7_data", info, "Counts behind the method notes: games analysed, rated / unrated, after a break, how tight the pairing is, the excluded thin controls.")
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
