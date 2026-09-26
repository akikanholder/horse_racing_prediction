"""Tests for JRA accessD race cards and merged race details."""

from datetime import date

from backend.models.race import Race, RaceCard, RaceEntry, RaceResult
from backend.scrapers.jra_scraper import JRAScraper
from backend.services.race_service import RaceService

INDEX_HTML = """
<html><body>
<a onclick="doAction('/JRADB/accessD.html','pw01dli00/F3');">出馬表</a>
<a onclick="doAction('/JRADB/accessD.html','pw01drl00062026040820260926/27');">4回中山8日</a>
<a onclick="doAction('/JRADB/accessD.html','pw01drl00092026040820260926/05');">4回阪神8日</a>
</body></html>
"""

LIST_HTML = """
<html><body>
<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604080120260926/D8">1R</a>
<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604081120260926%2F2A">11R</a>
<a href="/JRADB/accessS.html?CNAME=pw01sde0106202604081120260926/E6">結果</a>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<div class="race_header">
  <div class="cell date">2026年9月26日（土曜） 4回中山8日</div>
  <div class="cell time">発走時刻：<strong>15時30分</strong></div>
  <div class="race_number"><img alt="11レース" src="/race_num_11.png"></div>
  <span class="race_name">秋風ステークス</span>
  <span class="grade_icon no_grade"></span>
  <span class="grade">GIII</span>
  <div class="type">
    <div class="cell category">3歳以上</div>
    <div class="cell class">3勝クラス</div>
    <div class="cell rule">（混合）（特指）</div>
    <div class="cell weight">定量</div>
    <div class="cell course">
      コース：1,600<span class="unit">メートル</span>
      <span class="detail">（芝・右 外）</span>
    </div>
  </div>
  <ul class="prize">
    <li><div class="prize_unit">
      <div class="cell cap">本賞金<span class="unit">（万円）</span></div>
      <div class="cell data"><ol>
        <li><span class="cap">1着</span><span class="num">1,870</span></li>
      </ol></div>
    </div></li>
    <li><div class="prize_unit">
      <div class="cell cap">付加賞<span class="unit">（万円）</span></div>
      <div class="cell data"><ol>
        <li><span class="cap">1着</span><span class="num">30.8</span></li>
      </ol></div>
    </div></li>
  </ul>
</div>
<table class="basic">
  <tr>
    <td class="waku"><img alt="枠1白" src="/waku/1.png"></td>
    <td class="num">1</td>
    <td class="horse">
      <div class="name_line">
        <div class="name"><a href="/JRADB/accessU.html?CNAME=pw01dud1/AA">エフォートレス</a></div>
        <div class="odds"><div class="odds_line">
          <span class="num"><strong>11.8</strong></span>
          <span class="pop_rank">(6<span>番人気</span>)</span>
        </div></div>
      </div>
      <div class="result_line"><div class="cell weight">480kg<span class="transition">(-4)</span></div></div>
      <p class="owner">ライオンレースホース(株)</p>
      <p class="trainer"><a href="#">嘉藤 貴行</a><span class="division">(美浦)</span></p>
    </td>
    <td class="jockey">
      <p class="age">牡5/栗</p>
      <p class="weight">58.0 kg</p>
      <p class="jockey">横山 和生</p>
    </td>
  </tr>
  <tr>
    <td class="waku"><img alt="枠2黒" src="/waku/2.png"></td>
    <td class="num">2</td>
    <td class="horse">
      <div class="name"><a>サウンドムーブ</a></div>
      <div class="odds"><span class="num"><strong>3.4</strong></span>
        <span class="pop_rank">(1番人気)</span></div>
      <div class="cell weight">448kg<span class="transition">(-6)</span></div>
      <p class="owner">増田 雄一</p>
      <p class="trainer"><a href="#">斉藤 崇史</a><span class="division">(栗東)</span></p>
    </td>
    <td class="jockey">
      <p class="age">牡3/鹿</p>
      <p class="weight">56.0 kg</p>
      <p class="jockey">津村 明秀</p>
    </td>
  </tr>
</table>
</body></html>
"""

PAGES = {
    "pw01dli00/F3": INDEX_HTML,
    "pw01drl00062026040820260926/27": LIST_HTML,
    "pw01dde0106202604081120260926/2A": DETAIL_HTML,
}


def _stub_post(scraper: JRAScraper) -> list[str]:
    calls: list[str] = []

    def fake_post(url, data=None, headers=None, encoding=None):
        assert url == JRAScraper.ACCESS_D_URL
        assert encoding == "cp932"
        cname = data["cname"]
        calls.append(cname)
        return PAGES[cname]

    scraper.http_client.post = fake_post
    return calls


def test_project_and_netkeiba_ids_select_the_same_card():
    scraper = JRAScraper()
    calls = _stub_post(scraper)

    project_card = scraper.get_race_card("202609260611")
    netkeiba_card = scraper.get_race_card("202606040811")

    assert calls == [
        "pw01dli00/F3",
        "pw01drl00062026040820260926/27",
        "pw01dde0106202604081120260926/2A",
    ]
    assert "pw01drl00092026040820260926/05" not in calls
    assert project_card is not None
    assert netkeiba_card is not None
    assert project_card.race.race_id == "202609260611"
    assert netkeiba_card.race.race_id == "202606040811"


def test_shutuba_page_fills_race_and_entries():
    scraper = JRAScraper()
    cname = scraper._parse_race_cname("pw01dde0106202604081120260926/2A")
    assert cname is not None
    card = scraper._parse_race_card(DETAIL_HTML, "202609260611", cname)

    assert card is not None
    race = card.race
    assert race.race_name == "秋風ステークス"
    assert race.race_number == 11
    assert race.race_date == date(2026, 9, 26)
    assert race.venue == "中山"
    assert race.round_number == 4
    assert race.day_number == 8
    assert race.post_time == "15:30"
    assert race.distance == 1600
    assert race.track_type == "芝"
    assert race.direction == "右"
    assert race.race_class == "3勝クラス"
    assert race.race_grade == "GIII"
    assert race.age_condition == "3歳以上"
    assert race.sex_condition == "混合"
    assert race.weight_condition == "定量"
    assert race.prize_money == 1870
    assert race.entry_count == 2

    first, second = card.entries
    assert first.bracket_number == 1
    assert first.horse_number == 1
    assert first.horse_name == "エフォートレス"
    assert first.sex == "牡"
    assert first.age == 5
    assert first.weight == 58.0
    assert first.jockey_name == "横山 和生"
    assert first.trainer_name == "嘉藤 貴行"
    assert first.owner_name == "ライオンレースホース(株)"
    assert first.horse_weight == 480
    assert first.horse_weight_diff == -4
    assert first.odds == 11.8
    assert first.popularity == 6
    assert second.horse_name == "サウンドムーブ"
    assert second.popularity == 1
    assert second.horse_weight_diff == -6


def test_race_token_on_the_index_skips_meeting_pages():
    scraper = JRAScraper()
    calls: list[str] = []
    direct_index = INDEX_HTML.replace(
        "</body>",
        '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604081120260926/2A">11R</a></body>',
    )
    pages = {**PAGES, "pw01dli00/F3": direct_index}

    def fake_post(url, data=None, headers=None, encoding=None):
        cname = data["cname"]
        calls.append(cname)
        return pages[cname]

    scraper.http_client.post = fake_post
    card = scraper.get_race_card("202609260611")

    assert calls == [
        "pw01dli00/F3",
        "pw01dde0106202604081120260926/2A",
    ]
    assert card is not None
    assert card.race.race_name == "秋風ステークス"


def test_missing_race_does_not_open_other_meetings():
    scraper = JRAScraper()
    calls = _stub_post(scraper)

    assert scraper.get_race_card("202609260612") is None
    assert calls == [
        "pw01dli00/F3",
        "pw01drl00062026040820260926/27",
    ]


def test_unmapped_race_id_does_not_request_access_d():
    scraper = JRAScraper()
    calls = _stub_post(scraper)

    assert scraper.get_race_card("not-a-race") is None
    assert calls == []


def _race(**updates) -> Race:
    values = {
        "race_id": "202609260611",
        "race_name": "秋風",
        "race_number": 11,
        "race_date": date(2026, 9, 26),
        "venue": "中山",
        "distance": 1600,
        "track_type": "芝",
    }
    values.update(updates)
    return Race(**values)


def _entry(**updates) -> RaceEntry:
    values = {
        "bracket_number": 1,
        "horse_number": 1,
        "horse_name": "エフォートレス",
    }
    values.update(updates)
    return RaceEntry(**values)


def test_race_service_merges_cards_and_keeps_netkeiba_ids():
    netkeiba_card = RaceCard(
        race=_race(),
        entries=[_entry(horse_id="123", jockey_id="9", odds=None, owner_name=None)],
    )
    jra_card = RaceCard(
        race=_race(
            race_name="秋風ステークス",
            post_time="15:30",
            prize_money=1870,
            weight_condition="定量",
        ),
        entries=[
            _entry(horse_id="jra-horse", odds=11.8, owner_name="ライオン", popularity=6),
            _entry(bracket_number=2, horse_number=2, horse_name="サウンドムーブ"),
        ],
    )

    class Netkeiba:
        def get_race_card(self, race_id: str) -> RaceCard:
            assert race_id == "202609260611"
            return netkeiba_card

        def get_race_result(self, race_id: str) -> list[RaceResult]:
            return [
                RaceResult(
                    rank=1,
                    bracket_number=1,
                    horse_number=1,
                    horse_name="エフォートレス",
                )
            ]

    class Jra:
        def get_race_card(self, race_id: str) -> RaceCard:
            return jra_card

    service = RaceService()
    service._netkeiba_scraper = Netkeiba()
    service._jra_scraper = Jra()

    card = service.get_race_card("202609260611")
    assert card is not None
    assert card.race.race_name == "秋風ステークス"
    assert card.race.post_time == "15:30"
    assert card.race.prize_money == 1870
    assert card.race.weight_condition == "定量"
    assert [entry.horse_number for entry in card.entries] == [1, 2]
    assert card.entries[0].horse_id == "123"
    assert card.entries[0].jockey_id == "9"
    assert card.entries[0].odds == 11.8
    assert card.entries[0].owner_name == "ライオン"
    assert card.entries[1].horse_name == "サウンドムーブ"

    result = service.get_race_result("202609260611")
    assert result is not None
    assert result.race.prize_money == 1870
    assert result.results[0].rank == 1


def test_race_service_uses_the_source_that_succeeds():
    jra_card = RaceCard(race=_race(race_name="秋風ステークス"), entries=[_entry()])

    class NetkeibaDown:
        def get_race_card(self, race_id: str) -> RaceCard:
            raise RuntimeError("netkeiba down")

    class Jra:
        def get_race_card(self, race_id: str) -> RaceCard:
            return jra_card

    service = RaceService()
    service._netkeiba_scraper = NetkeibaDown()
    service._jra_scraper = Jra()
    card = service.get_race_card("202609260611")
    assert card is not None
    assert card.race.race_name == "秋風ステークス"

    class NetkeibaOnly:
        def get_race_card(self, race_id: str) -> RaceCard:
            return RaceCard(race=_race(post_time="15:30"), entries=[_entry(horse_id="123")])

    class JraDown:
        def get_race_card(self, race_id: str) -> RaceCard:
            raise RuntimeError("jra down")

    service._netkeiba_scraper = NetkeibaOnly()
    service._jra_scraper = JraDown()
    card = service.get_race_card("202609260611")
    assert card is not None
    assert card.entries[0].horse_id == "123"
    assert card.race.post_time == "15:30"
