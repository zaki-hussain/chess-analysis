"""Q5: sessions - how long you play, when you stop, fatigue, warm-up and tilt.

Does play degrade over a session (fatigue), start poorly (warm-up), or
collapse after losses (tilt), and does a long break matter? Sessions are
recomputed over the standard live games (`chess_analysis.sessions`, shared
with Q6). The stopping rule (you stop more often after a loss) is measured
first because it bends every curve by game number: fatigue is read from
game k paired with game 1 of the same session, a session-demeaned
regression and a fixed set of long sessions (the raw curves, with and
without the game you stopped after, are kept as a table). Tilt is the
next game's score against the calibration model's expectation (with an
Elo-equivalent) and both sides' move quality, by the previous result, the
losing streak and the previous two results. Everything is split by the
game's own exact time control (`data.tc_groups`) plus an "all games" pool;
session-level descriptions use each session's main control. CIs resample
whole sessions.

Writes CSVs to presentation/data/05_sessions/ and SVGs to
presentation/figures/05_sessions/ (no prose; the reading is in
presentation/README.md).

Run: .venv/bin/python analysis/05_sessions.py   (about 40 seconds)
"""

from __future__ import annotations

import time
import warnings

import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis import sessions as S
from chess_analysis.config import load_settings
from chess_analysis.toolkit import Results, cluster_ols, dummies, excess_to_elo, load_data, repo_root

SLUG = "05_sessions"
ALL = "all games"  # pooled row/panel over every reported control (thin controls excluded)
N_BOOT = S.N_BOOT
SEED = 5

# A cell resting on fewer games than this has a score CI wider than about +-0.18 (a fifth of the whole
# win-to-loss range): it is computed and kept, but flagged thin and not drawn.
MIN_CELL_GAMES = 30
# Every CI resamples whole sessions: with fewer sessions than this the bootstrap has too few clusters to give
# a trustworthy interval, so a cell is also thin under this many sessions (and paired comparisons count sessions).
MIN_SESSIONS = 30
# OLS needs about this many games per coefficient before the estimates stop jumping between resamples.
MIN_GAMES_PER_COEF = 10

# Session gaps for the sensitivity check, as multiples of the configured gap: half, the default, double.
GAP_CHECK_FACTORS = (0.5, 1.0, 2.0)

# Games-per-session histogram: single counts while sessions are common, widening as they thin out.
SESSION_GAMES_EDGES = [1, 2, 3, 4, 6, 10, 16, np.inf]
SESSION_GAMES_LABELS = ["1", "2", "3", "4-5", "6-9", "10-15", "16+"]
# Session duration (first start to last end), roughly doubling bins.
SESSION_MINUTE_EDGES = [0, 10, 20, 40, 60, 120, np.inf]
SESSION_MINUTE_LABELS = ["0-10", "10-20", "20-40", "40-60", "60-120", "120+"]
LONG_SESSION_GAMES = 10  # "a long sitting" in any control: ten games
LONG_SESSION_MINUTES = 60  # and an hour, the length people think of as a long session

# Game number in the session: single numbers while most sessions are still going (over a third of
# sessions reach game 4), then wider bins as sessions end.
GAME_NUMBER_EDGES = [1, 2, 3, 4, 6, 10, np.inf]
GAME_NUMBER_LABELS = ["1", "2", "3", "4-5", "6-9", "10+"]
FATIGUE_FROM = 5  # the sensitivity check's fatigue contrast: games 5+ vs game 1 (half of sessions end by game 3)
# Third fatigue check: only sessions that reached this many games, compared over their games before it (none of
# which is the last game). The same sessions feed every game number, so the stopping rule cannot change the mix.
FIXED_SESSION_GAMES = 6
MINUTES_SCALE = 30  # regression coefficient per half hour into the session

STREAK_CAP = 3  # three or more losses (wins) in a row are pooled: under 5% of games follow such a streak
RESULTS = ["win", "draw", "loss"]
TWO_BEFORE = ["WW", "LW", "WL", "LL"]  # previous two results, older first (draws dropped)

# Pause since the previous game (any control). The edge at the configured session gap is inserted at
# run time: below it the game continues a session, above it a new session starts.
BREAK_EDGES_BEFORE_GAP = [0, 1, 5]  # under a minute = straight re-queue; 1-5 minutes = a short pause
BREAK_EDGES_AFTER_GAP = [180, 1440, 4320, np.inf]  # 3 hours (same part of the day), a day, three days
BREAK_LABELS_BEFORE_GAP = ["<1 min", "1-5 min"]
BREAK_LABELS_AFTER_GAP = ["gap-3 h", "3-24 h", "1-3 days", "3+ days"]


# --- helpers ------------------------------------------------------------------------------------------


def groups_with_all(data) -> list[str]:
    return [*data.tc_groups, ALL]


def subset(games: pd.DataFrame, group: str) -> pd.DataFrame:
    return games if group == ALL else games[games["tc_group"] == group]


def counts(part: pd.DataFrame) -> dict:
    return {"games": int(len(part)), "sessions": int(part["session_id"].nunique())}


def thin_cell(part: pd.DataFrame, min_games: int = MIN_CELL_GAMES) -> bool:
    """Too few games, or too few sessions for the session bootstrap."""
    return len(part) < min_games or part["session_id"].nunique() < MIN_SESSIONS


def label_bins(values: pd.Series, edges, labels) -> pd.Series:
    return pd.cut(values, edges, labels=labels, right=False).astype(str).replace("nan", np.nan)


def by_level(games: pd.DataFrame, groups: list[str], column: str, levels: list[str], names: list[str], min_games: int = MIN_CELL_GAMES) -> pd.DataFrame:
    """Metrics per (group, level of `column`), tidy, with counts and a thin flag."""
    rows = []
    for group in groups:
        part_g = subset(games, group)
        for level in levels:
            part = part_g[part_g[column] == level]
            rows.append({"tc_group": group, column: level, **S.cell_stats(part, names), **counts(part), "thin": thin_cell(part, min_games)})
    return pd.DataFrame(rows)


def blank_thin(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Copy for drawing: thin rows' estimates set to NaN so they are not drawn as confident points."""
    out = frame.copy()
    for c in columns:
        if c in out:
            out.loc[out["thin"], c] = np.nan
    return out


def thin_panel(ax, text: str = "too few games") -> None:
    """charts.thin_note without clearing ticks (on shared axes that would clear every panel's ticks)."""
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
    ax.tick_params(axis="x", labelbottom=False, length=0)  # y labels stay: with shared axes they may be the row's only ones


def panel_estimates(ax, frame: pd.DataFrame, x: str, order: list[str], y: str = "excess", colour: str = charts.PLAYER, label: str | None = None, offset: float = 0.0) -> bool:
    """Dots + CIs for one series in one panel; thin cells left out. Returns False when nothing could be drawn."""
    part = blank_thin(frame, [y, f"{y}_lo", f"{y}_hi"])
    if part[y].notna().sum() == 0:
        return False
    charts.estimate(ax, part, x, y=y, lo=f"{y}_lo", hi=f"{y}_hi", order=order, colour=colour, label=label, offset=offset)
    ax.set_xlim(-0.5, len(order) - 0.5)
    return True


def finish_grid(fig, axes: dict, drawn: dict, ylabel: str, xlabel: str | None = None, rotate: int = 0, thin_text: str = "too few games") -> None:
    for group, ax in axes.items():
        if not drawn.get(group):
            thin_panel(ax, thin_text)
            continue
        charts.zero_line(ax)
        if rotate:
            plt_labels = ax.get_xticklabels()
            ax.set_xticks(ax.get_xticks(), [t.get_text() for t in plt_labels], rotation=rotate, ha="right", rotation_mode="anchor")
    first_col = {}
    for group, ax in axes.items():
        row = round(ax.get_position().y0, 3)
        first_col.setdefault(row, ax)
    for ax in first_col.values():
        ax.set_ylabel(ylabel)
    if xlabel:
        for ax in axes.values():
            ax.set_xlabel(xlabel)
    fig.tight_layout()


def contrast_figure(frame: pd.DataFrame, labels: list[str], title: str, contrast: str, ep_cols: tuple[str, str], thin_text: str = "too few games"):
    """Two panels with the controls on the y axis: score minus expectation (left), you vs opponents' expected-points change (right)."""
    import matplotlib.pyplot as plt

    fig, (left, right) = plt.subplots(1, 2, figsize=(9.2, 0.42 * len(labels) + 1.4), sharey=True)
    shown = blank_thin(frame, ["excess_diff", "excess_diff_lo", "excess_diff_hi", *[f"{c}{s}" for c in ep_cols for s in ("", "_lo", "_hi")]])
    charts.estimate(left, shown, "tc_group", y="excess_diff", lo="excess_diff_lo", hi="excess_diff_hi", order=labels, horizontal=True, colour=charts.PLAYER)
    charts.estimate(right, shown, "tc_group", y=ep_cols[0], lo=f"{ep_cols[0]}_lo", hi=f"{ep_cols[0]}_hi", order=labels, horizontal=True, colour=charts.PLAYER, label="you", offset=-0.15)
    charts.estimate(right, shown, "tc_group", y=ep_cols[1], lo=f"{ep_cols[1]}_lo", hi=f"{ep_cols[1]}_hi", order=labels, horizontal=True, colour=charts.OPPONENT, label="opponents", offset=0.15)
    if not left.yaxis_inverted():  # estimate() flips the shared axis on every call: first control on top
        left.invert_yaxis()
    for ax in (left, right):
        charts.zero_line(ax, horizontal=False)
        ax.grid(axis="y", visible=False)
    thin_rows = frame.set_index("tc_group")["thin"].reindex(labels)
    for i, group in enumerate(labels):
        if bool(thin_rows.get(group, True)):
            left.text(0.02, i, thin_text, transform=left.get_yaxis_transform(), va="center", fontsize=7.5, color=charts.INK_MUTED)
    left.set_title("Score vs model expectation")
    right.set_title("Expected points lost per move")
    left.set_xlabel(f"{contrast}, score minus expectation")
    right.set_xlabel(f"{contrast}, expected points lost per move")
    charts.legend(right, loc="lower right")
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    fig.tight_layout()
    return fig


# --- 5.1 session length -------------------------------------------------------------------------------


def session_table(games: pd.DataFrame) -> pd.DataFrame:
    """One row per session: main control, games, minutes, controls mixed."""
    return games.groupby("session_id").agg(tc_group=("session_main_tc", "first"), games=("uuid", "size"), minutes=("session_minutes", "first"), controls=("session_controls", "first")).reset_index()


def q1_length(results: Results, games: pd.DataFrame, all_games: pd.DataFrame, data) -> None:
    sessions = session_table(all_games)
    sessions = sessions[sessions["tc_group"].isin(data.tc_groups)]
    rows, hist_games, hist_minutes = [], [], []
    for group in groups_with_all(data):
        s = sessions if group == ALL else sessions[sessions["tc_group"] == group]
        g = subset(games, group)
        rows.append({
            "tc_group": group, "games_per_session_median": float(s["games"].median()), "games_per_session_mean": float(s["games"].mean()),
            "share_single_game": float((s["games"] == 1).mean()), "share_long_games": float((s["games"] >= LONG_SESSION_GAMES).mean()), "long_games_threshold": LONG_SESSION_GAMES,
            "minutes_median": float(s["minutes"].median()), "share_long_minutes": float((s["minutes"] >= LONG_SESSION_MINUTES).mean()), "long_minutes_threshold": LONG_SESSION_MINUTES,
            "share_sessions_mixing_controls": float((s["controls"] > 1).mean()), "share_games_in_mixed_sessions": float((g["session_controls"] > 1).mean()),
            "sessions": int(len(s)), "games_in_sessions": int(s["games"].sum()), "games_of_this_control": int(len(g)), "thin": len(s) < MIN_SESSIONS,
        })  # fmt: skip
        gbin = label_bins(s["games"], SESSION_GAMES_EDGES, SESSION_GAMES_LABELS)
        mbin = label_bins(s["minutes"], SESSION_MINUTE_EDGES, SESSION_MINUTE_LABELS)
        for label in SESSION_GAMES_LABELS:
            n = int((gbin == label).sum())
            hist_games.append({"tc_group": group, "games_per_session": label, "share": n / len(s) if len(s) else np.nan, "sessions": n, "sessions_total": int(len(s)), "thin": len(s) < MIN_SESSIONS})
        for label in SESSION_MINUTE_LABELS:
            n = int((mbin == label).sum())
            hist_minutes.append({"tc_group": group, "session_minutes": label, "share": n / len(s) if len(s) else np.nan, "sessions": n, "sessions_total": int(len(s)), "thin": len(s) < MIN_SESSIONS})
    table = pd.DataFrame(rows)
    results.table("1_session_length", table, "Sessions by their main control (the control with most games in the session): games per session, minutes, share mixing controls; plus the share of each control's games that sit in mixed sessions.")
    hist = pd.DataFrame(hist_games)
    results.table("1b_games_per_session", hist, "Histogram: share of sessions by number of games, by the session's main control.")
    results.table("1c_session_minutes", pd.DataFrame(hist_minutes), "Histogram: share of sessions by duration (first start to last end, minutes), by the session's main control.")

    fig, axes = charts.group_grid(data.tc_groups, sharey=True, title="How many games do you play in one sitting?")
    for group, ax in axes.items():
        h = hist[hist["tc_group"] == group]
        if h["thin"].all():
            thin_panel(ax, "too few sessions")
            continue
        ax.bar(range(len(SESSION_GAMES_LABELS)), h["share"].to_numpy(), color=charts.PLAYER, width=0.75)
        ax.set_xticks(range(len(SESSION_GAMES_LABELS)), SESSION_GAMES_LABELS)
        ax.tick_params(axis="x", labelsize=7)
        ax.grid(axis="x", visible=False)
        med = table.set_index("tc_group").loc[group]
        ax.text(0.97, 0.93, f"{int(med['sessions'])} sessions\nmedian {med['minutes_median']:.0f} min", transform=ax.transAxes, ha="right", va="top", fontsize=7.5, color=charts.INK_SECONDARY)
    for ax in axes.values():
        ax.set_xlabel("games in the session")
    first = {}
    for ax in axes.values():
        first.setdefault(round(ax.get_position().y0, 3), ax)
    for ax in first.values():
        ax.set_ylabel("share of sessions")
    fig.tight_layout()
    charts.footnote(fig, "Each session counted once, under the control it has most games of; a session can mix controls.")
    results.figure(fig, "1_session_length")


# --- 5.2 stopping rule --------------------------------------------------------------------------------


def q2_stopping(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    by_result = by_level(games, groups, "outcome", RESULTS, ["stop"])
    by_result = by_result.rename(columns={"outcome": "result", "stop": "p_stop", "stop_lo": "p_stop_lo", "stop_hi": "p_stop_hi"})
    results.table("2_stop_by_result", by_result, "Chance the game was the last of its session, by its result (CI clustered by session).")

    rows = []
    for group in groups:
        g = subset(games, group)
        loss, win = g[g["outcome"] == "loss"], g[g["outcome"] == "win"]
        d = S.diff_stats(loss, win, ["stop"])
        rows.append({"tc_group": group, "p_stop_loss_minus_win": d["stop_diff"], "p_stop_loss_minus_win_lo": d["stop_diff_lo"], "p_stop_loss_minus_win_hi": d["stop_diff_hi"],
                     "games_lost": len(loss), "games_won": len(win), "sessions": int(g["session_id"].nunique()), "thin": thin_cell(loss) or thin_cell(win)})  # fmt: skip
    results.table("2b_stop_loss_minus_win", pd.DataFrame(rows), "Chance of stopping after a loss minus after a win (same resampled sessions).")

    games = games.assign(game_bin=label_bins(games["game_in_session"], GAME_NUMBER_EDGES, GAME_NUMBER_LABELS))
    rows = []
    for group in groups:
        g = subset(games, group)
        for label in GAME_NUMBER_LABELS:
            for result in ("win", "loss"):
                part = g[(g["game_bin"] == label) & (g["outcome"] == result)]
                rows.append({"tc_group": group, "game_in_session": label, "result": result, **S.cell_stats(part, ["stop"]), **counts(part), "thin": thin_cell(part)})
    hazard = pd.DataFrame(rows).rename(columns={"stop": "p_stop", "stop_lo": "p_stop_lo", "stop_hi": "p_stop_hi"})
    results.table("2c_stop_by_game_number", hazard, "Chance of stopping after a win / a loss, by the game's number in the session: the gap is there at every game number.")

    rows = []
    for group in groups:
        g = subset(games, group)
        last, rest = g[g["is_last_in_session"]], g[~g["is_last_in_session"]]
        rows.append({"tc_group": group, **S.diff_stats(last, rest, ["excess", "my_ep", "opp_ep"]), "games_last": len(last), "games_other": len(rest), "thin": thin_cell(last) or thin_cell(rest)})
    results.table("2d_last_game_minus_others", pd.DataFrame(rows), "The game you stopped after minus the other games: score minus model expectation (with Elo at the last games' expectation) and both sides' expected points lost per move. This is why curves by game number exclude last games.")

    fig, axes = charts.group_grid(groups, sharey=True, title="How likely are you to stop after a win, a draw or a loss?")
    drawn = {}
    for group, ax in axes.items():
        drawn[group] = panel_estimates(ax, by_result[by_result["tc_group"] == group], "result", RESULTS, y="p_stop")
        if not drawn[group]:
            thin_panel(ax)
    first = {}
    for ax in axes.values():
        first.setdefault(round(ax.get_position().y0, 3), ax)
    for ax in first.values():
        ax.set_ylabel("chance it was your last game")
    next(iter(axes.values())).set_ylim(bottom=0)  # shared axis: a share starts at zero
    fig.tight_layout()
    charts.footnote(fig, "95% CIs resample whole sessions. Draws are rare; cells with too few games are left out.")
    results.figure(fig, "2_stop_by_result")


# --- 5.3 fatigue ----------------------------------------------------------------------------------------


def paired_vs_game1(g: pd.DataFrame, value: str, k_lo: float, k_hi: float) -> tuple[float, float, float, int, float]:
    """Game k (k_lo..k_hi, not the last of its session) minus game 1 of the same session, both in this control; mean over sessions."""
    first = g[g["game_in_session"] == 1].set_index("session_id")[value].rename("g1")
    later = g[(g["game_in_session"] >= k_lo) & (g["game_in_session"] <= k_hi) & ~g["is_last_in_session"]]
    per_session = later.groupby("session_id").agg(gk=(value, "mean"), expected=("my_ep_start", "mean")).join(first, how="inner").dropna(subset=["gk", "g1"]).reset_index()
    if per_session.empty:
        return np.nan, np.nan, np.nan, 0, np.nan
    est, lo, hi = S.paired_diff(per_session, "gk", "g1", "session_id")
    return est, lo, hi, int(len(per_session)), float(per_session["expected"].mean())


def regression_rows(sub: pd.DataFrame, group: str) -> list[dict]:
    """Pooled and session-demeaned OLS of per-game outcomes on game number, minutes, previous result (+ year, + control in the pool)."""
    rows = []
    X = pd.DataFrame({
        "intercept": 1.0,
        "log2(game number)": np.log2(sub["game_in_session"].astype(float)),
        f"minutes into session / {MINUTES_SCALE}": sub["minutes_into_session"] / MINUTES_SCALE,
        "previous game lost": (sub["prev_outcome"] == "loss").astype(float),
        "previous game won": (sub["prev_outcome"] == "win").astype(float),
    }, index=sub.index)  # fmt: skip
    years = sub["year"].astype(str)
    X = X.join(dummies(years, drop=years.value_counts().idxmax(), prefix="year"))
    if group == ALL:
        X = X.join(dummies(sub["tc_group"], drop=sub["tc_group"].value_counts().idxmax(), prefix="control"))
    within_terms = ["log2(game number)", f"minutes into session / {MINUTES_SCALE}"]  # the previous result is a lagged outcome: demeaned it is badly biased in panels this short
    shown = ["log2(game number)", f"minutes into session / {MINUTES_SCALE}", "previous game lost", "previous game won"]
    groups = sub["session_id"].to_numpy()
    outcomes = {"excess": sub["excess_model"], "my_ep": sub["my_ep_rate"], "opp_ep": sub["opp_ep_rate"], "ep_gap": sub["ep_rate_gap"]}
    expected = float(sub["my_ep_start"].mean())
    for model in ("pooled", "within"):
        for name, y in outcomes.items():
            ok = y.notna().to_numpy()
            if model == "pooled":
                Xm, ym, terms = X.to_numpy(float)[ok], y.to_numpy(float)[ok], list(X.columns)
            else:
                frame = X[within_terms].copy()
                frame["_y"] = y
                frame = frame[ok]
                frame = frame - frame.groupby(groups[ok]).transform("mean")
                Xm, ym, terms = frame[within_terms].to_numpy(float), frame["_y"].to_numpy(float), within_terms
            beta, draws = cluster_ols(Xm, ym, groups[ok], n_boot=N_BOOT, seed=SEED)
            with warnings.catch_warnings():  # a dummy absent from every resample of a thin group has no CI
                warnings.simplefilter("ignore", RuntimeWarning)
                lo, hi = np.nanquantile(draws, [0.025, 0.975], axis=0)
            n_coef = Xm.shape[1]
            for term, b, l, h in zip(terms, beta, lo, hi):
                if term not in shown:
                    continue
                row = {"tc_group": group, "model": model, "outcome": name, "term": term, "estimate": b, "lo": l, "hi": h}
                if name == "excess":
                    row["expected"] = expected
                    for k, v in (("estimate_elo", b), ("lo_elo", l), ("hi_elo", h)):
                        row[k] = float(excess_to_elo(expected, v)) if pd.notna(v) else np.nan
                rows.append({**row, "games": int(ok.sum()), "sessions": int(len(np.unique(groups[ok]))), "thin": ok.sum() < MIN_GAMES_PER_COEF * n_coef or len(np.unique(groups[ok])) < MIN_SESSIONS})
    return rows


def q3_fatigue(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    games = games.assign(game_bin=label_bins(games["game_in_session"], GAME_NUMBER_EDGES, GAME_NUMBER_LABELS))

    rows = []
    for group in groups:
        g = subset(games, group)
        for label, lo_k, hi_k in zip(GAME_NUMBER_LABELS[1:], GAME_NUMBER_EDGES[1:-1], [e - 1 for e in GAME_NUMBER_EDGES[2:]]):
            row = {"tc_group": group, "game_in_session": label}
            ex = paired_vs_game1(g, "excess_model", lo_k, hi_k)
            row.update({"expected": ex[4], "excess_diff": ex[0], "excess_diff_lo": ex[1], "excess_diff_hi": ex[2]})
            S.with_elo(row, prefix="excess_diff")
            for value, name in (("my_ep_rate", "my_ep"), ("opp_ep_rate", "opp_ep")):
                e = paired_vs_game1(g, value, lo_k, hi_k)
                row.update({f"{name}_diff": e[0], f"{name}_diff_lo": e[1], f"{name}_diff_hi": e[2]})
            row.update({"sessions": ex[3], "thin": ex[3] < MIN_SESSIONS})
            rows.append(row)
    paired = pd.DataFrame(rows)
    results.table("3_minus_game1", paired, "Game k minus game 1 of the same session (game k not the last of its session; both games in this control), per-game values averaged over sessions: score minus expectation (Elo at game k's expectation) and both sides' expected points lost per move. The main fatigue check.")

    rows = []
    for version, frame in (("all games", games), ("not the last of the session", games[~games["is_last_in_session"]])):
        t = by_level(frame, groups, "game_bin", GAME_NUMBER_LABELS, ["excess", "my_ep", "opp_ep"])
        t.insert(2, "version", version)
        rows.append(t)
    curve = pd.concat(rows, ignore_index=True).rename(columns={"game_bin": "game_in_session"})
    results.table("3b_game_number", curve, "Score minus model expectation (with Elo) and both sides' expected points lost per move by game number in the session, uncorrected; 'not the last of the session' drops the game you stopped after (which raises every bin).")

    rows = []
    for group in groups:
        sub = subset(games, group)
        sub = sub[~sub["is_last_in_session"]]
        rows.extend(regression_rows(sub, group))
    results.table("3c_regression", pd.DataFrame(rows), "Per-game OLS on non-last games, CIs by session: 'pooled' compares games across and within sessions (with year, and control in the pool), 'within' only within a session. Coefficients per doubling of the game number and per half hour.")

    fixed = games[(games["session_games"] >= FIXED_SESSION_GAMES) & (games["game_in_session"] < FIXED_SESSION_GAMES)]
    fixed_labels = [label for label, edge in zip(GAME_NUMBER_LABELS, GAME_NUMBER_EDGES) if edge < FIXED_SESSION_GAMES]
    table = by_level(fixed, groups, "game_bin", fixed_labels, ["excess", "my_ep", "opp_ep"]).rename(columns={"game_bin": "game_in_session"})
    table.insert(2, "min_session_games", FIXED_SESSION_GAMES)
    results.table("3d_fixed_sessions", table, f"Only sessions of {FIXED_SESSION_GAMES}+ games, their games 1 to {FIXED_SESSION_GAMES - 1} (none is the last game): the same sessions behind every game number, so stopping cannot change the mix.")

    fig, axes = charts.group_grid(groups, sharey=True, title="Do you score worse as a session goes on? (game k minus game 1 of the same session)")
    order = GAME_NUMBER_LABELS[1:]
    drawn = {g: panel_estimates(ax, paired[paired["tc_group"] == g], "game_in_session", order, y="excess_diff") for g, ax in axes.items()}
    finish_grid(fig, axes, drawn, "vs game 1, score", "game in session", thin_text="too few sessions")
    charts.footnote(fig, f"Score minus the model's expectation in game k minus game 1 of the same session (game k not the one you stopped after). 95% CIs resample sessions; under {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "3_minus_game1")


# --- 5.4 warm-up ----------------------------------------------------------------------------------------


def q4_warmup(results: Results, games: pd.DataFrame, data) -> None:
    rows = []
    for group in groups_with_all(data):
        g = subset(games, group)
        row = {"tc_group": group}
        first = g[g["game_in_session"] == 1]
        second = g[(g["game_in_session"] == 2) & ~g["is_last_in_session"]]
        pairs = first.merge(second, on="session_id", suffixes=("_1", "_2"), how="inner")
        for value, name in (("excess_model", "excess"), ("my_ep_rate", "my_ep"), ("opp_ep_rate", "opp_ep")):
            ok = pairs.dropna(subset=[f"{value}_1", f"{value}_2"])
            est, lo, hi = S.paired_diff(ok, f"{value}_2", f"{value}_1", "session_id") if len(ok) else (np.nan, np.nan, np.nan)
            row.update({f"{name}_game1": float(ok[f"{value}_1"].mean()), f"{name}_game2": float(ok[f"{value}_2"].mean())})
            if name == "excess":
                row["expected"] = float(ok["my_ep_start_2"].mean())
            row.update({f"{name}_diff": est, f"{name}_diff_lo": lo, f"{name}_diff_hi": hi})
            if name == "excess":
                S.with_elo(row, prefix="excess_diff")
        row.update({"sessions": int(len(pairs)), "thin": len(pairs) < MIN_SESSIONS})
        rows.append(row)
    table = pd.DataFrame(rows)
    results.table("4_warmup", table, "Game 2 minus game 1 of the same session (sessions of 3+ games, so game 2 is not the one you stopped after; both games in this control): score minus expectation (Elo at game 2's expectation) and both sides' expected points lost per move, averaged over sessions.")
    fig = contrast_figure(table, groups_with_all(data), "Do you start a session cold?", "game 2 minus game 1", ("my_ep_diff", "opp_ep_diff"), thin_text="too few sessions")
    charts.footnote(fig, "Paired within sessions of 3+ games; 95% CIs resample sessions. Right of zero on the right panel = more expected points lost in game 2.")
    results.figure(fig, "4_warmup")


# --- 5.5 after a loss -----------------------------------------------------------------------------------


def q5_after_result(results: Results, games: pd.DataFrame, data) -> None:
    later = games[games["game_in_session"] >= 2]
    groups = groups_with_all(data)
    t = by_level(later, groups, "prev_outcome", RESULTS, ["excess", "excess_rating", "my_ep", "opp_ep", "ep_gap", "lost_on_time", "resign_share"])
    t = t.rename(columns={"prev_outcome": "previous_result"})
    results.table("5_after_result", t, "Games 2+ of a session by the previous game's result: score minus the model's expectation (with Elo) and minus the Elo-formula expectation, both sides' expected points lost per move and their difference, share lost on time, share of losses resigned.")
    rows = []
    for group in groups:
        g = subset(later, group)
        loss, win = g[g["prev_outcome"] == "loss"], g[g["prev_outcome"] == "win"]
        rows.append({"tc_group": group, **S.diff_stats(loss, win, ["excess", "my_ep", "opp_ep", "ep_gap", "lost_on_time"]), "games_after_loss": len(loss), "games_after_win": len(win), "sessions": int(g["session_id"].nunique()), "thin": thin_cell(loss) or thin_cell(win)})
    table = pd.DataFrame(rows)
    results.table("5b_after_loss_minus_after_win", table, "After a loss minus after a win (same resampled sessions): score minus expectation (Elo at the after-loss expectation), both sides' expected points lost per move, their gap, share lost on time.")
    fig = contrast_figure(table, groups, "What happens in the game after a loss?", "after a loss minus after a win", ("my_ep_diff", "opp_ep_diff"))
    charts.footnote(fig, "Games 2+ of a session; 95% CIs resample whole sessions. Opponents' numbers come from the same games.")
    results.figure(fig, "5_after_loss")


# --- 5.6 streaks ----------------------------------------------------------------------------------------


def streak_label(values: pd.Series) -> pd.Series:
    return values.clip(upper=STREAK_CAP).astype(int).astype(str).replace({str(STREAK_CAP): f"{STREAK_CAP}+"})


def q6_streaks(results: Results, games: pd.DataFrame, data) -> None:
    later = games[games["game_in_session"] >= 2].copy()
    later["losses_before"] = streak_label(later["loss_streak"])
    later["wins_before"] = streak_label(later["win_streak"])
    levels = [str(k) for k in range(STREAK_CAP)] + [f"{STREAK_CAP}+"]
    groups = groups_with_all(data)
    names = ["excess", "my_ep", "opp_ep", "ep_gap"]
    losing = by_level(later, groups, "losses_before", levels, names)
    results.table("6_loss_streak", losing, "Games 2+ of a session by the number of losses in a row just before (0 = previous game won or drawn): score minus expectation (with Elo) and move quality.")
    results.table("6b_win_streak", by_level(later, groups, "wins_before", levels, names), "The same by the number of wins in a row just before.")
    two = later[later["prev2_outcomes"].isin(TWO_BEFORE)]
    results.table("6c_previous_two", by_level(two, groups, "prev2_outcomes", TWO_BEFORE, names).rename(columns={"prev2_outcomes": "previous_two"}), "Games 3+ of a session by the previous two results, older first (draws dropped): if only the last result mattered, WL and LL would look alike.")
    rows = []
    contrasts = [
        ("losses_before", f"{STREAK_CAP}+", "0", f"after {STREAK_CAP}+ losses minus after none", later),
        ("losses_before", "1", "0", "after 1 loss minus after none", later),
        ("losses_before", f"{STREAK_CAP}+", "1", f"after {STREAK_CAP}+ losses minus after 1", later),
        ("wins_before", f"{STREAK_CAP}+", "0", f"after {STREAK_CAP}+ wins minus after none", later),
        ("prev2_outcomes", "LL", "WL", "LL minus WL", two),
    ]
    for group in groups:
        for column, a_level, b_level, name, frame in contrasts:
            g = subset(frame, group)
            a, b = g[g[column] == a_level], g[g[column] == b_level]
            rows.append({"tc_group": group, "contrast": name, **S.diff_stats(a, b, ["excess", "my_ep"]), "games_a": len(a), "games_b": len(b), "thin": thin_cell(a) or thin_cell(b)})
    results.table("6d_streak_contrasts", pd.DataFrame(rows), "Contrasts between streak levels (first minus second, same resampled sessions): score minus expectation (Elo at the first level's expectation) and your expected points lost per move.")

    fig, axes = charts.group_grid(groups, sharey=True, title="After a losing streak, how do you score?")
    drawn = {g: panel_estimates(ax, losing[losing["tc_group"] == g], "losses_before", levels) for g, ax in axes.items()}
    finish_grid(fig, axes, drawn, "score minus expectation", "losses in a row before the game")
    charts.footnote(fig, f"Games 2+ of a session; 0 = the previous game was a win or a draw. 95% CIs resample sessions; cells under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "6_loss_streak")


# --- 5.7 breaks -----------------------------------------------------------------------------------------


def q7_breaks(results: Results, games: pd.DataFrame, data, gap: float) -> None:
    edges = [*BREAK_EDGES_BEFORE_GAP, gap, *BREAK_EDGES_AFTER_GAP]
    gap_label = f"{gap:g}"
    labels = [*BREAK_LABELS_BEFORE_GAP, f"5-{gap_label} min", *[label.replace("gap", f"{gap_label} min") for label in BREAK_LABELS_AFTER_GAP]]
    frame = games.assign(break_before=label_bins(games["break_before_minutes"], edges, labels))
    frame = frame[frame["break_before"].notna()]
    rows = []
    for group in groups_with_all(data):
        g = subset(frame, group)
        for label in labels:
            part = g[g["break_before"] == label]
            rows.append({"tc_group": group, "break_before": label, **S.cell_stats(part, ["excess", "my_ep", "opp_ep"]),
                         "share_after_a_loss": float((part["prev_any_outcome"] == "loss").mean()) if len(part) else np.nan,
                         "share_first_of_session": float(part["is_first_in_session"].mean()) if len(part) else np.nan,
                         **counts(part), "thin": thin_cell(part)})  # fmt: skip
    table = pd.DataFrame(rows)
    results.table("7_break_before", table, f"By the pause since your previous game (any control; above {gap_label} minutes the game starts a new session): score minus expectation (with Elo), both sides' expected points lost per move, and how many of those games follow a loss.")

    rows = []
    later = games[games["game_in_session"] >= 2]
    quick = BREAK_EDGES_BEFORE_GAP[1]
    for group in groups_with_all(data):
        g = subset(later, group)
        for result in RESULTS:
            part = g[g["prev_outcome"] == result]
            share = part.assign(_quick=(part["gap_before_minutes"] < quick).astype(float), _one=1.0)
            est, lo, hi = S.boot_sums(share, ["_quick", "_one"], "session_id", S.ratio) if len(part) else (np.nan, np.nan, np.nan)
            rows.append({"tc_group": group, "previous_result": result, "median_pause_seconds": float(60 * part["gap_before_minutes"].median()) if len(part) else np.nan,
                         "share_within_quick": est, "share_within_quick_lo": lo, "share_within_quick_hi": hi, "quick_minutes": quick, **counts(part), "thin": thin_cell(part)})  # fmt: skip
    results.table("7b_requeue_speed", pd.DataFrame(rows), "Within a session: median pause before the next game and the share started within a minute, by the previous result.")

    groups = groups_with_all(data)
    fig, axes = charts.group_grid(groups, sharey=True, title="Does a long break between games matter?")
    drawn = {g: panel_estimates(ax, table[table["tc_group"] == g], "break_before", labels) for g, ax in axes.items()}
    finish_grid(fig, axes, drawn, "score minus expectation", None, rotate=60)
    for ax in axes.values():
        ax.tick_params(axis="x", labelsize=7)
    fig.tight_layout()
    charts.footnote(fig, f"Pause since the previous game of any control; past {gap_label} minutes the game starts a new session. 95% CIs resample sessions; cells under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "7_break_before")


# --- session definition check -------------------------------------------------------------------------


def gap_sensitivity(results: Results, data, decomposed: pd.DataFrame, games_default: pd.DataFrame, gap: float) -> None:
    """Checks 5.2, 5.3 and 5.5 with other session gaps; numbered 0 because it belongs to no single sub-question."""
    rows = []
    for factor in GAP_CHECK_FACTORS:
        g_all = games_default if factor == 1.0 else S.build_games(data, decomposed, gap * factor)
        g_all = g_all[g_all["tc_group"].isin(data.tc_groups)]
        for group in groups_with_all(data):
            g = subset(g_all, group)
            later = g[g["game_in_session"] >= 2]
            stop = S.diff_stats(g[g["outcome"] == "loss"], g[g["outcome"] == "win"], ["stop"])
            tilt = S.diff_stats(later[later["prev_outcome"] == "loss"], later[later["prev_outcome"] == "win"], ["excess"])
            fat = paired_vs_game1(g, "my_ep_rate", FATIGUE_FROM, np.inf)
            rows.append({
                "gap_minutes": gap * factor, "tc_group": group,
                "p_stop_loss_minus_win": stop["stop_diff"], "p_stop_loss_minus_win_lo": stop["stop_diff_lo"], "p_stop_loss_minus_win_hi": stop["stop_diff_hi"],
                "after_loss_expected": tilt["expected"],
                "after_loss_minus_win_excess": tilt["excess_diff"], "after_loss_minus_win_excess_lo": tilt["excess_diff_lo"], "after_loss_minus_win_excess_hi": tilt["excess_diff_hi"],
                "after_loss_minus_win_elo": tilt["excess_diff_elo"], "after_loss_minus_win_elo_lo": tilt["excess_diff_elo_lo"], "after_loss_minus_win_elo_hi": tilt["excess_diff_elo_hi"],
                f"my_ep_game{FATIGUE_FROM}plus_minus_game1": fat[0], f"my_ep_game{FATIGUE_FROM}plus_minus_game1_lo": fat[1], f"my_ep_game{FATIGUE_FROM}plus_minus_game1_hi": fat[2],
                "sessions": int(g["session_id"].nunique()), "games": int(len(g)), "fatigue_sessions": fat[3], "thin": int(g["session_id"].nunique()) < MIN_SESSIONS,
            })  # fmt: skip
    results.table("0_gap_sensitivity", pd.DataFrame(rows), "Checks for 5.2, 5.3 and 5.5 with the session gap at half, one and two times the configured gap: stopping after a loss minus a win, score after a loss minus after a win (with the after-loss expectation and Elo), your expected points lost per move in games 5+ minus game 1.")


# --- main ---------------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    gap = float(load_settings().session_gap_minutes)
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    all_games = S.build_games(data, decomposed, gap)
    games = all_games[all_games["tc_group"].isin(data.tc_groups)].reset_index(drop=True)
    results = Results(SLUG)

    q1_length(results, games, all_games, data)
    gap_sensitivity(results, data, decomposed, all_games, gap)
    q2_stopping(results, games, data)
    q3_fatigue(results, games, data)
    q4_warmup(results, games, data)
    q5_after_result(results, games, data)
    q6_streaks(results, games, data)
    q7_breaks(results, games, data, gap)
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
