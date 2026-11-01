"""Q6: time of day and day of week.

When do you play best, after controlling for session position, time class
and the opponents? Hours are `local_hour` (Europe/London) and `weekday` from
`my_games`. The analysis reuses Q5's session features and per-game metric
machinery (imported from analysis/05_sessions.py via importlib): hourly
buckets and six hour bands with session-clustered CIs, the opponents' error
in the same games as the pool control, weekday and weekday x band cells,
and a per-game regression with the session controls Q5 recommends
(last game of the session, previous result, game number, minutes), time
class, year and pre-game rating, with a session-level permutation test of
the hour-band terms.

Run: .venv/bin/python analysis/06_time_of_day.py
"""

from __future__ import annotations

import importlib.util
import time
from collections.abc import Callable, Sequence

import numpy as np
import pandas as pd

from chess_analysis.toolkit import SIDE_COLOURS, TIME_CLASSES, Report, figure_style, load_data, repo_root

SLUG = "06_time_of_day"
N_BOOT = 500
SEED = 6
N_PERM = 300
MIN_GAMES = 20
CLUSTER = "session_id"  # games of one session share an hour, a mood and a pool: CIs are clustered by session throughout

# Hour bands (local time, Europe/London). Hours 1-6 are thin (<60 games each) and are merged with 0 into "night".
BAND_EDGES = [0, 7, 12, 15, 18, 21, 24]
BAND_LABELS = ["night 0-6", "morning 7-11", "midday 12-14", "afternoon 15-17", "evening 18-20", "late 21-23"]
REF_BAND = "afternoon 15-17"  # the busiest band is the regression reference
HOUR_ORDER = ["0", "1-6"] + [str(h) for h in range(7, 24)]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKEND = {"Saturday", "Sunday"}

_spec = importlib.util.spec_from_file_location("q5", repo_root() / "analysis" / "05_sessions.py")
q5 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q5)

# Per-game metrics: Q5's plus the opponents' flag share and the cp pair difference.
METRICS = {
    **q5.METRICS,
    "opp_timeout_share": ("their flags / their losses", "opp_lost_on_time", "opp_is_loss", 1.0),
    "cp_gap": ("your − their cp / move", ("my_cp_lost", "my_moves", "opp_cp_lost", "opp_moves"), "pairdiff", 1.0),
}
FMT = {
    "excess_model": ".3f", "excess_elo": ".3f", "score": ".3f", "my_ep": ".4f", "opp_ep": ".4f", "ep_gap": ".4f", "my_err20": ".2f",
    "my_cp": ".1f", "opp_cp": ".1f", "cp_gap": ".1f", "timeout_share": ".3f", "opp_timeout_share": ".3f", "lost_on_time": ".3f",
    "resign_share": ".3f", "my_sec": ".2f", "opp_sec": ".2f",
}  # fmt: skip
HEADLINE = ["excess_model", "my_ep", "opp_ep", "ep_gap", "timeout_share"]
FULL = ["excess_model", "my_ep", "opp_ep", "ep_gap", "my_cp", "opp_cp", "timeout_share", "opp_timeout_share", "resign_share", "my_sec", "opp_sec"]


# --- metrics (Q5's bootstrap over per-cluster sums, with the extended metric table) --------------


def _metric_spec(name: str) -> tuple[list[str], Callable[[np.ndarray], np.ndarray], float]:
    _, num, den, scale = METRICS[name]
    if den == "pairdiff":
        return list(num), q5._ratio_diff, scale
    return [num, den or "_one"], q5._ratio, scale


def metric(frame: pd.DataFrame, name: str, cluster: str = CLUSTER, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    cols, stat, scale = _metric_spec(name)
    est, lo, hi = q5.boot_sums(frame.assign(_one=1.0), cols, cluster, stat, n_boot=n_boot, seed=SEED)
    return est * scale, lo * scale, hi * scale


def metric_diff(a: pd.DataFrame, b: pd.DataFrame, name: str, cluster: str = CLUSTER, n_boot: int = N_BOOT) -> tuple[float, float, float]:
    """metric(a) − metric(b) on the same resampled clusters (a and b may share sessions)."""
    cols, stat, scale = _metric_spec(name)
    m = len(cols)
    both = pd.concat([a.assign(_one=1.0, _a=1.0), b.assign(_one=1.0, _a=0.0)])
    for c in cols:
        both[f"{c}__a"] = both[c] * both["_a"]
        both[f"{c}__b"] = both[c] * (1 - both["_a"])
    all_cols = [f"{c}__a" for c in cols] + [f"{c}__b" for c in cols]
    est, lo, hi = q5.boot_sums(both, all_cols, cluster, lambda s: stat(s[:, :m]) - stat(s[:, m:]), n_boot=n_boot, seed=SEED)
    return est * scale, lo * scale, hi * scale


def fmt(est: float, lo: float, hi: float, f: str = ".3f") -> str:
    return q5.fmt(est, lo, hi, f=f)


def cell(frame: pd.DataFrame, name: str, min_games: int = MIN_GAMES) -> str:
    if len(frame) < min_games:
        return ""
    return fmt(*metric(frame, name), f=FMT[name])


def excludes_zero(lo: float, hi: float) -> bool:
    return q5.excludes_zero(lo, hi)


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


# --- frame --------------------------------------------------------------------------------------


def build(data, decomposed: pd.DataFrame) -> pd.DataFrame:
    games = q5.build_games(data, decomposed)
    games = q5.add_move_metrics(games, data.moves)
    games["opp_is_loss"] = (games["outcome"] == "win").astype(float)
    games["band"] = pd.cut(games["local_hour"], BAND_EDGES, labels=BAND_LABELS, right=False)
    games["hour_bucket"] = np.where(games["local_hour"].between(1, 6), "1-6", games["local_hour"].astype(str))
    games["is_weekend"] = games["weekday"].isin(WEEKEND)
    games["daytype"] = np.where(games["is_weekend"], "weekend", "weekday")
    games["prev_loss"] = (games["prev_outcome"] == "loss").astype(float)
    games["prev_win"] = (games["prev_outcome"] == "win").astype(float)
    games["date"] = games["local_start"].dt.date
    return games


def metric_table(games: pd.DataFrame, by: str, order: Sequence, names: Sequence[str], label: str, extra_counts: bool = True) -> pd.DataFrame:
    rows = []
    for level in order:
        part = games[games[by].astype(str) == str(level)]
        row = {label: str(level), "games": len(part)}
        if extra_counts:
            row["sessions"] = int(part[CLUSTER].nunique())
        for name in names:
            row[METRICS[name][0]] = cell(part, name)
        rows.append(row)
    return pd.DataFrame(rows)


def values_table(games: pd.DataFrame, by: str, order: Sequence, name: str, min_games: int = MIN_GAMES) -> pd.DataFrame:
    rows = []
    for level in order:
        part = games[games[by].astype(str) == str(level)]
        est, lo, hi = metric(part, name) if len(part) >= min_games else (np.nan, np.nan, np.nan)
        rows.append({"level": str(level), "games": len(part), "est": est, "lo": lo, "hi": hi})
    return pd.DataFrame(rows)


# --- sections -----------------------------------------------------------------------------------


def section_when(report: Report, games: pd.DataFrame) -> dict:
    report.heading("1. When you play, and what differs between the hours")
    report.text(
        f"Hours are the local start time in Europe/London (`local_hour`), the configured timezone; the six bands merge the thin night hours "
        f"(hours 1–6 hold {int(games['local_hour'].between(1, 6).sum())} games together). Before comparing play across the hours it matters what else differs "
        "between them: the time-class mix, your rating (an era proxy), where in a session the game falls, and whether the previous game was lost — the "
        "things Q5 showed carry score effects of their own (last game of a session −0.2, after a loss −0.05). These are the controls of section 5."
    )
    rows = []
    for band in BAND_LABELS:
        g = games[games["band"] == band]
        rows.append({
            "band": band, "games": len(g), "sessions": g[CLUSTER].nunique(), "share of games": pct(len(g) / len(games)),
            "bullet": pct((g["time_class"] == "bullet").mean()), "blitz": pct((g["time_class"] == "blitz").mean()), "rapid": pct((g["time_class"] == "rapid").mean()),
            "mean rating before": f"{g['my_rating_before'].mean():.0f}", "median game in session": f"{g['game_in_session'].median():.0f}",
            "last of session": pct(g["is_last_in_session"].mean()), "after a loss": pct((g["prev_outcome"] == "loss").mean()),
            "2022–23": pct(g["year"].le(2023).mean()), "2025–26": pct(g["year"].ge(2025).mean()),
        })  # fmt: skip
    table = pd.DataFrame(rows)
    report.table(table)
    spanning = games.groupby(CLUSTER)["band"].nunique()
    night = games[games["local_hour"].between(1, 6)]
    night_days = night.groupby("date").size().sort_values(ascending=False)
    episodes = night.groupby(night["local_start"].dt.to_period("M")).size().sort_values(ascending=False)
    top = ", ".join(f"{str(p)} ({n})" for p, n in episodes.head(3).items())
    report.text(
        f"{int((spanning > 1).sum())} of {len(spanning):,} sessions span more than one band ({int(games[CLUSTER].isin(spanning[spanning > 1].index).sum()):,} games); "
        "every game is assigned to the band of its own start time. "
        f"The {len(night)} games started between 01:00 and 06:59 are concentrated in a few episodes — by month: {top} — and on those days you also played "
        f"at ordinary hours (busiest night date: {night_days.index[0]} with {int(night_days.iloc[0])} night games), so they look like late nights rather than a "
        "trip to another timezone; a sensitivity check without the largest episode is in section 6. If you did travel, the hour labels of those games are wrong."
    )
    wd = games.groupby("weekday").size().reindex(WEEKDAYS)
    report.text("Games by weekday: " + ", ".join(f"{d[:3]} {int(n):,}" for d, n in wd.items()) + f" (weekend share {pct(games['is_weekend'].mean())}).")
    comp = table.set_index("band")
    return {
        "night_episode_month": episodes.index[0], "night_episode_n": int(episodes.iloc[0]),
        "spanning": int((spanning > 1).sum()), "n_sessions": len(spanning),
        "night_bullet": comp.loc[BAND_LABELS[0], "bullet"], "night_recent": comp.loc[BAND_LABELS[0], "2025–26"], "morning_early": comp.loc[BAND_LABELS[1], "2022–23"],
    }


def section_hourly(report: Report, games: pd.DataFrame) -> dict:
    report.heading("2. Hour by hour, all time classes pooled")
    report.text(
        "Per-game metrics by the hour the game started (hours 1–6 pooled), CIs bootstrapped by session. `your ep lost / move` and `their ep lost / move` are "
        "move-weighted ratios of sums over the games of the hour; `your − their ep / move` is the paired difference within the same games (negative = you "
        "erred less than your opponents at that hour) and is the comparison that survives a changing pool; `score − model expectation` is the result minus the "
        "calibration model's move-1 expectation (it knows both pre-game ratings and the time control); `flags / your losses` is the share of your losses that "
        "were on time."
    )
    table = metric_table(games, "hour_bucket", HOUR_ORDER, HEADLINE, "hour")
    report.table(table)
    curves = {name: values_table(games, "hour_bucket", HOUR_ORDER, name) for name in ["my_ep", "opp_ep", "ep_gap", "excess_model"]}

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(8.5, 9), sharex=True)
    x = np.arange(len(HOUR_ORDER))
    ax = axes[0]
    for name, side, marker in [("my_ep", "player", "o"), ("opp_ep", "opponent", "s")]:
        c = curves[name]
        ax.errorbar(x, c["est"], yerr=[c["est"] - c["lo"], c["hi"] - c["est"]], fmt=marker + "-", color=SIDE_COLOURS[side], lw=2, ms=5, capsize=2, label="you" if side == "player" else "opponents")
    ax.set_ylabel("ep lost / move")
    ax.set_title("Expected points lost per move by hour of day (all time classes)")
    ax.legend(loc="upper right")
    ax = axes[1]
    c = curves["ep_gap"]
    ax.axhline(0, color="grey", lw=1)
    ax.errorbar(x, c["est"], yerr=[c["est"] - c["lo"], c["hi"] - c["est"]], fmt="o-", color=SIDE_COLOURS["player"], lw=2, ms=5, capsize=2)
    ax.set_ylabel("you − opponents, ep / move")
    ax.set_title("Paired difference in the same games (negative = you erred less)")
    ax = axes[2]
    c = curves["excess_model"]
    ax.axhline(0, color="grey", lw=1)
    ax.errorbar(x, c["est"], yerr=[c["est"] - c["lo"], c["hi"] - c["est"]], fmt="o-", color="#444444", lw=2, ms=5, capsize=2)
    ax.set_ylabel("score − expectation")
    ax.set_title("Result minus the model's move-1 expectation")
    ax.set_xticks(x)
    ax.set_xticklabels(HOUR_ORDER)
    ax.set_xlabel("hour of day (Europe/London), hours 1–6 pooled")
    for a in axes:
        a.grid(True, axis="y", alpha=0.25)
    fig.tight_layout()
    report.figure(fig, "hourly", "Hour-by-hour curves with 95% CIs (sessions resampled); thin night hours pooled.")
    plt.close(fig)

    gap = curves["ep_gap"].set_index("level")
    excess = curves["excess_model"].set_index("level")
    sig_gap = [h for h, r in gap.iterrows() if excludes_zero(r["lo"], r["hi"])]
    sig_excess = [h for h, r in excess.iterrows() if excludes_zero(r["lo"], r["hi"])]
    spread = excess["est"].max() - excess["est"].min()
    report.text(
        f"Across the {len(HOUR_ORDER)} hour buckets the score − expectation ranges from {excess['est'].min():.3f} (hour {excess['est'].idxmin()}) to "
        f"{excess['est'].max():.3f} (hour {excess['est'].idxmax()}), a spread of {spread:.3f}; "
        + (f"its CI excludes zero at {len(sig_excess)} bucket(s): {', '.join(sig_excess)}. " if sig_excess else "no bucket's CI excludes zero. ")
        + f"The paired you − opponents ep gap excludes zero at {len(sig_gap)} bucket(s)"
        + (f" ({', '.join(f'{h}: {gap.loc[h, 'est']:+.4f}' for h in sig_gap)})" if sig_gap else "")
        + f"; with {len(HOUR_ORDER)} buckets, about one such cell is expected by chance. Your ep per move and the opponents' move together hour by hour "
        f"(correlation across buckets {np.corrcoef(curves['my_ep']['est'], curves['opp_ep']['est'])[0, 1]:.2f}): the hour changes the games, not you alone."
    )
    return {"curves": curves, "sig_gap": sig_gap, "sig_excess": sig_excess, "spread": spread}


def section_bands(report: Report, games: pd.DataFrame) -> dict:
    report.heading("3. Hour bands by time class")
    report.text(
        "The same metrics in six bands, separately by time class, plus cp per move, the opponents' flag share, your resignation share of losses and seconds "
        f"per move for both sides. Cells with fewer than {MIN_GAMES} games are blank. The 'band − other bands' rows give the contrast of each band against all other "
        "games of the time class with a CI on the difference (sessions resampled jointly)."
    )
    out = {}
    for tc in ["all", *TIME_CLASSES]:
        g = games if tc == "all" else games[games["time_class"] == tc]
        report.heading(f"{tc}", level=3)
        table = metric_table(g, "band", BAND_LABELS, FULL, "band")
        report.table(table)
        rows = []
        for band in BAND_LABELS:
            a, b = g[g["band"] == band], g[g["band"] != band]
            row = {"band − other bands": band, "games": len(a)}
            for name in ["excess_model", "ep_gap", "my_ep", "opp_ep", "timeout_share", "my_sec"]:
                if len(a) >= MIN_GAMES:
                    est, lo, hi = metric_diff(a, b, name)
                    row[METRICS[name][0]] = fmt(est, lo, hi, f=FMT[name])
                    out[(tc, band, name)] = (est, lo, hi)
                else:
                    row[METRICS[name][0]] = ""
            rows.append(row)
        report.table(pd.DataFrame(rows))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(11, 6.5), sharex=True)
    x = np.arange(len(BAND_LABELS))
    short = [b.split(" ")[1] for b in BAND_LABELS]
    for j, tc in enumerate(TIME_CLASSES):
        g = games[games["time_class"] == tc]
        for i, (name, colour, ylabel) in enumerate([("ep_gap", SIDE_COLOURS["player"], "you − opponents, ep / move"), ("excess_model", "#444444", "score − expectation")]):
            ax = axes[i, j]
            c = values_table(g, "band", BAND_LABELS, name)
            ax.axhline(0, color="grey", lw=1)
            ax.errorbar(x, c["est"], yerr=[c["est"] - c["lo"], c["hi"] - c["est"]], fmt="o", color=colour, ms=6, capsize=3, lw=2)
            for xi, n in zip(x, c["games"]):
                ax.annotate(f"n={n}", (xi, ax.get_ylim()[0]), xytext=(0, 4), textcoords="offset points", ha="center", fontsize=7, color="#666666")
            if i == 0:
                ax.set_title(tc)
            if j == 0:
                ax.set_ylabel(ylabel)
            ax.set_xticks(x)
            ax.set_xticklabels(short, rotation=0, fontsize=8)
    axes[1, 1].set_xlabel("hour band (Europe/London)")
    fig.suptitle("Hour bands by time class: paired ep gap (top) and score − expectation (bottom), 95% CIs", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "bands_by_class", "Six hour bands by time class; cell sizes under each point.")
    plt.close(fig)

    sig = [(tc, band, name, v) for (tc, band, name), v in out.items() if excludes_zero(v[1], v[2]) and name in ("excess_model", "ep_gap")]
    n_tests = sum(1 for (tc, band, name) in out if name in ("excess_model", "ep_gap"))
    if sig:
        listed = "; ".join(f"{tc} {band}, {METRICS[name][0]} {fmt(*v, f=FMT[name])}" for tc, band, name, v in sig)
        report.text(f"Of the {n_tests} band-vs-rest contrasts on score − expectation and the paired ep gap, {len(sig)} exclude zero (about {0.05 * n_tests:.0f} expected by chance): {listed}.")
    else:
        report.text(f"None of the {n_tests} band-vs-rest contrasts on score − expectation and the paired ep gap excludes zero.")
    return {"contrasts": out, "sig": sig, "n_tests": n_tests}


def section_weekday(report: Report, games: pd.DataFrame) -> dict:
    report.heading("4. Day of the week, and weekday × hour band")
    report.text("By weekday (all time classes, then the headline metrics per time class), CIs by session.")
    report.table(metric_table(games, "weekday", WEEKDAYS, HEADLINE, "weekday"))
    for tc in TIME_CLASSES:
        report.heading(f"{tc}", level=3)
        report.table(metric_table(games[games["time_class"] == tc], "weekday", WEEKDAYS, ["excess_model", "ep_gap", "timeout_share"], "weekday"))
    wk = {}
    rows = []
    for name in ["excess_model", "ep_gap", "my_ep", "opp_ep", "timeout_share", "my_sec"]:
        est, lo, hi = metric_diff(games[games["is_weekend"]], games[~games["is_weekend"]], name)
        wk[name] = (est, lo, hi)
        rows.append({"weekend − weekday (all classes)": METRICS[name][0], "estimate": fmt(est, lo, hi, f=FMT[name])})
    report.table(pd.DataFrame(rows))

    report.heading("Weekday × hour band", level=3)
    report.text(
        f"Cells are weekday (Mon–Fri) or weekend (Sat–Sun) by hour band; the full 7 × 6 grid is in the figure with cell counts. Cells under {MIN_GAMES} games are blank."
    )
    rows = []
    for daytype in ["weekday", "weekend"]:
        for band in BAND_LABELS:
            part = games[(games["daytype"] == daytype) & (games["band"] == band)]
            row = {"day type": daytype, "band": band, "games": len(part), "sessions": part[CLUSTER].nunique()}
            for name in HEADLINE:
                row[METRICS[name][0]] = cell(part, name)
            rows.append(row)
    report.table(pd.DataFrame(rows))

    # Named contrast: weekend evenings vs weekday nights.
    we_eve = games[games["is_weekend"] & games["local_hour"].ge(18)]
    wd_night = games[~games["is_weekend"] & (games["local_hour"].ge(21) | games["local_hour"].le(6))]
    rows = []
    contrast = {}
    for name in ["excess_model", "ep_gap", "my_ep", "opp_ep", "timeout_share", "my_sec"]:
        a, b = metric(we_eve, name), metric(wd_night, name)
        d = metric_diff(we_eve, wd_night, name)
        contrast[name] = d
        rows.append({"metric": METRICS[name][0], f"weekend evening 18–23 ({len(we_eve)} games)": fmt(*a, f=FMT[name]), f"weekday night 21–06 ({len(wd_night)} games)": fmt(*b, f=FMT[name]), "difference": fmt(*d, f=FMT[name])})
    report.table(pd.DataFrame(rows))

    # Heatmaps: 7 x 6 grid of score - expectation and the paired ep gap, counts annotated.
    import matplotlib.pyplot as plt
    from matplotlib.colors import TwoSlopeNorm

    grids = {}
    for name in ["excess_model", "ep_gap"]:
        grid = np.full((len(WEEKDAYS), len(BAND_LABELS)), np.nan)
        counts = np.zeros((len(WEEKDAYS), len(BAND_LABELS)), dtype=int)
        for i, day in enumerate(WEEKDAYS):
            for j, band in enumerate(BAND_LABELS):
                part = games[(games["weekday"] == day) & (games["band"] == band)]
                counts[i, j] = len(part)
                if len(part) >= MIN_GAMES:
                    grid[i, j] = metric(part, name, n_boot=2)[0]  # point estimate only: the CIs for these cells are in the tables above
        grids[name] = (grid, counts)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    for ax, (name, title) in zip(axes, [("excess_model", "score − model expectation"), ("ep_gap", "you − opponents, ep / move")]):
        grid, counts = grids[name]
        lim = np.nanmax(np.abs(grid))
        im = ax.imshow(np.ma.masked_invalid(grid), cmap="RdBu" if name == "excess_model" else "RdBu_r", norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
        ax.set_xticks(range(len(BAND_LABELS)))
        ax.set_xticklabels([b.split(" ")[1] for b in BAND_LABELS])
        ax.set_yticks(range(len(WEEKDAYS)))
        ax.set_yticklabels([d[:3] for d in WEEKDAYS])
        ax.grid(False)
        for i in range(len(WEEKDAYS)):
            for j in range(len(BAND_LABELS)):
                v = grid[i, j]
                label = f"n={counts[i, j]}" if np.isnan(v) else (f"{v:+.3f}\nn={counts[i, j]}" if name == "excess_model" else f"{v:+.4f}\nn={counts[i, j]}")
                dark = (not np.isnan(v)) and abs(v) > 0.55 * lim
                ax.text(j, i, label, ha="center", va="center", fontsize=7, color="#f5f5f5" if dark else "#222222")
        ax.set_title(title + (" (blue = better for you)"))
        fig.colorbar(im, ax=ax, shrink=0.8)
    axes[0].set_xlabel("hour band")
    axes[1].set_xlabel("hour band")
    fig.suptitle(f"Weekday × hour band, point estimates (cells under {MIN_GAMES} games blank)", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "weekday_band", "Weekday × hour band grid; cell counts shown; blue is better for you in both panels.")
    plt.close(fig)

    wd_excess = values_table(games, "weekday", WEEKDAYS, "excess_model").set_index("level")
    wd_gap = values_table(games, "weekday", WEEKDAYS, "ep_gap").set_index("level")
    sig_days = [d for d, r in wd_excess.iterrows() if excludes_zero(r["lo"], r["hi"])]
    sig_gap_days = [d for d, r in wd_gap.iterrows() if excludes_zero(r["lo"], r["hi"])]
    report.text(
        f"Weekdays: score − expectation runs from {wd_excess['est'].min():.3f} ({wd_excess['est'].idxmin()}) to {wd_excess['est'].max():.3f} ({wd_excess['est'].idxmax()}); "
        + (f"CI excludes zero on {', '.join(sig_days)}. " if sig_days else "no weekday's CI excludes zero. ")
        + (f"The paired ep gap excludes zero on {', '.join(sig_gap_days)}. " if sig_gap_days else "No weekday's paired ep gap excludes zero. ")
        + f"Weekend − weekday: score − expectation {fmt(*wk['excess_model'])}, ep gap {fmt(*wk['ep_gap'], f='.4f')}. "
        f"Weekend evenings vs weekday nights: score − expectation {fmt(*contrast['excess_model'])}, ep gap {fmt(*contrast['ep_gap'], f='.4f')}."
    )
    return {"weekend": wk, "contrast": contrast, "wd_excess": wd_excess, "wd_gap": wd_gap, "sig_days": sig_days, "sig_gap_days": sig_gap_days, "n_we_eve": len(we_eve), "n_wd_night": len(wd_night)}


def design(games: pd.DataFrame) -> pd.DataFrame:
    X = pd.DataFrame({"intercept": 1.0}, index=games.index)
    for band in BAND_LABELS:
        if band != REF_BAND:
            X[band] = (games["band"] == band).astype(float)
    X["weekend"] = games["is_weekend"].astype(float)
    X["last game of session"] = games["is_last_in_session"].astype(float)
    X["previous game lost"] = games["prev_loss"]
    X["previous game won"] = games["prev_win"]
    X["log2(game number)"] = np.log2(games["game_in_session"].astype(float))
    X["minutes / 30"] = games["minutes_into_session"] / 30
    X["blitz"] = (games["time_class"] == "blitz").astype(float)
    X["rapid"] = (games["time_class"] == "rapid").astype(float)
    X["rating before / 100"] = games["my_rating_before"].astype(float) / 100
    for y in sorted(games["year"].unique()):
        if y != 2024:
            X[f"year {y}"] = (games["year"] == y).astype(float)
    return X


def section_regression(report: Report, games: pd.DataFrame) -> dict:
    report.heading("5. Regression with session, time-class and era controls")
    report.text(
        f"Per-game OLS, CIs bootstrapped by session ({N_BOOT} resamples): outcome ~ hour band (reference {REF_BAND}) + weekend + last game of the session + "
        "previous game lost + previous game won + log2(game number) + minutes into the session / 30 + time class + pre-game rating / 100 + year dummies "
        "(reference 2024, not shown). This is the pooled, between-session model Q5 recommends for anything involving the previous result (a within-session model "
        "cannot identify the hour: sessions rarely cross a band). The outcomes: score − model expectation, your and their ep per move, their paired difference, "
        "whether you lost on time (all games), your seconds per move and your cp per move. The joint test below asks whether the five band coefficients are "
        f"larger than they would be if the hour carried no information: the band labels are shuffled across sessions (whole sessions keep their games) {N_PERM} times "
        "and the largest absolute band coefficient and the sum of their squares are compared with the observed ones."
    )
    sub = games[games["my_rating_before"].notna() & games["my_rating_before"].gt(0)].copy()
    X = design(sub)
    groups = sub[CLUSTER].to_numpy()
    outcomes = {
        "excess_model": (sub["excess_model"], ".3f", "score − model exp."),
        "my_ep_rate": (sub["my_ep_rate"], ".4f", "your ep / move"),
        "opp_ep_rate": (sub["opp_ep_rate"], ".4f", "their ep / move"),
        "ep_rate_gap": (sub["ep_rate_gap"], ".4f", "your − their ep / move"),
        "lost_on_time": (sub["lost_on_time"], ".3f", "you lost on time"),
        "my_sec_per_move": (sub["my_sec_per_move"], ".2f", "your s / move"),
        "my_cp_rate": (sub["my_cp_lost"] / sub["my_moves"].replace(0, np.nan), ".1f", "your cp / move"),
    }  # fmt: skip
    band_terms = [b for b in BAND_LABELS if b != REF_BAND]
    band_idx = [list(X.columns).index(b) for b in band_terms]
    rows = {}
    results = {}
    perm_rows = []
    rng = np.random.default_rng(SEED)
    # Session-level band labels for the permutation: the band of the session's first game.
    first_band = sub.groupby(CLUSTER)["band"].first()
    session_ids = first_band.index.to_numpy()
    for name, (y, f, label) in outcomes.items():
        ok = y.notna().to_numpy()
        Xm, ym, gm = X.to_numpy(float)[ok], y.to_numpy(float)[ok], groups[ok]
        beta, draws = q5.cluster_ols(Xm, ym, gm, n_boot=N_BOOT, seed=SEED)
        lo, hi = np.nanquantile(draws, [0.025, 0.975], axis=0)
        rows[label] = [fmt(b, l, h, f=f) for b, l, h in zip(beta, lo, hi)]
        results[name] = dict(zip(X.columns, zip(beta, lo, hi)))
        # permutation test of the band block
        obs_max = np.max(np.abs(beta[band_idx]))
        obs_ss = float(np.sum(beta[band_idx] ** 2))
        Xp = Xm.copy()
        sub_ok = sub[ok]
        session_of_game = sub_ok[CLUSTER].to_numpy()
        pos = pd.Index(session_ids).get_indexer(session_of_game)
        perm_max, perm_ss = np.empty(N_PERM), np.empty(N_PERM)
        for p in range(N_PERM):
            shuffled = first_band.to_numpy()[rng.permutation(len(session_ids))]
            game_band = shuffled[pos]
            for k, b in zip(band_idx, band_terms):
                Xp[:, k] = (game_band == b).astype(float)
            bp = np.linalg.lstsq(Xp, ym, rcond=None)[0]
            perm_max[p] = np.max(np.abs(bp[band_idx]))
            perm_ss[p] = np.sum(bp[band_idx] ** 2)
        p_max = float((perm_max >= obs_max).mean())
        p_ss = float((perm_ss >= obs_ss).mean())
        results[name]["perm_p_max"], results[name]["perm_p_ss"] = p_max, p_ss
        perm_rows.append({"outcome": label, "largest |band coef.|": f"{obs_max:{f}}", "permutation p (max)": f"{p_max:.2f}", "permutation p (sum of squares)": f"{p_ss:.2f}"})
    table = pd.DataFrame({"term": list(X.columns), **rows})
    table = table[~table["term"].str.startswith("year")]
    report.table(table)
    report.text(f"{len(sub):,} games in {sub[CLUSTER].nunique():,} sessions ({len(games) - len(sub)} games without a pre-game rating dropped).")
    report.text("Joint test of the five hour-band terms (session-level permutation of the band labels):")
    report.table(pd.DataFrame(perm_rows))

    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(12, 4))
    terms = band_terms + ["weekend", "last game of session", "previous game lost"]
    for ax, (name, title) in zip(axes, [("excess_model", "score − expectation"), ("ep_rate_gap", "you − opponents, ep / move"), ("my_ep_rate", "your ep / move")]):
        r = results[name]
        est = [r[t][0] for t in terms]
        lo = [r[t][1] for t in terms]
        hi = [r[t][2] for t in terms]
        yy = np.arange(len(terms))[::-1]
        colours = [SIDE_COLOURS["player"] if t in band_terms else "#888888" for t in terms]
        ax.axvline(0, color="grey", lw=1)
        for e, l, h, y_, colour in zip(est, lo, hi, yy, colours):
            ax.errorbar([e], [y_], xerr=[[e - l], [h - e]], fmt="o", color=colour, elinewidth=2, capsize=3, ms=6)
        ax.set_yticks(yy)
        ax.set_yticklabels(terms if ax is axes[0] else [""] * len(terms))
        ax.set_title(title)
        ax.grid(True, axis="x", alpha=0.25)
        ax.grid(False, axis="y")
    fig.suptitle(f"Regression coefficients (hour bands vs {REF_BAND} in blue; session controls in grey), 95% session-bootstrap CIs", fontweight="bold")
    fig.tight_layout()
    report.figure(fig, "regression", "Hour-band coefficients next to the session controls they are estimated with.")
    plt.close(fig)

    sig = [(name, t) for name in ("excess_model", "ep_rate_gap", "my_ep_rate") for t in band_terms if excludes_zero(results[name][t][1], results[name][t][2])]
    ex = results["excess_model"]
    report.text(
        f"With the controls, the hour-band coefficients on score − expectation run from {min(ex[t][0] for t in band_terms):+.3f} to {max(ex[t][0] for t in band_terms):+.3f} "
        f"relative to {REF_BAND}; " + (f"CIs exclude zero for: {', '.join(f'{n} × {t}' for n, t in sig)}. " if sig else "no band coefficient's CI excludes zero on score, the paired ep gap or your ep per move. ")
        + f"The permutation p-values for the band block are {results['excess_model']['perm_p_max']:.2f} (score), {results['ep_rate_gap']['perm_p_max']:.2f} (paired ep gap) and "
        f"{results['my_ep_rate']['perm_p_max']:.2f} (your ep per move). For scale, the session controls in the same model: last game of the session {fmt(*ex['last game of session'])}, "
        f"previous game lost {fmt(*ex['previous game lost'])}, previous game won {fmt(*ex['previous game won'])} on score − expectation."
    )
    return results


def section_sensitivity(report: Report, games: pd.DataFrame, when: dict) -> dict:
    report.heading("6. Stability checks")
    report.text(
        "A real time-of-day effect should show the same sign in both halves of the history and should not hinge on one episode of late nights. "
        "Left: score − expectation and the paired ep gap by band for 2021–2023 and 2024–2026. Right: the night band without its largest episode."
    )
    rows = []
    halves = {"2021–2023": games[games["year"] <= 2023], "2024–2026": games[games["year"] >= 2024]}
    for band in BAND_LABELS:
        row = {"band": band}
        for label, g in halves.items():
            part = g[g["band"] == band]
            row[f"games {label}"] = len(part)
            row[f"score − exp. {label}"] = cell(part, "excess_model")
            row[f"ep gap {label}"] = cell(part, "ep_gap")
        rows.append(row)
    report.table(pd.DataFrame(rows))
    month = when["night_episode_month"]
    night = games[games["band"] == BAND_LABELS[0]]
    night_wo = night[night["local_start"].dt.to_period("M") != month]
    rows = []
    for label, part in [("night 0-6, all", night), (f"night 0-6 without {month}", night_wo), ("other bands", games[games["band"] != BAND_LABELS[0]])]:
        row = {"games": label, "n": len(part)}
        for name in HEADLINE:
            row[METRICS[name][0]] = cell(part, name)
        rows.append(row)
    report.table(pd.DataFrame(rows))
    # Sign agreement between halves, and the two halves' estimates per band for the summary
    agree = 0
    n = 0
    by_half = {}
    for band in BAND_LABELS:
        for label, g in halves.items():
            part = g[g["band"] == band]
            by_half[(band, label, "excess_model")] = metric(part, "excess_model") if len(part) >= MIN_GAMES else (np.nan, np.nan, np.nan)
            by_half[(band, label, "ep_gap")] = metric(part, "ep_gap") if len(part) >= MIN_GAMES else (np.nan, np.nan, np.nan)
        a, b = by_half[(band, "2021–2023", "excess_model")][0], by_half[(band, "2024–2026", "excess_model")][0]
        if pd.notna(a) and pd.notna(b):
            n += 1
            agree += int(np.sign(a) == np.sign(b))
    report.text(f"The sign of score − expectation agrees between the two halves in {agree} of {n} bands with enough games in both.")
    return {"agree": agree, "n_bands": n, "night_wo": len(night_wo), "by_half": by_half}


# --- main ---------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    figure_style()
    data = load_data()
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    games = build(data, decomposed)

    report = Report(SLUG, "Time of day and day of week")
    n_by_tc = games["time_class"].value_counts()
    report.text(
        f"Data: {len(games):,} standard live games with at least one move ({n_by_tc.get('bullet', 0):,} bullet, {n_by_tc.get('blitz', 0):,} blitz, "
        f"{n_by_tc.get('rapid', 0):,} rapid) in {games[CLUSTER].nunique():,} sessions (Q5's 30-minute gap over standard live games), {len(data.moves):,} analysed moves, "
        f"player {data.username}, {games['local_start'].min():%Y-%m-%d} to {games['local_start'].max():%Y-%m-%d}. Hours and weekdays are local (Europe/London). "
        "Per-game quality comes from Q1's `games_decomposed.parquet`; session variables and the metric machinery are Q5's. Numbers are estimate [95% CI], "
        f"CIs bootstrapped by session ({N_BOOT} resamples), since the games of a session share the hour. `ep_loss` (calibrated expected points lost per move) is the "
        "main quality measure, `cp_loss` secondary, and every number about you sits next to the opponents' number from the same games."
    )
    when = section_when(report, games)
    hourly = section_hourly(report, games)
    bands = section_bands(report, games)
    weekday = section_weekday(report, games)
    regression = section_regression(report, games)
    stability = section_sensitivity(report, games, when)

    # --- summary from the results ---
    ex = regression["excess_model"]
    gap = regression["ep_rate_gap"]
    band_terms = [b for b in BAND_LABELS if b != REF_BAND]
    worst_band = min(band_terms, key=lambda t: ex[t][0])
    best_band = max(band_terms, key=lambda t: ex[t][0])
    sig_terms = [(label, t) for label, r in (("score", ex), ("paired ep gap", gap)) for t in band_terms if excludes_zero(r[t][1], r[t][2])]
    p_score, p_gap = ex["perm_p_max"], gap["perm_p_max"]
    bh = stability["by_half"]
    candidate = ""
    if sig_terms:
        label, t = sig_terms[0]
        key = "excess_model" if label == "score" else "ep_gap"
        f = ".3f" if key == "excess_model" else ".4f"
        h1, h2 = bh[(t, "2021–2023", key)], bh[(t, "2024–2026", key)]
        replicates = pd.notna(h1[0]) and pd.notna(h2[0]) and np.sign(h1[0]) == np.sign(h2[0]) and excludes_zero(h1[1], h1[2]) and excludes_zero(h2[1], h2[2])
        candidate = (
            f"The one band coefficient whose CI excludes zero ({t}, {label}: {fmt(*(ex if key == 'excess_model' else gap)[t], f=f)}) is "
            + ("present in both halves of the history " if replicates else "not stable: ")
            + f"{fmt(*h1, f=f)} in 2021–2023 vs {fmt(*h2, f=f)} in 2024–2026, "
            + ("so it is a candidate worth re-checking with more games. " if replicates else "so it is best read as one of the chance hits a 46-contrast table produces. ")
        )
    late = bands["contrasts"].get(("all", "late 21-23", "excess_model"))
    late_gap = bands["contrasts"].get(("all", "late 21-23", "ep_gap"))
    late_flag = bands["contrasts"].get(("all", "late 21-23", "timeout_share"))
    night = bands["contrasts"].get(("all", "night 0-6", "excess_model"))
    midday = bands["contrasts"].get(("all", "midday 12-14", "excess_model"))
    wk = weekday["weekend"]
    report.heading("What this means for the player")
    if p_score < 0.05 or p_gap < 0.05:
        verdict = (
            f"There is a time-of-day signal after the session controls: the {worst_band} band is the weakest ({fmt(*ex[worst_band])} on score − expectation vs {REF_BAND}) "
            f"and {best_band} the strongest ({fmt(*ex[best_band])}); permutation p {p_score:.2f} on score and {p_gap:.2f} on the paired ep gap. "
        )
    else:
        verdict = (
            f"The hour of the day is not where your points go. Raw late-evening games (21–23) score {fmt(*late)} relative to the rest and night games {fmt(*night)}, midday games "
            f"{fmt(*midday)}, but once the session controls, time class, rating and year are in the model no band differs from the afternoon beyond its CI "
            f"(range {min(ex[t][0] for t in band_terms):+.3f} to {max(ex[t][0] for t in band_terms):+.3f}; permutation p {p_score:.2f} on score, {p_gap:.2f} on the paired ep gap), "
            f"and your opponents' error moves with yours hour by hour (late 21–23 paired ep gap {fmt(*late_gap, f='.4f')}, flags / your losses {fmt(*late_flag)} vs the rest). "
            + candidate
            + f"The sign of the raw band effects on score agrees between the two halves of the history in only {stability['agree']} of {stability['n_bands']} bands. "
        )
    report.text(
        verdict
        + f"Weekend − weekday is {fmt(*wk['excess_model'])} in score − expectation and {fmt(*wk['ep_gap'], f='.4f')} in the paired ep gap; weekend evenings vs weekday nights "
        f"{fmt(*weekday['contrast']['excess_model'])}. The session effects dwarf anything the clock on the wall does: last game of the session {fmt(*ex['last game of session'])}, "
        f"previous game lost {fmt(*ex['previous game lost'])}. Pick the hour you like; what matters is stopping after a loss, not the time it happens."
    )
    report.heading("Caveats")
    report.text(
        "- Hours are Europe/London from UTC start times; travel breaks the labels. The 01:00–06:59 games cluster in a few episodes "
        f"({when['night_episode_month']}: {when['night_episode_n']} games); the night band without that episode has {stability['night_wo']} games (section 6).\n"
        f"- You choose when to play. Hours are not assigned at random: the night band is {when['night_bullet']} bullet and {when['night_recent']} from 2025–26, "
        f"the morning band is {when['morning_early']} from 2022–23, and the sessions "
        "that run late are the ones that were going well (or badly) enough to continue. Time class, rating, year and the session position are controlled; the "
        "reason you were still playing at 23:00 is not.\n"
        "- Score − expectation in late-session games is positively selected between sessions (Q5); the regression includes the last-game and previous-result "
        "dummies but is a pooled, between-session model. The paired you − opponents ep gap is the measure least exposed to this and to the pool changing by hour.\n"
        f"- Many cells: {len(HOUR_ORDER)} hourly buckets, {len(BAND_LABELS)} bands × 4 classes, 7 weekdays, 42 weekday × band cells; a few CIs excluding zero are "
        "expected by chance, which is why the permutation test of the band block is the headline test. Cells under 20 games are blank; the heat-map shows point estimates only.\n"
        f"- Sessions can span bands ({when['spanning']} of {when['n_sessions']:,} do); each game carries the band of its own start. CIs are clustered by session, which is conservative for between-band comparisons.\n"
        "- The opponents' approximate pre-game rating and the model's move-1 expectation inherit Q1's caveats (pooled calibration, under-confident at the extremes); "
        "`my_ep_start` already prices in the pre-game ratings and the time control, so a weaker night-time pool is not mistaken for better play.\n"
        "- Seconds per move pool time controls within a class; compare within a time class only."
    )
    path = report.write()
    print(f"wrote {path} in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
