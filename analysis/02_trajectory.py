"""Q2: Rating trajectory, what changed as you improved (plus Q11: performance vs opponent rating).

Per-game metrics from Q1's decomposition (`games_decomposed.parquet`) plus clock
and phase metrics computed here, by 100-point band of your pre-game rating and
by year, for you and for the matched opponents in the same games; a ranked
"what moved" table (low vs high third of games by rating, you vs the field);
a clustered regression of rating on your per-game metrics; and result vs Elo
expectation by rating-difference bin, with error rates by bin for both sides.

Every number has a game-bootstrap CI; both sides are always computed on the
same resampled games, so differences are paired.
"""

from __future__ import annotations

import re
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
    rating_band,
    repo_root,
    load_data,
)

SLUG = "02_trajectory"
SEED = 2
N_BOOT = 400  # resamples per cell; the tables have many cells
MIN_GAMES = 20  # cells resting on fewer games are left out
BAND = 100
BREAK_DAYS = 7  # a gap in a rating pool longer than this is a "break" (rating reliability control)
LOW_CLOCK = 10.0  # seconds
UPSET_GAP = 100  # rating points: "upset" wins / losses
PHASES = ["opening", "middlegame", "endgame"]
MAIN_TC = {"bullet": "60+0", "blitz": "180+2", "rapid": "600+0"}
RATING_DIFF_EDGES = [-np.inf, -100, -50, -25, 0, 25, 50, 100, np.inf]
RATING_DIFF_LABELS = ["<= -100", "-100..-50", "-50..-25", "-25..0", "0..25", "25..50", "50..100", "> 100"]
LOW_ALIVE = 0.1  # Q1's "lost while still alive" threshold on the loser's last P(win)

# ---------------------------------------------------------------------------------------
# small statistics helpers (per-game bootstrap through weight matrices, so that every
# quantity of a table cell is computed on the same resampled games)
# ---------------------------------------------------------------------------------------


def boot_weights(n: int, n_boot: int = N_BOOT, seed: int = SEED) -> np.ndarray:
    """Multinomial resampling weights for n games: shape (n_boot, n)."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n, np.full(n, 1.0 / n), size=n_boot).astype(float)


def ratio_ci(num: np.ndarray, den: np.ndarray, w: np.ndarray) -> tuple[float, float, float]:
    """sum(num)/sum(den) with a CI from the weight matrix `w`."""
    num = np.nan_to_num(np.asarray(num, dtype=float))
    den = np.nan_to_num(np.asarray(den, dtype=float))
    if den.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    est = num.sum() / den.sum()
    draws = (w @ num) / np.maximum(w @ den, 1e-12)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(est), float(lo), float(hi)


def ratio_diff_ci(num_a, den_a, num_b, den_b, w: np.ndarray) -> tuple[float, float, float]:
    """ratio_a - ratio_b on the same resampled games."""
    num_a, den_a, num_b, den_b = (np.nan_to_num(np.asarray(x, dtype=float)) for x in (num_a, den_a, num_b, den_b))
    if den_a.sum() == 0 or den_b.sum() == 0:
        return float("nan"), float("nan"), float("nan")
    est = num_a.sum() / den_a.sum() - num_b.sum() / den_b.sum()
    draws = (w @ num_a) / np.maximum(w @ den_a, 1e-12) - (w @ num_b) / np.maximum(w @ den_b, 1e-12)
    lo, hi = np.quantile(draws, [0.025, 0.975])
    return float(est), float(lo), float(hi)


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    if pd.isna(est):
        return ""
    return f"{est:{f}} [{lo:{f}}, {hi:{f}}]" if pd.notna(lo) else f"{est:{f}}"


def calibration_brier() -> str:
    """Cross-validated expected-points Brier of the P(win) model, read from reports/calibration.md (so it cannot go stale here)."""
    path = repo_root() / "reports" / "calibration.md"
    if path.exists():
        hit = re.search(r"Expected points Brier \| ([0-9.]+)", path.read_text())
        if hit:
            return f"cross-validated expected-points Brier {float(hit.group(1)):.3f}, see `reports/calibration.md`"
    return "see `reports/calibration.md` for its cross-validated Brier score"


def opponent_rating_pre(frame: pd.DataFrame) -> pd.Series:
    """Proxy for the opponent's rating *before* the game (the data only hold post-game ratings).

    In a rated game the opponent's rating change is approximately minus yours,
    so `opponent_rating_after + my_rating_change` undoes the result's effect;
    in an unrated game ratings do not move. Approximate: chess.com uses Glicko,
    so the two changes differ when the rating deviations differ.
    """
    change = pd.to_numeric(frame["my_rating_change"], errors="coerce").astype(float)
    change = np.where(frame["rated"].astype(bool), change, 0.0)
    return frame["opponent_rating_after"].astype(float) + change


def cluster_ols(X: np.ndarray, y: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED) -> tuple[np.ndarray, np.ndarray]:
    """OLS coefficients and bootstrap draws, resampling rows (one row = one game here)."""
    k = X.shape[1]

    def solve(w: np.ndarray) -> np.ndarray:
        xw = X * w[:, None]
        return np.linalg.lstsq(xw.T @ X, xw.T @ y, rcond=None)[0]

    beta = solve(np.ones(len(y)))
    rng = np.random.default_rng(seed)
    draws = np.empty((n_boot, k))
    for b in range(n_boot):
        draws[b] = solve(np.bincount(rng.integers(0, len(y), len(y)), minlength=len(y)).astype(float))
    return beta, draws


# ---------------------------------------------------------------------------------------
# per-game table
# ---------------------------------------------------------------------------------------


def build_games(data) -> pd.DataFrame:
    """Q1's decomposition joined onto the games, plus clock, phase-count and result metrics per side."""
    games = data.games.merge(pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet"), on="uuid", how="left")
    games = games[games["n_plies"].notna() & games["my_rating_before"].notna()].copy()  # 2 games without a move; first game in each pool has no pre-game rating
    zero = (games["my_rating_before"] <= 0) | (games["opponent_rating_after"] <= 0)
    games = games[~zero].copy()  # a rating of 0 is a placeholder, not a rating
    games.attrs["dropped_zero_rating"] = int(zero.sum())
    moves = data.moves[data.moves["game_uuid"].isin(games["uuid"])]

    # phase move counts and clock use per side
    counts = moves.groupby(["game_uuid", "side", "phase"], observed=True).size().unstack(["side", "phase"], fill_value=0)
    per_side = moves.assign(
        low_clock=(moves["clock_before_seconds"] < LOW_CLOCK).astype(float),
        spent_frac=moves["time_spent_seconds"] / moves["base_seconds"],
    ).groupby(["game_uuid", "side"]).agg(time_spent=("time_spent_seconds", "sum"), spent_frac=("spent_frac", "sum"), low_clock=("low_clock", "sum"))
    per_side = per_side.unstack("side")
    last = moves.sort_values("ply").groupby(["game_uuid", "side"]).tail(1).set_index(["game_uuid", "side"])
    end_frac = (last["clock_seconds"] / last["base_seconds"]).unstack("side")

    for side, prefix in (("player", "my"), ("opponent", "opp")):
        for phase in PHASES:
            col = (side, phase)
            games[f"{prefix}_moves_{phase}"] = games["uuid"].map(counts[col] if col in counts else pd.Series(dtype=float)).fillna(0).astype(float)
        games[f"{prefix}_time_spent"] = games["uuid"].map(per_side[("time_spent", side)]).astype(float)
        games[f"{prefix}_spent_frac"] = games["uuid"].map(per_side[("spent_frac", side)]).astype(float)
        games[f"{prefix}_low_clock_moves"] = games["uuid"].map(per_side[("low_clock", side)]).astype(float)
        games[f"{prefix}_end_clock_frac"] = games["uuid"].map(end_frac[side]).astype(float)

    for col in games.columns:
        if str(games[col].dtype) in ("Int64", "boolean"):
            games[col] = games[col].astype(float)

    games["band"] = rating_band(games["my_rating_before"], BAND).astype(float)
    games["tc"] = games["base_seconds"].astype(int).astype(str) + "+" + games["increment_seconds"].astype(int).astype(str)
    games["opp_rating_pre"] = opponent_rating_pre(games)
    games["rating_diff"] = games["opp_rating_pre"] - games["my_rating_before"].astype(float)
    games["elo_expected"] = elo_expected_score(games["my_rating_before"], games["opp_rating_pre"])
    games["diff_bin"] = pd.cut(games["rating_diff"], RATING_DIFF_EDGES, labels=RATING_DIFF_LABELS, right=True)
    games["after_break"] = games["days_since_last_in_pool"] > BREAK_DAYS
    games["win"] = (games["outcome"] == "win").astype(float)
    games["loss"] = (games["outcome"] == "loss").astype(float)
    games["draw"] = (games["outcome"] == "draw").astype(float)
    # result-type indicators per side
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
    games["opp_spoiled"] = games["opp_reached_winning"] * (1 - games["loss"])
    games["one"] = 1.0
    return games.reset_index(drop=True)


# ---------------------------------------------------------------------------------------
# metric registry: every metric is a ratio of per-game sums, per side (my_/opp_)
# ---------------------------------------------------------------------------------------

Metric = dict


def metric(key: str, label: str, num: str, den: str, fmt_: str = ".3f", scale: float = 1.0, better: str = "lower", game_level: bool = False) -> Metric:
    return {"key": key, "label": label, "num": num, "den": den, "fmt": fmt_, "scale": scale, "better": better, "game_level": game_level}


METRICS: list[Metric] = [
    metric("ep_move", "ep lost per move", "{p}_ep_lost", "{p}_moves", ".4f"),
    metric("cp_move", "cp lost per move", "{p}_cp_lost", "{p}_moves", ".1f"),
    metric("err20", "moves losing >= 0.2 ep, per 100 moves", "{p}_errors_ge_20", "{p}_moves", ".2f", 100),
    metric("blunders", "blunders (cp_loss >= 300), per 100 moves", "{p}_blunders", "{p}_moves", ".2f", 100),
    metric("max_ep", "largest single ep loss in the game", "{p}_max_ep_loss", "one", ".3f"),
    metric("ep_opening", "opening ep lost per move", "{p}_ep_lost_opening", "{p}_moves_opening", ".4f"),
    metric("ep_middlegame", "middlegame ep lost per move", "{p}_ep_lost_middlegame", "{p}_moves_middlegame", ".4f"),
    metric("ep_endgame", "endgame ep lost per move", "{p}_ep_lost_endgame", "{p}_moves_endgame", ".4f"),
    metric("reached_win", "share of games reaching P(win) >= 0.8", "{p}_reached_winning", "one", ".3f", better="higher"),
    metric("conversion", "conversion: P(win | reached P(win) >= 0.8)", "{p}_converted", "{p}_reached_winning", ".3f", better="higher"),
    metric("swindle", "swindle: P(not loss | fell to P(win) <= 0.2)", "{p}_swindled", "{p}_reached_losing", ".3f", better="higher"),
    metric("lost_alive", "share of losses while still alive (loser P(win) >= 0.1)", "{p}_lost_alive", "{p}_losses", ".3f"),
    metric("flag_loss", "share of losses on time", "{p}_flag_loss", "{p}_losses", ".3f"),
    metric("time_move", "seconds spent per move", "{p}_time_spent", "{p}_moves", ".2f", better="n/a"),
    metric("spent_frac", "% of base time spent per move", "{p}_spent_frac", "{p}_moves", ".2f", 100, better="n/a"),
    metric("end_clock", "clock left after own last move, fraction of base", "{p}_end_clock_frac", "one", ".3f", better="higher"),
    metric("low_clock", "% of own moves played under 10 s", "{p}_low_clock_moves", "{p}_moves", ".2f", 100),
    metric("plies", "game length (plies)", "n_plies", "one", ".1f", better="n/a", game_level=True),
    metric("book_ply", "book depth (plies in a named line)", "book_ply", "one", ".2f", better="higher", game_level=True),
]
METRIC_BY_KEY = {m["key"]: m for m in METRICS}


def side_arrays(frame: pd.DataFrame, m: Metric, prefix: str) -> tuple[np.ndarray, np.ndarray]:
    """Numerator and denominator per game; a game with a missing numerator (e.g. no move by that side) drops out of the metric."""
    num = frame[m["num"].format(p=prefix)].to_numpy(dtype=float) * m["scale"]
    den = frame[m["den"].format(p=prefix)].to_numpy(dtype=float)
    missing = np.isnan(num) | np.isnan(den)
    return np.where(missing, 0.0, num), np.where(missing, 0.0, den)


def min_n(m: Metric) -> int:
    """Thin-cell threshold: 30 moves for per-move rates, 20 games for per-game rates and means."""
    return 30 if "moves" in m["den"] else MIN_GAMES


def add_loss_counts(games: pd.DataFrame) -> pd.DataFrame:
    games = games.copy()
    games["my_losses"] = games["loss"]
    games["opp_losses"] = games["win"]
    return games


def cell(frame: pd.DataFrame, m: Metric, w: np.ndarray) -> dict:
    """You, opponents and the paired difference for one metric on one set of games."""
    mine = side_arrays(frame, m, "my")
    out = {"games": len(frame)}
    out["you"], out["you_lo"], out["you_hi"] = ratio_ci(*mine, w)
    out["n_you"] = int(np.nan_to_num(mine[1]).sum()) if m["den"] != "one" else len(frame)
    if m["game_level"]:
        out.update(them=np.nan, them_lo=np.nan, them_hi=np.nan, diff=np.nan, diff_lo=np.nan, diff_hi=np.nan, n_them=0)
    else:
        theirs = side_arrays(frame, m, "opp")
        out["them"], out["them_lo"], out["them_hi"] = ratio_ci(*theirs, w)
        out["diff"], out["diff_lo"], out["diff_hi"] = ratio_diff_ci(*mine, *theirs, w)
        out["n_them"] = int(np.nan_to_num(theirs[1]).sum()) if m["den"] != "one" else len(frame)
    return out


def table_by(games: pd.DataFrame, by: str, metrics: Sequence[Metric], min_games: int = MIN_GAMES, n_boot: int = N_BOOT) -> pd.DataFrame:
    """Long table: time_class x `by` x metric -> you / them / diff with CIs."""
    rows = []
    for (tc, key), part in games.groupby(["time_class", by], observed=True):
        if len(part) < min_games:
            continue
        w = boot_weights(len(part), n_boot)
        for m in metrics:
            rows.append({"time_class": tc, by: key, "metric": m["key"], **cell(part, m, w)})
    return pd.DataFrame(rows)


def compact(long: pd.DataFrame, by: str, keys: Sequence[str], with_diff: bool = True) -> pd.DataFrame:
    """Wide, human-readable table: one row per `by` value, 'you' / 'them' (/ 'diff') CI strings per metric."""
    out = None
    for key in keys:
        m = METRIC_BY_KEY[key]
        part = long[long["metric"] == key].copy()
        f = m["fmt"]
        thin = (part["n_you"] < min_n(m)) | ((part["n_them"] < min_n(m)) & (not m["game_level"]))
        cols = {by: part[by], "games": part["games"]}
        cols[m["label"] if m["game_level"] else f"{m['label']}: you"] = [("" if t else fmt(a, b, c, f)) for a, b, c, t in zip(part["you"], part["you_lo"], part["you_hi"], thin)]
        if not m["game_level"]:
            cols[f"{m['label']}: them"] = [("" if t else fmt(a, b, c, f)) for a, b, c, t in zip(part["them"], part["them_lo"], part["them_hi"], thin)]
            if with_diff:
                cols[f"{m['label']}: you - them"] = [("" if t else fmt(a, b, c, f)) for a, b, c, t in zip(part["diff"], part["diff_lo"], part["diff_hi"], thin)]
        piece = pd.DataFrame(cols).reset_index(drop=True)
        out = piece if out is None else out.merge(piece, on=[by, "games"], how="outer")
    return out.sort_values(by).reset_index(drop=True)


def band_label(value: float) -> str:
    return f"{int(value)}-{int(value) + BAND - 1}"


# ---------------------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------------------


def section_trajectory(report: Report, games: pd.DataFrame) -> dict:
    report.heading("1. The trajectory itself")
    first = games.sort_values("start_datetime").groupby("time_class").first()
    last = games.sort_values("start_datetime").groupby("time_class").last()
    lines = []
    for tc in TIME_CLASSES:
        if tc in first.index:
            part = games[games["time_class"] == tc]
            lo_i, hi_i = part["my_rating_before"].idxmin(), part["my_rating_before"].idxmax()
            lines.append(f"{tc}: {int(first.loc[tc, 'my_rating_before'])} ({first.loc[tc, 'start_datetime']:%b %Y}) to {int(last.loc[tc, 'my_rating_before'])} ({last.loc[tc, 'start_datetime']:%b %Y}), low {int(part.loc[lo_i, 'my_rating_before'])} ({part.loc[lo_i, 'start_datetime']:%b %Y}), peak {int(part.loc[hi_i, 'my_rating_before'])} ({part.loc[hi_i, 'start_datetime']:%b %Y})")
    report.text("Pre-game rating from the first to the last game in each pool: " + "; ".join(lines) + ". The figure shows that the climb was not monotone: bands below the current rating hold games from the climb and, where the rating fell back, from the descent. Games by year and rating band, with the mean pre-game rating and your score:")

    rows = []
    for (tc, year), part in games.groupby(["time_class", "year"]):
        w = boot_weights(len(part))
        s = ratio_ci(part["my_score"], part["one"], w)
        rows.append({"time class": tc, "year": int(year), "games": len(part), "rated": f"{part['rated'].mean():.2f}", "mean pre-game rating": f"{part['my_rating_before'].mean():.0f}", "rating range": f"{int(part['my_rating_before'].min())}-{int(part['my_rating_before'].max())}", "score": fmt(*s), "main time control": part["tc"].mode().iloc[0], "share main tc": f"{(part['tc'] == part['tc'].mode().iloc[0]).mean():.2f}", "after a break (> 7 d)": int(part["after_break"].sum())})
    report.table(pd.DataFrame(rows))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    for ax, tc in zip(axes, TIME_CLASSES):
        part = games[games["time_class"] == tc].sort_values("start_datetime")
        ax.plot(part["start_datetime"], part["my_rating_before"], color=SIDE_COLOURS["player"], lw=0.8, label="you (pre-game)")
        ax.plot(part["start_datetime"], part["opp_rating_pre"].rolling(50, min_periods=10).median(), color=SIDE_COLOURS["opponent"], lw=1.0, label="opponents (pre-game proxy, rolling median of 50)")
        ax.set_title(f"{tc} ({len(part):,} games)")
        ax.tick_params(axis="x", rotation=30)
    axes[0].set_ylabel("rating")
    axes[0].legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    report.figure(fig, "rating_trajectory", "Your pre-game rating over time in each pool, with the opponents' reconstructed pre-game rating (rolling median).")
    plt.close(fig)

    bands = games.groupby(["time_class", "band"]).size().unstack(fill_value=0)
    bands.columns = [band_label(c) for c in bands.columns]
    report.text(f"Games per 100-point band of your pre-game rating (bands with fewer than {MIN_GAMES} games are left out of every table below):")
    report.table(bands.reindex(TIME_CLASSES), index=True)
    return {"bands": bands}


def section_by_band(report: Report, games: pd.DataFrame) -> pd.DataFrame:
    report.heading("2. Metrics by rating band: you vs the opponents you were matched with")
    report.text(
        "Each band holds the games you played with a pre-game rating in that band. Opponents are matched to within a few dozen points, so the opponents' column is the field at that level and the trend across bands is the ladder: what changes with level for everyone. "
        "Your column minus theirs is what distinguished you from the field at each level (differences are paired within the same resampled games). "
        "`ep` is calibrated expected points lost (the main measure; the calibration knows both ratings, so ep per move is close to flat in rating by construction and measures how much of the game you give away *at that level*); `cp` is centipawns lost (the absolute, level-free measure). "
        f"Cells with fewer than {MIN_GAMES} games or 30 moves are blank."
    )
    long = table_by(games, "band", METRICS)
    long["band_label"] = long["band"].map(band_label)

    groups = [
        ("Accuracy in expected points (main measure)", ["ep_move", "err20"], True),
        ("Accuracy in centipawns (secondary)", ["cp_move", "blunders"], True),
        ("Phases (ep lost per move in the phase)", ["ep_opening", "ep_middlegame", "ep_endgame"], True),
        ("Winning and losing positions", ["reached_win", "conversion", "swindle"], True),
        ("How losses end", ["lost_alive", "flag_loss"], False),
        ("Clock use", ["time_move", "spent_frac", "end_clock", "low_clock"], False),
        ("Game shape", ["plies", "book_ply"], False),
    ]
    for tc in TIME_CLASSES:
        part = long[long["time_class"] == tc]
        if part.empty:
            continue
        report.heading(tc, level=3)
        for title, keys, with_diff in groups:
            report.text(f"*{title}*")
            table = compact(part, "band", keys, with_diff=with_diff)
            table["band"] = table["band"].map(band_label)
            report.table(table)

    # figure: ladder of key metrics
    import matplotlib.pyplot as plt

    keys = ["cp_move", "ep_move", "err20", "conversion", "swindle", "time_move", "end_clock", "lost_alive"]
    fig, axes = plt.subplots(len(keys), 3, figsize=(13, 2.2 * len(keys)), sharex="col")
    for j, tc in enumerate(TIME_CLASSES):
        for i, key in enumerate(keys):
            ax = axes[i, j]
            part = long[(long["time_class"] == tc) & (long["metric"] == key)].sort_values("band")
            part = part[(part["n_you"] >= min_n(METRIC_BY_KEY[key])) & (part["n_them"] >= min_n(METRIC_BY_KEY[key]))]
            x = part["band"] + BAND / 2
            for who, colour in (("you", SIDE_COLOURS["player"]), ("them", SIDE_COLOURS["opponent"])):
                ax.plot(x, part[who], marker="o", ms=3, color=colour, label="you" if who == "you" else "opponents")
                ax.fill_between(x, part[f"{who}_lo"], part[f"{who}_hi"], color=colour, alpha=0.15, lw=0)
            if i == 0:
                ax.set_title(tc)
            if j == 0:
                ax.set_ylabel(METRIC_BY_KEY[key]["label"], fontsize=7)
            if i == len(keys) - 1:
                ax.set_xlabel("your pre-game rating band")
    axes[0, 0].legend(fontsize=8)
    fig.tight_layout()
    report.figure(fig, "ladder_by_band", "Key metrics by your rating band, you (blue) vs opponents in the same games (orange), shaded 95% CIs.")
    plt.close(fig)
    return long


def section_by_year(report: Report, games: pd.DataFrame) -> pd.DataFrame:
    report.heading("3. The same metrics by year")
    thin = games.groupby("year").size()
    report.text(f"Calendar years (games per year: {', '.join(f'{int(y)}: {n:,}' for y, n in thin.items())}). Years with fewer than {MIN_GAMES} games in a time class are left out; the mean pre-game rating of the year is in section 1.")
    long = table_by(games, "year", METRICS)
    long["year"] = long["year"].astype(int)
    groups = [
        ("Accuracy in expected points (main measure)", ["ep_move", "err20"], True),
        ("Accuracy in centipawns (secondary)", ["cp_move", "blunders"], True),
        ("Phases (ep lost per move in the phase)", ["ep_opening", "ep_middlegame", "ep_endgame"], False),
        ("Positions, conversion and clock", ["reached_win", "conversion", "swindle", "lost_alive", "time_move", "end_clock"], False),
    ]
    for tc in TIME_CLASSES:
        part = long[long["time_class"] == tc]
        if part.empty:
            continue
        report.heading(tc, level=3)
        for title, keys, with_diff in groups:
            report.text(f"*{title}*")
            report.table(compact(part, "year", keys, with_diff=with_diff))
    return long


def terciles(games: pd.DataFrame) -> pd.Series:
    """Low / mid / high third of games by pre-game rating, within time class."""
    out = pd.Series(index=games.index, dtype=object)
    for tc, part in games.groupby("time_class"):
        ranks = part["my_rating_before"].rank(method="first")
        out.loc[part.index] = pd.qcut(ranks, 3, labels=["low", "mid", "high"]).astype(str)
    return out


def what_moved(games: pd.DataFrame, n_boot: int = N_BOOT) -> pd.DataFrame:
    """For each time class and metric: you and the field in the low vs high third of games by rating, the changes and the difference of changes."""
    games = games.assign(third=terciles(games))
    rows = []
    for tc, part in games.groupby("time_class"):
        lo_part, hi_part = part[part["third"] == "low"], part[part["third"] == "high"]
        if min(len(lo_part), len(hi_part)) < MIN_GAMES:
            continue
        w_lo, w_hi = boot_weights(len(lo_part), n_boot, seed=SEED), boot_weights(len(hi_part), n_boot, seed=SEED + 1)
        for m in METRICS:
            my_lo, my_hi = side_arrays(lo_part, m, "my"), side_arrays(hi_part, m, "my")
            you_lo = ratio_ci(*my_lo, w_lo)
            you_hi = ratio_ci(*my_hi, w_hi)
            d_you = (w_hi @ my_hi[0]) / np.maximum(w_hi @ my_hi[1], 1e-12) - (w_lo @ my_lo[0]) / np.maximum(w_lo @ my_lo[1], 1e-12)
            row = {
                "time_class": tc, "metric": m["key"], "label": m["label"], "better": m["better"], "fmt": m["fmt"],
                "low_range": f"{int(lo_part['my_rating_before'].min())}-{int(lo_part['my_rating_before'].max())}",
                "high_range": f"{int(hi_part['my_rating_before'].min())}-{int(hi_part['my_rating_before'].max())}",
                "games_low": len(lo_part), "games_high": len(hi_part),
                "you_low": you_lo[0], "you_high": you_hi[0],
                "you_change": you_hi[0] - you_lo[0], "you_change_lo": float(np.quantile(d_you, 0.025)), "you_change_hi": float(np.quantile(d_you, 0.975)),
                "you_z": (you_hi[0] - you_lo[0]) / max(d_you.std(), 1e-12),
            }  # fmt: skip
            if m["game_level"]:
                row.update(them_low=np.nan, them_high=np.nan, them_change=np.nan, them_change_lo=np.nan, them_change_hi=np.nan, dd=np.nan, dd_lo=np.nan, dd_hi=np.nan)
            else:
                op_lo, op_hi = side_arrays(lo_part, m, "opp"), side_arrays(hi_part, m, "opp")
                them_lo, them_hi = ratio_ci(*op_lo, w_lo), ratio_ci(*op_hi, w_hi)
                d_them = (w_hi @ op_hi[0]) / np.maximum(w_hi @ op_hi[1], 1e-12) - (w_lo @ op_lo[0]) / np.maximum(w_lo @ op_lo[1], 1e-12)
                dd = d_you - d_them
                row.update(
                    them_low=them_lo[0], them_high=them_hi[0], them_change=them_hi[0] - them_lo[0],
                    them_change_lo=float(np.quantile(d_them, 0.025)), them_change_hi=float(np.quantile(d_them, 0.975)),
                    dd=(you_hi[0] - you_lo[0]) - (them_hi[0] - them_lo[0]), dd_lo=float(np.quantile(dd, 0.025)), dd_hi=float(np.quantile(dd, 0.975)),
                )  # fmt: skip
            rows.append(row)
    return pd.DataFrame(rows)


def section_what_moved(report: Report, games: pd.DataFrame) -> pd.DataFrame:
    report.heading("4. What moved: low third vs high third of your games by rating, ranked")
    moved = what_moved(games)
    report.text(
        "Games of each time class are split into thirds by your pre-game rating. For every metric: your value in the low and the high third, your change (high minus low, CI from a game bootstrap), the field's change over the same games (the opponents' value in the same thirds: the ladder), and your change minus the field's (negative for a 'lower is better' metric means you improved *more* than the level you were playing at). "
        "Ranked by the significance (|z|) of your change. A metric that moved for both you and the field in step is a property of the level; one that moved for you but not for the field is yours."
    )
    for tc in TIME_CLASSES:
        part = moved[moved["time_class"] == tc].copy()
        if part.empty:
            continue
        part["abs_z"] = part["you_z"].abs()
        part = part.sort_values("abs_z", ascending=False)
        report.heading(f"{tc}: low third {part['low_range'].iloc[0]} ({part['games_low'].iloc[0]} games) vs high third {part['high_range'].iloc[0]} ({part['games_high'].iloc[0]} games)", level=3)
        table = pd.DataFrame({
            "metric": part["label"], "better": part["better"],
            "you: low third": [f"{v:{f}}" for v, f in zip(part["you_low"], part["fmt"])],
            "you: high third": [f"{v:{f}}" for v, f in zip(part["you_high"], part["fmt"])],
            "your change": [fmt(a, b, c, f) for a, b, c, f in zip(part["you_change"], part["you_change_lo"], part["you_change_hi"], part["fmt"])],
            "field's change (opponents, same games)": [fmt(a, b, c, f) for a, b, c, f in zip(part["them_change"], part["them_change_lo"], part["them_change_hi"], part["fmt"])],
            "your change - field's": [fmt(a, b, c, f) for a, b, c, f in zip(part["dd"], part["dd_lo"], part["dd_hi"], part["fmt"])],
        })  # fmt: skip
        report.table(table)

    # sensitivity: without games played after a break, and within the main time control only
    sens = []
    alts = {}
    for name, subset in (("excluding games after a break", games[~games["after_break"]]), ("main time control only (60+0 / 180+2 / 600+0)", games[games["tc"] == games["time_class"].map(MAIN_TC)])):
        alt = what_moved(subset, n_boot=200)
        alts[name] = alt
        merged = moved.merge(alt[["time_class", "metric", "you_change", "you_change_lo", "you_change_hi", "dd", "dd_lo", "dd_hi"]], on=["time_class", "metric"], suffixes=("", "_alt"))
        sig = merged[(merged["you_change_lo"] > 0) | (merged["you_change_hi"] < 0)]
        same_sign = (np.sign(sig["you_change"]) == np.sign(sig["you_change_alt"])).sum()
        keeps = (sig["you_change_lo_alt"] > 0) | (sig["you_change_hi_alt"] < 0)
        lost = ", ".join(f"{tc} {METRIC_BY_KEY[k]['label']}" for tc, k in zip(sig.loc[~keeps, "time_class"], sig.loc[~keeps, "metric"]))
        width = (merged["you_change_hi"] - merged["you_change_lo"]).replace(0, np.nan)
        shifts = ((merged["you_change_alt"] - merged["you_change"]).abs() / width)
        worst = merged.loc[shifts.idxmax()]
        sens.append(f"{name} ({len(subset):,} games): of the {len(sig)} 'your change' estimates whose CI excludes zero, {same_sign} keep their sign and {int(keeps.sum())} keep a CI excluding zero" + (f" (lost: {lost})" if lost else "") + f"; the largest shift of any estimate is {shifts.max():.2f} CI widths ({worst['time_class']} {METRIC_BY_KEY[worst['metric']]['label']}: {worst['you_change']:{METRIC_BY_KEY[worst['metric']]['fmt']}} becomes {worst['you_change_alt']:{METRIC_BY_KEY[worst['metric']]['fmt']}})")
    report.text("Reliability and time-control controls. Ratings are less reliable right after a break (`days_since_last_in_pool` > 7 days), and the time controls played changed between bands (section 1), so the table was recomputed " + "; ".join(sens) + ". The clock metrics are the ones that depend on the time control: read their bullet changes from the main-time-control version.")
    moved.attrs["main_tc"] = alts["main time control only (60+0 / 180+2 / 600+0)"]
    return moved


def section_regression(report: Report, games: pd.DataFrame) -> pd.DataFrame:
    report.heading("5. Partial dependence of rating on your per-game metrics (clustered regression)")
    keys = ["cp_move", "err20", "ep_opening", "ep_middlegame", "ep_endgame", "max_ep", "my_spoiled", "my_swindled", "spent_frac", "end_clock", "low_clock", "plies", "book_ply"]
    labels = {**{k: METRIC_BY_KEY[k]["label"] for k in keys if k in METRIC_BY_KEY}, "my_spoiled": "spoiled a winning position (reached >= 0.8, did not win)", "my_swindled": "swindled (fell to <= 0.2, did not lose)"}
    report.text(
        "A linear regression of your pre-game rating on your own per-game metrics, all standardised within the time class, plus controls for the time control (dummies) and for rating reliability (log of days since your last game in the pool). "
        "Each coefficient is the rating difference, in points, associated with one standard deviation of the metric holding the others fixed (a game-level description of which habits go with a higher rating, not a causal effect: rating moves slowly, metrics vary game to game, so coefficients are small relative to the span of the trajectory). "
        "CIs from a bootstrap over games (one row = one game). The second column adds year dummies, which absorb the trajectory itself and leave only within-year variation; a coefficient that survives is a metric that also distinguishes your better-rated games within a year. Metrics are your own moves and results only; `ep` per move overall is left out because it is the sum of the three phase terms."
    )
    rows = []
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc].copy()
        if len(part) < 100:
            continue
        feats = {}
        for key in keys:
            if key in METRIC_BY_KEY:
                num, den = side_arrays(part, METRIC_BY_KEY[key], "my")
                with np.errstate(invalid="ignore", divide="ignore"):
                    feats[key] = np.where(den > 0, num / np.where(den > 0, den, 1), np.nan)
            else:
                feats[key] = part[key].to_numpy(dtype=float)
        F = pd.DataFrame(feats, index=part.index)
        F = F.fillna(F.mean())
        sd = F.std().replace(0, 1)
        Z = (F - F.mean()) / sd
        controls = pd.get_dummies(part["tc"], prefix="tc", dtype=float).iloc[:, 1:]
        controls["log_days"] = np.log1p(part["days_since_last_in_pool"].fillna(0).to_numpy(dtype=float))
        y = part["my_rating_before"].to_numpy(dtype=float)
        for with_year in (False, True):
            X = pd.concat([Z, controls], axis=1)
            if with_year:
                X = pd.concat([X, pd.get_dummies(part["year"].astype(int), prefix="y", dtype=float).iloc[:, 1:]], axis=1)
            X.insert(0, "const", 1.0)
            beta, draws = cluster_ols(X.to_numpy(dtype=float), y)
            fitted = X.to_numpy(dtype=float) @ beta
            r2 = 1 - ((y - fitted) ** 2).sum() / ((y - y.mean()) ** 2).sum()
            for i, name in enumerate(X.columns):
                if name in keys:
                    rows.append({"time_class": tc, "metric": labels[name], "key": name, "with_year": with_year, "coef": beta[i], "lo": np.quantile(draws[:, i], 0.025), "hi": np.quantile(draws[:, i], 0.975), "r2": r2, "games": len(part)})
    coefs = pd.DataFrame(rows)
    for tc in TIME_CLASSES:
        part = coefs[coefs["time_class"] == tc]
        if part.empty:
            continue
        base = part[~part["with_year"]].set_index("key")
        year = part[part["with_year"]].set_index("key")
        order = base["coef"].abs().sort_values(ascending=False).index
        table = pd.DataFrame({
            "metric": base.loc[order, "metric"],
            "rating points per 1 SD": [fmt(*base.loc[k, ["coef", "lo", "hi"]], ".0f") for k in order],
            "same, with year dummies": [fmt(*year.loc[k, ["coef", "lo", "hi"]], ".0f") for k in order],
        })  # fmt: skip
        report.heading(f"{tc} ({int(base['games'].iloc[0]):,} games; R2 {base['r2'].iloc[0]:.2f} without year, {year['r2'].iloc[0]:.2f} with year dummies)", level=3)
        report.table(table)
    return coefs


def section_upsets(report: Report, games: pd.DataFrame) -> dict:
    report.heading("6. Performance vs opponent rating and upsets (question 11)")
    rated = games[games["rated"].astype(bool) & games["rating_diff"].notna()].copy()
    unrated = games[~games["rated"].astype(bool) & games["rating_diff"].notna()].copy()
    abs_gap = rated["rating_diff"].abs()
    report.text(
        f"The opponent's pre-game rating is reconstructed as their post-game rating plus your rating change in rated games (the two changes are approximately opposite under Glicko; exact only when both rating deviations are equal) and as the post-game rating itself in unrated games, where ratings do not move. "
        f"Rating difference = opponent minus you (positive = stronger opponent). In the {len(rated):,} rated games the median |difference| is {abs_gap.median():.0f} points and {(abs_gap <= 50).mean():.1%} of games are within 50 points: the pairing is tight, so the outer bins are small. "
        "Expected score is the Elo logistic 1/(1+10^(diff/400)) (chess.com uses Glicko; the logistic is the standard approximation). 'score - expected' is the mean over games of your score minus that expectation, CI from a game bootstrap. Error rates are per move for both sides in the same games."
    )
    bin_metrics = [METRIC_BY_KEY[k] for k in ("ep_move", "cp_move", "err20", "conversion")]

    def by_bin(frame: pd.DataFrame, by: str) -> tuple[pd.DataFrame, pd.DataFrame]:
        rows, detail = [], []
        for key, part in frame.groupby(by, observed=True):
            if len(part) < MIN_GAMES:
                continue
            w = boot_weights(len(part))
            score = ratio_ci(part["my_score"], part["one"], w)
            expected = ratio_ci(part["elo_expected"], part["one"], w)
            excess = ratio_ci(part["my_score"] - part["elo_expected"], part["one"], w)
            change = pd.to_numeric(part["my_rating_change"], errors="coerce").astype(float)
            row = {by: key, "games": len(part), "mean diff": f"{part['rating_diff'].mean():.0f}", "score": fmt(*score), "Elo expected": f"{expected[0]:.3f}", "score - expected": fmt(*excess), "wins/draws/losses": f"{int(part['win'].sum())}/{int(part['draw'].sum())}/{int(part['loss'].sum())}",
                   "rating change per win / loss": f"{change[part['win'] == 1].mean():+.1f} / {change[part['loss'] == 1].mean():+.1f}" if part["rated"].all() else ""}
            for m in bin_metrics:
                c = cell(part, m, w)
                row[f"{m['label'].split(':')[0]}: you"] = fmt(c["you"], c["you_lo"], c["you_hi"], m["fmt"]) if c["n_you"] >= 30 else ""
                row[f"{m['label'].split(':')[0]}: them"] = fmt(c["them"], c["them_lo"], c["them_hi"], m["fmt"]) if c["n_them"] >= 30 else ""
                detail.append({by: key, "metric": m["key"], **c, "score": score[0], "expected": expected[0], "excess": excess[0], "excess_lo": excess[1], "excess_hi": excess[2]})
            rows.append(row)
        return pd.DataFrame(rows), pd.DataFrame(detail)

    results = {}
    report.heading("All rated games, by rating difference", level=3)
    table, detail = by_bin(rated, "diff_bin")
    report.table(table)
    results["pooled"] = detail
    # is the excess a feature of the early climb (low ratings, thin pool) or general?
    split_rows = []
    rated["low_level"] = rated["my_rating_before"] < 700
    for key, part in rated.groupby("diff_bin", observed=True):
        row = {"diff_bin": key}
        for low, label in ((True, "your rating < 700"), (False, "your rating >= 700")):
            sub = part[part["low_level"] == low]
            row[f"games, {label}"] = len(sub)
            row[f"score - expected, {label}"] = fmt(*ratio_ci(sub["my_score"] - sub["elo_expected"], sub["one"], boot_weights(len(sub)))) if len(sub) >= MIN_GAMES else ""
        split_rows.append(row)
    report.text("The same excess split by your own level at the time (below / from 700, all time classes), since wide pairings were more common when you were low-rated and climbing fast:")
    report.table(pd.DataFrame(split_rows))
    # diagnostic: do the rating changes scale with the difference as Elo says (a sign that the outer-bin opponents are not provisional accounts)?
    even = rated[rated["diff_bin"] == "0..25"]
    strong = rated[rated["diff_bin"] == "> 100"]
    weak = rated[rated["diff_bin"] == "<= -100"]
    ch = lambda f, col: pd.to_numeric(f.loc[f[col] == 1, "my_rating_change"], errors="coerce").astype(float).mean()
    if len(even) and len(strong) and len(weak):
        report.text(
            f"Check on the reconstruction: with a fixed K factor, Elo predicts the gain for a win to scale with 1 − expected score. Your mean gain for a win was {ch(even, 'win'):+.1f} against even opponents (0..25), {ch(strong, 'win'):+.1f} against opponents more than 100 points stronger (Elo ratio {(1 - strong['elo_expected'].mean()) / (1 - even['elo_expected'].mean()):.2f}, observed {ch(strong, 'win') / ch(even, 'win'):.2f}) and {ch(weak, 'win'):+.1f} against opponents more than 100 points weaker (Elo ratio {(1 - weak['elo_expected'].mean()) / (1 - even['elo_expected'].mean()):.2f}, observed {ch(weak, 'win') / ch(even, 'win'):.2f}); losses cost {ch(even, 'loss'):+.1f} / {ch(strong, 'loss'):+.1f} / {ch(weak, 'loss'):+.1f}. "
            "Changes that scale roughly as Elo predicts mean the outer-bin opponents were not, on the whole, provisional accounts with inflated or deflated ratings (where your change would be much smaller than Elo's). "
            "Note also the direction of the reconstruction bias: a high-deviation opponent loses more than you gain, which would push wins into the 'weaker' bins and losses into the 'stronger' bins, i.e. towards *under*-performance against stronger opponents, so an excess against stronger opponents is not an artefact of it."
        )
    for tc in TIME_CLASSES:
        part = rated[rated["time_class"] == tc]
        if len(part) < MIN_GAMES:
            continue
        report.heading(f"{tc}, rated games", level=3)
        t, d = by_bin(part, "diff_bin")
        report.table(t[["diff_bin", "games", "mean diff", "score", "Elo expected", "score - expected", "ep lost per move: you", "ep lost per move: them", "moves losing >= 0.2 ep, per 100 moves: you", "moves losing >= 0.2 ep, per 100 moves: them"]])
        results[tc] = d

    # unrated check with coarse bins (the opponent's rating there is exact)
    unrated["coarse"] = pd.cut(unrated["rating_diff"], [-np.inf, -100, 0, 100, np.inf], labels=["<= -100", "-100..0", "0..100", "> 100"])
    report.heading("Unrated games (opponent's rating exact, no reconstruction)", level=3)
    report.text(f"{len(unrated):,} unrated games, coarse bins because the pairing is loose there (median |difference| {unrated['rating_diff'].abs().median():.0f}):")
    t, d = by_bin(unrated, "coarse")
    report.table(t[["coarse", "games", "mean diff", "score", "Elo expected", "score - expected", "wins/draws/losses", "ep lost per move: you", "ep lost per move: them"]])
    results["unrated"] = d

    # figure
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(13, 3.8), sharey=True)
    for ax, tc in zip(axes, TIME_CLASSES):
        d = results.get(tc)
        if d is None or d.empty:
            continue
        d = d[d["metric"] == "ep_move"]
        x = np.arange(len(d))
        ax.plot(x, d["expected"], color="grey", ls="--", marker="s", ms=3, label="Elo expected")
        ax.errorbar(x, d["score"], yerr=[d["excess"] - d["excess_lo"], d["excess_hi"] - d["excess"]], color=SIDE_COLOURS["player"], marker="o", ms=4, capsize=2, label="your score")
        ax.set_xticks(x)
        ax.set_xticklabels(d["diff_bin"].astype(str), rotation=30, fontsize=8)
        ax.set_title(f"{tc} (rated)")
        ax.set_xlabel("opponent rating - yours (pre-game)")
    axes[0].set_ylabel("score")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    report.figure(fig, "score_vs_rating_diff", "Your score vs the Elo expectation by rating-difference bin, rated games (error bars: CI of score - expected, centred on the score).")
    plt.close(fig)

    # upsets: how they happened
    report.heading(f"Upsets: wins against opponents at least {UPSET_GAP} points stronger, losses against opponents at least {UPSET_GAP} weaker", level=3)
    frame = games[games["rating_diff"].notna()].copy()
    cats = {
        f"upset wins (opponent >= +{UPSET_GAP})": frame[(frame["win"] == 1) & (frame["rating_diff"] >= UPSET_GAP)],
        "ordinary wins (within 25 points)": frame[(frame["win"] == 1) & (frame["rating_diff"].abs() <= 25)],
        f"upset losses (opponent <= -{UPSET_GAP})": frame[(frame["loss"] == 1) & (frame["rating_diff"] <= -UPSET_GAP)],
        "ordinary losses (within 25 points)": frame[(frame["loss"] == 1) & (frame["rating_diff"].abs() <= 25)],
    }
    rows = []
    for name, part in cats.items():
        if len(part) < MIN_GAMES:
            rows.append({"games": name, "n": len(part)})
            continue
        w = boot_weights(len(part))
        is_win = name.endswith("wins") or "wins" in name
        loser_prefix, winner_prefix = ("opp", "my") if is_win else ("my", "opp")
        row = {"games": name, "n": len(part), "rated": f"{part['rated'].mean():.2f}", "bullet/blitz/rapid": "/".join(str(int((part['time_class'] == tc).sum())) for tc in TIME_CLASSES)}
        row["loser flagged"] = fmt(*ratio_ci(part[f"{loser_prefix}_flag_loss"], part["one"], w))
        row["loser resigned"] = fmt(*ratio_ci((part[f"{'my' if not is_win else 'opponent'}_result"] == "resigned").astype(float), part["one"], w))
        row["loser was still alive (P(win) >= 0.1)"] = fmt(*ratio_ci(part[f"{loser_prefix}_lost_alive"], part["one"], w))
        row["loser's decisive blunder found"] = fmt(*ratio_ci(part["decisive_found"].fillna(0), part["one"], w))
        row["your ep lost per move"] = fmt(*ratio_ci(part["my_ep_lost"], part["my_moves"], w), ".4f")
        row["their ep lost per move"] = fmt(*ratio_ci(part["opp_ep_lost"], part["opp_moves"], w), ".4f")
        row["your cp lost per move"] = fmt(*ratio_ci(part["my_cp_lost"], part["my_moves"], w), ".0f")
        row["their cp lost per move"] = fmt(*ratio_ci(part["opp_cp_lost"], part["opp_moves"], w), ".0f")
        row["plies"] = fmt(*ratio_ci(part["n_plies"], part["one"], w), ".0f")
        rows.append(row)
    report.text("How the upsets happened, next to ordinary wins and losses against evenly matched opponents (all games, rated and unrated; 'loser' is the opponent in the win rows and you in the loss rows):")
    report.table(pd.DataFrame(rows))
    results["upsets"] = pd.DataFrame(rows)
    return results


# ---------------------------------------------------------------------------------------


def pick(moved: pd.DataFrame, tc: str, key: str) -> pd.Series:
    row = moved[(moved["time_class"] == tc) & (moved["metric"] == key)]
    return row.iloc[0] if len(row) else None


def section_summary(report: Report, games: pd.DataFrame, moved: pd.DataFrame, coefs: pd.DataFrame, upsets: dict) -> None:
    report.heading("What this means for the player")
    parts = []
    for tc in TIME_CLASSES:
        cp, ep, tm = (pick(moved, tc, k) for k in ("cp_move", "ep_move", "time_move"))
        tm_main = pick(moved.attrs["main_tc"], tc, "time_move")
        if cp is None or tm_main is None:
            continue
        part = moved[(moved["time_class"] == tc) & moved["dd_lo"].notna()]
        own = part[(part["dd_lo"] > 0) | (part["dd_hi"] < 0)]
        own_text = "; ".join(f"{r['label']} ({fmt(r['dd'], r['dd_lo'], r['dd_hi'], r['fmt'])}, {'good' if (r['better'] == 'lower') == (r['dd'] < 0) else 'bad' if r['better'] in ('lower', 'higher') else 'neutral'})" for _, r in own.iterrows()) or "none"
        parts.append(
            f"In {tc} (low third {cp['low_range']} vs high third {cp['high_range']}): centipawns lost per move went from {cp['you_low']:.0f} to {cp['you_high']:.0f} for you ({fmt(cp['you_change'], cp['you_change_lo'], cp['you_change_hi'], '.0f')}) and by {fmt(cp['them_change'], cp['them_change_lo'], cp['them_change_hi'], '.0f')} for the field, "
            f"expected points lost per move by {fmt(ep['you_change'], ep['you_change_lo'], ep['you_change_hi'], '.4f')} for you vs {fmt(ep['them_change'], ep['them_change_lo'], ep['them_change_hi'], '.4f')} for the field, seconds per move from {tm['you_low']:.2f} to {tm['you_high']:.2f} (field {tm['them_low']:.2f} to {tm['them_high']:.2f}; in {MAIN_TC[tc]} games only your change is {fmt(tm_main['you_change'], tm_main['you_change_lo'], tm_main['you_change_hi'], '.2f')} vs the field's {fmt(tm_main['them_change'], tm_main['them_change_lo'], tm_main['them_change_hi'], '.2f')}). "
            f"Metrics where your change differed from the field's (CI excluding zero): {own_text}."
        )
    pooled = upsets.get("pooled")
    if pooled is not None and not pooled.empty:
        ep = pooled[pooled["metric"] == "ep_move"].set_index("diff_bin")
        strong = ep.loc["> 100"] if "> 100" in ep.index else None
        weak = ep.loc["<= -100"] if "<= -100" in ep.index else None
        if strong is not None and weak is not None:
            parts.append(
                f"Against rated opponents more than 100 points stronger you scored {strong['score']:.3f} vs an Elo expectation of {strong['expected']:.3f} (excess {fmt(strong['excess'], strong['excess_lo'], strong['excess_hi'])}, {int(strong['games'])} games); against opponents more than 100 points weaker {weak['score']:.3f} vs {weak['expected']:.3f} (excess {fmt(weak['excess'], weak['excess_lo'], weak['excess_hi'])}, {int(weak['games'])} games)."
            )
    parts.append(
        "The picture: in absolute terms you blunder less than you used to because the level you play at does (the field's centipawn loss fell as much as yours or more), and in expected points you give away the same share of the game as the field at every level. "
        "What is yours rather than the level's is in the clock column and the conversion/swindle columns of section 2: how often you lose on time relative to the field, how much time you leave, and what you make of clear positions. The metrics that did not move relative to the field are the open items for the synthesis."
    )
    report.text(" ".join(parts))

    report.heading("Caveats")
    n_break = int(games["after_break"].sum())
    falls = []
    for tc in TIME_CLASSES:
        part = games[games["time_class"] == tc].sort_values("start_datetime")
        if part.empty:
            continue
        peak_i = part["my_rating_before"].idxmax()
        drop = part.loc[peak_i, "my_rating_before"] - part["my_rating_before"].iloc[-1]
        if drop >= 100:
            falls.append(f"{tc} fell {drop:.0f} points from its peak in {part.loc[peak_i, 'start_datetime']:%b %Y} to the last game")
    fallback = "; ".join(falls) if falls else "pools drift up and down"
    report.text(
        "\n".join(
            [
                "- Rating bands are your *pre-game* rating; the opponents' pre-game rating is reconstructed (post-game rating plus your rating change in rated games) and approximate under Glicko. It is used descriptively (matching, rating-difference bins), never as a predictor of the result beyond the Elo expectation it defines.",
                "- Because the pairing is tight, 'you vs opponents in the same band' compares you with the field at your level, not with an absolute standard; the trend across bands mixes your improvement with the change of level and, in bullet and blitz, with a change of time control (section 1 shows the main time control per year; section 4 reports the main-time-control-only sensitivity).",
                "- `ep_loss` is calibrated with both players' ratings as features, so expected points lost per move is nearly flat in rating by construction; the absolute improvement shows in `cp_loss` and blunder counts, the relative one (how much of the game you give away at your level, vs the field) in `ep_loss`.",
                f"- Rating reliability: {n_break} games were played after a break of more than {BREAK_DAYS} days in the pool, where the rating is a weaker measure of strength; excluding them changes little (section 4). The regression in section 5 controls for log(days since the last game in the pool).",
                f"- Rating bands and years overlap heavily, so 'by band' and 'by year' are two views of one trajectory; the trajectory is not monotone ({fallback}), so a band can pool games from the climb and from a later fall; 2024 is thin (section 1).",
                f"- P(win) thresholds (conversion, swindle, 'still alive') use the pooled calibrated model ({calibration_brier()}), which is under-confident at the extremes and keeps flag chances alive in bullet, so about half of bullet checkmates count as 'still alive'; the you-vs-them comparison within a band is the robust part.",
                "- The regression in section 5 is descriptive: it reports which per-game habits co-vary with rating holding the others fixed; metrics are noisy per game and collinear (cp per move and big-error rate), so individual coefficients are wide and the signs, not the magnitudes, are the message.",
                f"- Upset cells are small (section 6): most rated games are within 50 points; results in the outer bins rest on tens of games. The reconstruction of the opponent's rating biases a win slightly towards the 'weaker opponent' bins (your gain is smaller than a high-deviation opponent's loss) and a loss towards the 'stronger' bins, which would produce a spurious over-performance against weaker and under-performance against stronger opponents, the opposite of what is observed; the unrated games, where the rating is exact, are a second check.",
                f"- Cells with fewer than {MIN_GAMES} games or 30 moves are blank; CIs use {N_BOOT} game-bootstrap resamples (200 in the sensitivity checks).",
            ]
        )
    )


def main() -> None:
    t0 = time.time()
    figure_style()
    data = load_data()
    built = build_games(data)
    games = add_loss_counts(built)
    games.attrs.update(built.attrs)
    report = Report(SLUG, "Rating trajectory: what changed as you improved (and performance vs opponent rating)")
    counts = games.groupby("time_class").size()
    report.text(
        f"Data: {len(games):,} standard live games with at least one move and a real pre-game rating (the first game in each pool has none; {games.attrs.get('dropped_zero_rating', 0)} game(s) with a rating of 0 dropped) ({', '.join(f'{counts.get(tc, 0):,} {tc}' for tc in TIME_CLASSES)}), {int(games['n_plies'].sum()):,} analysed moves ({int(games['my_moves'].sum()):,} yours), player {data.username}, "
        f"{games['start_datetime'].min():%b %Y} to {games['start_datetime'].max():%b %Y}. Per-game metrics come from Q1's decomposition (`games_decomposed.parquet`) joined on `uuid`; clock and phase-count metrics are computed here from the moves. "
        f"Numbers are estimate [95% CI], bootstrapped by game ({N_BOOT} resamples); every number about you sits next to the opponents' number from the same games, and differences are paired. `ep_loss` (calibrated expected points) first, `cp_loss` second."
    )
    section_trajectory(report, games)
    section_by_band(report, games)
    section_by_year(report, games)
    moved = section_what_moved(report, games)
    coefs = section_regression(report, games)
    upsets = section_upsets(report, games)
    section_summary(report, games, moved, coefs, upsets)
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
