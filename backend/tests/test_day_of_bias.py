"""Day-of bias is copied only from labeled official cells. Notes stay null."""

from datetime import date
from pathlib import Path

import pytest
import requests

from backend.models.race import Race, RaceCard, RaceEntry
from backend.scrapers.day_of_bias import (
    TRACK_BIAS_ACCESSD_403,
    TRACK_BIAS_ACCESSD_EMPTY_BABA_MISSING,
    TRACK_BIAS_DISAGREE,
    TRACK_BIAS_FETCH_FAILED,
    TRACK_BIAS_INSIDE_NOT_PUBLISHED,
    TRACK_BIAS_NOT_PUBLISHED,
    TRACK_BIAS_PACE_NOT_PUBLISHED,
    BiasLabels,
    DayOfBiasClient,
    assemble_publication,
    parse_bias_labels,
)
from backend.scrapers.jra_scraper import JRAScraper
from backend.services.race_service import RaceService
from backend.utils.http_client import HTTPClient

FIXTURES = Path(__file__).parent / "fixtures"
INDEX = "pw01dli00/F3"
MEETING = "pw01drl10062026040820260926/0E"
CARD = "pw01dde1006202604081120260926/09"
BABA_TOKYO = "https://www.jra.go.jp/keiba/baba/index.html"
BABA_KYOTO = "https://www.jra.go.jp/keiba/baba/index2.html"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _race(**updates) -> Race:
    values = {
        "race_id": "202606040811",
        "race_name": "秋風ステークス",
        "race_number": 11,
        "race_date": date(2026, 9, 26),
        "venue": "中山",
        "distance": 1600,
        "track_type": "芝",
        "track_condition": "稍重",
    }
    values.update(updates)
    return Race(**values)


class _Pages:
    """POST accessD by cname and GET baba tabs. No network."""

    def __init__(self, posts: dict, gets: dict | None = None, errors: dict | None = None):
        self.posts = posts
        self.gets = gets or {}
        self.errors = errors or {}

    def post(self, url, data=None, headers=None, encoding=None):
        cname = (data or {})["cname"]
        if cname in self.errors:
            raise self.errors[cname]
        return self.posts[cname]

    def get(self, url, params=None, headers=None, encoding=None):
        if url in self.errors:
            raise self.errors[url]
        return self.gets[url]


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    error = requests.HTTPError(f"http {status}")
    error.response = response
    return error


def _menu(card_html: str) -> dict[str, str]:
    return {
        INDEX: f"<a onclick=\"doAction('/JRADB/accessD.html','{MEETING}');\">4回中山8日</a>",
        MEETING: f'<a href="/JRADB/accessD.html?CNAME={CARD}">11R</a>',
        CARD: card_html,
    }


def _tokyo_page(body: str) -> str:
    return f"<html><head><title>馬場情報（東京競馬場）　JRA</title></head><body>{body}</body></html>"


def _kyoto_page() -> str:
    return "<html><head><title>馬場情報（京都競馬場）　JRA</title></head><body></body></html>"


def test_captured_accessd_header_does_not_treat_outer_course_as_bias():
    labels = parse_bias_labels(_load("jra_accessd_shutuba_header.html"))
    assert "外" in _load("jra_accessd_shutuba_header.html")
    assert labels.inside_outside is None
    assert labels.pace_bias is None


def test_captured_baba_page_does_not_turn_rail_position_into_bias():
    html = _load("jra_baba_tokyo_20260928.html")
    assert "内柵を最内に設置" in html
    labels = parse_bias_labels(html)
    assert labels.inside_outside is None
    assert labels.pace_bias is None


def test_grass_damage_note_is_not_an_inside_bias():
    html = _load("jra_baba_grass_note.html")
    assert "傷み" in html
    labels = parse_bias_labels(html)
    assert labels.inside_outside is None
    assert labels.pace_bias is None


def test_labeled_baba_units_are_copied_verbatim():
    labels = parse_bias_labels(_load("jra_baba_labeled_units.html"))
    assert labels.inside_outside == "内有利"
    assert labels.pace_bias == "先行有利"


def test_table_row_labels_are_copied_and_odds_rows_are_not():
    html = """
    <table>
      <tr><th>内外バイアス</th><td>外有利</td></tr>
      <tr><th>展開バイアス</th><td>差し有利</td></tr>
      <tr><th>単勝</th><td>3.4</td></tr>
      <tr><th>人気</th><td>1番人気</td></tr>
    </table>
    """
    labels = parse_bias_labels(html)
    assert labels.inside_outside == "外有利"
    assert labels.pace_bias == "差し有利"


def test_conflicting_labels_on_one_page_are_dropped():
    html = """
    <div class="data_list_unit"><div class="head"><h3>内外</h3></div>
    <div class="content"><p>内有利</p></div></div>
    <div class="data_list_unit"><div class="head"><h3>内外</h3></div>
    <div class="content"><p>外有利</p></div></div>
    """
    assert parse_bias_labels(html).inside_outside is None


def test_sources_that_disagree_stay_null():
    publication = assemble_publication(
        BiasLabels(inside_outside="内有利", pace_bias="先行有利"),
        "ok",
        BiasLabels(inside_outside="外有利", pace_bias="先行有利"),
        "ok",
    )
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.warning == TRACK_BIAS_DISAGREE
    assert publication.data_sufficiency == "unavailable"


def test_partial_label_is_thin():
    publication = assemble_publication(
        BiasLabels(inside_outside="内"),
        "ok",
        BiasLabels(),
        "ok",
    )
    assert publication.inside_outside == "内"
    assert publication.pace_bias is None
    assert publication.source == "accessd"
    assert publication.warning == TRACK_BIAS_PACE_NOT_PUBLISHED
    assert publication.data_sufficiency == "thin"

    pace_only = assemble_publication(
        BiasLabels(),
        "ok",
        BiasLabels(pace_bias="ハイ"),
        "ok",
    )
    assert pace_only.pace_bias == "ハイ"
    assert pace_only.source == "jra_baba"
    assert pace_only.warning == TRACK_BIAS_INSIDE_NOT_PUBLISHED
    assert pace_only.data_sufficiency == "thin"


def test_both_pages_with_the_same_label_name_both_sources():
    publication = assemble_publication(
        BiasLabels(inside_outside="内有利", pace_bias="先行有利"),
        "ok",
        BiasLabels(inside_outside="内有利", pace_bias="先行有利"),
        "ok",
    )
    assert publication.source == "accessd+jra_baba"
    assert publication.warning is None
    assert publication.data_sufficiency == "sufficient"


def test_checked_pages_without_labels_stay_null():
    publication = assemble_publication(BiasLabels(), "ok", BiasLabels(), "ok")
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.warning == TRACK_BIAS_NOT_PUBLISHED
    assert publication.data_sufficiency == "unavailable"


def test_client_reads_accessd_and_leaves_outer_course_null():
    pages = _Pages(
        _menu(_load("jra_accessd_shutuba_header.html")),
        {
            BABA_TOKYO: _load("jra_baba_tokyo_20260928.html"),
            BABA_KYOTO: _kyoto_page(),
        },
    )
    client = DayOfBiasClient(JRAScraper(http_client=pages))  # type: ignore[arg-type]
    publication = client.publication_for(_race())
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.warning == TRACK_BIAS_ACCESSD_EMPTY_BABA_MISSING
    assert publication.data_sufficiency == "unavailable"


def test_captured_tokyo_baba_and_accessd_card_publish_no_bias_labels():
    pages = _Pages(
        _menu(_load("jra_accessd_shutuba_header.html")),
        {
            BABA_TOKYO: _tokyo_page(_load("jra_baba_tokyo_20260928.html")),
            BABA_KYOTO: _kyoto_page(),
        },
    )
    client = DayOfBiasClient(JRAScraper(http_client=pages))  # type: ignore[arg-type]
    publication = client.publication_for(_race(venue="東京"))
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.warning == TRACK_BIAS_NOT_PUBLISHED
    assert publication.data_sufficiency == "unavailable"


def test_client_copies_baba_labels_for_the_matching_venue():
    pages = _Pages(
        _menu(_load("jra_accessd_shutuba_header.html")),
        {
            BABA_TOKYO: _tokyo_page(_load("jra_baba_labeled_units.html")),
            BABA_KYOTO: _kyoto_page(),
        },
    )
    client = DayOfBiasClient(JRAScraper(http_client=pages))  # type: ignore[arg-type]
    publication = client.publication_for(_race(venue="東京"))
    assert publication.inside_outside == "内有利"
    assert publication.pace_bias == "先行有利"
    assert publication.source == "jra_baba"
    assert publication.warning is None
    assert publication.data_sufficiency == "sufficient"


def test_accessd_403_names_the_block_and_does_not_invent_bias():
    pages = _Pages({}, errors={INDEX: _http_error(403)})
    pages.gets = {BABA_TOKYO: _kyoto_page(), BABA_KYOTO: _kyoto_page()}
    client = DayOfBiasClient(JRAScraper(http_client=pages))  # type: ignore[arg-type]
    publication = client.publication_for(_race())
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.warning is not None
    assert publication.warning.startswith(TRACK_BIAS_ACCESSD_403)
    assert "内有利" not in publication.warning


def test_service_keeps_race_conditions_when_bias_labels_are_published():
    pages = _Pages(
        _menu(_load("jra_baba_labeled_units.html")),
        {BABA_TOKYO: _kyoto_page(), BABA_KYOTO: _kyoto_page()},
    )

    class Netkeiba:
        def get_race_card(self, race_id: str) -> RaceCard:
            return RaceCard(
                race=_race(race_id=race_id),
                entries=[
                    RaceEntry(
                        bracket_number=1,
                        horse_number=1,
                        horse_name="エフォートレス",
                        horse_id="h1",
                    )
                ],
            )

    class Jra:
        def get_race_card(self, race_id: str):
            return None

    service = RaceService()
    service._netkeiba_scraper = Netkeiba()  # type: ignore[assignment]
    service._jra_scraper = Jra()  # type: ignore[assignment]
    service._day_of_bias = DayOfBiasClient(JRAScraper(http_client=pages))  # type: ignore[arg-type]
    card = service.get_race_card("202606040811")
    assert card is not None
    bias = card.race.track_bias
    assert bias is not None
    assert bias.venue == "中山"
    assert bias.surface == "芝"
    assert bias.distance == 1600
    assert bias.going == "稍重"
    assert bias.inside_outside == "内有利"
    assert bias.pace_bias == "先行有利"
    assert bias.source == "accessd"
    assert bias.data_sufficiency == "sufficient"
    assert bias.warning is None


def test_fetch_failure_constant_does_not_fill_labels():
    publication = assemble_publication(BiasLabels(), "fetch_failed", BiasLabels(), "fetch_failed")
    assert publication.warning == TRACK_BIAS_FETCH_FAILED
    assert publication.inside_outside is None
    assert publication.source is None


@pytest.mark.integration
def test_live_accessd_card_does_not_invent_day_of_bias():
    scraper = JRAScraper(http_client=HTTPClient(min_request_interval=0.2))
    client = DayOfBiasClient(scraper)
    publication = client.publication_for(_race())
    assert publication.inside_outside is None
    assert publication.pace_bias is None
    assert publication.source is None
    assert publication.data_sufficiency == "unavailable"
    assert publication.warning
    assert "内有利" not in publication.warning
    assert "先行" not in publication.warning
