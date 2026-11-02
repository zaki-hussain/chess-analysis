"""Q6: time of day and day of week.

Do you score better at some hours or on some days, once the sessions, your
rating and the year are accounted for? Hours are `local_hour` (the configured
timezone) and `weekday` from `my_games`. The per-game frame and session
machinery come from `chess_analysis.sessions` (shared with Q5). Computed per
exact time control (`data.tc_groups`) plus an "all games" pool:

- score minus the calibration model's pre-game expectation (with Elo-equivalents)
  and both sides' expected points lost per move, by hour band (and pooled by
  hour) and by weekday; weekend minus weekday;
- hour band x weekday, pooled by time class (per control nearly every cell
  is thin);
- raw band differences next to the same differences from a per-game
  regression with pre-game session controls (previous result, game number,
  minutes into the session), weekend, pre-game rating, year (and the
  control, in the pool), with a session-level permutation test of the
  hour-band block;
- the raw band numbers in 2021-23 and 2024-26 separately;
- the share of your (and your opponents') losses that were on time, by band.

CIs resample whole sessions throughout: the games of one session share an
hour, a mood and a pool of opponents. Writes CSVs to
presentation/data/06_time_of_day/ and SVGs to
presentation/figures/06_time_of_day/ (no prose).

Run: .venv/bin/python analysis/06_time_of_day.py   (about a minute; the regressions and the 300-shuffle test dominate)
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

SLUG = "06_time_of_day"
ALL = "all games"  # pooled row/panel over every reported control (thin controls excluded)
N_BOOT = S.N_BOOT
SEED = 6
N_PERM = 300  # permutations of the band labels: resolves a p-value near 0.05 to about +-0.013
CLUSTER = "session_id"

# A cell resting on fewer games than this has a score CI wider than about +-0.18: computed, flagged thin, not drawn.
MIN_CELL_GAMES = 30
MIN_CELL_LOSSES = 30  # the same floor for shares of losses (flag share)
# Every CI resamples whole sessions: with fewer sessions than this the bootstrap has too few clusters to give a
# trustworthy interval, so a cell is also thin under this many sessions.
MIN_SESSIONS = 30
MIN_GAMES_PER_COEF = 10  # OLS needs about this many games per coefficient before its estimates settle

# Hour bands (local time). Hours 1-6 hold under 60 games each, so they are merged with midnight into "night";
# the day is cut where your playing pattern changes (morning before lunch, the afternoon block, evening, late).
BAND_EDGES = [0, 7, 12, 15, 18, 21, 24]
BAND_LABELS = ["night 0-6", "morning 7-11", "midday 12-14", "afternoon 15-17", "evening 18-20", "late 21-23"]
LATE_BANDS = ["late 21-23", "night 0-6"]  # "late at night": from 21:00 to 06:59
THIN_HOURS = range(1, 7)  # pooled as one bucket in the hour-by-hour table, for the same reason
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKEND = ["Saturday", "Sunday"]
SPLIT_YEAR = 2024  # 2021-23 vs 2024-26: two roughly equal halves of the history, the split the question asks for

MINUTES_SCALE = 30  # regression coefficient per half hour into the session
RATING_SCALE = 100  # and per 100 rating points

CHANCE_LEVEL = 0.05  # share of cells whose 95% CI excludes zero when nothing is going on
CONTROLS_SUMMED = "all controls, cells summed"  # row of the clear-cell counts that adds up the per-control cells

QUALITY = ["excess", "my_ep", "opp_ep", "ep_gap"]


# --- helpers ------------------------------------------------------------------------------------------


def groups_with_all(data) -> list[str]:
    return [*data.tc_groups, ALL]


def subset(games: pd.DataFrame, group: str) -> pd.DataFrame:
    if group == ALL:
        return games
    if group.startswith("all "):  # pooled time class, e.g. "all bullet"
        return games[games["time_class"] == group[4:]]
    return games[games["tc_group"] == group]


def counts(part: pd.DataFrame) -> dict:
    return {"games": int(len(part)), "sessions": int(part[CLUSTER].nunique())}


def thin_cell(part: pd.DataFrame) -> bool:
    """Too few games, or too few sessions for the session bootstrap."""
    return len(part) < MIN_CELL_GAMES or part[CLUSTER].nunique() < MIN_SESSIONS


def by_level(games: pd.DataFrame, groups: list[str], column: str, levels: list[str], names: list[str], label: str = "tc_group") -> pd.DataFrame:
    rows = []
    for group in groups:
        part_g = subset(games, group)
        for level in levels:
            part = part_g[part_g[column] == level]
            rows.append({label: group, column: level, **S.cell_stats(part, names, cluster=CLUSTER, seed=SEED), **counts(part), "thin": thin_cell(part)})
    return pd.DataFrame(rows)


def blank_thin(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
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
    part = blank_thin(frame, [y, f"{y}_lo", f"{y}_hi"])
    if part[y].notna().sum() == 0:
        return False
    charts.estimate(ax, part, x, y=y, lo=f"{y}_lo", hi=f"{y}_hi", order=order, colour=colour, label=label, offset=offset)
    ax.set_xlim(-0.5, len(order) - 0.5)
    return True


def finish_grid(fig, axes: dict, drawn: dict, ylabel: str, xlabel: str | None = None, rotate: int = 0, zero: bool = True) -> None:
    for group, ax in axes.items():
        if not drawn.get(group):
            thin_panel(ax)
            continue
        if zero:
            charts.zero_line(ax)
        if rotate:
            labels = [t.get_text() for t in ax.get_xticklabels()]
            ax.set_xticks(ax.get_xticks(), labels, rotation=rotate, ha="right", rotation_mode="anchor")
    first = {}
    for ax in axes.values():
        first.setdefault(round(ax.get_position().y0, 3), ax)
    for ax in first.values():
        ax.set_ylabel(ylabel)
    if xlabel:
        for ax in axes.values():
            ax.set_xlabel(xlabel)
    fig.tight_layout()


def clear_cells(table: pd.DataFrame, level: str, controls: list[str]) -> pd.DataFrame:
    """Per group, and summed over the single controls: usable (not thin) cells, cells whose 95% CI excludes zero, and how many chance alone would give."""
    rows = []
    for group in [*controls, CONTROLS_SUMMED, ALL]:
        part = table[table["tc_group"].isin(controls)] if group == CONTROLS_SUMMED else table[table["tc_group"] == group]
        usable = part[~part["thin"]]
        clear = usable[(usable["excess_lo"] > 0) | (usable["excess_hi"] < 0)]
        rows.append({"tc_group": group, "level": level, "cells": len(part), "cells_thin": int(part["thin"].sum()), "cells_usable": len(usable),
                     "cells_ci_clear_of_zero": len(clear), "cells_clear_above_zero": int((clear["excess"] > 0).sum()), "cells_clear_below_zero": int((clear["excess"] < 0).sum()),
                     "cells_clear_expected_by_chance": CHANCE_LEVEL * len(usable)})  # fmt: skip
    return pd.DataFrame(rows)


def short_band(label: str) -> str:
    return label.split(" ", 1)[1]


# --- frame -----------------------------------------------------------------------------------------------


def build(data, decomposed: pd.DataFrame, gap: float) -> pd.DataFrame:
    games = S.build_games(data, decomposed, gap)
    games = games[games["tc_group"].isin(data.tc_groups)].reset_index(drop=True)
    games["band"] = pd.cut(games["local_hour"], BAND_EDGES, labels=BAND_LABELS, right=False).astype(str)
    first_thin, last_thin = min(THIN_HOURS), max(THIN_HOURS)
    games["hour"] = np.where(games["local_hour"].isin(THIN_HOURS), f"{first_thin}-{last_thin}", games["local_hour"].astype(int).astype(str))
    games["is_weekend"] = games["weekday"].isin(WEEKEND)
    games["period"] = np.where(games["year"] < SPLIT_YEAR, f"{games['year'].min()}-{SPLIT_YEAR - 1}", f"{SPLIT_YEAR}-{games['year'].max()}")
    return games


def hour_order() -> list[str]:
    thin = f"{min(THIN_HOURS)}-{max(THIN_HOURS)}"
    return [str(h) for h in range(0, min(THIN_HOURS))] + [thin] + [str(h) for h in range(max(THIN_HOURS) + 1, 24)]


# --- 6.1 hour bands --------------------------------------------------------------------------------------


def q1_hours(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    table = by_level(games, groups, "band", BAND_LABELS, QUALITY)
    totals = {g: len(subset(games, g)) for g in groups}
    table.insert(2, "share_of_games", table["games"] / table["tc_group"].map(totals))
    results.table("1_hour_band", table, "By hour band of the game's local start: score minus model expectation (with Elo), both sides' expected points lost per move and their difference. CIs resample sessions.")
    results.table("1b_hour", by_level(games, [ALL], "hour", hour_order(), QUALITY), "The same hour by hour, all games pooled (hours 1-6 merged: too few games each).")
    results.table("1e_clear_cells", clear_cells(table, "hour band", list(data.tc_groups)), f"How many control x hour-band cells of 1_hour_band have a score CI clear of zero, against the number expected by chance ({CHANCE_LEVEL:g} of usable cells).")

    rows = []
    for band in BAND_LABELS:
        g = games[games["band"] == band]
        rows.append({
            "band": band, "mean_rating_before": float(g["my_rating_before"].mean()), "share_last_of_session": float(g["is_last_in_session"].mean()),
            "share_after_a_loss": float((g["prev_outcome"] == "loss").mean()), "median_game_in_session": float(g["game_in_session"].median()),
            **{f"share_{c}": float((g["time_class"] == c).mean()) for c in dict.fromkeys(data.tc_class.values())},
            **{f"share_{p}": float((g["period"] == p).mean()) for p in sorted(games["period"].unique())},
            **counts(g),
        })  # fmt: skip
    results.table("1c_band_profile", pd.DataFrame(rows), "What else differs between the bands (all games): your rating, the session position, the time-class mix and the era. These are the controls of 6.4.")
    mix = games.groupby(["band", "tc_group"]).size().rename("games").reset_index()
    mix["share_of_band"] = mix["games"] / mix["band"].map(games["band"].value_counts())
    mix["band"] = pd.Categorical(mix["band"], BAND_LABELS, ordered=True)
    mix["tc_group"] = pd.Categorical(mix["tc_group"], data.tc_groups, ordered=True)
    results.table("1d_band_control_mix", mix.sort_values(["band", "tc_group"])[["band", "tc_group", "share_of_band", "games"]], "Share of each band's games in each control.")

    fig, axes = charts.group_grid(groups, sharey=True, title="Do you score better at some hours?")
    labels = [short_band(b) for b in BAND_LABELS]
    drawn = {}
    for group, ax in axes.items():
        part = table[table["tc_group"] == group].assign(hours=lambda f: f["band"].map(short_band))
        drawn[group] = panel_estimates(ax, part, "hours", labels)
    finish_grid(fig, axes, drawn, "score minus expectation", "local hour")
    charts.footnote(fig, f"Score minus the calibration model's pre-game expectation, by the hour the game started (local time). 95% CIs resample sessions; cells under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "1_hour_band")


# --- 6.2 weekdays ---------------------------------------------------------------------------------------


def q2_weekdays(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    table = by_level(games, groups, "weekday", WEEKDAYS, QUALITY)
    results.table("2_weekday", table, "By weekday: score minus model expectation (with Elo) and both sides' expected points lost per move. CIs resample sessions.")
    results.table("2c_clear_cells", clear_cells(table, "weekday", list(data.tc_groups)), f"How many control x weekday cells of 2_weekday have a score CI clear of zero, against the number expected by chance ({CHANCE_LEVEL:g} of usable cells).")
    rows = []
    for group in groups:
        g = subset(games, group)
        a, b = g[g["is_weekend"]], g[~g["is_weekend"]]
        rows.append({"tc_group": group, **S.diff_stats(a, b, ["excess", "ep_gap"], cluster=CLUSTER, seed=SEED), "games_weekend": len(a), "games_weekday": len(b), "thin": thin_cell(a) or thin_cell(b)})
    results.table("2b_weekend_minus_weekday", pd.DataFrame(rows), "Weekend (Saturday, Sunday) minus weekdays: score minus expectation (Elo at the weekend expectation) and your-minus-opponents' expected points lost per move.")

    fig, axes = charts.group_grid(groups, sharey=True, title="Do you score better on some days?")
    labels = [d[:3] for d in WEEKDAYS]
    drawn = {}
    for group, ax in axes.items():
        part = table[table["tc_group"] == group].assign(day=lambda f: f["weekday"].str[:3])
        drawn[group] = panel_estimates(ax, part, "day", labels)
    finish_grid(fig, axes, drawn, "score minus expectation")
    charts.footnote(fig, f"Score minus the calibration model's pre-game expectation, by local weekday. 95% CIs resample sessions; cells under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "2_weekday")


# --- 6.3 band x weekday, pooled -----------------------------------------------------------------------


def q3_grid(results: Results, games: pd.DataFrame, data) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm

    pools = [ALL, *[f"all {c}" for c in dict.fromkeys(data.tc_class.values())]]
    rows = []
    for pool in pools:
        g = subset(games, pool)
        for day in WEEKDAYS:
            for band in BAND_LABELS:
                part = g[(g["weekday"] == day) & (g["band"] == band)]
                rows.append({"pool": pool, "weekday": day, "band": band, **S.cell_stats(part, ["excess"], cluster=CLUSTER, seed=SEED), **counts(part), "thin": thin_cell(part)})
    table = pd.DataFrame(rows)
    results.table("3_band_weekday", table, "Hour band x weekday, pooled over the controls of each time class and over all games (per control almost every cell is thin): score minus model expectation with Elo, CIs resample sessions.")
    rows = []
    for pool in pools:
        part = table[table["pool"] == pool]
        usable = part[~part["thin"]]
        clear = usable[(usable["excess_lo"] > 0) | (usable["excess_hi"] < 0)]
        rows.append({"pool": pool, "cells": len(part), "cells_thin": int(part["thin"].sum()), "cells_usable": len(usable), "cells_ci_clear_of_zero": len(clear),
                     "cells_clear_expected_by_chance": CHANCE_LEVEL * len(usable), "games": int(part["games"].sum())})  # fmt: skip
    results.table("3b_grid_summary", pd.DataFrame(rows), f"Per pool: how many hour band x weekday cells are thin, how many usable cells have a 95% CI clear of zero, and how many would be by chance alone ({CHANCE_LEVEL:g} of usable cells).")

    shown = table[~table["thin"]]
    lim = float(np.nanmax(np.abs(shown["excess"]))) if len(shown) else 0.1
    ncols = 2
    nrows = int(np.ceil(len(pools) / ncols))
    fig, grid = plt.subplots(nrows, ncols, figsize=(11, 3.4 * nrows + 0.6), squeeze=False)
    norm = TwoSlopeNorm(0, -lim, lim)
    cmap = plt.get_cmap(charts.DIVERGING).with_extremes(bad=charts.GRID)
    image = None
    for k, pool in enumerate(pools):
        ax = grid[k // ncols][k % ncols]
        part = table[table["pool"] == pool]
        values = part.pivot(index="weekday", columns="band", values="excess").reindex(index=WEEKDAYS, columns=BAND_LABELS)
        n = part.pivot(index="weekday", columns="band", values="games").reindex(index=WEEKDAYS, columns=BAND_LABELS)
        thin = part.pivot(index="weekday", columns="band", values="thin").reindex(index=WEEKDAYS, columns=BAND_LABELS).astype(bool)
        masked = np.ma.masked_array(values.to_numpy(dtype=float), mask=thin.to_numpy() | values.isna().to_numpy())
        image = ax.imshow(masked, cmap=cmap, norm=norm, aspect="auto")
        for i in range(len(WEEKDAYS)):
            for j in range(len(BAND_LABELS)):
                v, count = values.iat[i, j], int(n.iat[i, j])
                text = f"n={count}" if thin.iat[i, j] else f"{v:+.2f}\nn={count}"
                dark = not thin.iat[i, j] and abs(v) > 0.6 * lim
                ax.text(j, i, text, ha="center", va="center", fontsize=6.5, color=charts.SURFACE if dark else charts.INK_SECONDARY)
        ax.set_xticks(range(len(BAND_LABELS)), [short_band(b) for b in BAND_LABELS])
        ax.set_yticks(range(len(WEEKDAYS)), [d[:3] for d in WEEKDAYS])
        ax.grid(False)
        ax.set_title(pool)
        for spine in ax.spines.values():
            spine.set_visible(False)
    for k in range(len(pools), nrows * ncols):
        grid[k // ncols][k % ncols].set_visible(False)
    fig.suptitle("Is there a best hour on a particular day?", x=0.01, ha="left", fontweight="bold", fontsize=11, color=charts.INK)
    fig.tight_layout()
    cbar = fig.colorbar(image, ax=grid.ravel().tolist(), shrink=0.6, pad=0.02)
    cbar.set_label("score minus expectation (blue = better for you)")
    cbar.outline.set_visible(False)
    charts.footnote(fig, f"Pooled over the controls of each time class. Grey cells: under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions (game count shown). Colour and number = score minus the model's expectation.")
    results.figure(fig, "3_band_weekday")


# --- 6.4 regression and permutation ---------------------------------------------------------------------


def design(sub: pd.DataFrame, group: str, ref_band: str) -> pd.DataFrame:
    X = pd.DataFrame({"intercept": 1.0}, index=sub.index)
    X = X.join(dummies(sub["band"], drop=ref_band, prefix="band"))
    X["weekend"] = sub["is_weekend"].astype(float)
    # Not "last game of the session": you stop because of this game's result (Q5), so it is an outcome, not a control.
    X["previous game lost"] = (sub["prev_outcome"] == "loss").astype(float)
    X["previous game won"] = (sub["prev_outcome"] == "win").astype(float)
    X["log2(game number)"] = np.log2(sub["game_in_session"].astype(float))
    X[f"minutes into session / {MINUTES_SCALE}"] = sub["minutes_into_session"] / MINUTES_SCALE
    X[f"rating before / {RATING_SCALE}"] = sub["my_rating_before"].astype(float) / RATING_SCALE
    years = sub["year"].astype(str)
    X = X.join(dummies(years, drop=years.value_counts().idxmax(), prefix="year"))
    if group == ALL:
        X = X.join(dummies(sub["tc_group"], drop=sub["tc_group"].value_counts().idxmax(), prefix="control"))
    return X


OUTCOMES = {"excess": "excess_model", "ep_gap": "ep_rate_gap", "my_ep": "my_ep_rate", "lost_on_time": "lost_on_time"}


def q4_adjusted(results: Results, games: pd.DataFrame, data) -> None:
    ref_band = games["band"].value_counts().idxmax()  # the busiest band is the reference
    rng = np.random.default_rng(SEED)
    raw_rows, coef_rows, perm_rows = [], [], []
    for group in groups_with_all(data):
        g = subset(games, group)
        sub = g[g["my_rating_before"].notna() & g["my_rating_before"].gt(0)]
        ref = sub[sub["band"] == ref_band]
        X = design(sub, group, ref_band)
        band_terms = [f"band={b}" for b in BAND_LABELS if b != ref_band and f"band={b}" in X]
        band_idx = [list(X.columns).index(t) for t in band_terms]
        sessions = sub[CLUSTER].to_numpy()
        thin_model = len(sub) < MIN_GAMES_PER_COEF * X.shape[1] or sub[CLUSTER].nunique() < MIN_SESSIONS
        # session-level band labels for the permutation: the band of each session's first game
        first_band = sub.sort_values("start_datetime").groupby(CLUSTER)["band"].first()
        pos = pd.Index(first_band.index).get_indexer(sessions)
        expected_all = float(sub["my_ep_start"].mean())
        for name, column in OUTCOMES.items():
            y = sub[column]
            ok = y.notna().to_numpy()
            Xm, ym = X.to_numpy(float)[ok], y.to_numpy(float)[ok]
            beta, draws = cluster_ols(Xm, ym, sessions[ok], n_boot=N_BOOT, seed=SEED)
            with warnings.catch_warnings():  # a dummy absent from every resample of a thin group has no CI
                warnings.simplefilter("ignore", RuntimeWarning)
                lo, hi = np.nanquantile(draws, [0.025, 0.975], axis=0)
            for term, b, l, h in zip(X.columns, beta, lo, hi):
                if term == "intercept" or term.startswith(("year=", "control=")):
                    continue
                row = {"tc_group": group, "outcome": name, "term": term, "estimate": b, "lo": l, "hi": h}
                if name == "excess":
                    row["expected"] = expected_all
                    for k, v in (("estimate_elo", b), ("lo_elo", l), ("hi_elo", h)):
                        row[k] = float(excess_to_elo(expected_all, v)) if pd.notna(v) else np.nan
                coef_rows.append({**row, "games": int(ok.sum()), "sessions": int(len(np.unique(sessions[ok]))), "thin": thin_model})
            # Permutation test of the band block. The unit shuffled is the session, so the test labels every game with
            # its session's band (that of the session's first game) both in the observed fit and in the shuffles.
            Xp = Xm.copy()
            pos_ok = pos[ok]
            session_bands = first_band.to_numpy()

            def band_fit(labels: np.ndarray) -> np.ndarray:
                for k, term in zip(band_idx, band_terms):
                    Xp[:, k] = (labels == term.split("=", 1)[1]).astype(float)
                return np.linalg.lstsq(Xp.T @ Xp, Xp.T @ ym, rcond=None)[0][band_idx]  # normal equations: same fit, k x k solve

            obs = band_fit(session_bands[pos_ok])
            obs_max, obs_ss = float(np.max(np.abs(obs))), float(np.sum(obs**2))
            perm_max, perm_ss = np.empty(N_PERM), np.empty(N_PERM)
            for p in range(N_PERM):
                bp = band_fit(session_bands[rng.permutation(len(session_bands))][pos_ok])
                perm_max[p], perm_ss[p] = np.max(np.abs(bp)), np.sum(bp**2)
            p_largest = (1 + int((perm_max >= obs_max).sum())) / (1 + N_PERM)  # the observed labelling counts as one draw: p is never 0
            p_ss = (1 + int((perm_ss >= obs_ss).sum())) / (1 + N_PERM)
            tests = 1 if group == ALL else len(data.tc_groups)  # one test per control: Bonferroni over the controls
            perm_rows.append({"tc_group": group, "outcome": name, "largest_band_coefficient": obs_max, "p_largest": p_largest, "p_sum_of_squares": p_ss,
                              "tests_in_family": tests, "p_largest_bonferroni": min(1.0, p_largest * tests), "p_sum_of_squares_bonferroni": min(1.0, p_ss * tests),
                              "permutations": N_PERM, "reference_band": ref_band, "games": int(ok.sum()), "sessions": int(len(np.unique(sessions[ok]))), "thin": thin_model})  # fmt: skip
            if name == "excess":
                coefs = dict(zip(X.columns, zip(beta, lo, hi)))
                for band in BAND_LABELS:
                    if band == ref_band:
                        continue
                    part = sub[sub["band"] == band]
                    raw = S.diff_stats(part, ref, ["excess"], cluster=CLUSTER, seed=SEED)
                    base = {"tc_group": group, "band": band, "reference_band": ref_band, "expected": raw["expected"]}
                    n = {**counts(part), "thin": thin_cell(part) or thin_cell(ref)}
                    raw_rows.append({**base, "version": "raw", "estimate": raw["excess_diff"], "lo": raw["excess_diff_lo"], "hi": raw["excess_diff_hi"], **n})
                    b, l, h = coefs.get(f"band={band}", (np.nan, np.nan, np.nan))
                    raw_rows.append({**base, "version": "adjusted", "estimate": b, "lo": l, "hi": h, **n, "thin": n["thin"] or thin_model})
    raw = pd.DataFrame(raw_rows)
    for col in ("estimate", "lo", "hi"):
        raw[f"{col}_elo"] = [float(excess_to_elo(e, v)) if pd.notna(v) and pd.notna(e) else np.nan for e, v in zip(raw["expected"], raw[col])]
    raw = raw[["tc_group", "band", "reference_band", "version", "estimate", "lo", "hi", "expected", "estimate_elo", "lo_elo", "hi_elo", "games", "sessions", "thin"]]
    results.table("4_raw_vs_adjusted", raw, "Each band minus the busiest band in score minus expectation: raw, and adjusted by the per-game regression (game number, minutes into the session, previous result, weekend, rating, year; control in the pool). Elo at the band's expectation.")
    results.table("4b_permutation", pd.DataFrame(perm_rows), "Joint test of the hour-band terms: each game labelled with its session's band (the band of the session's first game), labels shuffled across whole sessions, regression refitted; p = (1 + shuffles with a band effect at least as large as observed) / (1 + shuffles); Bonferroni columns multiply each control's p by the number of controls tested.")
    results.table("4c_regression", pd.DataFrame(coef_rows), "All regression coefficients except intercept, year and control dummies (outcomes: score minus expectation, your-minus-opponents' and your expected points lost per move, lost on time), CIs by session.")

    groups = groups_with_all(data)
    fig, axes = charts.group_grid(groups, sharey=True, title="Does any hour effect survive accounting for sessions, rating and year?")
    order = [short_band(b) for b in BAND_LABELS if b != ref_band]
    drawn = {}
    for group, ax in axes.items():
        part = raw[raw["tc_group"] == group].assign(hours=lambda f: f["band"].map(short_band)).rename(columns={"lo": "estimate_lo", "hi": "estimate_hi"})
        a = panel_estimates(ax, part[part["version"] == "raw"], "hours", order, y="estimate", colour=charts.INK_MUTED, label="raw", offset=-0.14)
        b = panel_estimates(ax, part[part["version"] == "adjusted"], "hours", order, y="estimate", colour=charts.PLAYER, label="adjusted", offset=0.14)
        drawn[group] = a or b
    finish_grid(fig, axes, drawn, f"vs {short_band(ref_band)}, score", "local hour")
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    charts.footnote(fig, f"Each band minus {ref_band} (the busiest), score minus the model's expectation. Adjusted: per-game regression with game number, minutes into the session, previous result, weekend, rating and year. 95% CIs resample sessions.")
    results.figure(fig, "4_raw_vs_adjusted")


# --- 6.5 halves -----------------------------------------------------------------------------------------


def q5_halves(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    periods = sorted(games["period"].unique())
    rows = []
    for group in groups:
        g = subset(games, group)
        for band in BAND_LABELS:
            for period in periods:
                part = g[(g["band"] == band) & (g["period"] == period)]
                rows.append({"tc_group": group, "band": band, "period": period, **S.cell_stats(part, ["excess", "ep_gap"], cluster=CLUSTER, seed=SEED), **counts(part), "thin": thin_cell(part)})
    table = pd.DataFrame(rows)
    results.table("5_halves", table, f"Hour bands in {periods[0]} and {periods[1]} separately: score minus expectation (with Elo) and your-minus-opponents' expected points lost per move.")
    rows = []
    for group in groups:
        part = table[table["tc_group"] == group]
        wide = part.pivot(index="band", columns="period", values="excess")
        thin = part.pivot(index="band", columns="period", values="thin").astype(bool)
        both = ~thin.any(axis=1)
        same = (np.sign(wide.loc[both, periods[0]]) == np.sign(wide.loc[both, periods[1]])).sum()
        lo = part.pivot(index="band", columns="period", values="excess_lo")
        hi = part.pivot(index="band", columns="period", values="excess_hi")
        clear = ((lo > 0) | (hi < 0)) & ~thin
        rows.append({"tc_group": group, "bands_compared": int(both.sum()), "bands_same_sign": int(same), "bands_clear_in_first": int(clear[periods[0]].sum()), "bands_clear_in_second": int(clear[periods[1]].sum()),
                     "bands_clear_in_both_same_sign": int((clear[periods[0]] & clear[periods[1]] & (np.sign(wide[periods[0]]) == np.sign(wide[periods[1]]))).sum()), "thin": int(both.sum()) == 0})  # fmt: skip
    results.table("5b_sign_agreement", pd.DataFrame(rows), "Per control: bands with enough games in both periods, how many keep the sign of score minus expectation, and how many have a CI clear of zero in each period and in both.")

    fig, axes = charts.group_grid(groups, sharey=True, title="Does any hour effect repeat across the years?")
    labels = [short_band(b) for b in BAND_LABELS]
    colours = [charts.SERIES[1], charts.SERIES[2]]
    drawn = {}
    for group, ax in axes.items():
        part = table[table["tc_group"] == group].assign(hours=lambda f: f["band"].map(short_band))
        ok = [panel_estimates(ax, part[part["period"] == p], "hours", labels, colour=c, label=p, offset=o) for p, c, o in zip(periods, colours, (-0.14, 0.14))]
        drawn[group] = any(ok)
    finish_grid(fig, axes, drawn, "score minus expectation", "local hour")
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    charts.footnote(fig, f"Score minus the model's expectation by hour band, each half of the history separately. 95% CIs resample sessions; cells under {MIN_CELL_GAMES} games or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "5_halves")


# --- 6.6 flags ------------------------------------------------------------------------------------------


def q6_flags(results: Results, games: pd.DataFrame, data) -> None:
    groups = groups_with_all(data)
    rows = []
    for group in groups:
        g = subset(games, group)
        for band in BAND_LABELS:
            part = g[g["band"] == band]
            losses, opp_losses = int(part["is_loss"].sum()), int(part["opp_is_loss"].sum())
            stats = S.cell_stats(part, ["timeout_share", "opp_timeout_share", "lost_on_time"], cluster=CLUSTER, seed=SEED)
            rows.append({"tc_group": group, "band": band, **stats, "lost_on_time_per_100_games": 100 * stats["lost_on_time"], "losses": losses, "opponent_losses": opp_losses, **counts(part),
                         "thin": min(losses, opp_losses) < MIN_CELL_LOSSES or part[CLUSTER].nunique() < MIN_SESSIONS})  # fmt: skip
    table = pd.DataFrame(rows)
    results.table("6_flags_by_band", table, "By hour band: share of your losses that were on time, the same for your opponents' losses, and games you lost on time (share and per 100 games). CIs resample sessions.")
    rows = []
    classes = list(dict.fromkeys(data.tc_class.values()))
    for group in groups:
        g = subset(games, group)
        late, rest = g[g["band"].isin(LATE_BANDS)], g[~g["band"].isin(LATE_BANDS)]
        d = S.diff_stats(late, rest, ["timeout_share", "opp_timeout_share", "lost_on_time"], cluster=CLUSTER, seed=SEED)
        mix = {f"share_{c}_{name}": float((part["time_class"] == c).mean()) if len(part) else np.nan for c in classes for name, part in (("late", late), ("rest", rest))}
        rows.append({"tc_group": group, "late_bands": " + ".join(LATE_BANDS), **d, **mix, "losses_late": int(late["is_loss"].sum()), "losses_rest": int(rest["is_loss"].sum()), "games_late": len(late), "games_rest": len(rest),
                     "thin": min(int(late["is_loss"].sum()), int(rest["is_loss"].sum())) < MIN_CELL_LOSSES or min(late[CLUSTER].nunique(), rest[CLUSTER].nunique()) < MIN_SESSIONS})  # fmt: skip
    results.table("6b_late_minus_rest", pd.DataFrame(rows), "Late (21:00-06:59) minus the rest of the day: share of your losses on time, share of opponents' losses on time, share of games you lost on time; plus the time-class mix of the late games and of the rest.")

    wide = table.rename(columns={"timeout_share": "player", "timeout_share_lo": "player_lo", "timeout_share_hi": "player_hi", "opp_timeout_share": "opponent", "opp_timeout_share_lo": "opponent_lo", "opp_timeout_share_hi": "opponent_hi"})
    wide = blank_thin(wide, ["player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi"])
    fig, axes = charts.group_grid(groups, sharey=True, title="Do you lose on time more late at night?")
    labels = [short_band(b) for b in BAND_LABELS]
    drawn = {}
    for group, ax in axes.items():
        part = wide[wide["tc_group"] == group].assign(hours=lambda f: f["band"].map(short_band))
        drawn[group] = part["player"].notna().any()
        if drawn[group]:
            charts.side_bars(ax, part, "hours", order=labels)
            ax.set_xlim(-0.5, len(labels) - 0.5)
            ax.tick_params(axis="x", labelsize=7)
    finish_grid(fig, axes, drawn, "share of losses on time", "local hour", zero=False)
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.0))
    charts.footnote(fig, f"Of the games each side lost, the share lost on time. 95% CIs resample sessions; bands with under {MIN_CELL_LOSSES} losses on either side or {MIN_SESSIONS} sessions left out.")
    results.figure(fig, "6_flags_by_band")


# --- main ---------------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    gap = float(load_settings().session_gap_minutes)
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    games = build(data, decomposed, gap)
    results = Results(SLUG)
    q1_hours(results, games, data)
    q2_weekdays(results, games, data)
    q3_grid(results, games, data)
    q4_adjusted(results, games, data)
    q5_halves(results, games, data)
    q6_flags(results, games, data)
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
