"""Unit tests for netkeiba win odds quotes."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.cli import main as cli_main
from backend.main import app
from backend.models.odds import OddsQuote
from backend.scrapers.netkeiba_odds_scraper import (
    NetkeibaOddsScraper,
    parse_netkeiba_win_odds,
)
from backend.services.race_service import RaceService
from backend.utils.http_client import HTTPClient

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 9, 26, 16, 0, 0)
RACE_ID = "202609040711"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _live_payload() -> dict:
    return _load("netkeiba_jra_odds_win.json")


def _parse_live():
    return parse_netkeiba_win_odds(_live_payload(), RACE_ID, fetched_at=FETCHED_AT)


class TestParseWinOdds:
    """Parser tests against captured api_get_jra_odds JSON."""

    def test_live_quotes_keep_source_kind_and_timestamps(self):
        snapshot = _parse_live()

        assert snapshot.status == "live"
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "post"
        assert snapshot.quotes
        assert len(snapshot.quotes) == 11
        first = snapshot.quotes[0]
        assert first.race_id == RACE_ID
        assert first.bet_type == "win"
        assert first.combination == "1"
        assert first.odds == 1.4
        assert first.source == "netkeiba_live"
        assert first.kind == "live"
        assert first.fetched_at == FETCHED_AT
        assert first.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
        assert snapshot.fetched_at == FETCHED_AT
        by_horse = {quote.combination: quote.odds for quote in snapshot.quotes}
        assert by_horse["8"] == 113.7
        assert by_horse["9"] == 8.9
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.bet_type == "win" for quote in snapshot.quotes)

    def test_combination_is_unpadded_horse_number(self):
        combinations = [quote.combination for quote in _parse_live().quotes]
        assert combinations == [str(number) for number in range(1, 12)]

    def test_empty_market_is_an_empty_list(self):
        snapshot = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_empty.json"),
            "202606040901",
            fetched_at=FETCHED_AT,
        )
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "pre"
        assert snapshot.reason == "result odds empty"

    def test_middle_with_numbers_is_live_despite_empty_reason(self):
        """In-progress markets still say reason=result odds empty."""
        snapshot = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_middle.json"),
            "202606040901",
            fetched_at=FETCHED_AT,
        )
        assert snapshot.status == "live"
        assert snapshot.reason is None
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "pre"
        assert len(snapshot.quotes) == 16
        first = snapshot.quotes[0]
        assert first.combination == "1"
        assert first.odds == 30.5
        assert first.source == "netkeiba_live"
        assert first.kind == "live"
        assert first.fetched_at == FETCHED_AT
        assert first.market_updated_at == datetime(2026, 9, 27, 0, 55, 5)
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)

    def test_yoso_status_is_not_relabeled_as_live(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        snapshot = parse_netkeiba_win_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "yoso"
        assert snapshot.race_phase == "pre"
        assert snapshot.encoding == "utf-8"
        assert snapshot.quotes
        assert {quote.kind for quote in snapshot.quotes} == {"yoso"}
        assert {quote.source for quote in snapshot.quotes} == {"netkeiba_yoso"}

    def test_placeholders_and_non_positive_odds_are_omitted(self):
        payload = _live_payload()
        payload["data"]["odds"]["1"]["12"] = ["---.-", "0.0", "12"]
        payload["data"]["odds"]["1"]["13"] = ["0.0", "0.0", "13"]
        payload["data"]["odds"]["1"]["14"] = ["", "", ""]
        snapshot = parse_netkeiba_win_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert [quote.combination for quote in snapshot.quotes] == [
            str(number) for number in range(1, 12)
        ]

    def test_unknown_status_does_not_invent_live_quotes(self):
        payload = _live_payload()
        payload["status"] = "limit"
        snapshot = parse_netkeiba_win_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase is None

    def test_missing_update_time_leaves_market_updated_at_empty(self):
        payload = _live_payload()
        del payload["data"]["official_datetime"]
        snapshot = parse_netkeiba_win_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "live"
        assert all(quote.market_updated_at is None for quote in snapshot.quotes)


class _FakeHTTP:
    def __init__(self, body: str):
        self.body = body
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append(
            {"url": url, "params": params, "headers": headers, "encoding": encoding}
        )
        return self.body

    def close(self):
        return None


class TestNetkeibaOddsScraper:
    def test_requests_uncompressed_win_odds(self):
        body = json.dumps(_live_payload())
        http = _FakeHTTP(body)
        scraper = NetkeibaOddsScraper(http_client=http)
        snapshot = scraper.get_win_odds(RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "live"
        assert http.calls == [
            {
                "url": "https://race.netkeiba.com/api/api_get_jra_odds.html",
                "params": {
                    "race_id": RACE_ID,
                    "type": "1",
                    "action": "init",
                    "compress": "0",
                },
                "headers": {
                    **NetkeibaOddsScraper.BROWSER_HEADERS,
                    "Referer": (
                        "https://race.netkeiba.com/odds/index.html"
                        f"?type=b1&race_id={RACE_ID}"
                    ),
                },
                "encoding": "utf-8",
            }
        ]
        sent = http.calls[0]["headers"]
        assert "Chrome/" in sent["User-Agent"]
        assert sent["Accept"].startswith("application/json")
        assert sent["X-Requested-With"] == "XMLHttpRequest"

    def test_empty_free_odds_keep_the_api_reason(self):
        body = (FIXTURES / "netkeiba_jra_odds_empty.json").read_text(encoding="utf-8")
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP(body))
        snapshot = scraper.get_win_odds("202606040901", fetched_at=FETCHED_AT)

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "pre"
        assert snapshot.reason == "result odds empty"


class TestOddsSessionHeaders:
    def test_browser_headers_stay_on_the_session(self):
        client = HTTPClient(headers=NetkeibaOddsScraper.BROWSER_HEADERS, min_request_interval=0)
        referer = "https://race.netkeiba.com/odds/index.html?type=b1&race_id=202606040901"
        try:
            merged = client._headers_for({"Referer": referer})
            session_headers = client._get_session().headers
        finally:
            client.close()

        assert merged["X-Requested-With"] == "XMLHttpRequest"
        assert merged["Referer"] == referer
        assert "Chrome/" in merged["User-Agent"]
        assert session_headers["Referer"] == referer
        assert session_headers["X-Requested-With"] == "XMLHttpRequest"
        assert session_headers["Accept"].startswith("application/json")

    def test_invalid_json_is_unavailable(self):
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP("not-json"))
        snapshot = scraper.get_win_odds(RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase is None
        assert snapshot.reason == "malformed odds payload"


class _FakeOddsScraper:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls: list[str] = []

    def get_win_odds(self, race_id, fetched_at=None):
        self.calls.append(race_id)
        return self.snapshot

    def close(self):
        return None


class _CardGuard:
    def get_race_card(self, race_id):
        raise AssertionError(f"odds lookup must not load the race card for {race_id}")


class TestRaceServiceOdds:
    def test_prefer_live_keeps_yoso_quotes(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        yoso = parse_netkeiba_win_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(yoso)
        service._netkeiba_scraper = _CardGuard()

        snapshot = service.get_race_odds(RACE_ID, bet_types="win", prefer="live")

        assert snapshot.status == "yoso"
        assert snapshot.prefer == "live"
        assert snapshot.quotes
        assert all(quote.kind == "yoso" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_yoso" for quote in snapshot.quotes)

    def test_non_win_bet_type_does_not_fetch(self):
        service = RaceService()
        odds = _FakeOddsScraper(_parse_live())
        service._odds_scraper = odds

        snapshot = service.get_race_odds(RACE_ID, bet_types="quinella", prefer="live")

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase is None
        assert snapshot.reason == "win odds were not requested"
        assert odds.calls == []

    def test_caller_label_is_not_stored_as_source(self):
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(_parse_live())

        snapshot = service.get_race_odds(RACE_ID, prefer="cli")

        assert snapshot.prefer == "live"
        assert {quote.source for quote in snapshot.quotes} == {"netkeiba_live"}


class TestOddsApiAndCli:
    def test_get_odds_route(self, monkeypatch):
        snapshot = _parse_live()

        def _fake(self, race_id, bet_types="win", prefer="live"):
            assert race_id == RACE_ID
            assert bet_types == "win"
            assert prefer == "live"
            return snapshot

        monkeypatch.setattr(RaceService, "get_race_odds", _fake)
        with TestClient(app) as client:
            response = client.get(
                f"/api/races/{RACE_ID}/odds",
                params={"bet_types": "win", "prefer": "live"},
            )

        assert response.status_code == 200
        body = response.json()
        quote = body["quotes"][0]
        assert body["status"] == "live"
        assert body["encoding"] == "utf-8"
        assert body["race_phase"] == "post"
        assert quote["bet_type"] == "win"
        assert quote["combination"] == "1"
        assert quote["odds"] == 1.4
        assert quote["source"] == "netkeiba_live"
        assert quote["kind"] == "live"
        assert quote["fetched_at"]
        assert quote["market_updated_at"] == "2026-09-21T15:53:44"

    def test_invalid_prefer_is_rejected(self):
        with TestClient(app) as client:
            response = client.get(
                f"/api/races/{RACE_ID}/odds",
                params={"prefer": "cli"},
            )
        assert response.status_code == 422

    def test_cli_odds_json(self, monkeypatch, capsys):
        snapshot = _parse_live()

        class _Service:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_race_odds(self, race_id, bet_types="win", prefer="live"):
                assert race_id == RACE_ID
                return snapshot

        monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
        monkeypatch.setattr(
            sys, "argv", ["backend.cli", "odds", RACE_ID, "--json", "--prefer", "live"]
        )
        cli_main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "live"
        assert payload["encoding"] == "utf-8"
        assert payload["race_phase"] == "post"
        assert payload["quotes"][0]["source"] == "netkeiba_live"
        assert payload["quotes"][0]["kind"] == "live"
        assert payload["quotes"][0]["fetched_at"].startswith("2026-09-26T16:00:00")


def test_yoso_quote_cannot_be_marked_live():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            combination="1",
            odds=2.5,
            source="netkeiba_live",
            kind="yoso",
            fetched_at=FETCHED_AT,
        )


@pytest.mark.integration
def test_fetch_live_win_odds_from_netkeiba():
    """Live check against a finished race. Final win odds stay on the API."""
    with NetkeibaOddsScraper() as scraper:
        snapshot = scraper.get_win_odds("202609040711")

    assert snapshot.status == "live"
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    by_horse = {quote.combination: quote for quote in snapshot.quotes}
    quote = by_horse["1"]
    assert quote.odds == 1.4
    assert quote.source == "netkeiba_live"
    assert quote.kind == "live"
    assert quote.bet_type == "win"
    assert quote.fetched_at is not None
    assert quote.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
