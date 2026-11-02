"""One chart style for presentation/figures/.

Every chart in presentation/README.md is a static SVG on a light surface, so
it reads the same on GitHub in light and dark mode. The rules:

- the player is always `PLAYER` (blue) and the opponents always `OPPONENT`
  (grey): one colour carries the story, the comparison recedes;
- other series take `SERIES` slots in fixed order, never cycled;
- every estimate is drawn with its 95% CI (error bar or band);
- one panel per time-control group, laid out by `group_grid`: one row per
  time class, controls left to right, so a reader always finds 1+0 in the
  same place;
- thin data is labelled on the panel (`thin_note`), not silently dropped;
- hairline grid and axes, no chart junk, the title is the question.

Palette from the dataviz reference instance (validated categorical order,
light surface #fcfcfb).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
PLAYER = "#2a78d6"
OPPONENT = "#8a8880"  # grey on purpose (the comparison recedes); 3:1 on the surface
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SIDE_COLOURS = {"player": PLAYER, "opponent": OPPONENT}
SIDE_LABELS = {"player": "you", "opponent": "opponents"}
DIVERGING = "RdBu"  # blue = good for you, red = bad, grey-white midpoint
SEQUENTIAL = "Blues"


def style() -> None:
    """Apply the shared matplotlib style (call once before plotting)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.family": "sans-serif",
            "font.sans-serif": ["DejaVu Sans", "Arial", "Helvetica"],
            "font.size": 9,
            "axes.titlesize": 9.5,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.labelsize": 8.5,
            "axes.labelcolor": INK_SECONDARY,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.axisbelow": True,
            "axes.facecolor": SURFACE,
            "grid.color": GRID,
            "grid.linewidth": 0.6,
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "xtick.labelcolor": INK_SECONDARY,
            "ytick.labelcolor": INK_SECONDARY,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.frameon": False,
            "legend.fontsize": 8.5,
            "figure.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "figure.dpi": 100,
            "lines.linewidth": 2,
            "lines.markersize": 5,
            "errorbar.capsize": 0,
            "svg.fonttype": "path",  # text as outlines: identical rendering everywhere, no font dependency
            "svg.hashsalt": "chess-analysis",  # deterministic ids: reruns are byte-identical
        }
    )


def group_grid(groups: Sequence[str], panel: tuple[float, float] = (2.9, 2.3), sharey: bool | str = True, title: str | None = None):
    """One panel per time-control group: a row per time class, controls left to right.

    Returns (fig, axes) where axes maps each group label to its Axes; empty
    grid cells are hidden. `sharey` as in plt.subplots ("row" shares within a
    time class only).
    """
    import matplotlib.pyplot as plt

    classes: list[str] = []
    rows: dict[str, list[str]] = {}
    for g in groups:
        c = g.split(" ", 1)[0]
        if c not in rows:
            classes.append(c)
            rows[c] = []
        rows[c].append(g)
    ncols = max(len(v) for v in rows.values())
    fig, grid = plt.subplots(len(classes), ncols, figsize=(panel[0] * ncols, panel[1] * len(classes) + (0.5 if title else 0)), sharey=sharey, squeeze=False)
    axes = {}
    for r, c in enumerate(classes):
        for k in range(ncols):
            ax = grid[r][k]
            if k < len(rows[c]):
                axes[rows[c][k]] = ax
                ax.set_title(rows[c][k])
            else:
                ax.set_visible(False)
    if title:
        fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11, color=INK)
    fig.tight_layout()
    return fig, axes


def single(title: str | None = None, size: tuple[float, float] = (6.5, 3.4)):
    """One panel."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=size)
    if title:
        ax.set_title(title, fontsize=11, color=INK)
    return fig, ax


def _first_on_top(ax) -> None:
    """Puts the first level at the top of a horizontal chart. Only inverts once: several series on one
    axes (or on axes sharing y) would otherwise flip it back and forth."""
    if not ax.yaxis_inverted():
        ax.invert_yaxis()


def side_bars(ax, frame: pd.DataFrame, x: str, value: str = "", order: Sequence[str] | None = None, sides: Sequence[str] = ("player", "opponent"), width: float = 0.38, horizontal: bool = False) -> None:
    """You vs opponents as paired bars with 95% CI whiskers.

    `frame` is wide, as `compare_sides` returns it: columns `player`,
    `player_lo`, `player_hi`, `opponent`, ... (or with a `value` prefix:
    `<value>player`). One pair of bars per level of `x`.
    """
    levels = list(order) if order is not None else list(dict.fromkeys(frame[x].astype(str)))
    part = frame.assign(_x=frame[x].astype(str)).set_index("_x").reindex(levels)
    pos = np.arange(len(levels))
    for k, side in enumerate(sides):
        offset = (k - (len(sides) - 1) / 2) * width
        est = part[f"{value}{side}"].to_numpy(dtype=float)
        lo = part.get(f"{value}{side}_lo", pd.Series(np.nan, index=part.index)).to_numpy(dtype=float)
        hi = part.get(f"{value}{side}_hi", pd.Series(np.nan, index=part.index)).to_numpy(dtype=float)
        err = np.vstack([est - lo, hi - est])
        colour = SIDE_COLOURS.get(side, SERIES[k % len(SERIES)])
        if horizontal:
            ax.barh(pos + offset, est, height=width * 0.9, color=colour, label=SIDE_LABELS.get(side, side))
            ax.errorbar(est, pos + offset, xerr=err, fmt="none", ecolor=INK_SECONDARY, elinewidth=1)
        else:
            ax.bar(pos + offset, est, width=width * 0.9, color=colour, label=SIDE_LABELS.get(side, side))
            ax.errorbar(pos + offset, est, yerr=err, fmt="none", ecolor=INK_SECONDARY, elinewidth=1)
    if horizontal:
        ax.set_yticks(pos, levels)
        _first_on_top(ax)
        ax.grid(axis="y", visible=False)
    else:
        ax.set_xticks(pos, levels)
        ax.grid(axis="x", visible=False)


def side_lines(ax, frame: pd.DataFrame, x: str, value: str = "", sides: Sequence[str] = ("player", "opponent"), order: Sequence | None = None, markers: bool = True) -> None:
    """You vs opponents as lines with 95% CI bands. `x` numeric, or categorical with `order`."""
    part = frame.copy()
    if order is not None:
        rank = {str(v): i for i, v in enumerate(order)}
        part = part.assign(_x=part[x].astype(str).map(rank)).dropna(subset=["_x"]).sort_values("_x")
        xs_name = "_x"
    else:
        part = part.sort_values(x)
        xs_name = x
    for k, side in enumerate(sides):
        colour = SIDE_COLOURS.get(side, SERIES[k % len(SERIES)])
        xs = part[xs_name].to_numpy(dtype=float)
        est = part[f"{value}{side}"].to_numpy(dtype=float)
        ax.plot(xs, est, color=colour, marker="o" if markers else None, label=SIDE_LABELS.get(side, side), zorder=3 if side == "player" else 2)
        if f"{value}{side}_lo" in part:
            ax.fill_between(xs, part[f"{value}{side}_lo"].to_numpy(dtype=float), part[f"{value}{side}_hi"].to_numpy(dtype=float), color=colour, alpha=0.18, linewidth=0)
    if order is not None:
        ax.set_xticks(range(len(order)), [str(v) for v in order])


def estimate(ax, frame: pd.DataFrame, x: str, y: str = "estimate", lo: str = "lo", hi: str = "hi", order: Sequence | None = None, colour: str = PLAYER, label: str | None = None, offset: float = 0.0, horizontal: bool = False, line: bool = False) -> None:
    """Point estimates with 95% CI whiskers (one series). Use `offset` to dodge several series."""
    levels = list(order) if order is not None else list(dict.fromkeys(frame[x].astype(str)))
    part = frame.assign(_x=frame[x].astype(str)).set_index("_x").reindex([str(v) for v in levels])
    pos = np.arange(len(levels)) + offset
    est = part[y].to_numpy(dtype=float)
    err = np.vstack([est - part[lo].to_numpy(dtype=float), part[hi].to_numpy(dtype=float) - est])
    if horizontal:
        ax.errorbar(est, pos, xerr=err, fmt="o", color=colour, ecolor=colour, elinewidth=1.5, markersize=6, label=label)
        ax.set_yticks(np.arange(len(levels)), [str(v) for v in levels])
        _first_on_top(ax)
    else:
        ax.errorbar(pos, est, yerr=err, fmt="o-" if line else "o", color=colour, ecolor=colour, elinewidth=1.5, markersize=6, label=label, linewidth=1.5)
        ax.set_xticks(np.arange(len(levels)), [str(v) for v in levels])


def zero_line(ax, value: float = 0.0, horizontal: bool = True) -> None:
    """A baseline at `value` (0 = no difference)."""
    (ax.axhline if horizontal else ax.axvline)(value, color=INK_MUTED, linewidth=1, zorder=1)


def thin_note(ax, text: str = "too few games", keep_y: bool = False) -> None:
    """Grey note on a panel whose data are too thin to draw.

    Hides the panel's ticks and grid. With `keep_y` only the x ticks go (the
    y axis and the grid stay): in a grid sharing the y axis the first panel of
    a row carries the row's only y labels, so a thin first panel keeps them.
    """
    ax.text(0.5, 0.5, text, transform=ax.transAxes, ha="center", va="center", color=INK_MUTED, fontsize=8.5)
    # tick_params, not set_*ticks([]): on shared axes the latter would clear every panel's ticks
    if keep_y:
        ax.tick_params(axis="x", bottom=False, labelbottom=False)
    else:
        ax.tick_params(left=False, labelleft=False, bottom=False, labelbottom=False)
        ax.grid(False)


def blank_thin(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """Copy for drawing: the `columns` (those present) set to NaN in rows flagged `thin`, so thin cells are not drawn as confident points."""
    out = frame.copy()
    thin = out["thin"].astype(bool)
    for c in columns:
        if c in out:
            out.loc[thin, c] = np.nan
    return out


def panel_estimates(ax, frame: pd.DataFrame, x: str, order: Sequence[str], y: str = "estimate", colour: str = PLAYER, label: str | None = None, offset: float = 0.0) -> bool:
    """`estimate` for one series in one panel (columns `y`, `<y>_lo`, `<y>_hi`), thin rows left out, every level of `order` kept on the x axis. Returns False when nothing could be drawn."""
    part = blank_thin(frame, [y, f"{y}_lo", f"{y}_hi"])
    if part[y].notna().sum() == 0:
        return False
    estimate(ax, part, x, y=y, lo=f"{y}_lo", hi=f"{y}_hi", order=order, colour=colour, label=label, offset=offset)
    ax.set_xlim(-0.5, len(order) - 0.5)
    return True


def label_first_column(axes, text: str) -> None:
    """Y label on the first panel of each row (a dict of axes, as from `group_grid`, or any iterable of axes)."""
    for ax in (axes.values() if hasattr(axes, "values") else axes):
        if ax.get_subplotspec().is_first_col():
            ax.set_ylabel(text)


def finish_grid(fig, axes: dict, drawn: dict, ylabel: str, xlabel: str | None = None, rotate: int = 0, zero: bool = True, thin_text: str = "too few games") -> None:
    """Completes a `group_grid` of estimates: a thin note (y labels kept) on panels where `drawn[group]` is false, a zero line on the others (unless `zero` is false), x labels rotated by `rotate` degrees, the y label on each row's first panel, `xlabel` on every panel; then tight_layout."""
    for group, ax in axes.items():
        if not drawn.get(group):
            thin_note(ax, thin_text, keep_y=True)
            continue
        if zero:
            zero_line(ax)
        if rotate:
            labels = [t.get_text() for t in ax.get_xticklabels()]
            ax.set_xticks(ax.get_xticks(), labels, rotation=rotate, ha="right", rotation_mode="anchor")
    label_first_column(axes, ylabel)
    if xlabel:
        for ax in axes.values():
            ax.set_xlabel(xlabel)
    fig.tight_layout()


def rotate_labels(ax, angle: float = 45) -> None:
    """Rotates the current x tick labels by `angle` degrees, right-aligned to their ticks."""
    for tick in ax.get_xticklabels():
        tick.set_rotation(angle)
        tick.set_ha("right")


def stacked_bars(ax, positions, segments: Sequence, colours: Sequence, labels: Sequence[str] | None = None, width: float = 0.8, horizontal: bool = False, **kwargs) -> np.ndarray:
    """Stacked bars: at each of `positions`, the segments' values one after another from zero.

    `segments` holds one array of values per segment (aligned with
    `positions`), drawn in order; `colours` and `labels` give one entry per
    segment. A NaN value draws nothing and adds nothing to the stack. Extra
    keyword arguments (edgecolor, alpha, ...) go to every bar. Returns the
    stack totals.
    """
    pos = np.atleast_1d(np.asarray(positions, dtype=float))
    base = np.zeros(len(pos))
    for k, values in enumerate(segments):
        values = np.atleast_1d(np.asarray(values, dtype=float))
        extra = dict(kwargs) if labels is None else {**kwargs, "label": labels[k]}
        if horizontal:
            ax.barh(pos, values, left=base, height=width, color=colours[k], **extra)
        else:
            ax.bar(pos, values, bottom=base, width=width, color=colours[k], **extra)
        base = base + np.nan_to_num(values)
    return base


def heatmap(ax, values, cmap, norm, texts=None, text_colours=INK, fontsize: float = 7.5, xlabels: Sequence[str] | None = None, ylabels: Sequence[str] | None = None):
    """A grid of values (2-D, first row on top) as coloured cells, with optional text in each cell.

    NaN or masked cells take the colour map's "bad" colour. `texts` is 2-D
    (None or "" = no text); `text_colours` and `fontsize` are one value or
    2-D. `xlabels` / `ylabels` name the columns and rows. No grid, no spines.
    Returns the image, for a colour bar.
    """
    image = ax.imshow(values, cmap=cmap, norm=norm, aspect="auto")
    if texts is not None:
        shape = np.shape(values)
        colours = np.broadcast_to(np.asarray(text_colours, dtype=object), shape)
        sizes = np.broadcast_to(np.asarray(fontsize, dtype=object), shape)
        for i in range(shape[0]):
            for j in range(shape[1]):
                if texts[i][j]:
                    ax.text(j, i, texts[i][j], ha="center", va="center", fontsize=sizes[i, j], color=colours[i, j])
    if xlabels is not None:
        ax.set_xticks(range(len(xlabels)), xlabels)
    if ylabels is not None:
        ax.set_yticks(range(len(ylabels)), ylabels)
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return image


def legend(fig_or_ax, loc: str = "upper right", **kwargs) -> None:
    """A legend from the first panel's handles (axes) or all panels (figure), de-duplicated."""
    import matplotlib.figure

    if isinstance(fig_or_ax, matplotlib.figure.Figure):
        handles, labels = [], []
        for ax in fig_or_ax.axes:
            for h, lab in zip(*ax.get_legend_handles_labels()):
                if lab not in labels:
                    handles.append(h)
                    labels.append(lab)
        fig_or_ax.legend(handles, labels, loc=loc, ncol=len(labels), **kwargs)
    else:
        fig_or_ax.legend(loc=loc, **kwargs)


def footnote(fig, text: str) -> None:
    """Small grey note under the chart (units, what the bars are)."""
    fig.text(0.01, -0.01, text, ha="left", va="top", fontsize=7.5, color=INK_MUTED, wrap=True)
