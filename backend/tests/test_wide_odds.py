"""Unit tests for netkeiba wide odds quotes."""

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
    parse_netkeiba_wide_odds,
    parse_netkeiba_win_odds,
)
from backend.services.race_service import RaceService

FIXTURES = Path(__file__).parent / "fixtures"
FETCHED_AT = datetime(2026, 9, 26, 16, 0, 0)
RACE_ID = "202609040711"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _live_payload() -> dict:
    return _load("netkeiba_jra_odds_wide.json")


def _parse_live():
    return parse_netkeiba_wide_odds(_live_payload(), RACE_ID, fetched_at=FETCHED_AT)


def _pair_key(combination: str) -> tuple[int, int]:
    left, right = combination.split("-")
    return int(left), int(right)


class TestParseWideOdds:
    """Parser tests against a captured type=5 api_get_jra_odds payload."""

    def test_live_quotes_keep_range_source_kind_and_timestamps(self):
        snapshot = _parse_live()

        assert snapshot.status == "live"
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "post"
        assert snapshot.bet_types == ["wide"]
        assert len(snapshot.quotes) == 55
        first = snapshot.quotes[0]
        assert first.race_id == RACE_ID
        assert first.bet_type == "wide"
        assert first.combination == "1-2"
        assert first.odds is None
        assert first.odds_min == 3.8
        assert first.odds_max == 4.3
        assert first.source == "netkeiba_live"
        assert first.kind == "live"
        assert first.fetched_at == FETCHED_AT
        assert first.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
        assert snapshot.fetched_at == FETCHED_AT
        by_pair = {quote.combination: quote for quote in snapshot.quotes}
        assert by_pair["3-7"].odds_min == 12.1
        assert by_pair["3-7"].odds_max == 14.6
        assert by_pair["1-9"].odds_min == 2.4
        assert by_pair["1-9"].odds_max == 2.6
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.bet_type == "wide" for quote in snapshot.quotes)
        assert all(quote.odds is None for quote in snapshot.quotes)

    def test_combination_is_unordered_lower_higher(self):
        combinations = [quote.combination for quote in _parse_live().quotes]
        assert combinations == sorted(combinations, key=_pair_key)
        assert combinations[0] == "1-2"
        assert combinations.index("1-2") < combinations.index("1-10")
        assert all(left < right for left, right in map(_pair_key, combinations))

    def test_reversed_key_is_stored_lower_higher(self):
        payload = _live_payload()
        wide_map = payload["data"]["odds"]["5"]
        wide_map["0703"] = wide_map.pop("0307")
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "3-7")
        assert quote.odds_min == 12.1
        assert quote.odds_max == 14.6
        assert [item.combination for item in snapshot.quotes].count("3-7") == 1

    def test_ordered_key_wins_when_both_directions_exist(self):
        payload = _live_payload()
        payload["data"]["odds"]["5"]["0703"] = ["1.1", "1.2", "1"]
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "3-7")
        assert quote.odds_min == 12.1
        assert quote.odds_max == 14.6

    def test_swapped_range_is_stored_low_to_high(self):
        payload = _live_payload()
        payload["data"]["odds"]["5"]["0307"] = ["14.6", "12.1", "15"]
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "3-7")
        assert quote.odds_min == 12.1
        assert quote.odds_max == 14.6

    def test_empty_market_keeps_the_api_reason(self):
        snapshot = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_empty.json"),
            "202606040901",
            fetched_at=FETCHED_AT,
        )
        assert snapshot.status == "unavailable"
        assert snapshot.bet_types == ["wide"]
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "pre"
        assert snapshot.reason == "result odds empty"

    def test_middle_with_numbers_is_live(self):
        payload = _live_payload()
        payload["status"] = "middle"
        payload["reason"] = "result odds empty"
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "live"
        assert snapshot.reason is None
        assert snapshot.race_phase == "pre"
        assert snapshot.quotes
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)

    def test_yoso_status_is_not_relabeled_as_live(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "yoso"
        assert snapshot.race_phase == "pre"
        assert snapshot.quotes
        assert {quote.kind for quote in snapshot.quotes} == {"yoso"}
        assert {quote.source for quote in snapshot.quotes} == {"netkeiba_yoso"}

    def test_incomplete_ranges_are_omitted(self):
        payload = _live_payload()
        wide_map = payload["data"]["odds"]["5"]
        wide_map["0102"] = ["---.-", "4.3", "5"]
        wide_map["0103"] = ["2.9", "0.0", "3"]
        wide_map["0104"] = ["2.5"]
        wide_map["0105"] = "11.2"
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        combinations = [quote.combination for quote in snapshot.quotes]
        assert "1-2" not in combinations
        assert "1-3" not in combinations
        assert "1-4" not in combinations
        assert "1-5" not in combinations
        assert "1-6" in combinations
        assert len(snapshot.quotes) == 51

    def test_unknown_status_does_not_invent_quotes(self):
        payload = _live_payload()
        payload["status"] = "limit"
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.race_phase is None

    def test_missing_update_time_leaves_market_updated_at_empty(self):
        payload = _live_payload()
        del payload["data"]["official_datetime"]
        snapshot = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "live"
        assert all(quote.market_updated_at is None for quote in snapshot.quotes)


class _FakeHTTP:
    def __init__(self, body: str):
        self.body = body
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, encoding=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "encoding": encoding})
        return self.body

    def close(self):
        return None


class TestWideOddsScraper:
    def test_requests_uncompressed_wide_odds(self):
        body = json.dumps(_live_payload())
        http = _FakeHTTP(body)
        scraper = NetkeibaOddsScraper(http_client=http)
        snapshot = scraper.get_wide_odds(RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "live"
        assert http.calls == [
            {
                "url": "https://race.netkeiba.com/api/api_get_jra_odds.html",
                "params": {
                    "race_id": RACE_ID,
                    "type": "5",
                    "action": "init",
                    "compress": "0",
                },
                "headers": {
                    **NetkeibaOddsScraper.BROWSER_HEADERS,
                    "Referer": (
                        f"https://race.netkeiba.com/odds/index.html?type=b5&race_id={RACE_ID}"
                    ),
                },
                "encoding": "utf-8",
            }
        ]
        sent = http.calls[0]["headers"]
        assert "Chrome/" in sent["User-Agent"]
        assert sent["Accept"].startswith("application/json")
        assert sent["X-Requested-With"] == "XMLHttpRequest"

    def test_empty_wide_odds_keep_the_api_reason(self):
        body = (FIXTURES / "netkeiba_jra_odds_empty.json").read_text(encoding="utf-8")
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP(body))
        snapshot = scraper.get_wide_odds("202606040901", fetched_at=FETCHED_AT)

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.reason == "result odds empty"

    def test_invalid_json_is_unavailable(self):
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP("not-json"))
        snapshot = scraper.get_wide_odds(RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase is None
        assert snapshot.reason == "malformed odds payload"


class _FakeOddsScraper:
    def __init__(self, win=None, wide=None):
        self.win = win
        self.wide = wide
        self.calls: list[tuple[str, str]] = []

    def get_win_odds(self, race_id, fetched_at=None):
        self.calls.append(("win", race_id))
        return self.win

    def get_wide_odds(self, race_id, fetched_at=None):
        self.calls.append(("wide", race_id))
        return self.wide

    def close(self):
        return None


class TestRaceServiceWideOdds:
    def test_prefer_live_keeps_yoso_kind(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        yoso = parse_netkeiba_wide_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(wide=yoso)

        snapshot = service.get_race_odds(RACE_ID, bet_types="wide", prefer="live")

        assert snapshot.status == "yoso"
        assert snapshot.prefer == "live"
        assert snapshot.bet_types == ["wide"]
        assert snapshot.quotes
        assert all(quote.kind == "yoso" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_yoso" for quote in snapshot.quotes)

    def test_prefer_live_unavailable_does_not_fall_back_to_yoso(self):
        empty = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        odds = _FakeOddsScraper(wide=empty)
        service._odds_scraper = odds

        snapshot = service.get_race_odds(RACE_ID, bet_types="wide", prefer="live")

        assert snapshot.status == "unavailable"
        assert snapshot.prefer == "live"
        assert snapshot.quotes == []
        assert snapshot.reason == "result odds empty"
        assert odds.calls == [("wide", RACE_ID)]

    def test_prefer_yoso_does_not_rewrite_live_kind(self):
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(wide=_parse_live())

        snapshot = service.get_race_odds(RACE_ID, bet_types="wide", prefer="yoso")

        assert snapshot.prefer == "yoso"
        assert snapshot.status == "live"
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)

    def test_win_and_wide_can_be_combined_in_request_order(self):
        win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_win.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        wide = _parse_live()
        service = RaceService()
        odds = _FakeOddsScraper(win=win, wide=wide)
        service._odds_scraper = odds

        snapshot = service.get_race_odds(RACE_ID, bet_types="wide,win", prefer="live")

        assert snapshot.bet_types == ["wide", "win"]
        assert snapshot.status == "live"
        assert snapshot.quotes[0].bet_type == "wide"
        assert snapshot.quotes[0].combination == "1-2"
        assert snapshot.quotes[-1].bet_type == "win"
        assert odds.calls == [("wide", RACE_ID), ("win", RACE_ID)]
        assert {quote.bet_type for quote in snapshot.quotes} == {"win", "wide"}

    def test_partial_empty_wide_does_not_drop_win_quotes(self):
        win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_win.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        empty = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(win=win, wide=empty)

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide", prefer="live")

        assert snapshot.status == "live"
        assert snapshot.reason is None
        assert snapshot.bet_types == ["win", "wide"]
        assert snapshot.quotes
        assert {quote.bet_type for quote in snapshot.quotes} == {"win"}

    def test_both_empty_keeps_each_api_reason(self):
        empty_wide = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        empty_win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(win=empty_win, wide=empty_wide)

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide", prefer="live")

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.reason == "win: result odds empty; wide: result odds empty"


class TestWideOddsStack:
    def test_service_fetches_type_5_and_returns_the_range(self):
        http = _FakeHTTP(json.dumps(_live_payload()))
        service = RaceService()
        service._odds_scraper = NetkeibaOddsScraper(http_client=http)

        snapshot = service.get_race_odds(RACE_ID, bet_types="wide", prefer="live")

        assert http.calls[0]["params"]["type"] == "5"
        assert snapshot.status == "live"
        assert snapshot.prefer == "live"
        assert snapshot.quotes[0].combination == "1-2"
        assert snapshot.quotes[0].odds_min == 3.8
        assert snapshot.quotes[0].odds_max == 4.3
        assert snapshot.quotes[0].source == "netkeiba_live"
        assert snapshot.quotes[0].kind == "live"
        assert snapshot.quotes[0].fetched_at is not None

    def test_service_can_fetch_win_and_wide_from_one_client(self):
        bodies = {
            "1": json.dumps(_load("netkeiba_jra_odds_win.json")),
            "5": json.dumps(_live_payload()),
        }

        class _RoutingHTTP:
            def __init__(self):
                self.types: list[str] = []

            def get(self, url, params=None, headers=None, encoding=None):
                self.types.append(params["type"])
                return bodies[params["type"]]

            def close(self):
                return None

        http = _RoutingHTTP()
        service = RaceService()
        service._odds_scraper = NetkeibaOddsScraper(http_client=http)

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide", prefer="live")

        assert http.types == ["1", "5"]
        assert snapshot.bet_types == ["win", "wide"]
        win = next(quote for quote in snapshot.quotes if quote.bet_type == "win")
        wide = next(quote for quote in snapshot.quotes if quote.bet_type == "wide")
        assert win.combination == "1"
        assert win.odds == 1.4
        assert win.odds_min is None
        assert wide.combination == "1-2"
        assert wide.odds is None
        assert wide.odds_min == 3.8
        assert wide.odds_max == 4.3


class TestWideOddsApiAndCli:
    def test_get_wide_odds_route(self, monkeypatch):
        snapshot = _parse_live()

        def _fake(self, race_id, bet_types="win", prefer="live"):
            assert race_id == RACE_ID
            assert bet_types == "wide"
            assert prefer == "live"
            return snapshot

        monkeypatch.setattr(RaceService, "get_race_odds", _fake)
        with TestClient(app) as client:
            response = client.get(
                f"/api/races/{RACE_ID}/odds",
                params={"bet_types": "wide", "prefer": "live"},
            )

        assert response.status_code == 200
        body = response.json()
        quote = body["quotes"][0]
        assert body["status"] == "live"
        assert body["encoding"] == "utf-8"
        assert body["race_phase"] == "post"
        assert body["prefer"] == "live"
        assert quote["bet_type"] == "wide"
        assert quote["combination"] == "1-2"
        assert quote["odds"] is None
        assert quote["odds_min"] == 3.8
        assert quote["odds_max"] == 4.3
        assert quote["source"] == "netkeiba_live"
        assert quote["kind"] == "live"
        assert quote["fetched_at"]
        assert quote["market_updated_at"] == "2026-09-21T15:53:44"

    def test_cli_wide_json(self, monkeypatch, capsys):
        snapshot = _parse_live()

        class _Service:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_race_odds(self, race_id, bet_types="win", prefer="live"):
                assert race_id == RACE_ID
                assert bet_types == "wide"
                assert prefer == "live"
                return snapshot

        monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
        monkeypatch.setattr(
            sys,
            "argv",
            ["backend.cli", "odds", RACE_ID, "--json", "--bet-types", "wide", "--prefer", "live"],
        )
        cli_main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "live"
        assert payload["encoding"] == "utf-8"
        assert payload["race_phase"] == "post"
        assert payload["quotes"][0]["combination"] == "1-2"
        assert payload["quotes"][0]["odds_min"] == 3.8
        assert payload["quotes"][0]["odds_max"] == 4.3
        assert payload["quotes"][0]["source"] == "netkeiba_live"
        assert payload["quotes"][0]["kind"] == "live"
        assert payload["quotes"][0]["fetched_at"].startswith("2026-09-26T16:00:00")


def test_wide_quote_requires_a_range():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            bet_type="wide",
            combination="7-3",
            odds_min=1.2,
            odds_max=1.5,
            source="netkeiba_live",
            kind="live",
            fetched_at=FETCHED_AT,
        )


def test_wide_quote_rejects_a_single_price():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            bet_type="wide",
            combination="3-7",
            odds=1.4,
            odds_min=1.2,
            odds_max=1.5,
            source="netkeiba_live",
            kind="live",
            fetched_at=FETCHED_AT,
        )


@pytest.mark.integration
def test_fetch_live_wide_odds_from_netkeiba():
    """Live check against a finished race. Final wide odds stay on the API."""
    with NetkeibaOddsScraper() as scraper:
        snapshot = scraper.get_wide_odds("202609040711")

    assert snapshot.status == "live"
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    by_pair = {quote.combination: quote for quote in snapshot.quotes}
    quote = by_pair["3-7"]
    assert quote.odds_min == 12.1
    assert quote.odds_max == 14.6
    assert quote.odds is None
    assert quote.source == "netkeiba_live"
    assert quote.kind == "live"
    assert quote.bet_type == "wide"
    assert quote.fetched_at is not None
    assert quote.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
    assert by_pair["1-2"].odds_min == 3.8
    assert by_pair["1-2"].odds_max == 4.3
