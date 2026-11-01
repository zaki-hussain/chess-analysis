"""Template for a question script: copy, rename to <nn>_<slug>.py, fill in."""

from chess_analysis.toolkit import TIME_CLASSES, Report, compare_sides, load_data

SLUG = "00_template"


def main() -> None:
    data = load_data()
    report = Report(SLUG, "Template question")
    report.text(f"{len(data.games):,} games, {len(data.moves):,} moves.")

    report.heading("Expected points lost per move, by time class")
    table = compare_sides(data.moves, "ep_loss", by=["time_class"])
    table = table[table["time_class"].isin(TIME_CLASSES)]
    report.table(table[["time_class", "player", "player_lo", "player_hi", "opponent", "opponent_lo", "opponent_hi", "diff", "diff_lo", "diff_hi", "games"]], floatfmt=".4f")

    path = report.write()
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
