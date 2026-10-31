import pytest

from chess_analysis.parse import header_column, parse_clock, parse_game, parse_moves, split_pgn


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("0:03:01.7", 181.7), ("0:01:01", 61.0), ("2:00:01.6", 7201.6), ("10:00:00", 36000.0), ("0:00:00", 0.0)],
)
def test_parse_clock(text, seconds):
    assert parse_clock(text) == seconds


@pytest.mark.parametrize(
    ("header", "column"),
    [("UTCDate", "pgn_utc_date"), ("ECOUrl", "pgn_eco_url"), ("WhiteElo", "pgn_white_elo"), ("SetUp", "pgn_set_up"), ("FEN", "pgn_fen")],
)
def test_header_column(header, column):
    assert header_column(header) == column


def test_split_pgn_unescapes_header_values():
    headers, movetext = split_pgn('[Event "Let\\"s \\\\ Play"]\n[Site "Chess.com"]\n\n1. e4 1-0\n')
    assert headers == {"Event": 'Let"s \\ Play', "Site": "Chess.com"}
    assert movetext.strip() == "1. e4 1-0"


def test_moves_without_comments_or_black_move_numbers():
    moves = parse_moves("1. e4 e5 2. Nf3 $1 Nc6 3. Bb5 1/2-1/2")
    assert [(m["move_number"], m["color"], m["san"]) for m in moves] == [
        (1, "white", "e4"),
        (1, "black", "e5"),
        (2, "white", "Nf3"),
        (2, "black", "Nc6"),
        (3, "white", "Bb5"),
    ]
    assert all(m["clock"] is None for m in moves)


def test_game_starting_with_black_to_move():
    moves = parse_moves("12... Qh4 {[%clk 0:00:10]} 13. g3 {[%clk 0:00:09.5]} 0-1")
    assert [(m["ply"], m["move_number"], m["color"]) for m in moves] == [(1, 12, "black"), (2, 13, "white")]


def test_live_game(real_games):
    game = real_games["live_with_extras"]
    row, moves = parse_game(game, "2023-06")

    assert row["uuid"] == game["uuid"]
    assert row["archive_month"] == "2023-06"
    # Ratings and results from the JSON are flattened.
    assert row["white_username"] == "zakihussa1n"
    assert row["white_rating"] == 829
    assert row["white_result"] == "checkmated"
    assert row["black_rating"] == 763
    assert row["white_id"] == "https://api.chess.com/pub/player/zakihussa1n"
    # Optional fields are kept when present.
    assert row["accuracies_white"] == 41.88
    assert row["tournament"].startswith("https://api.chess.com/pub/tournament/")
    # Every PGN header becomes a column.
    assert row["pgn_utc_date"] == "2023.06.06"
    assert row["pgn_utc_time"] == "12:30:09"
    assert row["pgn_result"] == "0-1"
    assert row["pgn"] == game["pgn"]

    assert len(moves) == len(game["tcn"]) // 2 == 32
    assert moves[0] == {
        "game_uuid": game["uuid"],
        "ply": 1,
        "move_number": 1,
        "color": "white",
        "san": "e4",
        "clock": "0:03:01.7",
        "clock_seconds": 181.7,
    }
    assert moves[1]["color"] == "black" and moves[1]["clock_seconds"] == 178.2
    assert all(m["clock_seconds"] is not None for m in moves)


def test_daily_game_keeps_clock_readings_as_given(real_games):
    row, moves = parse_game(real_games["daily"], "2023-05")
    assert row["time_class"] == "daily"
    assert "start_time" in row
    assert moves[0]["clock"] == "2:00:01.6"
    assert moves[0]["clock_seconds"] == 7201.6


def test_crazyhouse_drops_are_moves(real_games):
    game = real_games["crazyhouse"]
    _, moves = parse_game(game, "2022-04")
    assert len(moves) == len(game["tcn"]) // 2
    assert any("@" in m["san"] for m in moves)


def test_odds_game_keeps_starting_position(real_games):
    row, moves = parse_game(real_games["odds"], "2022-01")
    assert row["pgn_set_up"] == "1"
    assert row["pgn_fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/R2QKB1R w KQkq - 0 1"
    assert len(moves) == 6


def test_game_with_no_moves(real_games):
    row, moves = parse_game(real_games["no_moves"], "2023-08")
    assert moves == []
    assert row["pgn_termination"] == "zakihussa1n won by resignation"
