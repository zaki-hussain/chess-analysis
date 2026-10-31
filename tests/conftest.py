import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def real_games() -> dict[str, dict]:
    """A few real games from the zakihussa1n archive, keyed by what they cover."""
    return json.loads((FIXTURES / "games.json").read_text(encoding="utf-8"))
