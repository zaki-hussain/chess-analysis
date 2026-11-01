from pathlib import Path

import pandas as pd

from chess_analysis import openings

OPENINGS_DIR = Path(__file__).parent.parent / "data" / "openings"


def test_book_loads_named_lines():
    book = openings.load_book(OPENINGS_DIR)
    assert len(book) > 3000
    assert book["rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -"] == ("C20", "King's Pawn Game")


def test_game_openings_use_deepest_match_and_transpositions():
    book = {
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq -": ("C20", "King's Pawn Game"),
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq -": ("C40", "King's Knight Opening"),
    }
    moves = pd.DataFrame(
        {
            "game_uuid": ["g1", "g1", "g1", "g2"],
            "ply": [1, 2, 3, 1],
            "fen_after": [
                "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
                "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2",
                "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2",
                "rnbqkbnr/pppppppp/8/8/8/7N/PPPPPPPP/RNBQKB1R b KQkq - 1 1",
            ],
        }
    )
    assert list(openings.in_book(moves, book)) == [False, True, True, False]
    out = openings.game_openings(moves, book)
    assert out.loc["g1"].to_dict() == {"opening_eco": "C40", "opening_name": "King's Knight Opening", "book_ply": 3}
    assert out.loc["g2"].to_dict() == {"opening_eco": "", "opening_name": "", "book_ply": 0}
