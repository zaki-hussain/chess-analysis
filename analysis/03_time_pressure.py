"""Question 3: time pressure. How often you are short of time, how your move
quality changes as the clock runs down, and whether you degrade more than
your opponents in the same games, per exact time control.

Computes, for every reported time control (and pooled by time class where
the per-control cells are thin):
1. the share of moves played under 5/10/30 s (seconds left, buffer beyond
   the increment, share of base time), you vs opponents;
2. the share of games in which each side drops under the low-clock line;
3. expected points (and centipawns) lost per move by clock bucket, in
   seconds left, in buffer and as a share of the base time, plus what
   low-clock positions look like (phase, ply, stakes);
4. the susceptibility regression: move quality ~ clock bucket x side with
   phase, stakes, ply, material and the opponent's clock held fixed
   (game-clustered bootstrap), with and without the stakes control and
   within endgames / earlier phases;
5. the rating your play resembles at each clock level, on the opponents'
   cp_loss-by-rating curve;
6. what low-clock moves cost per game.

Writes CSVs to presentation/data/03_time_pressure/ and SVGs to
presentation/figures/03_time_pressure/ (no prose). Shared clock machinery
lives in chess_analysis.clocks.

Run: .venv/bin/python analysis/03_time_pressure.py  (about 2-4 minutes; 230 s measured with other jobs running)
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from chess_analysis import charts, clocks
from chess_analysis.toolkit import MIN_GROUP_GAMES, Results, compare_sides, excess_to_elo, load_data, ratio_bootstrap

SLUG = "03_time_pressure"
SEED = 3
N_BOOT = 500  # resamples for means and shares (fast path)
N_BOOT_REG = 400  # resamples for the regressions and rating curves (each replicate is a solve)
N_BOOT_COST = 1000  # resamples for the per-game cost and share-of-losses CIs (cheap sums; tighter CI ends)

# Cumulative "under X" lines for exposure: 5 s (a scramble), 10 s (the classic low clock), 30 s (still a few
# seconds a move in a bullet ending, the regression's reference line).
EXPOSURE_SECONDS = [5.0, clocks.LOW_CLOCK_SECONDS, clocks.COMFORT_SECONDS]
# The same idea relative to the base time: 10% of base is 6 s in 1+0, 18 s in 3+0, 60 s in 10+0.
LOW_SHARE_OF_BASE = 0.10
# A regression coefficient for a clock bucket needs this many moves per side: with ~30 controls the
# bucket's own coefficient is unstable below a few hundred moves, and 100 is where CIs stop being
# dominated by single games in these data.
MIN_REG_MOVES = 100
# A rating-equivalent target needs this many of your moves (same reasoning as clocks.MIN_CELL_MOVES).
MIN_TARGET_MOVES = clocks.MIN_CELL_MOVES
# Opponents rated under this (pre-game proxy) are left out of the rating curve: chess.com floors ratings at
# 100, so lower values are reconstruction noise.
MIN_CURVE_RATING = 100
# Ratings shown on the rating-equivalent chart (CI ends beyond it are cut at the edge). An estimate outside the
# opponents' rating range in that control (curve_rating_min..max) is an extrapolation of a straight line, not a
# rating: it is flagged `extrapolated` and drawn as a hollow marker at the edge of the chart.
RATING_PLOT_RANGE = (0, 2000)
# A "blunder" for the share-of-losses table: a move losing 0.1 expected points or more (a tenth of a game).
BLUNDER_EP = 0.10
# Elo-equivalents of per-game point differences are taken around an even score.
EVEN_SCORE = 0.5


def exposure_flags(moves: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    """(measure, threshold label) -> boolean per move: the mover was under the line before the move."""
    flags = {}
    for thr in EXPOSURE_SECONDS:
        flags[("seconds left", f"<{thr:g} s")] = moves["clock_before_seconds"] < thr
        flags[("buffer beyond increment", f"<{thr:g} s")] = moves["clock_buffer_seconds"] < thr
    flags[("share of base", f"<{100 * LOW_SHARE_OF_BASE:g}% of base")] = moves["clock_fraction"] < LOW_SHARE_OF_BASE
    return flags


def buffer_differs(measure: str, g: str, increment: pd.Series) -> bool:
    """False for the buffer version of a control without increment (there it repeats the seconds-left row)."""
    return "buffer" not in measure or increment.get(g, 0) > 0


def tidy_compare(table: pd.DataFrame, label: str, **levels) -> pd.DataFrame:
    out = table.copy()
    for k, (name, value) in enumerate(levels.items()):
        out.insert(k, name, value)
    out.insert(0, "tc_group", label)
    return out


def by_group(frame: pd.DataFrame, order: list[str]) -> pd.DataFrame:
    """Rows in time-control order (pooled "all <class>" rows last), otherwise as they came."""
    rank = {g: i for i, g in enumerate(order)}
    return frame.sort_values("tc_group", key=lambda s: s.map(rank).fillna(len(rank)), kind="stable").reset_index(drop=True)


def thin(ax, text: str) -> None:
    """charts.thin_note for a panel in a shared-axis grid: hides this panel's ticks only (thin_note's
    set_yticks([]) would clear the shared axis of every panel)."""
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
    ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
    ax.grid(False)


def rotate(ax) -> None:
    for tick in ax.get_xticklabels():
        tick.set_rotation(45)
        tick.set_ha("right")


def curve_chart(table: pd.DataFrame, bucket: str, labels: list[str], groups: list[str], ylabel: str, title: str, note: str):
    fig, axes = charts.group_grid(groups, sharey=True, title=title, panel=(2.9, 2.5))
    for g, ax in axes.items():
        part = table[(table["tc_group"] == g) & ~table["thin"]]
        if part.empty:
            thin(ax, "too few low-clock moves")
            continue
        charts.side_lines(ax, part, bucket, order=labels)
        rotate(ax)
    axes[groups[0]].set_ylabel(ylabel)
    charts.legend(fig, loc="upper right", bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, note)
    fig.tight_layout()
    return fig


def gap_chart(table: pd.DataFrame, labels: list[str], groups: list[str], title: str, ylabel: str, note: str):
    fig, axes = charts.group_grid(groups, sharey=True, title=title, panel=(2.6, 2.3))
    for g, ax in axes.items():
        part = table[(table["tc_group"] == g) & ~table["thin"]]
        if part.empty:
            thin(ax, "too few low-clock moves")
            continue
        charts.zero_line(ax)
        charts.estimate(ax, part, "clock", y="diff", lo="diff_lo", hi="diff_hi", order=labels, label="you minus opponents")
    axes[groups[0]].set_ylabel(ylabel)
    charts.footnote(fig, note)
    fig.tight_layout()
    return fig


def main() -> None:
    t0 = time.time()
    charts.style()
    data = load_data()
    groups = data.tc_groups
    moves = clocks.add_clock_buckets(data.moves[data.moves["tc_group"].isin(groups)])
    moves["tc_group"] = moves["tc_group"].astype(str)
    games = data.games[data.games["tc_group"].isin(groups)].copy()
    games["tc_group"] = games["tc_group"].astype(str)
    increment = games.groupby("tc_group")["increment_seconds"].first()
    results = Results(SLUG)

    # 1. how often each side is short of time ------------------------------------------------
    flags = exposure_flags(moves)
    rows = []
    for (measure, line), flag in flags.items():
        frame = moves.assign(under=flag.astype(float))
        for g, part in clocks.group_frames(frame, groups):
            if not buffer_differs(measure, g, increment):
                continue
            t = compare_sides(part, "under", n_boot=N_BOOT)
            t["n_player_under"] = int(part.loc[part["is_player_move"], "under"].sum())
            t["n_opponent_under"] = int(part.loc[~part["is_player_move"], "under"].sum())
            rows.append(tidy_compare(t, g, measure=measure, line=line))
    share = pd.concat(rows, ignore_index=True).rename(columns={"n": "moves"})
    share["thin"] = share["games"] < MIN_GROUP_GAMES
    share = by_group(share, groups)
    results.table("1_low_clock_share", share, "Share of each side's moves played under a clock line (seconds left, buffer beyond the increment (increment controls only), or share of base time), you vs opponents, per time control; CIs by game.")
    seconds = share[share["measure"] == "seconds left"]
    fig, axes = charts.group_grid(groups, sharey="row", title="Share of moves played under 5, 10 and 30 seconds")
    for g, ax in axes.items():
        charts.side_bars(ax, seconds[seconds["tc_group"] == g], "line", order=[f"<{t:g} s" for t in EXPOSURE_SECONDS])
        ax.yaxis.set_major_formatter(_percent())
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Seconds left on the mover's clock before the move. Rows share a scale within a time class. Whiskers: 95% CI, resampling games.")
    fig.tight_layout()
    results.figure(fig, "1_low_clock_share")
    print(f"1 done at {time.time() - t0:.0f} s")

    # 2. how often a game goes under the line --------------------------------------------------
    lines = {
        "<10 s left": moves["clock_before_seconds"] < clocks.LOW_CLOCK_SECONDS,
        "<10 s buffer": moves["clock_buffer_seconds"] < clocks.LOW_CLOCK_SECONDS,
        "<5 s left": moves["clock_before_seconds"] < EXPOSURE_SECONDS[0],
        "<5 s buffer": moves["clock_buffer_seconds"] < EXPOSURE_SECONDS[0],
        f"<{100 * LOW_SHARE_OF_BASE:g}% of base": moves["clock_fraction"] < LOW_SHARE_OF_BASE,
    }
    rows = []
    for line, flag in lines.items():
        per_game = moves.assign(under=flag.astype(float)).groupby(["tc_group", "game_uuid", "is_player_move"], observed=True)["under"].max().reset_index()
        for g, part in clocks.group_frames(per_game, groups):
            if not buffer_differs(line, g, increment):
                continue
            t = compare_sides(part, "under", n_boot=N_BOOT)
            rows.append(tidy_compare(t, g, line=line))
    ever = pd.concat(rows, ignore_index=True).drop(columns="n")
    ever["thin"] = ever["games"] < MIN_GROUP_GAMES
    ever = by_group(ever, groups)
    results.table("2_games_under_line", ever, "Share of games in which each side dropped under the line at least once before a move (games with at least one move by that side), you vs opponents; buffer lines for increment controls only; CIs by game.")
    chart_lines = ["<10 s left", "<10 s buffer", f"<{100 * LOW_SHARE_OF_BASE:g}% of base"]
    fig, axes = charts.group_grid(groups, sharey="row", title="Share of games in which you drop under the low-clock line")
    for g, ax in axes.items():
        part = ever[ever["tc_group"] == g]
        charts.side_bars(ax, part, "line", order=[c for c in chart_lines if c in set(part["line"])])
        ax.yaxis.set_major_formatter(_percent())
        rotate(ax)
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Buffer = seconds left minus one increment (shown for increment controls only). 10% of base is 6 s in 1+0, 18 s in 3+0, 60 s in 10+0.")
    fig.tight_layout()
    results.figure(fig, "2_games_under_line")
    print(f"2 done at {time.time() - t0:.0f} s")

    # 3. quality by clock ------------------------------------------------------------------
    curve = clocks.bucket_curve(moves, "clock_bucket", "ep_loss", groups, n_boot=N_BOOT)
    results.table("3_quality_by_clock", curve, "Expected points lost per move by seconds left before the move, you vs opponents (diff = you minus opponents), per time control; thin = fewer than 30 moves on a side.")
    fig = curve_chart(curve, "clock_bucket", clocks.CLOCK_SECONDS_LABELS, groups, "expected points lost per move", "Expected points lost per move as the clock runs down", "Seconds left before the move. Bands: 95% CI, resampling games. Buckets with fewer than 30 moves on a side are left out.")
    results.figure(fig, "3_quality_by_clock")
    inc_groups = [g for g in groups if increment.get(g, 0) > 0]
    buffer_curve = clocks.bucket_curve(moves, "buffer_bucket", "ep_loss", inc_groups, n_boot=N_BOOT)
    results.table("3b_quality_by_buffer", buffer_curve, "As 3_quality_by_clock, by clock buffer (seconds left minus one increment), for the controls with an increment (without one it equals seconds left).")
    fig = curve_chart(buffer_curve, "buffer_bucket", clocks.CLOCK_SECONDS_LABELS, inc_groups, "expected points lost per move", "Expected points lost per move by clock buffer (increment games)", "Buffer = seconds left minus one increment. Bands: 95% CI, resampling games. Buckets with fewer than 30 moves on a side are left out.")
    results.figure(fig, "3b_quality_by_buffer")
    cp_curve = clocks.bucket_curve(moves, "clock_bucket", "cp_loss", groups, n_boot=N_BOOT)
    results.table("3c_cp_loss_by_clock", cp_curve, "Centipawns lost per move by seconds left before the move, you vs opponents, per time control (clock-free currency).")
    frac_curve = clocks.bucket_curve(moves, "fraction_bucket", "ep_loss", groups, n_boot=N_BOOT)
    results.table("3d_quality_by_share_of_base", frac_curve, "Expected points lost per move by clock left as a share of the base time (above 100% with increments), you vs opponents.")
    comp = (
        moves.groupby(["tc_group", "clock_coarse"], observed=True)
        .agg(endgame_share=("is_endgame", "mean"), mean_ply=("ply", "mean"), mean_ep_criticality=("ep_criticality", "mean"), moves=("ply", "size"), games=("game_uuid", "nunique"))
        .reset_index()
    )
    comp["clock_coarse"] = comp["clock_coarse"].astype(str)
    comp = by_group(comp, groups)
    comp["thin"] = comp["moves"] < 2 * clocks.MIN_CELL_MOVES
    results.table("3e_low_clock_positions", comp, "What moves at each clock level look like (both sides): share of endgame moves, mean ply, mean stakes (ep_criticality: expected points between the best and second-best move).")
    print(f"3 done at {time.time() - t0:.0f} s")

    # 4. adjusted susceptibility -------------------------------------------------------------
    rows = []
    for g, part in clocks.group_frames(moves, groups, pooled=True, tc_class=data.tc_class):
        has_increment = bool((part["increment_seconds"] > 0).any())
        for measure in clocks.CLOCK_MEASURES:
            if measure != "seconds left" and not has_increment:
                continue  # identical to seconds left
            strata = [("all", part)]
            if measure == "seconds left":
                strata += [("endgame", part[part["is_endgame"]]), ("opening/middlegame", part[~part["is_endgame"]])]
            for stratum, sub in strata:
                for outcome in ("ep_loss", "cp_loss"):
                    for stakes in ((True, False) if stratum == "all" and measure == "seconds left" else (True,)):
                        t = clocks.susceptibility_regression(sub, outcome, measure=measure, stakes=stakes, n_boot=N_BOOT_REG, seed=SEED)
                        rows.append(tidy_compare(t, g, measure=measure, stratum=stratum, outcome=outcome, stakes_control=stakes))
        print(f"  regression {g} at {time.time() - t0:.0f} s")
    reg = pd.concat(rows, ignore_index=True)
    reg["thin"] = (reg["n_player"] < MIN_REG_MOVES) | (reg["n_opponent"] < MIN_REG_MOVES) | reg["diff"].isna()
    results.table(
        "4_adjusted_gap",
        reg,
        "Adjusted change in move quality per move relative to 30+ s on the clock (opponent, player) and you minus opponents (diff: the susceptibility test), from outcome ~ clock bucket x side + phase + stakes + ply + pieces + opponent's clock bucket (+ time control when pooled); CIs resample games; thin = fewer than 100 moves on a side.",
    )
    panels = [*groups, *[g for g, _ in clocks.group_frames(moves, groups, pooled=True, tc_class=data.tc_class) if g.startswith("all ")]]
    sel = reg[(reg["measure"] == "seconds left") & (reg["stratum"] == "all") & reg["stakes_control"]]
    fig = gap_chart(sel[sel["outcome"] == "ep_loss"], clocks.CLOCK_COARSE_LABELS[:-1], panels, "Under time pressure, do you lose more than your opponents? (stakes held fixed)", "you minus opponents,\nexpected points per move", "Extra expected points lost per move relative to 30+ s, you minus opponents, holding phase, stakes, ply, material and the opponent's clock fixed. Above 0: you degrade more. Bottom row: controls of a class pooled. 95% CI, resampling games; buckets with fewer than 100 moves a side are left out.")
    results.figure(fig, "4_adjusted_gap")
    fig = gap_chart(sel[sel["outcome"] == "cp_loss"], clocks.CLOCK_COARSE_LABELS[:-1], panels, "The same gap in centipawns", "you minus opponents,\ncentipawns per move", "As above in centipawns lost per move (clock-free).")
    results.figure(fig, "4b_adjusted_gap_cp")
    print(f"4 done at {time.time() - t0:.0f} s")

    # 5. rating equivalent -------------------------------------------------------------------
    moves["blunder"] = (moves["label"] == "blunder").astype(float)
    rows = []
    for g, part in clocks.group_frames(moves, groups, pooled=True, tc_class=data.tc_class):
        curve_moves = part[~part["is_player_move"] & (part["opponent_rating_pre"] >= MIN_CURVE_RATING)]
        mine = part[part["is_player_move"]]
        actual = games.loc[games["uuid"].isin(part["game_uuid"].unique()), "my_rating_before"].astype(float)
        has_increment = bool((part["increment_seconds"] > 0).any())
        targets = {
            f"<{clocks.LOW_CLOCK_SECONDS:g} s left": mine[mine["low_clock"]],
            **({f"<{clocks.LOW_CLOCK_SECONDS:g} s buffer": mine[mine["low_buffer"]]} if has_increment else {}),
            f"{clocks.COMFORT_SECONDS:g}+ s left": mine[mine["clock_before_seconds"] >= clocks.COMFORT_SECONDS],
            **({f"{clocks.COMFORT_SECONDS:g}+ s buffer": mine[mine["clock_buffer_seconds"] >= clocks.COMFORT_SECONDS]} if has_increment else {}),
            "all moves": mine,
        }
        for currency in ("cp_loss", "blunder"):
            for name, target in targets.items():
                r = clocks.rating_equivalent(curve_moves, target, currency, n_boot=N_BOOT_REG, seed=SEED)
                rows.append(
                    {
                        "tc_group": g, "currency": currency, "your_moves": name,
                        "mean": r["mean"], "mean_lo": r["mean_lo"], "mean_hi": r["mean_hi"],
                        "slope_per_100": r["slope_per_100"], "slope_lo": r["slope_lo"], "slope_hi": r["slope_hi"],
                        "rating_equivalent": r["rating_equiv"], "rating_lo": r["rating_lo"], "rating_hi": r["rating_hi"],
                        "your_mean_rating": actual.mean(), "slope_clear": bool(pd.notna(r["rating_equiv"])),
                        "n_target_moves": r["n_target_moves"], "n_curve_moves": r["n_curve_moves"],
                        "curve_rating_min": curve_moves["opponent_rating_pre"].min(), "curve_rating_max": curve_moves["opponent_rating_pre"].max(),
                    }  # fmt: skip
                )
    equiv = pd.DataFrame(rows)
    equiv["thin"] = equiv["n_target_moves"] < MIN_TARGET_MOVES
    for col in ("rating_equivalent", "rating_lo", "rating_hi"):
        equiv.loc[equiv["thin"], col] = np.nan
    equiv["extrapolated"] = (equiv["rating_equivalent"] < equiv["curve_rating_min"]) | (equiv["rating_equivalent"] > equiv["curve_rating_max"])
    results.table("5_rating_equivalent", equiv, "Rating your play resembles: your mean cp_loss (or share of 300+ cp blunders) on a set of moves, read off the opponents' linear cp_loss-by-rating curve in the same games (pre-game rating proxy); NaN where the curve's slope CI includes zero (slope_clear False) or the target is thin; extrapolated = the estimate lies outside the opponents' rating range (curve_rating_min..max). Buffer targets for increment controls only.")
    targets_order = [f"<{clocks.LOW_CLOCK_SECONDS:g} s left", f"{clocks.COMFORT_SECONDS:g}+ s left", "all moves"]
    fig, axes = charts.group_grid(panels, sharey=True, title="What rating does your play look like at each clock level? (centipawn loss)", panel=(2.6, 2.4))
    for g, ax in axes.items():
        part = equiv[(equiv["tc_group"] == g) & (equiv["currency"] == "cp_loss")].copy()
        ok = part[part["slope_clear"] & ~part["thin"]]
        rating = part["your_mean_rating"].iloc[0]
        if ok.empty:
            thin(ax, "rating curve not identified")
            continue
        ax.axhline(rating, color=charts.INK_MUTED, linewidth=1, linestyle="--", label="your mean rating")
        shown = part.set_index("your_moves").reindex(targets_order).reset_index()
        off = shown["extrapolated"].fillna(False).astype(bool).to_numpy()
        for col in ("rating_equivalent", "rating_lo", "rating_hi"):
            shown[col] = shown[col].clip(*RATING_PLOT_RANGE)
            shown.loc[off, col] = np.nan
        charts.estimate(ax, shown, "your_moves", y="rating_equivalent", lo="rating_lo", hi="rating_hi", order=targets_order, label="rating equivalent")
        raw = part.set_index("your_moves").reindex(targets_order)["rating_equivalent"].to_numpy(dtype=float)
        for k in np.flatnonzero(off):
            span = RATING_PLOT_RANGE[1] - RATING_PLOT_RANGE[0]
            low = raw[k] < RATING_PLOT_RANGE[0] + span / 2
            edge = RATING_PLOT_RANGE[0] + 0.04 * span if low else RATING_PLOT_RANGE[1] - 0.04 * span  # just inside the frame
            ax.plot([k], [edge], marker="v" if low else "^", markersize=8, markerfacecolor="none", markeredgecolor=charts.PLAYER, markeredgewidth=1.5, linestyle="none", label="outside the opponents' rating range")
        ax.set_ylim(*RATING_PLOT_RANGE)
        rotate(ax)
    axes[panels[0]].set_ylabel("rating")
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Your mean centipawn loss on those moves, placed on the opponents' cp-loss-by-rating line in the same games. 95% CI, resampling games; whiskers cut at 0 and 2000. Hollow triangle: the estimate lies outside the opponents' rating range (an extrapolation, not drawn). Missing panel: the line has no clear slope.")
    fig.tight_layout()
    results.figure(fig, "5_rating_equivalent")
    print(f"5 done at {time.time() - t0:.0f} s")

    # 6. what low-clock moves cost per game ----------------------------------------------------
    rows, share_rows = [], []
    for measure, flag_col in (("seconds left", "low_clock"), ("buffer beyond increment", "low_buffer")):
        frame = moves.assign(ep_low=np.where(moves[flag_col], moves["ep_loss"], 0.0))
        per_game = frame.groupby(["tc_group", "game_uuid", "is_player_move"], observed=True)["ep_low"].sum().reset_index()
        for g, part in clocks.group_frames(per_game, groups):
            if not buffer_differs(measure, g, increment):
                continue
            t = compare_sides(part, "ep_low", n_boot=N_BOOT_COST)
            rows.append(tidy_compare(t, g, measure=measure, line=f"<{clocks.LOW_CLOCK_SECONDS:g} s"))
        for g, part in clocks.group_frames(frame, groups):
            if not buffer_differs(measure, g, increment):
                continue
            for side, sub in (("player", part[part["is_player_move"]]), ("opponent", part[~part["is_player_move"]])):
                low = sub[flag_col].astype(float)
                blunder = (sub["ep_loss"] >= BLUNDER_EP).astype(float)
                s_ep = ratio_bootstrap(sub["ep_loss"] * low, sub["ep_loss"], sub["game_uuid"], n_boot=N_BOOT_COST, seed=SEED)
                s_bl = ratio_bootstrap(blunder * low, blunder, sub["game_uuid"], n_boot=N_BOOT_COST, seed=SEED)
                share_rows.append(
                    {
                        "tc_group": g, "measure": measure, "side": side,
                        "share_of_ep_loss": s_ep[0], "share_of_ep_loss_lo": s_ep[1], "share_of_ep_loss_hi": s_ep[2],
                        "share_of_blunders": s_bl[0], "share_of_blunders_lo": s_bl[1], "share_of_blunders_hi": s_bl[2],
                        "ep_loss_per_game": sub.groupby("game_uuid")["ep_loss"].sum().mean(),
                        "games": int(sub["game_uuid"].nunique()), "moves": len(sub), "low_moves": int(low.sum()), "blunders": int(blunder.sum()),
                    }  # fmt: skip
                )
    cost = pd.concat(rows, ignore_index=True).drop(columns="n")
    cost = by_group(cost, groups)
    for col in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"):
        cost[f"{col}_per_100_games"] = 100 * cost[col]
    # Net to you = what opponents lose minus what you lose; Elo-equivalent around an even score.
    cost["net_to_you"], cost["net_to_you_lo"], cost["net_to_you_hi"] = -cost["diff"], -cost["diff_hi"], -cost["diff_lo"]
    cost["expected_score"] = EVEN_SCORE
    for col in ("net_to_you", "net_to_you_lo", "net_to_you_hi"):
        cost[f"{col}_elo"] = excess_to_elo(cost["expected_score"], cost[col])
    cost["thin"] = cost["games"] < MIN_GROUP_GAMES
    results.table("6_low_clock_cost", cost, "Expected points lost per game on moves made under 10 s (seconds left, or buffer for increment controls; zero in games that never got that low), you vs opponents; net_to_you = opponents minus you, with its Elo-equivalent around an even score; CIs by game.")
    shares = pd.DataFrame(share_rows)
    shares["thin"] = shares["games"] < MIN_GROUP_GAMES
    results.table("6b_low_clock_share_of_losses", shares, f"Share of each side's total expected points lost, and of its blunders (ep_loss >= {BLUNDER_EP}), that came on moves under 10 s; CIs by game.")
    fig, axes = charts.group_grid(groups, sharey="row", title="Expected points lost per game on moves under 10 s")
    for g, ax in axes.items():
        part = cost[cost["tc_group"] == g].assign(level=lambda f: np.where(f["measure"] == "seconds left", "<10 s left", "<10 s buffer"))
        charts.side_bars(ax, part, "level", order=list(dict.fromkeys(part["level"])))
    charts.legend(fig, bbox_to_anchor=(1.0, 1.02))
    charts.footnote(fig, "Sum over a game of the expected points lost on moves made with under 10 s before the move (0 if never that low). Buffer = seconds left minus one increment (shown for increment games). Rows share a scale within a time class. 95% CI, resampling games.")
    fig.tight_layout()
    results.figure(fig, "6_low_clock_cost")

    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


def _percent():
    from matplotlib.ticker import PercentFormatter

    return PercentFormatter(xmax=1, decimals=0)


if __name__ == "__main__":
    main()
