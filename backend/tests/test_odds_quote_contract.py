"""Odds quote contract: encoding, race_phase, and empty failures.

Failures return quotes=[] and reason. Prices in an HTTP 400 body are not quotes.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
import requests

from backend.cli import main as cli_main
from backend.scrapers.netkeiba_odds_scraper import (
    NetkeibaOddsScraper,
    parse_netkeiba_sanrenpuku_odds,
    parse_netkeiba_umaren_odds,
    parse_netkeiba_wide_odds,
    parse_netkeiba_win_odds,
)
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 9, 26, 16, 0, 0)
RACE_ID = "202609040711"

PARSERS = {
    "win": parse_netkeiba_win_odds,
    "wide": parse_netkeiba_wide_odds,
    "umaren": parse_netkeiba_umaren_odds,
    "sanrenpuku": parse_netkeiba_sanrenpuku_odds,
}
FETCHERS = {
    "win": "get_win_odds",
    "wide": "get_wide_odds",
    "umaren": "get_umaren_odds",
    "sanrenpuku": "get_sanrenpuku_odds",
}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _http_error(status: int, body: str) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return requests.HTTPError(f"http {status}", response=response)


class _RaisingHTTP:
    def __init__(self, error: Exception):
        self.error = error

    def get(self, url, params=None, headers=None, encoding=None):
        raise self.error

    def close(self):
        return None


class _SplitOdds:
    """One snapshot per bet type. Unused types are not called."""

    def __init__(self, **snapshots):
        self.snapshots = snapshots
        self.calls: list[str] = []

    def get_win_odds(self, race_id, fetched_at=None):
        return self._get("win", race_id)

    def get_wide_odds(self, race_id, fetched_at=None):
        return self._get("wide", race_id)

    def get_umaren_odds(self, race_id, fetched_at=None):
        return self._get("umaren", race_id)

    def get_sanrenpuku_odds(self, race_id, fetched_at=None):
        return self._get("sanrenpuku", race_id)

    def _get(self, bet_type: str, race_id: str):
        self.calls.append(bet_type)
        assert race_id == RACE_ID
        return self.snapshots[bet_type]

    def close(self):
        return None


@pytest.mark.parametrize("bet_type", ["win", "wide", "umaren", "sanrenpuku"])
def test_post_race_empty_stays_empty(bet_type):
    snapshot = PARSERS[bet_type](
        _load("netkeiba_jra_odds_post_empty.json"),
        RACE_ID,
        fetched_at=FETCHED_AT,
    )

    assert snapshot.bet_types == [bet_type]
    assert snapshot.status == "unavailable"
    assert snapshot.quotes == []
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    assert snapshot.reason == "result odds empty"
    assert snapshot.fetched_at == FETCHED_AT


@pytest.mark.parametrize("bet_type", ["win", "wide", "umaren", "sanrenpuku"])
def test_http_400_with_prices_does_not_become_quotes(bet_type):
    body = (FIXTURES / "netkeiba_jra_odds_http_400.json").read_text(encoding="utf-8")
    scraper = NetkeibaOddsScraper(http_client=_RaisingHTTP(_http_error(400, body)))
    snapshot = getattr(scraper, FETCHERS[bet_type])(RACE_ID, fetched_at=FETCHED_AT)

    assert snapshot.status == "unavailable"
    assert snapshot.quotes == []
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    assert snapshot.reason == "http 400: result odds empty"
    dumped = json.dumps(snapshot.model_dump(mode="json"))
    assert "99.9" not in dumped
    assert "88.8" not in json.dumps(dumped)
    assert "77.7" not in json.dumps(dumped)


@pytest.mark.parametrize("bet_type", ["win", "wide", "umaren", "sanrenpuku"])
def test_http_400_without_json_stays_empty(bet_type):
    scraper = NetkeibaOddsScraper(http_client=_RaisingHTTP(_http_error(400, "<html>bad</html>")))
    snapshot = getattr(scraper, FETCHERS[bet_type])(RACE_ID, fetched_at=FETCHED_AT)

    assert snapshot.quotes == []
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase is None
    assert snapshot.reason == "http 400"
    assert snapshot.status == "unavailable"


def test_http_500_is_an_empty_quote_list():
    scraper = NetkeibaOddsScraper(http_client=_RaisingHTTP(_http_error(500, "")))
    snapshot = scraper.get_win_odds(RACE_ID, fetched_at=FETCHED_AT)

    assert snapshot.quotes == []
    assert snapshot.reason == "http 500"
    assert snapshot.race_phase is None
    assert snapshot.encoding == "utf-8"


def test_connection_error_is_fetch_failed():
    scraper = NetkeibaOddsScraper(http_client=_RaisingHTTP(requests.ConnectionError("down")))
    snapshot = scraper.get_wide_odds(RACE_ID, fetched_at=FETCHED_AT)

    assert snapshot.quotes == []
    assert snapshot.reason == "fetch failed"
    assert snapshot.race_phase is None
    assert snapshot.encoding == "utf-8"


def test_non_object_json_is_invalid_and_empty():
    snapshot = parse_netkeiba_umaren_odds([1, 2, 3], RACE_ID, fetched_at=FETCHED_AT)

    assert snapshot.quotes == []
    assert snapshot.reason == "invalid odds payload"
    assert snapshot.race_phase is None
    assert snapshot.encoding == "utf-8"


def test_merged_empty_post_keeps_one_phase():
    post_empty = _load("netkeiba_jra_odds_post_empty.json")
    service = RaceService()
    service._odds_scraper = _SplitOdds(
        win=parse_netkeiba_win_odds(post_empty, RACE_ID, fetched_at=FETCHED_AT),
        wide=parse_netkeiba_wide_odds(post_empty, RACE_ID, fetched_at=FETCHED_AT),
    )

    snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide", prefer="live")

    assert snapshot.quotes == []
    assert snapshot.status == "unavailable"
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    assert snapshot.reason == "win: result odds empty; wide: result odds empty"
    assert service._odds_scraper.calls == ["win", "wide"]


def test_disagreement_on_phase_stays_unset():
    post = parse_netkeiba_win_odds(
        _load("netkeiba_jra_odds_win.json"), RACE_ID, fetched_at=FETCHED_AT
    )
    yoso_payload = _load("netkeiba_jra_odds_wide.json")
    yoso_payload["status"] = "yoso"
    pre = parse_netkeiba_wide_odds(yoso_payload, RACE_ID, fetched_at=FETCHED_AT)
    service = RaceService()
    service._odds_scraper = _SplitOdds(win=post, wide=pre)

    snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide", prefer="live")

    assert snapshot.quotes
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase is None
    assert {quote.kind for quote in snapshot.quotes} == {"live", "yoso"}
    assert "99.9" not in {quote.odds for quote in snapshot.quotes}


def test_odds_help_documents_encoding_and_race_phase(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backend.cli", "odds", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli_main()

    assert exc.value.code == 0
    text = capsys.readouterr().out
    assert "encoding" in text
    assert "utf-8" in text
    assert "race_phase" in text
    assert "pre" in text
    assert "post" in text
    assert "HTTP 400" in text


def test_odds_text_shows_phase_and_encoding_when_empty(monkeypatch, capsys):
    empty = parse_netkeiba_sanrenpuku_odds(
        _load("netkeiba_jra_odds_post_empty.json"),
        RACE_ID,
        fetched_at=datetime(2026, 9, 26, 16, 0, 0),
    )

    class _Service:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get_race_odds(self, race_id, bet_types="win", prefer="live"):
            assert bet_types == "sanrenpuku"
            return empty

    monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
    monkeypatch.setattr(
        sys,
        "argv",
        ["backend.cli", "odds", RACE_ID, "--bet-types", "sanrenpuku"],
    )
    cli_main()
    text = capsys.readouterr().out
    assert "局面: post（レース後）" in text
    assert "文字コード: utf-8" in text
    assert "理由: result odds empty" in text
    assert "99.9" not in text
