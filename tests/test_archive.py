import pytest

from chess_analysis import archive
from chess_analysis.api import NotFound

BASE = "https://api.chess.com/pub/player/someplayer/games"


def urls(*months):
    return [f"{BASE}/{m.replace('-', '/')}" for m in months]


def test_month_key():
    assert archive.month_key(f"{BASE}/2026/10") == "2026-10"


def test_everything_is_fetched_when_nothing_is_cached():
    assert archive.months_to_fetch(urls("2026-08", "2026-09"), set()) == urls("2026-08", "2026-09")


def test_only_new_and_latest_cached_months_are_fetched():
    available = urls("2026-07", "2026-08", "2026-09", "2026-10")
    cached = {"2026-07", "2026-08", "2026-09"}
    # 2026-09 may have been saved before the month ended, so it is fetched again.
    assert archive.months_to_fetch(available, cached) == urls("2026-09", "2026-10")


def test_gaps_in_the_cache_are_filled():
    available = urls("2026-07", "2026-08", "2026-09")
    assert archive.months_to_fetch(available, {"2026-07", "2026-09"}) == urls("2026-08", "2026-09")


def test_refresh_all_fetches_everything():
    available = urls("2026-08", "2026-09")
    assert archive.months_to_fetch(available, {"2026-08", "2026-09"}, refresh_all=True) == available


def test_save_and_load_round_trip(tmp_path):
    archive.save_month(tmp_path, "2026-10", {"games": [{"uuid": "b"}]})
    archive.save_month(tmp_path, "2026-09", {"games": [{"uuid": "a"}]})
    assert list(archive.load_months(tmp_path)) == [
        ("2026-09", {"games": [{"uuid": "a"}]}),
        ("2026-10", {"games": [{"uuid": "b"}]}),
    ]
    assert not list(tmp_path.glob("*.tmp"))


class FakeClient:
    def __init__(self, months, known_users=("someplayer",)):
        self.months = months
        self.known_users = known_users
        self.requested = []

    def player(self, username):
        if username not in self.known_users:
            raise NotFound(username)
        return {"username": username}

    def archive_urls(self, username):
        return urls(*self.months)

    def month(self, url):
        self.requested.append(archive.month_key(url))
        return {"games": [{"uuid": url}]}


def test_download_saves_only_what_is_needed(tmp_path):
    archive.save_month(tmp_path, "2026-08", {"games": []})
    archive.save_month(tmp_path, "2026-09", {"games": []})
    client = FakeClient(["2026-08", "2026-09", "2026-10"])

    count = archive.download(client, "someplayer", tmp_path, log=lambda _: None)

    assert count == 2
    assert client.requested == ["2026-09", "2026-10"]
    assert archive.cached_months(tmp_path) == {"2026-08", "2026-09", "2026-10"}


def test_download_reports_unknown_user(tmp_path):
    with pytest.raises(NotFound, match="'nobody' not found"):
        archive.download(FakeClient([]), "nobody", tmp_path, log=lambda _: None)
    assert not tmp_path.joinpath("2026-10.json").exists()
