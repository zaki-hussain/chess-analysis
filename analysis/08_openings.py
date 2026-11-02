"""Q8: opening repertoire.

What do you play, which openings score above or below the rating
expectation, are you still playing the ones that work, who leaves the known
lines first and what that costs, whether deeper book helps, how your opening
moves compare with your opponents', and which repeated opening moves cost the
most.

Inputs: `load_data()` (standard live games in the reported controls,
`data.tc_groups`), `data/<user>/games_decomposed.parquet` (Q1: per-game
expected points lost in the opening phase by each side, the model's
expectation at move 1), `opening_name` / `book_ply` on the games and
`in_book` / `phase` / `ep_loss` on the moves.

What is split how (openings are a repertoire, not a time-control habit):
- repertoire, family scores, recency and the costliest repeated moves are
  pooled over every reported control ("all games"; family x colour x control
  is almost always thin), with a per-time-class table ("all bullet", ...) as
  a secondary view and the line-start shares per control;
- who leaves the book, book depth and opening move quality are per control
  (plus the pool), because they are about how you play, which the clock
  changes.

Expected score is the Elo formula on your pre-game rating and the opponent's
reconstructed pre-game rating (`opponent_rating_pre`); every score above
expectation carries its Elo-equivalent. CIs are bootstrapped by game.

Writes CSVs to presentation/data/08_openings/ and SVGs to
presentation/figures/08_openings/ (no prose).

Run: .venv/bin/python analysis/08_openings.py   (about a minute)
"""

from __future__ import annotations

import time

import chess
import numpy as np
import pandas as pd

from chess_analysis import charts
from chess_analysis.toolkit import (
    Results,
    add_elo,
    cluster_bootstrap_mean,
    cluster_ols,
    compare_sides,
    elo_expected_score,
    excess_to_elo,
    load_data,
    opponent_rating_pre,
    repo_root,
)

SLUG = "08_openings"
ALL = "all games"  # pooled over every reported control (the thin controls are excluded everywhere)
N_BOOT = 1000
SEED = 8
COLOURS = ["white", "black"]

# A family, line or cell with fewer games than this has a score CI of about +-0.18: computed, flagged thin,
# left out of the charts (the same floor as Q2, Q5 and Q6).
MIN_CELL_GAMES = 30
# chess.com's rating floor is 100; a rating below it is a placeholder, so no Elo expectation is formed from it.
MIN_VALID_RATING = 100
# "Last 12 months": the year up to the last game in the data, the window the question asks about.
RECENT_DAYS = 365
# Book depth in full moves (two plies): the opening list names lines move by move, and the median game leaves
# it after 4 plies; 5+ moves (9+ plies, rounded up) holds the deepest ~9% of games.
BOOK_MOVE_EDGES = [0, 1, 2, 3, 4, np.inf]
BOOK_MOVE_LABELS = ["1", "2", "3", "4", "5+"]
# Opening-phase move quality by ply: blocks of 10 plies (5 moves each side) over the first 30, where the
# material-heuristic opening phase almost always ends.
PLY_EDGES = [1, 11, 21, 31]
PLY_LABELS = ["1-10", "11-20", "21-30"]
PER_100 = 100  # expected points per 100 moves: the per-move numbers sit in the third decimal
# A repeated move: the same move in the same position at least this often (fewer is an anecdote, not a habit)...
RECUR_MIN_TIMES = 5
# ...losing at least this much on average: about 1.7x the average move (0.03), i.e. a clear inaccuracy each time.
RECUR_MIN_EP = 0.05
# ...and at least this many centipawns on average: the inaccuracy floor of the move labels, so the loss is about the
# move itself and not only the clock time it took (expected points lost include time spent).
RECUR_MIN_CP = 50
RECUR_TOP = 15  # rows in the costliest-moves tables and chart: about one screen
FIRST_PLIES = 4  # move order shown for each family (labels follow positions, so transpositions hide here)
CHANCE = 0.05  # share of 95% CIs that exclude zero when nothing is going on
BELOW = charts.SERIES[7]  # red: an estimate clearly below zero (blue = clearly above, grey = unclear)
STATUS_COLOUR = {"above": charts.PLAYER, "below": BELOW, "unclear": charts.OPPONENT}
LABEL_X_GAP, LABEL_Y_GAP = (9, 0.022)  # recency chart: labels closer than this (games, points) are nudged apart
LABEL_X_PAD = 0.03  # and start this share of the x range right of their dot, clear of the largest bubbles
LABEL_TOP_RECENT = 4  # recency chart: the most played recent families labelled besides every clear one
X_HEADROOM = 1.3  # recency chart: x range beyond the rightmost dot, room for its label
BAR_HEADROOM = 1.15  # costly-moves chart: x range beyond the longest bar, room for its "xN" label
DARK_CELL = 0.55  # heatmap: shares above this sit on a dark blue cell, so their text is white
WHITE_BLACK = {"white": charts.SERIES[0], "black": charts.SERIES[1]}  # series colours when both lines are "you"

GAME_STATS = ["my_ep_lost_opening", "opp_ep_lost_opening", "open_diff", "book_gain", "cp_after_book"]


# --- helpers ---------------------------------------------------------------------------------------


def mean_ci(frame: pd.DataFrame, column: str) -> tuple[float, float, float]:
    """Mean of a per-game column, CI from resampling games (one row per game)."""
    return cluster_bootstrap_mean(frame[column].astype("float64"), frame["uuid"], n_boot=N_BOOT, seed=SEED)


def independent_diff(a: pd.Series, b: pd.Series) -> tuple[float, float, float]:
    """mean(a) - mean(b) for two disjoint sets of games (independent bootstraps of each)."""
    a = a.astype("float64").dropna().to_numpy()
    b = b.astype("float64").dropna().to_numpy()
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan"), float("nan")
    rng = np.random.default_rng(SEED)
    ba = a[rng.integers(0, len(a), size=(N_BOOT, len(a)))].mean(axis=1)
    bb = b[rng.integers(0, len(b), size=(N_BOOT, len(b)))].mean(axis=1)
    lo, hi = np.quantile(ba - bb, [0.025, 0.975])
    return float(a.mean() - b.mean()), float(lo), float(hi)


def vs_colour(mine: pd.DataFrame, key: str) -> dict:
    """Each level of `key` minus the whole colour: score above expectation, CI from resampling the colour's games.

    The Elo formula has no colour term, so White sits above it and Black below it for everyone; a family is
    only special if it differs from your own average with that colour. One bootstrap of the colour's games
    serves every family at once (a resample-by-game count matrix times the family indicators).
    """
    valid = mine[mine["expected"].notna()]
    y = valid["excess"].to_numpy(dtype="float64")
    n = len(y)
    if n < 2:
        return {}
    levels = sorted(valid[key].unique())
    indicator = (valid[key].to_numpy()[:, None] == np.array(levels, dtype=object)[None, :]).astype("float64")
    rng = np.random.default_rng(SEED)
    idx = rng.integers(0, n, size=(N_BOOT, n))
    counts = np.bincount((idx + np.arange(N_BOOT)[:, None] * n).ravel(), minlength=N_BOOT * n).reshape(N_BOOT, n).astype("float64")
    games_in = counts @ indicator
    with np.errstate(invalid="ignore", divide="ignore"):
        boot = (counts @ (indicator * y[:, None])) / games_in - (counts @ y)[:, None] / n
    point = (indicator * y[:, None]).sum(axis=0) / indicator.sum(axis=0) - y.mean()
    lo, hi = np.nanquantile(boot, [0.025, 0.975], axis=0)
    return {name: (float(point[k]), float(lo[k]), float(hi[k]), float(y.mean())) for k, name in enumerate(levels)}


def score_stats(part: pd.DataFrame) -> dict:
    """Score, Elo expectation and score above expectation (games with a usable expectation), with CIs."""
    valid = part[part["expected"].notna()]
    s, s_lo, s_hi = mean_ci(valid, "my_score")
    e, e_lo, e_hi = mean_ci(valid, "excess")
    return {
        "score": s,
        "score_lo": s_lo,
        "score_hi": s_hi,
        "expected": float(valid["expected"].mean()) if len(valid) else np.nan,
        "excess": e,
        "excess_lo": e_lo,
        "excess_hi": e_hi,
        "games": len(part),
        "games_with_expectation": len(valid),
    }


def opening_stats(part: pd.DataFrame) -> dict:
    """Per-game opening-phase expected points lost by each side, their difference, and the book gain."""
    out = {}
    for column in GAME_STATS:
        est, lo, hi = mean_ci(part, column)
        out.update({column: est, f"{column}_lo": lo, f"{column}_hi": hi})
    return out


def with_elo(frame: pd.DataFrame) -> pd.DataFrame:
    return add_elo(frame, "expected", ["excess", "excess_lo", "excess_hi"])


def years(part: pd.DataFrame) -> dict:
    return {"first_year": int(part["year"].min()), "last_year": int(part["year"].max())}


def scopes(games: pd.DataFrame, groups: list[str]) -> list[tuple[str, pd.DataFrame]]:
    """('all games', every game), then one entry per control."""
    return [(ALL, games), *[(g, games[games["tc_group"] == g]) for g in groups]]


def class_scopes(games: pd.DataFrame, classes: list[str]) -> list[tuple[str, pd.DataFrame]]:
    return [(f"all {c}", games[games["tc_class"] == c]) for c in classes]


def san_of(fen: str, uci: str) -> str:
    try:
        return chess.Board(fen).san(chess.Move.from_uci(uci))
    except (ValueError, AssertionError):
        return uci or ""


def move_label(fen: str, san: str) -> str:
    """'7. Ng5' or '7... Nf6' from the position the mover faced."""
    parts = fen.split()
    return f"{parts[5]}. {san}" if parts[1] == "w" else f"{parts[5]}... {san}"


def thin_panel(ax, text: str = "too few games") -> None:
    """charts.thin_note without clearing ticks (on shared axes that would clear every panel's ticks)."""
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=charts.INK_MUTED, fontsize=8.5)
    ax.tick_params(labelleft=False, labelbottom=False, length=0)
    ax.grid(False)


def short_family(name: str) -> str:
    return name.replace(" Defense", " Def.").replace(" Opening", " Op.").replace("Queen's Gambit", "QG")


# --- data -------------------------------------------------------------------------------------------


def prepare(data) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    games = data.games[data.games["tc_group"].astype(str).isin(data.tc_groups)].copy()
    games["tc_group"] = games["tc_group"].astype(str)
    games["tc_class"] = games["tc_group"].map(data.tc_class)
    decomposed = pd.read_parquet(repo_root() / "data" / data.username / "games_decomposed.parquet")
    games = games.merge(decomposed, on="uuid", how="left")
    info = {"games_in_controls": len(games), "no_moves": int((games["n_plies"].fillna(0) == 0).sum())}
    games = games[games["n_plies"].fillna(0) > 0].copy()

    games["family"] = games["opening_name"].str.split(":").str[0].str.strip()
    games["colour"] = games["my_color"]
    games["opp_rating_pre"] = opponent_rating_pre(games)
    mine = games["my_rating_before"].astype("float64")
    valid = (mine >= MIN_VALID_RATING) & (games["opp_rating_pre"] >= MIN_VALID_RATING)
    games["expected"] = np.where(valid, elo_expected_score(mine.fillna(0), games["opp_rating_pre"].fillna(0)), np.nan)
    games["excess"] = games["my_score"] - games["expected"]
    info["no_expectation"] = int((~valid).sum())
    games["open_diff"] = games["opp_ep_lost_opening"] - games["my_ep_lost_opening"]

    moves = data.moves[data.moves["game_uuid"].isin(games["uuid"])].copy()
    # The position after the last book move, from your side.
    last_book = moves[moves["in_book"]].sort_values(["game_uuid", "ply"]).groupby("game_uuid").tail(1)
    after = pd.DataFrame(
        {
            "uuid": last_book["game_uuid"].to_numpy(),
            "ep_after_book": np.where(last_book["is_player_move"], last_book["ep_after"], 1 - last_book["ep_after"]),
            "cp_after_book": last_book["cp_after"].to_numpy() * np.where(last_book["is_player_move"], 1.0, -1.0),
        }
    )
    games = games.merge(after, on="uuid", how="left")
    games["book_gain"] = games["ep_after_book"] - games["my_ep_start"]

    # The first move outside the named lines: who played it and what it cost.
    deviation = moves[moves["ply"] == moves["book_ply"] + 1]
    dev = pd.DataFrame(
        {
            "uuid": deviation["game_uuid"].to_numpy(),
            "deviator": np.where(deviation["is_player_move"], "you", "opponent"),
            "deviation_ep_loss": deviation["ep_loss"].to_numpy(),
            "deviation_cp_loss": deviation["cp_loss"].to_numpy(),
        }
    )
    games = games.merge(dev, on="uuid", how="left")
    games["you_left_first"] = np.where(games["deviator"].isna(), np.nan, (games["deviator"] == "you").astype(float))
    games["book_moves"] = np.ceil(games["book_ply"].astype("float64") / 2)
    games["book_depth"] = pd.cut(games["book_moves"], bins=BOOK_MOVE_EDGES, labels=BOOK_MOVE_LABELS, right=True).astype(str)

    first = moves[moves["ply"] <= FIRST_PLIES].sort_values(["game_uuid", "ply"])
    plies = first.pivot(index="game_uuid", columns="ply", values="san")
    games["ply1"] = games["uuid"].map(plies[1])
    games["ply2"] = games["uuid"].map(plies[2]) if 2 in plies else np.nan
    games["line_start"] = np.where(
        games["colour"] == "white", "1." + games["ply1"].fillna("?"), "1." + games["ply1"].fillna("?") + " " + games["ply2"].fillna("(no reply)")
    )
    games["first_moves"] = games["uuid"].map(first.groupby("game_uuid")["san"].agg(" ".join))

    cutoff = games["start_datetime"].max() - pd.Timedelta(days=RECENT_DAYS)
    games["recent"] = games["start_datetime"] >= cutoff
    info["cutoff"] = cutoff.date().isoformat()

    moves = moves.merge(games[["uuid", "family", "colour", "recent", "year", "tc_class"]].rename(columns={"uuid": "game_uuid"}), on="game_uuid", how="inner")
    return (games.sort_values(["start_datetime", "uuid"]).reset_index(drop=True), moves, info)


# --- 8.1 repertoire -----------------------------------------------------------------------------------


def line_starts(games: pd.DataFrame, groups: list[str]) -> pd.DataFrame:
    """Share of each colour's games per line start, pooled and per control; rare starts lumped as 'other'."""
    rows = []
    for colour in COLOURS:
        mine = games[games["colour"] == colour]
        counts = mine["line_start"].value_counts()
        main = sorted(counts[counts >= MIN_CELL_GAMES].index, key=lambda s: (-counts[s], s))
        for scope, part in scopes(mine, groups):
            label = part["line_start"].where(part["line_start"].isin(main), "other")
            for start in [*main, "other"]:
                sel = part[label == start]
                rows.append(
                    {
                        "tc_group": scope,
                        "colour": colour,
                        "line_start": start,
                        "games": len(sel),
                        "colour_games": len(part),
                        "share": len(sel) / len(part) if len(part) else np.nan,
                        "recent_games": int(sel["recent"].sum()),
                        "thin": len(part) < MIN_CELL_GAMES,
                    }
                )
    return pd.DataFrame(rows)


def families(games: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for colour in COLOURS:
        mine = games[games["colour"] == colour]
        for family, part in mine.groupby("family"):
            rows.append(
                {
                    "tc_group": ALL,
                    "colour": colour,
                    "family": family,
                    "games": len(part),
                    "share": len(part) / len(mine),
                    "recent_games": int(part["recent"].sum()),
                    **years(part),
                    "thin": len(part) < MIN_CELL_GAMES,
                }
            )
    out = pd.DataFrame(rows)
    out["_c"] = out["colour"].map({c: i for i, c in enumerate(COLOURS)})
    return out.sort_values(["_c", "games", "family"], ascending=[True, False, True]).drop(columns="_c")


def move_orders(games: pd.DataFrame) -> pd.DataFrame:
    """The most common first plies of each family: names follow positions, so a label can hide its move order."""
    rows = []
    for colour in COLOURS:
        mine = games[games["colour"] == colour]
        for family, part in mine.groupby("family"):
            if len(part) < MIN_CELL_GAMES:
                continue
            counts = part["first_moves"].value_counts()
            top = min(counts.index, key=lambda s: (-counts[s], s))
            rows.append(
                {
                    "tc_group": ALL,
                    "colour": colour,
                    "family": family,
                    "most_common_start": top,
                    "games_with_it": int(counts[top]),
                    "games": len(part),
                    "share": counts[top] / len(part),
                    "thin": False,  # only families with at least MIN_CELL_GAMES games are listed
                }
            )
    return pd.DataFrame(rows)


def chart_line_starts(table: pd.DataFrame, groups: list[str], tc_class: dict[str, str]):
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize

    cols = [ALL, *groups]
    heights = [table[table["colour"] == c]["line_start"].nunique() for c in COLOURS]
    fig, axes = plt.subplots(2, 1, figsize=(9.2, 0.36 * sum(heights) + 2.2), gridspec_kw={"height_ratios": heights})
    norm = Normalize(0, 1)
    cmap = plt.get_cmap(charts.SEQUENTIAL)
    for ax, colour in zip(axes, COLOURS):
        part = table[table["colour"] == colour]
        rows = list(dict.fromkeys(part["line_start"]))
        grid = part.pivot(index="line_start", columns="tc_group", values="share").reindex(index=rows, columns=cols)
        totals = part.drop_duplicates("tc_group").set_index("tc_group")["colour_games"].reindex(cols)
        ax.imshow(grid.to_numpy(dtype=float), cmap=cmap, norm=norm, aspect="auto")
        for i in range(len(rows)):
            for j in range(len(cols)):
                v = grid.iloc[i, j]
                if pd.notna(v):
                    ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=7.5, color="white" if v > DARK_CELL else charts.INK)
        ax.set_yticks(range(len(rows)), rows)
        ax.set_xticks(
            range(len(cols)),
            [f"{c.removeprefix(tc_class.get(c, '') + ' ')}\n({int(n)})" for c, n in zip(cols, totals)],
            fontsize=7.5,
        )
        ax.grid(False)
        ax.set_title(f"You as {colour}: share of games by how the game started")
        for spine in ax.spines.values():
            spine.set_visible(False)
    fig.suptitle("What do you play as White and as Black?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(
        fig,
        "Columns: all games, then each control (games with that colour in brackets). As Black the start is the opponent's first move and your reply. "
        f"Starts with fewer than {MIN_CELL_GAMES} games overall are lumped into 'other'.",
    )
    return fig


# --- 8.2 family scores ----------------------------------------------------------------------------------


def family_scores(games: pd.DataFrame, scope_list: list[tuple[str, pd.DataFrame]], key: str = "family") -> pd.DataFrame:
    rows = []
    for scope, part_scope in scope_list:
        for colour in COLOURS:
            mine = part_scope[part_scope["colour"] == colour]
            versus = vs_colour(mine, key)
            for name, part in mine.groupby(key):
                v = versus.get(name, (np.nan,) * 4)
                rows.append(
                    {
                        "tc_group": scope,
                        "colour": colour,
                        key: name,
                        **score_stats(part),
                        "colour_excess": v[3],
                        "excess_vs_colour": v[0],
                        "excess_vs_colour_lo": v[1],
                        "excess_vs_colour_hi": v[2],
                        **opening_stats(part),
                        "recent_games": int(part["recent"].sum()),
                        **years(part),
                        "thin": len(part) < MIN_CELL_GAMES,
                    }
                )
    out = with_elo(pd.DataFrame(rows))
    # Elo-equivalent of the gap to your colour average: relative to the score you would make at that average.
    out["expected_at_colour_average"] = out["expected"] + out["colour_excess"]
    out = add_elo(out, "expected_at_colour_average", ["excess_vs_colour", "excess_vs_colour_lo", "excess_vs_colour_hi"])
    out["vs_colour"] = np.select([out["excess_vs_colour_lo"] > 0, out["excess_vs_colour_hi"] < 0], ["above", "below"], "unclear")
    out["_c"] = out["colour"].map({c: i for i, c in enumerate(COLOURS)})
    out["_s"] = out["tc_group"].map({s: i for i, (s, _) in enumerate(scope_list)})
    return out.sort_values(["_s", "_c", "games", key], ascending=[True, True, False, True]).drop(columns=["_c", "_s"]).reset_index(drop=True)


def colour_scores(games: pd.DataFrame, groups: list[str], classes: list[str]) -> pd.DataFrame:
    rows = []
    for scope, part_scope in [*scopes(games, groups)[:1], *class_scopes(games, classes), *scopes(games, groups)[1:]]:
        for colour in COLOURS:
            part = part_scope[part_scope["colour"] == colour]
            rows.append({"tc_group": scope, "colour": colour, **score_stats(part), "thin": len(part) < MIN_CELL_GAMES})
    return with_elo(pd.DataFrame(rows))


def chance_check(table: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    for (scope, colour), part in table.groupby(["tc_group", "colour"], sort=False):
        tested = part[~part["thin"]]
        rows.append(
            {
                "tc_group": scope,
                "colour": colour,
                f"{key}_tested": len(tested),
                "clear_above_average": int((tested["excess_vs_colour_lo"] > 0).sum()),
                "clear_below_average": int((tested["excess_vs_colour_hi"] < 0).sum()),
                "clear_above_zero": int((tested["excess_lo"] > 0).sum()),
                "clear_below_zero": int((tested["excess_hi"] < 0).sum()),
                "expected_by_chance": CHANCE * len(tested),
                "games_covered": int(tested["games"].sum()),
                "thin": len(tested) == 0,  # the counts are over non-thin families only; thin = nothing to test
            }
        )
    return pd.DataFrame(rows)


def chart_family_scores(table: pd.DataFrame, colour_table: pd.DataFrame):
    import matplotlib.pyplot as plt

    shown = {c: table[(table["colour"] == c) & ~table["thin"]].sort_values(["excess_vs_colour", "family"], ascending=[False, True]) for c in COLOURS}
    heights = [max(len(shown[c]), 1) for c in COLOURS]
    fig, axes = plt.subplots(1, 2, figsize=(11, 0.3 * max(heights) + 1.8), sharex=True)
    for ax, colour in zip(axes, COLOURS):
        part = shown[colour].assign(label=lambda f: f["family"].map(short_family) + " (" + f["games"].astype(str) + ")")
        colours = [STATUS_COLOUR[v] for v in part["vs_colour"]]
        pos = np.arange(len(part))
        est, lo, hi = part["excess_vs_colour"], part["excess_vs_colour_lo"], part["excess_vs_colour_hi"]
        ax.errorbar(est, pos, xerr=[est - lo, hi - est], fmt="none", ecolor=colours, elinewidth=1.4)
        ax.scatter(est, pos, color=colours, s=28, zorder=3)
        ax.set_yticks(pos, part["label"])
        ax.invert_yaxis()
        charts.zero_line(ax, horizontal=False)
        base = colour_table[(colour_table["tc_group"] == ALL) & (colour_table["colour"] == colour)].iloc[0]["excess"]
        ax.set_title(f"You as {colour} (0 = your {colour} average, {base:+.3f} vs Elo)")
        ax.set_xlabel(f"score vs your {colour} average (points per game)")
        ax.grid(axis="y", visible=False)
    fig.suptitle("Which openings score above or below expectation?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(
        fig,
        f"All controls pooled; opening families with at least {MIN_CELL_GAMES} games (games in brackets). Each family's score above the Elo expectation minus that of all your games "
        "with the colour (the Elo formula has no colour term). Blue / red: 95% CI clearly above / below your average; grey: no clear difference. CIs bootstrapped by game.",
    )
    return fig


# --- 8.3 recency --------------------------------------------------------------------------------------------


def recency(games: pd.DataFrame, family_table: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    pooled = family_table[family_table["tc_group"] == ALL].set_index(["colour", "family"])
    for colour in COLOURS:
        mine = games[games["colour"] == colour]
        n_recent, n_before = int(mine["recent"].sum()), int((~mine["recent"]).sum())
        for family, part in mine.groupby("family"):
            ref = pooled.loc[(colour, family)]
            recent = part[part["recent"]]
            r = score_stats(recent) if len(recent) else {}
            rows.append(
                {
                    "tc_group": ALL,
                    "colour": colour,
                    "family": family,
                    "games": len(part),
                    "games_recent": len(recent),
                    "games_before": int(len(part) - len(recent)),
                    "share_recent": len(recent) / n_recent if n_recent else np.nan,
                    "share_before": (len(part) - len(recent)) / n_before if n_before else np.nan,
                    "excess": ref["excess"],
                    "excess_lo": ref["excess_lo"],
                    "excess_hi": ref["excess_hi"],
                    "expected": ref["expected"],
                    "excess_vs_colour": ref["excess_vs_colour"],
                    "excess_vs_colour_lo": ref["excess_vs_colour_lo"],
                    "excess_vs_colour_hi": ref["excess_vs_colour_hi"],
                    "vs_colour": ref["vs_colour"],
                    "excess_recent": r.get("excess", np.nan),
                    "excess_recent_lo": r.get("excess_lo", np.nan),
                    "excess_recent_hi": r.get("excess_hi", np.nan),
                    "expected_recent": r.get("expected", np.nan),
                    "last_year": int(part["year"].max()),
                    "thin": len(part) < MIN_CELL_GAMES,
                    "thin_recent": len(recent) < MIN_CELL_GAMES,
                }
            )
    out = pd.DataFrame(rows)
    out["share_change"] = out["share_recent"] - out["share_before"]
    out = add_elo(out, "expected", ["excess", "excess_lo", "excess_hi"])
    out = add_elo(out, "expected_recent", ["excess_recent", "excess_recent_lo", "excess_recent_hi"])
    out["_c"] = out["colour"].map({c: i for i, c in enumerate(COLOURS)})
    out = out.sort_values(["_c", "games", "family"], ascending=[True, False, True]).drop(columns="_c").reset_index(drop=True)

    summary = []
    for colour in COLOURS:
        part = out[out["colour"] == colour]
        tested = part[~part["thin"]]
        status = tested["vs_colour"].to_numpy()  # clearly above / below your average with that colour, all years
        for period, count in (("last 12 months", "games_recent"), ("earlier", "games_before")):
            total = part[count].sum()
            weights = tested[count] / total if total else np.nan
            summary.append(
                {
                    "tc_group": ALL,
                    "colour": colour,
                    "period": period,
                    "games": int(total),
                    "share_clear_above": float(tested.loc[status == "above", count].sum() / total),
                    "share_clear_below": float(tested.loc[status == "below", count].sum() / total),
                    "share_unclear": float(tested.loc[status == "unclear", count].sum() / total),
                    "share_thin_families": float(part.loc[part["thin"], count].sum() / total),
                    # the repertoire's expected edge: each tested family's all-time excess weighted by how often you played it in the period
                    "weighted_excess": float((weights * tested["excess"]).sum() / weights.sum()),
                }
            )
    return out, pd.DataFrame(summary)


def chart_recency(table: pd.DataFrame):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), sharey=True)
    for ax, colour in zip(axes, COLOURS):
        part = table[(table["colour"] == colour) & ~table["thin"]]
        colours = [STATUS_COLOUR[v] for v in part["vs_colour"]]
        est, lo, hi = part["excess_vs_colour"], part["excess_vs_colour_lo"], part["excess_vs_colour_hi"]
        ax.errorbar(part["games_recent"], est, yerr=[est - lo, hi - est], fmt="none", ecolor=colours, elinewidth=1, alpha=0.35, zorder=1)
        ax.scatter(part["games_recent"], est, s=np.sqrt(part["games"]) * 5, c=colours, alpha=0.85, zorder=2)
        charts.zero_line(ax)
        x_max = part["games_recent"].max() * X_HEADROOM
        pad = LABEL_X_PAD * x_max
        top = set(part.nlargest(LABEL_TOP_RECENT, "games_recent")["family"])
        labelled = part[part["family"].isin(top) | (part["vs_colour"] != "unclear")].sort_values(["excess_vs_colour", "family"])
        placed: list[tuple[float, float]] = []
        for r in labelled.itertuples():
            y = r.excess_vs_colour
            while any(abs(r.games_recent - px) < LABEL_X_GAP and abs(y - py) < LABEL_Y_GAP for px, py in placed):
                y += LABEL_Y_GAP  # nudge up until clear of labels already placed near this point
            placed.append((r.games_recent, y))
            ax.annotate(
                short_family(r.family),
                (r.games_recent, r.excess_vs_colour),
                xytext=(r.games_recent + pad, y),
                textcoords="data",
                fontsize=7,
                color=charts.INK_SECONDARY,
                va="center",
            )
        ax.set_xlim(-2, x_max)
        ax.set_title(f"You as {colour}")
        ax.set_xlabel("games in the last 12 months")
    axes[0].set_ylabel("score vs your average with that colour,\nall years (points per game)")
    fig.suptitle("Are you still playing the openings that work?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(
        fig,
        f"Each dot is an opening family with at least {MIN_CELL_GAMES} games (area ~ all games). Blue / red: 95% CI of the all-years score clearly above / below "
        f"your average with that colour. Labelled: the {LABEL_TOP_RECENT} most played recently and every clear family.",
    )
    return fig


# --- 8.4 leaving the book -------------------------------------------------------------------------------------


def deviation(games: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rows, score_rows, diff_rows = [], [], []
    for scope, part in scopes(games, groups):
        part = part[part["deviator"].notna()]
        you, opp = part[part["deviator"] == "you"], part[part["deviator"] == "opponent"]
        share = mean_ci(part, "you_left_first")
        row = {"tc_group": scope, "share_you_left_first": share[0], "share_you_left_first_lo": share[1], "share_you_left_first_hi": share[2]}
        for unit in ("ep", "cp"):
            col = f"deviation_{unit}_loss"
            p, o, d = (mean_ci(you, col), mean_ci(opp, col), independent_diff(you[col], opp[col]))
            scale = PER_100 if unit == "ep" else 1
            row.update(
                {
                    f"{unit}_player": p[0],
                    f"{unit}_player_lo": p[1],
                    f"{unit}_player_hi": p[2],
                    f"{unit}_opponent": o[0],
                    f"{unit}_opponent_lo": o[1],
                    f"{unit}_opponent_hi": o[2],
                    f"{unit}_diff": d[0],
                    f"{unit}_diff_lo": d[1],
                    f"{unit}_diff_hi": d[2],
                }
            )
            if unit == "ep":
                row.update(
                    {
                        f"ep_per100_{k}": row[f"ep_{k}"] * scale
                        for k in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi")
                    }
                )
        row.update({"games": len(part), "games_you_left": len(you), "games_they_left": len(opp), "thin": min(len(you), len(opp)) < MIN_CELL_GAMES})
        rows.append(row)
        for who, sel in (("you", you), ("opponent", opp)):
            score_rows.append({"tc_group": scope, "left_first": who, **score_stats(sel), "thin": len(sel) < MIN_CELL_GAMES})
        y, o = you[you["expected"].notna()], opp[opp["expected"].notna()]
        d = independent_diff(y["excess"], o["excess"])
        base = float(o["excess"].mean() + o["expected"].mean()) if len(o) else np.nan  # their-deviation score: the reference for the Elo-equivalent
        diff_rows.append(
            {
                "tc_group": scope,
                "diff": d[0],
                "diff_lo": d[1],
                "diff_hi": d[2],
                "reference_score": base,
                "diff_elo": float(excess_to_elo(base, d[0])),
                "diff_lo_elo": float(excess_to_elo(base, d[1])),
                "diff_hi_elo": float(excess_to_elo(base, d[2])),
                "games_you_left": len(y),
                "games_they_left": len(o),
                "thin": min(len(y), len(o)) < MIN_CELL_GAMES,
            }
        )
    return (pd.DataFrame(rows), with_elo(pd.DataFrame(score_rows)), pd.DataFrame(diff_rows))


def chart_deviation(table: pd.DataFrame, order: list[str]):
    import matplotlib.pyplot as plt

    table = table[~table["thin"]]  # a thin control keeps its slot on the axis but gets no dot or bar
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True)
    charts.estimate(ax1, table, "tc_group", y="share_you_left_first", lo="share_you_left_first_lo", hi="share_you_left_first_hi", order=order)
    ax1.axhline(0.5, color=charts.INK_MUTED, linewidth=1, linestyle=":")
    ax1.set_ylabel("share of games")
    ax1.set_title("How often you played the first move outside the named lines (dotted: half)")
    ax1.grid(axis="x", visible=False)
    charts.side_bars(ax2, table, "tc_group", value="ep_per100_", order=order)
    ax2.set_ylabel("expected points lost\nper 100 such moves")
    ax2.set_title("What that first out-of-book move cost: yours vs your opponents'")
    charts.legend(ax2, loc="upper left")
    ax2.tick_params(axis="x", labelsize=7.5)
    fig.suptitle("Who leaves the known lines first, and does it cost?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(
        fig,
        f"The first move after the deepest named line (Lichess opening list). 95% CIs bootstrapped by game; the two sides' moves come from different games. "
        f"Controls where either side left first in fewer than {MIN_CELL_GAMES} games are not drawn.",
    )
    return fig


# --- 8.5 book depth ------------------------------------------------------------------------------------------------


def book_depth(games: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, slopes = [], []
    for scope, part_scope in scopes(games, groups):
        for label in BOOK_MOVE_LABELS:
            part = part_scope[part_scope["book_depth"] == label]
            gain, diff = mean_ci(part, "book_gain"), mean_ci(part, "open_diff")
            rows.append(
                {
                    "tc_group": scope,
                    "book_moves": label,
                    **score_stats(part),
                    "book_gain": gain[0],
                    "book_gain_lo": gain[1],
                    "book_gain_hi": gain[2],
                    "open_diff": diff[0],
                    "open_diff_lo": diff[1],
                    "open_diff_hi": diff[2],
                    "white_share": float((part["colour"] == "white").mean()) if len(part) else np.nan,
                    "share_you_left_first": float(part["you_left_first"].mean()) if len(part) else np.nan,
                    "thin": len(part) < MIN_CELL_GAMES,
                }
            )
        # Score above expectation per extra full move of book, raw and holding colour fixed (deep lines are mostly
        # your Black games, and you score less above expectation as Black). Each game is its own cluster.
        valid = part_scope[part_scope["expected"].notna()]
        depth = np.minimum(valid["book_moves"].to_numpy(dtype=float), BOOK_MOVE_EDGES[-2] + 1)  # cap at the open-ended bin
        y = valid["excess"].to_numpy(dtype=float)
        ids = valid["uuid"].to_numpy()
        row = {"tc_group": scope}
        for name, X in (
            ("slope", np.column_stack([np.ones(len(y)), depth])),
            ("slope_colour_adjusted", np.column_stack([np.ones(len(y)), depth, (valid["colour"] == "white").to_numpy(dtype=float)])),
        ):
            beta, draws = cluster_ols(X, y, ids, n_boot=N_BOOT, seed=SEED)
            lo, hi = np.nanquantile(draws[:, 1], [0.025, 0.975])
            row.update({name: beta[1], f"{name}_lo": lo, f"{name}_hi": hi})
        row.update({"games": len(valid), "thin": len(valid) < MIN_CELL_GAMES * len(BOOK_MOVE_LABELS)})
        slopes.append(row)
    return with_elo(pd.DataFrame(rows)), pd.DataFrame(slopes)


def chart_book_depth(table: pd.DataFrame, groups: list[str]):
    fig, axes = charts.group_grid([*groups, ALL], sharey=True, title="Does going deeper into known lines help?")
    for group, ax in axes.items():
        part = table[(table["tc_group"] == group) & ~table["thin"]]
        charts.zero_line(ax)
        if part.empty:
            thin_panel(ax)
            continue
        charts.estimate(ax, part, "book_moves", y="excess", lo="excess_lo", hi="excess_hi", order=BOOK_MOVE_LABELS, line=True)
        ax.grid(axis="x", visible=False)
    seen = set()
    for group, ax in axes.items():
        row = group.split(" ")[0]
        if row not in seen:
            seen.add(row)
            ax.set_ylabel("score above expectation")
    charts.footnote(
        fig,
        f"x: how many full moves the game stayed in a named opening line; y: score above the Elo expectation (points per game). "
        f"Depths with fewer than {MIN_CELL_GAMES} games are not drawn. 95% CIs by game.",
    )
    return fig


# --- 8.6 opening move quality -----------------------------------------------------------------------------------------


def opening_quality(moves: pd.DataFrame, groups: list[str]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    opening = moves[moves["phase"] == "opening"].copy()
    pooled = opening.assign(tc_group=ALL)
    both = pd.concat([pooled, opening.assign(tc_group=opening["tc_group"].astype(str))], ignore_index=True)
    order = {g: i for i, g in enumerate([ALL, *groups])}

    def tidy(frame: pd.DataFrame, per100: bool) -> pd.DataFrame:
        frame = (
            frame.assign(_g=frame["tc_group"].map(order), _c=frame["colour"].map({c: i for i, c in enumerate(COLOURS)}))
            .sort_values(["_g", "_c"])
            .drop(columns=["_g", "_c"])
        )
        if per100:
            for k in ("player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi"):
                frame[f"per100_{k}"] = frame[k] * PER_100
        frame = frame.rename(columns={"n": "moves"})
        frame["thin"] = frame["games"] < MIN_CELL_GAMES
        return frame.reset_index(drop=True)

    ep = tidy(compare_sides(both, "ep_loss", by=["tc_group", "colour"], n_boot=N_BOOT), per100=True)
    cp = tidy(compare_sides(both, "cp_loss", by=["tc_group", "colour"], n_boot=N_BOOT), per100=False)
    early_all = moves[moves["ply"] < PLY_EDGES[-1]].copy()
    early_all["plies"] = pd.cut(early_all["ply"], bins=PLY_EDGES, labels=PLY_LABELS, right=False).astype(str)
    early_all["tc_group"] = early_all["tc_group"].astype(str)
    early_all = pd.concat([early_all.assign(tc_group=ALL), early_all], ignore_index=True)
    by_ply = compare_sides(early_all, "ep_loss", by=["tc_group", "colour", "plies"], n_boot=N_BOOT)
    by_ply = (
        by_ply.assign(_g=by_ply["tc_group"].map(order), _c=by_ply["colour"].map({c: i for i, c in enumerate(COLOURS)}))
        .sort_values(["_g", "_c", "plies"])
        .drop(columns=["_g", "_c"])
    )
    for k in ("player", "opponent", "diff", "diff_lo", "diff_hi"):
        by_ply[f"per100_{k}"] = by_ply[k] * PER_100
    by_ply = by_ply.rename(columns={"n": "moves"})
    by_ply["thin"] = by_ply["games"] < MIN_CELL_GAMES
    return ep, cp, by_ply.reset_index(drop=True)


def chart_opening_quality(ep: pd.DataFrame, cp: pd.DataFrame, groups: list[str]):
    import matplotlib.pyplot as plt

    order = [*groups, ALL]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6.4), sharex=True)
    for ax, table, prefix, label in (
        (ax1, ep, "per100_", "expected points lost per 100\nopening moves, you minus them"),
        (ax2, cp, "", "centipawns lost per opening\nmove, you minus them"),
    ):
        for k, colour in enumerate(COLOURS):
            part = table[(table["colour"] == colour) & ~table["thin"]]
            charts.estimate(
                ax,
                part,
                "tc_group",
                y=f"{prefix}diff",
                lo=f"{prefix}diff_lo",
                hi=f"{prefix}diff_hi",
                order=order,
                colour=WHITE_BLACK[colour],
                label=f"you as {colour}",
                offset=(k - 0.5) * 0.25,
            )
        charts.zero_line(ax)
        ax.set_ylabel(label)
        ax.grid(axis="x", visible=False)
    ax1.set_title("Below zero: you lose less than your opponents in the same games")
    charts.legend(ax1, loc="upper left")
    ax2.tick_params(axis="x", labelsize=7.5)
    fig.suptitle("How does your opening play compare with your opponents'?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(fig, "Moves in the opening phase (material heuristic, roughly the first 10-12 moves). 95% CIs from a paired bootstrap of games.")
    return fig


# --- 8.7 costliest repeated moves -----------------------------------------------------------------------------------------


def costly_moves(moves: pd.DataFrame, games: pd.DataFrame, classes: list[str], player: bool) -> pd.DataFrame:
    """Repeated (position, move) pairs in the opening phase with the largest total expected points lost."""
    part = moves[(moves["is_player_move"] == player) & (moves["phase"] == "opening")]
    agg = (
        part.groupby(["fen_before", "san"])
        .agg(times=("ep_loss", "size"), total=("ep_loss", "sum"), mean_ep=("ep_loss", "mean"), mean_cp=("cp_loss", "mean"), engine_choice=("is_best_move", "mean"))
        .reset_index()
    )
    # A habit, a clear loss in expected points, a real move-quality loss in centipawns, and not the engine's own move.
    agg = agg[(agg["times"] >= RECUR_MIN_TIMES) & (agg["mean_ep"] >= RECUR_MIN_EP) & (agg["mean_cp"] >= RECUR_MIN_CP) & (agg["engine_choice"] == 0)]
    agg = agg.sort_values(["total", "fen_before", "san"], ascending=[False, True, True]).head(RECUR_TOP)
    rows = []
    for rank, r in enumerate(agg.itertuples(), start=1):
        occ = part[(part["fen_before"] == r.fen_before) & (part["san"] == r.san)]
        e, lo, hi = cluster_bootstrap_mean(occ["ep_loss"], occ["game_uuid"], n_boot=N_BOOT, seed=SEED)
        gs = games[games["uuid"].isin(occ["game_uuid"].unique())]
        best = occ["best_move"].mode()
        row = {
            "tc_group": ALL,
            "rank": rank,
            "your_colour": occ["colour"].mode().iloc[0],
            "family": occ["family"].mode().iloc[0],
            "move": move_label(r.fen_before, r.san),
            "engine_move": san_of(r.fen_before, best.iloc[0]) if len(best) else "",
            "times": int(r.times),
            "games": int(occ["game_uuid"].nunique()),
            "times_recent": int(occ["recent"].sum()),
            "first_year": int(occ["year"].min()),
            "last_year": int(occ["year"].max()),
            "total_ep_lost": float(r.total),
            "ep_loss": e,
            "ep_loss_lo": lo,
            "ep_loss_hi": hi,
            "cp_loss": float(occ["cp_loss"].mean()),
            "eval_before_cp": float(occ["cp_before"].mean()),
            "your_score": float(gs["my_score"].mean()),
            "expected": float(gs["expected"].mean()),
            "excess": float((gs["my_score"] - gs["expected"]).mean()),
            "thin": occ["game_uuid"].nunique() < MIN_CELL_GAMES,
        }
        for c in classes:
            row[f"times_{c}"] = int((occ["tc_class"] == c).sum())
        row["fen_before"] = r.fen_before
        rows.append(row)
    out = pd.DataFrame(rows)
    return add_elo(out, "expected", ["excess"]) if len(out) else out


def chart_costly(mine: pd.DataFrame, theirs: pd.DataFrame):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 0.33 * RECUR_TOP + 1.8))
    for ax, table, colour, title in (
        (axes[0], mine, charts.PLAYER, "Your costliest repeated opening moves"),
        (axes[1], theirs, charts.OPPONENT, "Your opponents' costliest repeated moves against you"),
    ):
        if table.empty:
            thin_panel(ax, "none")
            continue
        labels = [f"{r.move} ({short_family(r.family)}, you {r.your_colour[0].upper()}); engine {r.engine_move}" for r in table.itertuples()]
        pos = np.arange(len(table))
        ax.barh(pos, table["total_ep_lost"], color=colour, height=0.7)
        for p, r in zip(pos, table.itertuples()):
            ax.text(r.total_ep_lost, p, f" x{r.times}", va="center", fontsize=7, color=charts.INK_SECONDARY)
        ax.set_yticks(pos, labels, fontsize=7.5)
        ax.invert_yaxis()
        ax.grid(axis="y", visible=False)
        ax.set_xlabel("expected points lost, all occurrences")
        ax.set_title(title)
        ax.set_xlim(0, table["total_ep_lost"].max() * BAR_HEADROOM)
    fig.suptitle("What are your costliest repeated moves?", x=0.01, ha="left", fontweight="bold", fontsize=11)
    fig.tight_layout()
    charts.footnote(
        fig,
        f"Same move in the same position in the opening phase, played at least {RECUR_MIN_TIMES} times, losing at least {RECUR_MIN_EP} expected points and "
        f"{RECUR_MIN_CP} centipawns on average, never the engine's move; xN = times played. All controls pooled.",
    )
    return fig


# --- main ---------------------------------------------------------------------------------------------------------------


def main() -> None:
    t0 = time.time()
    data = load_data()
    charts.style()
    results = Results(SLUG)
    groups = data.tc_groups
    classes = list(dict.fromkeys(data.tc_class[g] for g in groups))
    games, moves, info = prepare(data)
    print(f"{len(games):,} games, {len(moves):,} moves; {info}")

    # 8.1 repertoire
    starts = line_starts(games, groups)
    results.table(
        "1_line_starts",
        starts,
        f"Share of your games with each colour by how the game started (as White your first move; as Black the opponent's first move and your reply), all games and per control. Starts with fewer than {MIN_CELL_GAMES} games overall are lumped as 'other'. recent_games: since {info['cutoff']}.",
    )
    results.figure(chart_line_starts(starts, groups, data.tc_class), "1_line_starts")
    results.table(
        "1b_families",
        families(games),
        f"Opening families (opening name before the colon) per colour, all controls pooled: games, share of the colour's games, games since {info['cutoff']}, first and last year. thin: under {MIN_CELL_GAMES} games.",
    )
    results.table(
        "1c_family_move_order",
        move_orders(games),
        f"Most common first {FIRST_PLIES} plies of each family with at least {MIN_CELL_GAMES} games: opening names follow positions, so a family can be reached by an unexpected move order.",
    )

    # 8.2 family scores
    fam = family_scores(games, scopes(games, groups)[:1])
    results.table(
        "2_family_score",
        fam,
        "Per opening family and colour, all controls pooled: score, Elo-expected score and score above expectation (with Elo-equivalents); "
        "excess_vs_colour = that minus the score above expectation of all your games with the colour (colour_excess), CI from one joint bootstrap of the colour's games, "
        "Elo relative to expected_at_colour_average, vs_colour = clearly above / below / unclear; "
        "opening-phase expected points lost per game by you and your opponents and the difference (theirs minus yours, positive = they lose more), "
        "book_gain (your expected points after the last book move minus the model's expectation at move 1) and the engine eval after book (cp, your side). "
        f"CIs bootstrapped by game; thin under {MIN_CELL_GAMES} games.",
    )
    colour_table = colour_scores(games, groups, classes)
    results.figure(chart_family_scores(fam, colour_table), "2_family_score")
    fam_class = family_scores(games, class_scopes(games, classes))
    results.table(
        "2b_family_score_by_class",
        fam_class,
        f"The same per family and colour within each time class (all controls of the class pooled). Most cells are thin (under {MIN_CELL_GAMES} games).",
    )
    results.table(
        "2c_colour_score",
        colour_table,
        "Score above the Elo expectation per colour: all games, per time class and per control (the baseline each family is compared with).",
    )
    lines = family_scores(games, scopes(games, groups)[:1], key="opening_name")
    lines["deficit_points"] = lines["games_with_expectation"] * -lines["excess"]
    results.table(
        "2d_named_lines",
        lines,
        "Per named opening line (deepest named line reached) and colour, all controls pooled: as 2_family_score, plus deficit_points = total points below the expectation over the line's games (negative = above).",
    )
    chance = pd.concat(
        [
            chance_check(fam, "families"),
            chance_check(fam_class, "families"),
            chance_check(lines, "lines").rename(columns={"lines_tested": "families_tested"}).assign(unit="named lines"),
        ],
        ignore_index=True,
    )
    chance["unit"] = chance["unit"].fillna("families")
    results.table(
        "2e_chance_check",
        chance[
            [
                "tc_group",
                "colour",
                "unit",
                "families_tested",
                "clear_above_average",
                "clear_below_average",
                "clear_above_zero",
                "clear_below_zero",
                "expected_by_chance",
                "games_covered",
                "thin",
            ]
        ],
        f"How many non-thin families (or named lines, at least {MIN_CELL_GAMES} games) have a 95% CI clear of your average with that colour (the main test) and clear of the Elo expectation (zero), against the {CHANCE:.0%} expected by chance alone (in each direction about half of that).",
    )

    # 8.3 recency
    rec, rec_summary = recency(games, fam)
    results.table(
        "3_recency",
        rec,
        f"Per family and colour: games in the last 12 months (since {info['cutoff']}) and before, the share of the colour's games in each period, the all-years score above expectation and versus your average with that colour (vs_colour: clearly above / below / unclear), and the last-12-months score above expectation (thin_recent under {MIN_CELL_GAMES} games).",
    )
    results.table(
        "3b_recency_summary",
        rec_summary,
        f"Share of your games per colour and period in families whose all-years score is clearly above your average with that colour, clearly below, unclear, or thin (under {MIN_CELL_GAMES} games); weighted_excess = the families' all-years score above the Elo expectation weighted by how often you played them in the period.",
    )
    results.figure(chart_recency(rec), "3_recency")

    # 8.4 leaving the book
    dev, dev_score, dev_diff = deviation(games, groups)
    results.table(
        "4_deviation",
        dev,
        "Who played the first move after the deepest named line, per control and pooled: share of games where it was you (CI by game), and the expected points (ep_*, ep_per100_* per 100 such moves) and centipawns (cp_*) that move lost, yours vs your opponents' (disjoint sets of games: independent bootstraps).",
    )
    results.table(
        "4b_deviation_score", dev_score, "Score above the Elo expectation in the games where you left the named lines first vs where your opponent did."
    )
    results.table(
        "4c_deviation_score_diff",
        dev_diff,
        "Score above expectation when you left the book first minus when your opponent did (independent bootstraps), with Elo-equivalents relative to the score in the games your opponent left first (reference_score).",
    )
    results.figure(chart_deviation(dev, [*groups, ALL]), "4_deviation")

    # 8.5 book depth
    depth, slopes = book_depth(games, groups)
    results.table(
        "5_book_depth",
        depth,
        "Per control and pooled, by how many full moves the game stayed in a named line: score above the Elo expectation (with Elo), book_gain (expected points after book minus at move 1), opening-phase expected points lost theirs minus yours per game, share of your White games and share where you left the book first.",
    )
    results.table(
        "5b_book_depth_slope",
        slopes,
        f"Score above expectation per extra full move of named line (depth capped at {BOOK_MOVE_EDGES[-2] + 1:.0f}), least squares raw and holding your colour fixed; CIs by bootstrapping games. thin: under {MIN_CELL_GAMES} games per depth bin on average.",
    )
    results.figure(chart_book_depth(depth, groups), "5_book_depth")

    # 8.6 opening move quality
    ep, cp, by_ply = opening_quality(moves, groups)
    results.table(
        "6_opening_quality",
        ep,
        "Expected points lost per opening-phase move (and per 100 moves, per100_*), you vs your opponents in the same games, per control and colour and pooled; diff = you minus opponents (negative = you lose less), paired bootstrap by game.",
    )
    results.table("6b_opening_cp_loss", cp, "The same in centipawns lost per opening-phase move.")
    results.table(
        "6c_opening_by_ply",
        by_ply,
        "Expected points lost per move in plies 1-10, 11-20 and 21-30 (each side's own moves), you vs opponents, per control, colour and pooled.",
    )
    results.figure(chart_opening_quality(ep, cp, groups), "6_opening_quality")

    # 8.7 costliest repeated moves
    mine = costly_moves(moves, games, classes, player=True)
    theirs = costly_moves(moves, games, classes, player=False)
    results.table(
        "7_costly_moves",
        mine,
        f"Your repeated opening-phase moves (same move in the same position at least {RECUR_MIN_TIMES} times, mean loss at least {RECUR_MIN_EP} expected points and {RECUR_MIN_CP} centipawns, never the engine's best move), top {RECUR_TOP} by total expected points lost, all controls pooled: the engine's move, times played (overall, in the last 12 months, per time class), mean loss with CI by game, cp loss, the eval before (cp, your side), your score, Elo expectation and score above it (with Elo) in those games. thin: under {MIN_CELL_GAMES} games, so the score columns are anecdotes.",
    )
    results.table("7b_costly_moves_opponents", theirs, "The same for your opponents' moves against you (your_colour is your colour in those games).")
    results.figure(chart_costly(mine, theirs), "7_costly_moves")

    meta = pd.DataFrame(
        [
            {"item": "games_analysed", "value": len(games)},
            {"item": "games_without_moves", "value": info["no_moves"]},
            {"item": "games_without_expectation", "value": info["no_expectation"]},
            {"item": "moves", "value": len(moves)},
            {"item": "families", "value": games["family"].nunique()},
            {"item": "named_lines", "value": games["opening_name"].nunique()},
            {"item": "median_book_ply", "value": float(games["book_ply"].median())},
            {"item": "recent_cutoff", "value": info["cutoff"]},
            *[{"item": f"excluded_{g.replace(' ', '_')}_games", "value": n} for g, n in data.tc_noted.items()],
        ]
    )
    results.table(
        "8_data",
        meta,
        "Counts behind this question: games and moves analysed, games left out, distinct families and named lines, the recency cutoff and the thin controls excluded.",
    )
    results.finish()
    print(f"done in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
