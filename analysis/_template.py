"""Template for a question script: copy, rename to <nn>_<slug>.py, fill in.

Run: .venv/bin/python analysis/<nn>_<slug>.py
"""

from chess_analysis import charts
from chess_analysis.toolkit import Results, compare_sides, load_data

SLUG = "00_template"


def main() -> None:
    data = load_data()
    charts.style()
    results = Results(SLUG)

    # 1. Expected points lost per move, you vs opponents, per time control.
    table = compare_sides(data.moves, "ep_loss", by=["tc_group"])
    table = table[table["tc_group"].isin(data.tc_groups)]
    results.table("1_ep_loss", table, "Expected points lost per move, you vs opponents, per time control (CIs clustered by game).")
    fig, ax = charts.single("How much do you give away per move?")
    charts.side_bars(ax, table, "tc_group", order=data.tc_groups)
    ax.set_ylabel("expected points lost per move")
    charts.legend(ax)
    results.figure(fig, "1_ep_loss")

    results.finish()


if __name__ == "__main__":
    main()
