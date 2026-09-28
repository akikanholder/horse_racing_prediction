"""Unit tests for netkeiba umaren (quinella) odds quotes."""

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
    parse_netkeiba_umaren_odds,
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
    return _load("netkeiba_jra_odds_umaren.json")


def _parse_live():
    return parse_netkeiba_umaren_odds(_live_payload(), RACE_ID, fetched_at=FETCHED_AT)


def _pair_key(combination: str) -> tuple[int, int]:
    left, right = combination.split("-")
    return int(left), int(right)


class TestParseUmarenOdds:
    """Parser tests against a captured type=4 api_get_jra_odds payload."""

    def test_live_quotes_keep_single_price_source_kind_and_timestamps(self):
        snapshot = _parse_live()

        assert snapshot.status == "live"
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "post"
        assert snapshot.bet_types == ["umaren"]
        assert len(snapshot.quotes) == 55
        first = snapshot.quotes[0]
        assert first.race_id == RACE_ID
        assert first.bet_type == "umaren"
        assert first.combination == "1-2"
        assert first.odds == 10.2
        assert first.odds_min is None
        assert first.odds_max is None
        assert first.source == "netkeiba_live"
        assert first.kind == "live"
        assert first.fetched_at == FETCHED_AT
        assert first.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
        assert snapshot.fetched_at == FETCHED_AT
        by_pair = {quote.combination: quote for quote in snapshot.quotes}
        assert by_pair["3-7"].odds == 53.9
        assert by_pair["3-7"].odds_min is None
        assert by_pair["3-7"].odds_max is None
        assert by_pair["5-8"].odds == 1287.8
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.bet_type == "umaren" for quote in snapshot.quotes)
        assert all(quote.odds_min is None and quote.odds_max is None for quote in snapshot.quotes)

    def test_change_field_is_not_stored_as_a_range(self):
        payload = _live_payload()
        payload["data"]["odds"]["4"]["0102"] = ["10.2", "1.5", "5"]
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "1-2")
        assert quote.odds == 10.2
        assert quote.odds_min is None
        assert quote.odds_max is None

    def test_combination_is_unordered_lower_higher(self):
        combinations = [quote.combination for quote in _parse_live().quotes]
        assert combinations == sorted(combinations, key=_pair_key)
        assert combinations[0] == "1-2"
        assert combinations.index("1-2") < combinations.index("1-10")
        assert all(left < right for left, right in map(_pair_key, combinations))

    def test_reversed_key_is_stored_lower_higher(self):
        payload = _live_payload()
        umaren_map = payload["data"]["odds"]["4"]
        umaren_map["0703"] = umaren_map.pop("0307")
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "3-7")
        assert quote.odds == 53.9
        assert [item.combination for item in snapshot.quotes].count("3-7") == 1

    def test_ordered_key_wins_when_both_directions_exist(self):
        payload = _live_payload()
        payload["data"]["odds"]["4"]["0703"] = ["1.1", "0.0", "1"]
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        quote = next(item for item in snapshot.quotes if item.combination == "3-7")
        assert quote.odds == 53.9

    def test_empty_market_keeps_the_api_reason(self):
        snapshot = parse_netkeiba_umaren_odds(
            _load("netkeiba_jra_odds_empty.json"),
            "202606040901",
            fetched_at=FETCHED_AT,
        )
        assert snapshot.status == "unavailable"
        assert snapshot.bet_types == ["umaren"]
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase == "pre"
        assert snapshot.reason == "result odds empty"

    def test_middle_with_numbers_is_live(self):
        payload = _live_payload()
        payload["status"] = "middle"
        payload["reason"] = "result odds empty"
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "live"
        assert snapshot.reason is None
        assert snapshot.race_phase == "pre"
        assert snapshot.quotes
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)

    def test_yoso_status_is_not_relabeled_as_live(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "yoso"
        assert snapshot.race_phase == "pre"
        assert snapshot.quotes
        assert {quote.kind for quote in snapshot.quotes} == {"yoso"}
        assert {quote.source for quote in snapshot.quotes} == {"netkeiba_yoso"}

    def test_incomplete_prices_are_omitted(self):
        payload = _live_payload()
        umaren_map = payload["data"]["odds"]["4"]
        umaren_map["0102"] = ["---.-", "0.0", "5"]
        umaren_map["0103"] = ["0.0", "0.0", "3"]
        umaren_map["0104"] = []
        umaren_map["0105"] = "---"
        umaren_map["0106"] = ["---.-", "24.8", "8"]
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)

        combinations = [quote.combination for quote in snapshot.quotes]
        assert "1-2" not in combinations
        assert "1-3" not in combinations
        assert "1-4" not in combinations
        assert "1-5" not in combinations
        assert "1-6" not in combinations
        assert "1-7" in combinations
        assert len(snapshot.quotes) == 50

    def test_unknown_status_does_not_invent_quotes(self):
        payload = _live_payload()
        payload["status"] = "limit"
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.race_phase is None

    def test_missing_update_time_leaves_market_updated_at_empty(self):
        payload = _live_payload()
        del payload["data"]["official_datetime"]
        snapshot = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
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


class TestUmarenOddsScraper:
    def test_requests_uncompressed_umaren_odds(self):
        body = json.dumps(_live_payload())
        http = _FakeHTTP(body)
        scraper = NetkeibaOddsScraper(http_client=http)
        snapshot = scraper.get_umaren_odds(RACE_ID, fetched_at=FETCHED_AT)

        assert snapshot.status == "live"
        assert http.calls == [
            {
                "url": "https://race.netkeiba.com/api/api_get_jra_odds.html",
                "params": {
                    "race_id": RACE_ID,
                    "type": "4",
                    "action": "init",
                    "compress": "0",
                },
                "headers": {
                    **NetkeibaOddsScraper.BROWSER_HEADERS,
                    "Referer": (
                        f"https://race.netkeiba.com/odds/index.html?type=b4&race_id={RACE_ID}"
                    ),
                },
                "encoding": "utf-8",
            }
        ]
        sent = http.calls[0]["headers"]
        assert "Chrome/" in sent["User-Agent"]
        assert sent["Accept"].startswith("application/json")
        assert sent["X-Requested-With"] == "XMLHttpRequest"

    def test_empty_umaren_odds_keep_the_api_reason(self):
        body = (FIXTURES / "netkeiba_jra_odds_empty.json").read_text(encoding="utf-8")
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP(body))
        snapshot = scraper.get_umaren_odds("202606040901", fetched_at=FETCHED_AT)

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.reason == "result odds empty"

    def test_invalid_json_is_unavailable(self):
        scraper = NetkeibaOddsScraper(http_client=_FakeHTTP("not-json"))
        snapshot = scraper.get_umaren_odds(RACE_ID, fetched_at=FETCHED_AT)
        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.encoding == "utf-8"
        assert snapshot.race_phase is None
        assert snapshot.reason == "malformed odds payload"


class _FakeOddsScraper:
    def __init__(self, win=None, wide=None, umaren=None):
        self.win = win
        self.wide = wide
        self.umaren = umaren
        self.calls: list[tuple[str, str]] = []

    def get_win_odds(self, race_id, fetched_at=None):
        self.calls.append(("win", race_id))
        return self.win

    def get_wide_odds(self, race_id, fetched_at=None):
        self.calls.append(("wide", race_id))
        return self.wide

    def get_umaren_odds(self, race_id, fetched_at=None):
        self.calls.append(("umaren", race_id))
        return self.umaren

    def close(self):
        return None


class TestRaceServiceUmarenOdds:
    def test_prefer_live_keeps_yoso_kind(self):
        payload = _live_payload()
        payload["status"] = "yoso"
        yoso = parse_netkeiba_umaren_odds(payload, RACE_ID, fetched_at=FETCHED_AT)
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(umaren=yoso)

        snapshot = service.get_race_odds(RACE_ID, bet_types="umaren", prefer="live")

        assert snapshot.status == "yoso"
        assert snapshot.prefer == "live"
        assert snapshot.bet_types == ["umaren"]
        assert snapshot.quotes
        assert all(quote.kind == "yoso" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_yoso" for quote in snapshot.quotes)

    def test_prefer_live_unavailable_does_not_fall_back_to_yoso(self):
        empty = parse_netkeiba_umaren_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        odds = _FakeOddsScraper(umaren=empty)
        service._odds_scraper = odds

        snapshot = service.get_race_odds(RACE_ID, bet_types="umaren", prefer="live")

        assert snapshot.status == "unavailable"
        assert snapshot.prefer == "live"
        assert snapshot.quotes == []
        assert snapshot.reason == "result odds empty"
        assert odds.calls == [("umaren", RACE_ID)]

    def test_prefer_yoso_does_not_rewrite_live_kind(self):
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(umaren=_parse_live())

        snapshot = service.get_race_odds(RACE_ID, bet_types="umaren", prefer="yoso")

        assert snapshot.prefer == "yoso"
        assert snapshot.status == "live"
        assert all(quote.kind == "live" for quote in snapshot.quotes)
        assert all(quote.source == "netkeiba_live" for quote in snapshot.quotes)

    def test_win_wide_and_umaren_can_be_combined_in_request_order(self):
        win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_win.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        wide = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_wide.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        umaren = _parse_live()
        service = RaceService()
        odds = _FakeOddsScraper(win=win, wide=wide, umaren=umaren)
        service._odds_scraper = odds

        snapshot = service.get_race_odds(
            RACE_ID,
            bet_types="win,wide,umaren",
            prefer="live",
        )

        assert snapshot.bet_types == ["win", "wide", "umaren"]
        assert snapshot.status == "live"
        assert [quote.bet_type for quote in snapshot.quotes[:1]] == ["win"]
        assert snapshot.quotes[0].combination == "1"
        assert snapshot.quotes[0].odds == 1.4
        wide_quote = next(quote for quote in snapshot.quotes if quote.bet_type == "wide")
        umaren_quote = next(quote for quote in snapshot.quotes if quote.bet_type == "umaren")
        assert wide_quote.combination == "1-2"
        assert wide_quote.odds is None
        assert wide_quote.odds_min == 3.8
        assert umaren_quote.combination == "1-2"
        assert umaren_quote.odds == 10.2
        assert umaren_quote.odds_min is None
        assert odds.calls == [("win", RACE_ID), ("wide", RACE_ID), ("umaren", RACE_ID)]

    def test_partial_empty_umaren_does_not_drop_other_quotes(self):
        win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_win.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        wide = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_wide.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        empty = parse_netkeiba_umaren_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(win=win, wide=wide, umaren=empty)

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide,umaren", prefer="live")

        assert snapshot.status == "live"
        assert snapshot.reason is None
        assert snapshot.bet_types == ["win", "wide", "umaren"]
        assert {quote.bet_type for quote in snapshot.quotes} == {"win", "wide"}

    def test_all_empty_keeps_each_api_reason(self):
        empty_win = parse_netkeiba_win_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        empty_wide = parse_netkeiba_wide_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        empty_umaren = parse_netkeiba_umaren_odds(
            _load("netkeiba_jra_odds_empty.json"),
            RACE_ID,
            fetched_at=FETCHED_AT,
        )
        service = RaceService()
        service._odds_scraper = _FakeOddsScraper(
            win=empty_win,
            wide=empty_wide,
            umaren=empty_umaren,
        )

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide,umaren", prefer="live")

        assert snapshot.status == "unavailable"
        assert snapshot.quotes == []
        assert snapshot.reason == (
            "win: result odds empty; wide: result odds empty; umaren: result odds empty"
        )


class TestUmarenOddsStack:
    def test_service_fetches_type_4_and_returns_the_single_price(self):
        http = _FakeHTTP(json.dumps(_live_payload()))
        service = RaceService()
        service._odds_scraper = NetkeibaOddsScraper(http_client=http)

        snapshot = service.get_race_odds(RACE_ID, bet_types="umaren", prefer="live")

        assert http.calls[0]["params"]["type"] == "4"
        assert "type=b4" in http.calls[0]["headers"]["Referer"]
        assert snapshot.status == "live"
        assert snapshot.prefer == "live"
        assert snapshot.quotes[0].combination == "1-2"
        assert snapshot.quotes[0].odds == 10.2
        assert snapshot.quotes[0].odds_min is None
        assert snapshot.quotes[0].odds_max is None
        assert snapshot.quotes[0].source == "netkeiba_live"
        assert snapshot.quotes[0].kind == "live"
        assert snapshot.quotes[0].fetched_at is not None

    def test_service_can_fetch_win_wide_and_umaren_from_one_client(self):
        bodies = {
            "1": json.dumps(_load("netkeiba_jra_odds_win.json")),
            "5": json.dumps(_load("netkeiba_jra_odds_wide.json")),
            "4": json.dumps(_live_payload()),
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

        snapshot = service.get_race_odds(RACE_ID, bet_types="win,wide,umaren", prefer="live")

        assert http.types == ["1", "5", "4"]
        assert snapshot.bet_types == ["win", "wide", "umaren"]
        win = next(quote for quote in snapshot.quotes if quote.bet_type == "win")
        wide = next(quote for quote in snapshot.quotes if quote.bet_type == "wide")
        umaren = next(
            quote
            for quote in snapshot.quotes
            if quote.bet_type == "umaren" and quote.combination == "3-7"
        )
        assert win.odds == 1.4
        assert wide.odds_min == 3.8
        assert wide.odds_max == 4.3
        assert umaren.odds == 53.9
        assert umaren.odds_min is None
        assert umaren.odds_max is None


class TestUmarenOddsApiAndCli:
    def test_get_umaren_odds_route(self, monkeypatch):
        snapshot = _parse_live()

        def _fake(self, race_id, bet_types="win", prefer="live"):
            assert race_id == RACE_ID
            assert bet_types == "umaren"
            assert prefer == "live"
            return snapshot

        monkeypatch.setattr(RaceService, "get_race_odds", _fake)
        with TestClient(app) as client:
            response = client.get(
                f"/api/races/{RACE_ID}/odds",
                params={"bet_types": "umaren", "prefer": "live"},
            )

        assert response.status_code == 200
        body = response.json()
        quote = body["quotes"][0]
        assert body["status"] == "live"
        assert body["encoding"] == "utf-8"
        assert body["race_phase"] == "post"
        assert body["prefer"] == "live"
        assert quote["bet_type"] == "umaren"
        assert quote["combination"] == "1-2"
        assert quote["odds"] == 10.2
        assert quote["odds_min"] is None
        assert quote["odds_max"] is None
        assert quote["source"] == "netkeiba_live"
        assert quote["kind"] == "live"
        assert quote["fetched_at"]
        assert quote["market_updated_at"] == "2026-09-21T15:53:44"

    def test_multi_bet_types_are_forwarded(self, monkeypatch):
        snapshot = _parse_live()

        def _fake(self, race_id, bet_types="win", prefer="live"):
            assert bet_types == "win,wide,umaren"
            assert prefer == "live"
            return snapshot

        monkeypatch.setattr(RaceService, "get_race_odds", _fake)
        with TestClient(app) as client:
            response = client.get(
                f"/api/races/{RACE_ID}/odds",
                params={"bet_types": "win,wide,umaren"},
            )

        assert response.status_code == 200
        assert response.json()["quotes"][0]["bet_type"] == "umaren"

    def test_cli_umaren_json(self, monkeypatch, capsys):
        snapshot = _parse_live()

        class _Service:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_race_odds(self, race_id, bet_types="win", prefer="live"):
                assert race_id == RACE_ID
                assert bet_types == "umaren"
                assert prefer == "live"
                return snapshot

        monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "backend.cli",
                "odds",
                RACE_ID,
                "--json",
                "--bet-types",
                "umaren",
                "--prefer",
                "live",
            ],
        )
        cli_main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "live"
        assert payload["encoding"] == "utf-8"
        assert payload["race_phase"] == "post"
        assert payload["quotes"][0]["combination"] == "1-2"
        assert payload["quotes"][0]["odds"] == 10.2
        assert payload["quotes"][0]["odds_min"] is None
        assert payload["quotes"][0]["odds_max"] is None
        assert payload["quotes"][0]["source"] == "netkeiba_live"
        assert payload["quotes"][0]["kind"] == "live"
        assert payload["quotes"][0]["fetched_at"].startswith("2026-09-26T16:00:00")

    def test_cli_umaren_text_uses_the_single_price(self, monkeypatch, capsys):
        snapshot = _parse_live()

        class _Service:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def get_race_odds(self, race_id, bet_types="win", prefer="live"):
                return snapshot

        monkeypatch.setattr("backend.cli.RaceService", lambda: _Service())
        monkeypatch.setattr(
            sys,
            "argv",
            ["backend.cli", "odds", RACE_ID, "--bet-types", "umaren"],
        )
        cli_main()
        text = capsys.readouterr().out
        assert "馬連オッズ" in text
        assert "馬連 1-2 10.2 [netkeiba_live / live]" in text


def test_umaren_quote_requires_lower_higher():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            bet_type="umaren",
            combination="7-3",
            odds=53.9,
            source="netkeiba_live",
            kind="live",
            fetched_at=FETCHED_AT,
        )


def test_umaren_quote_rejects_a_range():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            bet_type="umaren",
            combination="3-7",
            odds=53.9,
            odds_min=53.9,
            odds_max=53.9,
            source="netkeiba_live",
            kind="live",
            fetched_at=FETCHED_AT,
        )


def test_umaren_quote_requires_odds():
    with pytest.raises(ValueError):
        OddsQuote(
            race_id=RACE_ID,
            bet_type="umaren",
            combination="3-7",
            source="netkeiba_live",
            kind="live",
            fetched_at=FETCHED_AT,
        )


@pytest.mark.integration
def test_fetch_live_umaren_odds_from_netkeiba():
    """Live check against a finished race. Final umaren odds stay on the API."""
    with NetkeibaOddsScraper() as scraper:
        snapshot = scraper.get_umaren_odds("202609040711")

    assert snapshot.status == "live"
    assert snapshot.encoding == "utf-8"
    assert snapshot.race_phase == "post"
    by_pair = {quote.combination: quote for quote in snapshot.quotes}
    quote = by_pair["3-7"]
    assert quote.odds == 53.9
    assert quote.odds_min is None
    assert quote.odds_max is None
    assert quote.source == "netkeiba_live"
    assert quote.kind == "live"
    assert quote.bet_type == "umaren"
    assert quote.fetched_at is not None
    assert quote.market_updated_at == datetime(2026, 9, 21, 15, 53, 44)
    assert by_pair["1-2"].odds == 10.2
