"""Turn one game from a monthly archive into a flat game row and one row per move.

Only data chess.com provides is kept: the work here is flattening the JSON,
splitting the PGN into headers and moves, and turning clock readings into
seconds. Anything built on top (time spent, ratings before a game, ...)
belongs in later stages.
"""

import re

_HEADER_RE = re.compile(r'^\[(\w+)\s+"((?:[^"\\]|\\.)*)"\]\s*$', re.M)
_UNESCAPE_RE = re.compile(r"\\(.)")
_TOKEN_RE = re.compile(
    r"""
      (?P<comment>\{[^}]*\})
    | (?P<result>1-0|0-1|1/2-1/2|\*)
    | (?P<number>\d+\.+)
    | (?P<nag>\$\d+)
    | (?P<move>[^\s{}]+)
    """,
    re.X,
)
_CLOCK_RE = re.compile(r"\[%clk\s+([^\]\s]+)\]")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def parse_clock(text: str) -> float:
    """"1:02:03.4" -> 3723.4 seconds."""
    seconds = 0.0
    for part, unit in zip(reversed(text.split(":")), (1, 60, 3600, 86400)):
        seconds += float(part) * unit
    return round(seconds, 3)


def split_pgn(pgn: str) -> tuple[dict[str, str], str]:
    """PGN text -> ({header: value}, move text)."""
    headers, end = {}, 0
    for match in _HEADER_RE.finditer(pgn):
        headers[match[1]] = _UNESCAPE_RE.sub(r"\1", match[2])
        end = match.end()
    return headers, pgn[end:]


def parse_moves(movetext: str) -> list[dict]:
    """One dict per half-move: ply, move_number, color, san and the clock reading after it."""
    moves: list[dict] = []
    number, color = 1, "white"
    for token in _TOKEN_RE.finditer(movetext):
        kind, text = token.lastgroup, token.group()
        if kind == "number":
            number = int(text.rstrip("."))
            color = "black" if text.endswith("...") else "white"
        elif kind == "move":
            moves.append(
                {"ply": len(moves) + 1, "move_number": number, "color": color, "san": text, "clock": None, "clock_seconds": None}
            )
            if color == "black":
                number += 1
            color = "black" if color == "white" else "white"
        elif kind == "comment" and moves:
            clock = _CLOCK_RE.search(text)
            if clock:
                moves[-1]["clock"] = clock.group(1)
                moves[-1]["clock_seconds"] = parse_clock(clock.group(1))
    return moves


def header_column(name: str) -> str:
    """PGN header -> column name: "UTCDate" -> "pgn_utc_date"."""
    return "pgn_" + _CAMEL_BOUNDARY_RE.sub("_", name).lower()


def parse_game(game: dict, archive_month: str) -> tuple[dict, list[dict]]:
    """A game from the archive JSON -> (game row, move rows).

    Nested objects are flattened ("white": {"rating": ...} -> "white_rating")
    and every PGN header becomes a "pgn_..." column. Fields that only some
    games have (accuracies, tournament, ...) are kept when present.
    """
    row: dict = {"uuid": game["uuid"], "archive_month": archive_month}
    for key, value in game.items():
        if key == "pgn":
            continue
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                row[f"{key}_{sub_key.lstrip('@')}"] = sub_value
        else:
            row[key] = value

    pgn = game.get("pgn", "")
    headers, movetext = split_pgn(pgn)
    row.update({header_column(name): value for name, value in headers.items()})
    row["pgn"] = pgn

    moves = [{"game_uuid": game["uuid"], **move} for move in parse_moves(movetext)]
    return row, moves
