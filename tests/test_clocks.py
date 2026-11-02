import numpy as np
import pandas as pd

from chess_analysis import clocks


def _moves():
    """Two games: one 1+0, one 1+1; clocks chosen to sit on bucket edges."""
    rows = []
    for g, (group, inc) in enumerate([("bullet 1+0", 0.0), ("bullet 1+1", 1.0)]):
        for ply, clock in enumerate([60.0, 30.0, 10.0, 5.0, 4.9, 0.5], start=1):
            rows.append(
                {
                    "game_uuid": f"g{g}", "ply": ply, "tc_group": group, "is_player_move": ply % 2 == 1,
                    "clock_before_seconds": clock, "opponent_clock_seconds": clock, "increment_seconds": inc,
                    "clock_buffer_seconds": clock - inc, "opponent_clock_buffer_seconds": clock - inc,
                    "clock_fraction": clock / 60, "phase": "endgame" if ply > 3 else "middlegame",
                    "rated": True, "my_rating_change": 8.0, "opponent_rating_after": 1000.0,
                    "ep_loss": 0.01 * ply, "side": "player" if ply % 2 == 1 else "opponent",
                }  # fmt: skip
            )
    return pd.DataFrame(rows)


def test_buckets_are_left_closed_and_cover_negative_buffers():
    out = clocks.cut_bucket(pd.Series([-0.5, 0.0, 4.99, 5.0, 10.0, 29.9, 30.0, 1e6]), clocks.CLOCK_COARSE_EDGES, clocks.CLOCK_COARSE_LABELS)
    assert list(out.astype(str)) == ["<5 s", "<5 s", "<5 s", "5-10 s", "10-30 s", "10-30 s", "30+ s", "30+ s"]
    fine = clocks.cut_bucket(pd.Series([59.9, 60.0, 299.0, 300.0]), clocks.CLOCK_SECONDS_EDGES, clocks.CLOCK_SECONDS_LABELS)
    assert list(fine.astype(str)) == ["30-60 s", "1-2 min", "2-5 min", "5+ min"]
    assert len(clocks.CLOCK_SECONDS_LABELS) == len(clocks.CLOCK_SECONDS_EDGES) - 1
    assert len(clocks.CLOCK_FRACTION_LABELS) == len(clocks.CLOCK_FRACTION_EDGES) - 1


def test_add_clock_buckets_seconds_vs_buffer():
    out = clocks.add_clock_buckets(_moves())
    inc = out[out["tc_group"] == "bullet 1+1"].set_index("ply")
    flat = out[out["tc_group"] == "bullet 1+0"].set_index("ply")
    # 10 s on the clock with a 1 s increment is 9 s of buffer: low in buffer terms, not in seconds.
    assert inc.loc[3, "clock_coarse"] == "10-30 s" and inc.loc[3, "buffer_coarse"] == "5-10 s"
    assert not inc.loc[3, "low_clock"] and inc.loc[3, "low_buffer"]
    # Without increment the two measures agree.
    assert (flat["clock_bucket"].astype(str) == flat["buffer_bucket"].astype(str)).all()
    assert (flat["low_clock"] == flat["low_buffer"]).all()
    assert inc.loc[6, "buffer_bucket"] == "<5 s"  # negative buffer
    assert out["opponent_rating_pre"].eq(1008.0).all()


def test_group_frames_pools_classes_with_two_groups():
    m = _moves()
    labels = [label for label, _ in clocks.group_frames(m, ["bullet 1+0", "bullet 1+1"], pooled=True)]
    assert labels == ["bullet 1+0", "bullet 1+1", "all bullet"]
    assert [label for label, _ in clocks.group_frames(m, ["bullet 1+0"], pooled=True)] == ["bullet 1+0"]


def test_bucket_curve_counts_and_thin_flag():
    m = clocks.add_clock_buckets(_moves())
    out = clocks.bucket_curve(m, "clock_coarse", "ep_loss", ["bullet 1+0", "bullet 1+1"], n_boot=20, min_moves=2)
    assert list(out.columns[:2]) == ["tc_group", "clock_coarse"]
    first = out[out["tc_group"] == "bullet 1+0"]
    assert list(first["clock_coarse"]) == ["<5 s", "5-10 s", "10-30 s", "30+ s"]  # category order, not alphabetical
    row = first.set_index("clock_coarse").loc["<5 s"]
    assert row["n_player"] == 1 and row["n_opponent"] == 1 and row["thin"]


def test_stakes_bucket_marks_forced_moves():
    frame = pd.DataFrame({"ep_criticality": [np.nan, 0.0, 0.005, 0.2], "criticality": [np.nan, 0.0, 50.0, 400.0]})
    assert list(clocks.stakes_bucket(frame, "ep_loss").astype(str)) == ["one move", "0", "<0.01", "0.10+"]
    assert list(clocks.stakes_bucket(frame, "cp_loss").astype(str)) == ["one move", "0", "<100", "300+"]


def test_group_frames_uses_the_class_map_and_comfort_line():
    m = _moves()
    labels = [label for label, _ in clocks.group_frames(m, ["bullet 1+0", "bullet 1+1"], pooled=True, tc_class={"bullet 1+0": "fast", "bullet 1+1": "fast"})]
    assert labels == ["bullet 1+0", "bullet 1+1", "all fast"]
    assert clocks.COMFORT_SECONDS == 30.0 and clocks.CLOCK_REFERENCE == f"{clocks.COMFORT_SECONDS:g}+ s"
