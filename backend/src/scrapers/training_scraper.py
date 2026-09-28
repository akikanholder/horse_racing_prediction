"""Training clocks from 競馬ブック and speedkeiba.

競馬ブックの調教は ``https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}``。
列名（5F、半哩、坂路の 4F）が HTML にある。Smart の ``s.keibabook.co.jp`` は
同じ表だが列名が無いので、位置からハロンは作らない。

匿名の応答に載っている馬だけを埋める。載っていない馬は null と、
ログインが要るという warning。馬個別の調教履歴 ``/db/uma/{umacd}/crireki`` は
ログインしたセッションでのみ読む。``umacd`` は出馬表の馬名リンク、または
馬検索の結果行 / 馬ページに印刷された生年月日が netkeiba と一致するときの
リンクから取る。netkeiba の馬IDからは作らない。

環境変数が無いときはログインしない。ログインチェックが「ログインしていない」
のままなら、匿名の結果を維持し、成功したことにはしない。

speedkeiba は今週の重賞記事。時計の行と、ページが印刷した ``(平均)`` と
``今走`` のラップだけを使う。``好調`` と ``凡走`` はページ自身が
人気・着順・着差から作った行なのでコピーしない。
"""

import logging
import os
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

from backend.models.training import (
    SOURCE_KEIBABOOK_CRIREKI,
    SOURCE_KEIBABOOK_SMART,
    SOURCE_SPEEDKEIBA,
    TRAINING_AMBIGUOUS,
    TRAINING_FETCH_FAILED,
    TRAINING_HISTORY_EMPTY,
    TRAINING_HISTORY_REQUIRES_LOGIN,
    TRAINING_HORSE_UNAVAILABLE,
    TRAINING_LOGIN_FAILED,
    TRAINING_NOT_ON_EITHER,
    TRAINING_NOT_ON_KEIBABOOK,
    TRAINING_TABLE_MALFORMED,
    TRAINING_TABLE_NOT_FOUND,
    TRAINING_UMACD_UNMAPPED,
    PublishedAverage,
    Training,
    TrainingLap,
    TrainingSplit,
    TrainingSummary,
    Workout,
    build_training,
    unavailable_training,
)
from backend.scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

KEIBABOOK_ORIGIN = "https://p.keibabook.co.jp"
KEIBABOOK_CYOKYO = "https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}"
KEIBABOOK_SYUTUBA = "https://p.keibabook.co.jp/cyuou/syutuba/{race_id}"
KEIBABOOK_CRIREKI = "https://p.keibabook.co.jp/db/uma/{umacd}/crireki"
KEIBABOOK_LOGIN = "https://p.keibabook.co.jp/login/login"
KEIBABOOK_LOGINCHECK = "https://p.keibabook.co.jp/logincheck/logincheck"
KEIBABOOK_HORSE_SEARCH = "https://p.keibabook.co.jp/db/search/horse"
ENV_KEIBABOOK_LOGIN_ID = "KEIBABOOK_LOGIN_ID"
ENV_KEIBABOOK_PASSWORD = "KEIBABOOK_PASSWORD"
ENV_KEIBABOOK_COOKIE = "KEIBABOOK_COOKIE"
SPEEDKEIBA_ORIGIN = "https://speedkeiba.com"
SPEEDKEIBA_INDEX = "https://speedkeiba.com/%E4%BB%8A%E9%80%B1%E3%81%AE%E3%83%AC%E3%83%BC%E3%82%B9/"
_MAX_ARTICLES = 12

_UMA_HREF = re.compile(r"/db/uma/(\d{5,})/?")
_PROFILE_UMACD = re.compile(r"/db/uma/(\d{5,})/(?:seiseki|crireki|cyukan|kettou)")
_FULL_DATE = re.compile(r"^[*＊]?\s*(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})")
_PROFILE_BIRTH = re.compile(r"((?:19|20)\d{2})[./](\d{1,2})[./](\d{1,2})")
_BIRTH_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_SEX = re.compile(r"[牡牝セ騸]")

_RACE_ID = re.compile(r"^\d{12}$")
_RACE_DATE = re.compile(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日")
_MONTH_DAY = re.compile(r"^[*＊]?\s*(\d{1,2})/(\d{1,2})")
_REPS = re.compile(r"^(\d+)回$")
_CLOCK = re.compile(r"^\d+(?:\.\d+)?$")
_CLOCK_ROW = re.compile(r"^(?:\d+(?:\.\d+)?)(?:\s*[–—\-]\s*(?:\d+(?:\.\d+)?))*$")
_RANK = re.compile(r"^[［\[]\s*(\d+)\s*[］\]]$")
_YEAR = re.compile(r"20\d{2}")
_KAI = re.compile(r"^第\d+回")

_FLAT_OR_SLOPE = (
    ("6F(坂路)", "6F", "reps"),
    ("5F(4F)", "5F", "4F"),
    ("半哩(3F)", "半哩", "3F"),
    ("3F(2F)", "3F", "2F"),
    ("1F(1F)", "1F", "1F"),
)


@dataclass
class TrainingPage:
    """Horses parsed from one HTML document. ``page_warning`` is page-level."""

    by_name: dict[str, Training] = field(default_factory=dict)
    page_warning: str | None = None


@dataclass(frozen=True)
class KeibabookCredentials:
    """Optional keibabook login. Blank fields are not a session."""

    login_id: str = ""
    password: str = ""
    cookie: str = ""

    def __repr__(self) -> str:
        return f"KeibabookCredentials(configured={self.configured})"

    @property
    def password_login(self) -> bool:
        return bool(self.login_id.strip()) and bool(self.password.strip())

    @property
    def configured(self) -> bool:
        return self.password_login or bool(self.cookie.strip())


@dataclass(frozen=True)
class UmacdCandidate:
    """One horse a keibabook page actually linked."""

    umacd: str
    name: str
    birth_date: date | None = None
    birth_year: int | None = None
    sex: str | None = None


def credentials_from_env(environ: Mapping[str, str] | None = None) -> KeibabookCredentials:
    """Read login fields. Missing variables are empty, not a successful login."""
    env = os.environ if environ is None else environ
    return KeibabookCredentials(
        login_id=env.get(ENV_KEIBABOOK_LOGIN_ID, ""),
        password=env.get(ENV_KEIBABOOK_PASSWORD, ""),
        cookie=env.get(ENV_KEIBABOOK_COOKIE, ""),
    )


def name_key(name: str) -> str:
    """Match 馬名 without spaces. Do not strip a prefix or invent an alias."""
    text = unicodedata.normalize("NFKC", name)
    return re.sub(r"\s+", "", text).casefold()


def _normalize_sex(text: str | None) -> str | None:
    if not text:
        return None
    match = _SEX.search(text)
    if match is None:
        return None
    sex = match.group(0)
    if sex == "騸":
        return "セ"
    return sex


def parse_keibabook_cyokyo(html: str) -> TrainingPage:
    """Read every horse table on a 競馬ブック調教 page."""
    soup = BeautifulSoup(html, "lxml")
    tables = [
        table
        for table in soup.find_all("table")
        if isinstance(table, Tag) and "cyokyo" in (table.get("class") or [])
    ]
    if not tables:
        return TrainingPage(page_warning=TRAINING_TABLE_NOT_FOUND)
    race_day = _race_day(soup)
    by_name: dict[str, Training] = {}
    for table in tables:
        for name, training in _read_keibabook_table(table, race_day):
            by_name[name_key(name)] = training
    if not by_name:
        return TrainingPage(page_warning=TRAINING_TABLE_MALFORMED)
    return TrainingPage(by_name=by_name)


def parse_keibabook_crireki(html: str) -> Training:
    """One horse's 調教履歴. No table means no clocks."""
    if not _has_workout_table(html):
        if "調教履歴データがありません" in html:
            return unavailable_training(TRAINING_HISTORY_EMPTY)
        if _crireki_is_locked(html):
            return unavailable_training(TRAINING_HISTORY_REQUIRES_LOGIN)
        return unavailable_training(TRAINING_TABLE_NOT_FOUND)
    page = parse_keibabook_cyokyo(html)
    if page.page_warning:
        return unavailable_training(page.page_warning)
    if len(page.by_name) != 1:
        return unavailable_training(TRAINING_AMBIGUOUS)
    training = next(iter(page.by_name.values()))
    if training.source == SOURCE_KEIBABOOK_SMART:
        return training.model_copy(update={"source": SOURCE_KEIBABOOK_CRIREKI})
    return training


def parse_keibabook_syutuba(html: str) -> dict[str, str]:
    """``name_key -> umacd`` from horse links. Pedigree ``/sanku`` links are ignored.

    Two different umacd for one name are dropped. The id is the href, not a guess.
    """
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, str] = {}
    ambiguous: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        href = str(anchor.get("href") or "")
        match = _UMA_HREF.fullmatch(href)
        if match is None:
            continue
        umacd = str(anchor.get("umacd") or match.group(1))
        if umacd != match.group(1):
            continue
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        key = name_key(name)
        if key in ambiguous:
            continue
        previous = found.get(key)
        if previous is not None and previous != umacd:
            ambiguous.add(key)
            del found[key]
            continue
        found[key] = umacd
    return found


def parse_keibabook_form_token(html: str) -> str | None:
    """CSRF ``_token`` from a keibabook form. Absent on a page that is not the form."""
    soup = BeautifulSoup(html, "lxml")
    for node in soup.find_all("input"):
        if not isinstance(node, Tag):
            continue
        if node.get("name") == "_token" and node.get("value"):
            return str(node.get("value"))
    return None


def logincheck_authenticated(html: str) -> bool:
    """True only when the logincheck body shows a session, not the logged-out prompt."""
    if "ログインしていない" in html or "ログインして下さい" in html:
        return False
    return "ログアウト" in html or "/mypage/" in html


def parse_keibabook_horse_search(html: str) -> list[UmacdCandidate]:
    """Search rows, or the single profile the site opened for one name."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="search")
    if isinstance(table, Tag):
        return _search_candidates(table)
    profile = parse_keibabook_horse_profile(html)
    if profile is None:
        return []
    return [profile]


def parse_keibabook_horse_profile(html: str) -> UmacdCandidate | None:
    """Identity printed on a keibabook horse page, plus the umacd in its own links."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.select_one("dt.kanzendata_uma h2")
    if not isinstance(title, Tag):
        return None
    name = title.get_text(" ", strip=True)
    umacd = _profile_umacd(html)
    if not name or umacd is None:
        return None
    birth: date | None = None
    sex: str | None = None
    for node in soup.select("dt.kanzendata_uma p.other"):
        if not isinstance(node, Tag):
            continue
        text = node.get_text(" ", strip=True)
        if "生" not in text:
            continue
        match = _PROFILE_BIRTH.search(text)
        if match:
            try:
                birth = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                birth = None
        sex = _normalize_sex(text)
    year = birth.year if birth is not None else None
    return UmacdCandidate(umacd=umacd, name=name, birth_date=birth, birth_year=year, sex=sex)


def _search_candidates(table: Tag) -> list[UmacdCandidate]:
    headers = _search_headers(table)
    name_at = _header_index(headers, "馬名")
    year_at = _header_index(headers, "生年")
    sex_at = _header_index(headers, "性別")
    if name_at is None or year_at is None:
        return []
    candidates: list[UmacdCandidate] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag):
            continue
        cells = [cell for cell in row.find_all("td") if isinstance(cell, Tag)]
        if not cells or name_at >= len(cells) or year_at >= len(cells):
            continue
        anchor = cells[name_at].find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        match = _UMA_HREF.fullmatch(str(anchor.get("href") or ""))
        if match is None:
            continue
        umacd = str(anchor.get("umacd") or match.group(1))
        if umacd != match.group(1):
            continue
        name = anchor.get_text(" ", strip=True)
        year_text = cells[year_at].get_text(" ", strip=True)
        if not name or _BIRTH_YEAR.fullmatch(year_text) is None:
            continue
        sex = None
        if sex_at is not None and sex_at < len(cells):
            sex = _normalize_sex(cells[sex_at].get_text(" ", strip=True))
        candidates.append(
            UmacdCandidate(
                umacd=umacd,
                name=name,
                birth_date=None,
                birth_year=int(year_text),
                sex=sex,
            )
        )
    return candidates


def _search_headers(table: Tag) -> list[str]:
    thead = table.find("thead")
    if isinstance(thead, Tag):
        cells = [cell for cell in thead.find_all("th") if isinstance(cell, Tag)]
        if cells:
            return [re.sub(r"\s+", "", cell.get_text()) for cell in cells]
    return []


def _header_index(headers: list[str], label: str) -> int | None:
    for index, header in enumerate(headers):
        if header == label:
            return index
    return None


def _profile_umacd(html: str) -> str | None:
    found = list(dict.fromkeys(_PROFILE_UMACD.findall(html)))
    if len(found) == 1:
        return found[0]
    return None


def _has_workout_table(html: str) -> bool:
    return "cyokyodata" in html


def _crireki_is_locked(html: str) -> bool:
    return (
        "ログインしていない" in html
        or "ログインして下さい" in html
        or 'id="web_web"' in html
        or "調教履歴" in html
    )


def select_umacd(
    candidates: list[UmacdCandidate],
    horse_name: str,
    birth_date: date | None,
    sex: str | None,
) -> str | None:
    """One umacd when the page's name and birth match. Otherwise none."""
    caller_year = birth_date.year if birth_date is not None else None
    caller_sex = _normalize_sex(sex)
    if caller_sex == "不明":
        caller_sex = None
    if birth_date is None and caller_year is None:
        return None
    matches: list[str] = []
    for candidate in candidates:
        if name_key(candidate.name) != name_key(horse_name):
            continue
        if not _birth_matches(candidate, birth_date, caller_year):
            continue
        candidate_sex = _normalize_sex(candidate.sex)
        if caller_sex and candidate_sex and caller_sex != candidate_sex:
            continue
        matches.append(candidate.umacd)
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0]
    return None


def _birth_matches(
    candidate: UmacdCandidate, birth_date: date | None, caller_year: int | None
) -> bool:
    if candidate.birth_date is not None and birth_date is not None:
        return candidate.birth_date == birth_date
    year = candidate.birth_year
    if year is None and candidate.birth_date is not None:
        year = candidate.birth_date.year
    if year is None or caller_year is None:
        return False
    return year == caller_year


def parse_speedkeiba_article(html: str) -> TrainingPage:
    """Read one speedkeiba race article. 好調 and 凡走 rows are dropped."""
    soup = BeautifulSoup(html, "lxml")
    year = _article_year(soup)
    headings = [tag for tag in soup.find_all("h4") if isinstance(tag, Tag)]
    if not headings:
        return TrainingPage(page_warning=TRAINING_TABLE_NOT_FOUND)
    by_name: dict[str, Training] = {}
    for heading in headings:
        name = heading.get_text(" ", strip=True)
        if not name:
            continue
        by_name[name_key(name)] = _read_speedkeiba_horse(heading, year)
    if not by_name:
        return TrainingPage(page_warning=TRAINING_TABLE_MALFORMED)
    return TrainingPage(by_name=by_name)


def parse_speedkeiba_index(html: str) -> list[tuple[str, str]]:
    """``(absolute url, link text)`` for this week's 調教 articles."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for heading in soup.find_all("h3"):
        if not isinstance(heading, Tag):
            continue
        anchor = heading.find("a", href=True)
        if not isinstance(anchor, Tag):
            continue
        title = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "")
        if "調教" not in title or not href:
            continue
        url = urljoin(SPEEDKEIBA_ORIGIN + "/", href)
        if url in seen:
            continue
        seen.add(url)
        found.append((url, title))
    return found


def match_speedkeiba_article(race_name: str, articles: list[tuple[str, str]]) -> str | None:
    """One article whose title is this race. A partial or double match is no match."""
    key = _race_key(race_name)
    if len(key) < 4:
        return None
    hits: list[str] = []
    for url, title in articles:
        if _race_key(title) == key:
            hits.append(url)
    unique = list(dict.fromkeys(hits))
    if len(unique) == 1:
        return unique[0]
    return None


class TrainingScraper(BaseScraper):
    """Fetch 調教 HTML and return only clocks that were on the page."""

    ENCODING = "utf-8"

    def __init__(
        self,
        http_client=None,
        credentials: KeibabookCredentials | None = None,
    ):
        """Initialize the scraper.

        Args:
            http_client: HTTP client. A new one is created when omitted.
            credentials: Keibabook login. Omitted means anonymous, even if env vars exist.
                RaceService passes ``credentials_from_env()``.
        """
        super().__init__(http_client)
        self.credentials = credentials or KeibabookCredentials()
        self._login_state: str | None = None

    def training_for_horse(
        self,
        horse_name: str,
        *,
        birth_date: date | None = None,
        sex: str | None = None,
    ) -> Training:
        """speedkeiba by horse name, then keibabook history when login is real.

        Without credentials this stays on speedkeiba. ``umacd`` comes from a
        search row or profile link whose printed birth matches ``birth_date``.
        The netkeiba horse id is not an umacd.
        """
        if not horse_name or horse_name == "不明":
            return unavailable_training(TRAINING_HORSE_UNAVAILABLE)
        speed = self._training_from_speedkeiba(horse_name)
        login = self._ensure_login()
        if login != "in":
            if (
                login == "failed"
                and not _has_time(speed)
                and speed.warning != TRAINING_FETCH_FAILED
                and speed.warning != TRAINING_TABLE_MALFORMED
                and speed.warning != TRAINING_AMBIGUOUS
            ):
                return unavailable_training(TRAINING_LOGIN_FAILED)
            return speed
        umacd = self._resolve_umacd(horse_name, birth_date, sex)
        if umacd is None:
            if _has_time(speed) or _kept_summary(speed):
                return speed
            if speed.warning in {
                TRAINING_AMBIGUOUS,
                TRAINING_TABLE_MALFORMED,
                TRAINING_FETCH_FAILED,
            }:
                return speed
            return unavailable_training(TRAINING_UMACD_UNMAPPED)
        history = self._fetch_crireki(umacd)
        if _has_time(history):
            return history
        if history.warning == TRAINING_TABLE_MALFORMED:
            return history
        if _has_time(speed) or _kept_summary(speed):
            return speed
        if speed.warning == TRAINING_AMBIGUOUS:
            return speed
        return history

    def training_for_race(
        self, race_id: str, race_name: str, horse_names: list[str]
    ) -> dict[str, Training]:
        """Keibabook clocks when that horse is on the page, else a speedkeiba article.

        A logged-in session also reads ``/db/uma/{umacd}/crireki`` for horses
        the anonymous 調教 page skipped. ``umacd`` is the shutuba horse-name link.
        """
        login = self._ensure_login()
        keibabook, kb_warning = self._load_keibabook(race_id)
        histories: dict[str, Training] = {}
        if login == "in" and _RACE_ID.fullmatch(race_id):
            histories = self._load_crireki_for_race(race_id, horse_names, keibabook)
        speed, speed_checked = self._load_speed_for_race(race_name)
        chosen: dict[str, Training] = {}
        for horse_name in horse_names:
            chosen[horse_name] = _choose_training(
                horse_name,
                keibabook,
                kb_warning,
                speed,
                speed_checked,
                crireki=histories.get(horse_name),
                login_state=login,
            )
        return chosen

    def _training_from_speedkeiba(self, horse_name: str) -> Training:
        try:
            index_html = self.http_client.get(SPEEDKEIBA_INDEX, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba index: %s", e)
            return unavailable_training(TRAINING_FETCH_FAILED)
        articles = parse_speedkeiba_index(index_html)[:_MAX_ARTICLES]
        if not articles:
            return unavailable_training(TRAINING_HORSE_UNAVAILABLE)

        matches: list[Training] = []
        malformed: Training | None = None
        fetched = False
        for url, _title in articles:
            try:
                html = self.http_client.get(url, encoding=self.ENCODING)
            except Exception as e:
                logger.error("Failed to fetch speedkeiba article %s: %s", url, e)
                continue
            fetched = True
            page = parse_speedkeiba_article(html)
            found = page.by_name.get(name_key(horse_name))
            if found is None:
                continue
            if found.warning == TRAINING_TABLE_MALFORMED:
                malformed = found
                continue
            if any(row.time for row in found.workouts) or found.summary is not None:
                matches.append(found)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return unavailable_training(TRAINING_AMBIGUOUS)
        if malformed is not None:
            return malformed
        if not fetched:
            return unavailable_training(TRAINING_FETCH_FAILED)
        return unavailable_training(TRAINING_HORSE_UNAVAILABLE)

    def _ensure_login(self) -> str:
        """``anonymous`` when creds are absent, ``in`` or ``failed`` after logincheck."""
        if self._login_state is not None:
            return self._login_state
        creds = self.credentials
        if not creds.configured:
            self._login_state = "anonymous"
            return "anonymous"
        try:
            if creds.password_login:
                form = self.http_client.get(KEIBABOOK_LOGIN, encoding=self.ENCODING)
                token = parse_keibabook_form_token(form)
                if not token:
                    self._login_state = "failed"
                    return "failed"
                self.http_client.post(
                    KEIBABOOK_LOGIN,
                    data={
                        "_token": token,
                        "referer": "",
                        "service": "keibabook",
                        "login_id": creds.login_id.strip(),
                        "pswd": creds.password,
                    },
                    encoding=self.ENCODING,
                )
            else:
                self.http_client.update_headers({"Cookie": creds.cookie.strip()})
            check = self.http_client.post(
                KEIBABOOK_LOGINCHECK,
                data={"motourl": "/"},
                encoding=self.ENCODING,
            )
        except Exception as e:
            logger.error("Keibabook login failed: %s", e)
            self._login_state = "failed"
            return "failed"
        if logincheck_authenticated(check):
            self._login_state = "in"
            return "in"
        logger.info("Keibabook logincheck still shows a logged-out session")
        self._login_state = "failed"
        return "failed"

    def _resolve_umacd(
        self, horse_name: str, birth_date: date | None, sex: str | None
    ) -> str | None:
        try:
            form = self.http_client.get(KEIBABOOK_HORSE_SEARCH, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to open keibabook horse search: %s", e)
            return None
        token = parse_keibabook_form_token(form)
        data = {"bamei": horse_name}
        if token:
            data["_token"] = token
        try:
            html = self.http_client.post(KEIBABOOK_HORSE_SEARCH, data=data, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to search keibabook horse %s: %s", horse_name, e)
            return None
        return select_umacd(parse_keibabook_horse_search(html), horse_name, birth_date, sex)

    def _load_crireki_for_race(
        self,
        race_id: str,
        horse_names: list[str],
        keibabook: dict[str, Training],
    ) -> dict[str, Training]:
        url = KEIBABOOK_SYUTUBA.format(race_id=race_id)
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch keibabook shutuba for %s: %s", race_id, e)
            return {
                name: unavailable_training(TRAINING_FETCH_FAILED)
                for name in horse_names
                if not _has_time(keibabook.get(name_key(name)))
            }
        mapping = parse_keibabook_syutuba(html)
        histories: dict[str, Training] = {}
        for horse_name in horse_names:
            if _has_time(keibabook.get(name_key(horse_name))):
                continue
            umacd = mapping.get(name_key(horse_name))
            if umacd is None:
                continue
            histories[horse_name] = self._fetch_crireki(umacd)
        return histories

    def _fetch_crireki(self, umacd: str) -> Training:
        url = KEIBABOOK_CRIREKI.format(umacd=umacd)
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch keibabook training history %s: %s", umacd, e)
            return unavailable_training(TRAINING_FETCH_FAILED)
        return parse_keibabook_crireki(html)

    def _load_keibabook(self, race_id: str) -> tuple[dict[str, Training], str | None]:
        if _RACE_ID.fullmatch(race_id) is None:
            return {}, TRAINING_NOT_ON_KEIBABOOK
        url = KEIBABOOK_CYOKYO.format(race_id=race_id)
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch keibabook training for %s: %s", race_id, e)
            return {}, TRAINING_FETCH_FAILED
        page = parse_keibabook_cyokyo(html)
        if page.page_warning:
            return {}, page.page_warning
        return page.by_name, None

    def _load_speed_for_race(self, race_name: str) -> tuple[dict[str, Training], bool]:
        """Return parsed horses and whether an article for this race was opened."""
        try:
            index_html = self.http_client.get(SPEEDKEIBA_INDEX, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba index: %s", e)
            return {}, False
        url = match_speedkeiba_article(race_name, parse_speedkeiba_index(index_html))
        if url is None:
            return {}, False
        try:
            html = self.http_client.get(url, encoding=self.ENCODING)
        except Exception as e:
            logger.error("Failed to fetch speedkeiba article %s: %s", url, e)
            return {}, False
        page = parse_speedkeiba_article(html)
        if page.page_warning:
            return {}, True
        return page.by_name, True


def _choose_training(
    horse_name: str,
    keibabook: dict[str, Training],
    kb_warning: str | None,
    speed: dict[str, Training],
    speed_checked: bool,
    crireki: Training | None = None,
    login_state: str = "anonymous",
) -> Training:
    kb = keibabook.get(name_key(horse_name))
    sp = speed.get(name_key(horse_name))
    if _has_time(kb):
        return kb  # type: ignore[return-value]
    if _has_time(crireki):
        return crireki  # type: ignore[return-value]
    if _has_time(sp):
        return sp  # type: ignore[return-value]
    if kb is not None and kb.warning == TRAINING_TABLE_MALFORMED:
        return kb
    if crireki is not None and crireki.warning == TRAINING_TABLE_MALFORMED:
        return crireki
    if kb is not None and kb.summary is not None and kb.warning != TRAINING_TABLE_MALFORMED:
        return kb
    if _kept_summary(crireki):
        return crireki  # type: ignore[return-value]
    if sp is not None and sp.summary is not None:
        return sp
    if kb_warning == TRAINING_FETCH_FAILED and not speed_checked and crireki is None:
        return unavailable_training(TRAINING_FETCH_FAILED)
    if crireki is not None and crireki.warning == TRAINING_FETCH_FAILED:
        return crireki
    if login_state == "failed":
        return unavailable_training(TRAINING_LOGIN_FAILED)
    if crireki is not None and crireki.warning in {
        TRAINING_HISTORY_EMPTY,
        TRAINING_HISTORY_REQUIRES_LOGIN,
    }:
        return crireki
    if kb_warning == TRAINING_TABLE_MALFORMED and not _has_time(sp):
        return unavailable_training(TRAINING_TABLE_MALFORMED)
    if speed_checked:
        return unavailable_training(TRAINING_NOT_ON_EITHER)
    if kb_warning == TRAINING_TABLE_NOT_FOUND:
        return unavailable_training(TRAINING_TABLE_NOT_FOUND)
    return unavailable_training(kb_warning or TRAINING_NOT_ON_KEIBABOOK)


def _kept_summary(training: Training | None) -> bool:
    if training is None or training.warning == TRAINING_TABLE_MALFORMED:
        return False
    summary = training.summary
    if summary is None:
        return False
    return bool(
        summary.comment or summary.arrow or summary.evaluation or summary.average or summary.laps
    )


def _has_time(training: Training | None) -> bool:
    if training is None or training.warning == TRAINING_TABLE_MALFORMED:
        return False
    return any(row.time for row in training.workouts)


def _read_keibabook_table(table: Tag, race_day: date | None) -> list[tuple[str, Training]]:
    horses: list[tuple[str, Training]] = []
    current_name: str | None = None
    current_summary = TrainingSummary()
    workouts: list[Workout] = []
    malformed = False

    def flush() -> None:
        nonlocal current_name, current_summary, workouts, malformed
        if current_name is None:
            return
        warning = TRAINING_TABLE_MALFORMED if malformed else None
        kept = [] if malformed else workouts
        summary = current_summary
        horses.append(
            (
                current_name,
                build_training(
                    workouts=kept,
                    summary=summary,
                    source=SOURCE_KEIBABOOK_SMART,
                    warning=warning,
                ),
            )
        )
        current_name = None
        current_summary = TrainingSummary()
        workouts = []
        malformed = False

    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row.find_parent("table") is not table:
            continue
        name_cell = _direct_cell(row, "kbamei")
        if name_cell is not None:
            flush()
            current_name = name_cell.get_text(" ", strip=True) or None
            comment = _direct_text(row, "tanpyo")
            arrow = _direct_text(row, "yajirusi")
            current_summary = TrainingSummary(comment=comment, arrow=arrow)
            continue
        if current_name is None:
            continue
        inner = row.find("table", class_="cyokyodata")
        if not isinstance(inner, Tag):
            continue
        parsed, bad = _read_cyokyodata(inner, race_day)
        if bad:
            malformed = True
            workouts = []
        elif not malformed:
            workouts.extend(parsed)
    flush()
    return horses


def _read_cyokyodata(table: Tag, race_day: date | None) -> tuple[list[Workout], bool]:
    headers = _header_cells(table)
    columns = _column_index(headers)
    if columns is None:
        return [], True
    workouts: list[Workout] = []
    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row.find_parent("table") is not table:
            continue
        classes = set(row.get("class") or [])
        if "awase" in classes:
            note = row.get_text(" ", strip=True) or None
            if workouts and note:
                workouts[-1] = workouts[-1].model_copy(update={"partner_note": note})
            continue
        if "time" not in classes and "oikiri" not in classes:
            continue
        cells = [cell for cell in row.find_all("td", recursive=False) if isinstance(cell, Tag)]
        if len(cells) != len(headers):
            return [], True
        workout = _workout_from_cells(cells, columns, race_day, "oikiri" in classes)
        if workout is None:
            return [], True
        workouts.append(workout)
    return workouts, False


def _workout_from_cells(
    cells: list[Tag],
    columns: dict[str, int | list[tuple[int, str]]],
    race_day: date | None,
    oikiri: bool,
) -> Workout | None:
    def text_at(key: str) -> str:
        index = columns[key]
        if not isinstance(index, int) or index < 0:
            return ""
        return cells[index].get_text(" ", strip=True)

    date_label = text_at("date") or None
    course = text_at("course") or None
    condition = text_at("condition") or None
    rider = text_at("rider") or None
    gait = text_at("gait") or None
    comment = text_at("comment") or None
    mark = text_at("mark") or None
    slope = bool(course and "坂" in course)
    time_columns = columns["times"]
    if not isinstance(time_columns, list):
        return None
    if not slope:
        for index, _label in time_columns:
            if _REPS.fullmatch(cells[index].get_text(" ", strip=True)):
                slope = True
                break

    splits: list[TrainingSplit] = []
    slope_reps: int | None = None
    for index, header in time_columns:
        raw = cells[index].get_text(" ", strip=True)
        if not raw:
            continue
        label = _split_label(header, slope)
        reps = _REPS.fullmatch(raw)
        if label == "reps" or reps:
            if reps is None:
                return None
            slope_reps = int(reps.group(1))
            continue
        if _CLOCK.fullmatch(raw) is None:
            return None
        if label is None:
            return None
        splits.append(TrainingSplit(label=label, time=raw))

    rank = _rank_in_group(text_at("rank"))
    parsed_date, long_gap = _resolve_date(date_label, race_day)
    distance = splits[0].label if splits else None
    clock = splits[0].time if splits else None
    final = True if oikiri or mark == "☆" else None
    return Workout(
        date=parsed_date,
        date_label=date_label,
        course=course,
        distance=distance,
        time=clock,
        condition=condition,
        rank_in_group=rank,
        rider=rider,
        gait=gait,
        comment=comment,
        mark=mark,
        final_workout=final,
        long_gap=long_gap,
        slope_reps=slope_reps,
        splits=splits,
    )


def _header_cells(table: Tag) -> list[str]:
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return []
    headers: list[str] = []
    for cell in header_row.find_all(["th", "td"], recursive=False):
        if isinstance(cell, Tag):
            headers.append(re.sub(r"\s+", "", cell.get_text()))
    return headers


def _column_index(
    headers: list[str],
) -> dict[str, int | list[tuple[int, str]]] | None:
    if not headers:
        return None
    found: dict[str, int] = {}
    times: list[tuple[int, str]] = []
    for index, header in enumerate(headers):
        if header in {"騎乗者"}:
            found["rider"] = index
        elif header in {"日付"}:
            found["date"] = index
        elif header in {"コース"}:
            found["course"] = index
        elif header.startswith("馬場") or header == "馬場":
            found["condition"] = index
        elif "回り" in header and "位置" in header:
            found["rank"] = index
        elif header in {"脚色"}:
            found["gait"] = index
        elif header in {"短評"}:
            found["comment"] = index
        elif header in {"動画"}:
            found["movie"] = index
        elif _is_time_header(header):
            times.append((index, header))
        elif index == 0 and header == "":
            found["mark"] = index
    if "mark" not in found and headers and headers[0] == "":
        found["mark"] = 0
    required = ("date", "course", "condition", "rank")
    if any(key not in found for key in required) or not times:
        return None
    found.setdefault("rider", -1)
    found.setdefault("gait", -1)
    found.setdefault("comment", -1)
    found.setdefault("mark", -1)
    return {
        "rider": found["rider"],
        "date": found["date"],
        "course": found["course"],
        "condition": found["condition"],
        "rank": found["rank"],
        "gait": found["gait"],
        "comment": found["comment"],
        "mark": found["mark"],
        "times": times,
    }


def _is_time_header(header: str) -> bool:
    if not header:
        return False
    base = header.split("(")[0]
    return base in {"1哩", "7F", "6F", "5F", "半哩", "3F", "1F"} or header in {
        "6F(坂路)",
        "5F(4F)",
        "半哩(3F)",
        "3F(2F)",
        "1F(1F)",
    }


def _split_label(header: str, slope: bool) -> str | None:
    """Use the header's own parenthetical for 坂路. Do not convert 半哩 to meters."""
    if slope:
        for raw, _flat, slope_label in _FLAT_OR_SLOPE:
            if header == raw:
                return slope_label
        if header.startswith("6F"):
            return "reps"
    base = header.split("(")[0]
    if base:
        return base
    return None


def _rank_in_group(text: str) -> int | None:
    match = _RANK.fullmatch(text)
    if match is None:
        return None
    return int(match.group(1))


def _resolve_date(label: str | None, race_day: date | None) -> tuple[date | None, bool | None]:
    """A full date from the cell, or month/day when the page prints a race day.

    ``■`` stays null. A month/day with no year on the page stays null.
    """
    if not label:
        return None, None
    full = _FULL_DATE.match(label)
    if full:
        try:
            parsed = date(int(full.group(1)), int(full.group(2)), int(full.group(3)))
        except ValueError:
            return None, None
        long_gap = label.startswith("*") or label.startswith("＊")
        return parsed, long_gap
    match = _MONTH_DAY.match(label)
    if match is None or race_day is None:
        return None, None
    month = int(match.group(1))
    day = int(match.group(2))
    try:
        candidate = date(race_day.year, month, day)
    except ValueError:
        return None, None
    if candidate > race_day:
        try:
            candidate = date(race_day.year - 1, month, day)
        except ValueError:
            return None, None
    long_gap = label.startswith("*") or label.startswith("＊")
    return candidate, long_gap


def _race_day(soup: BeautifulSoup) -> date | None:
    match = _RACE_DATE.search(soup.get_text(" ", strip=True))
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _direct_cell(row: Tag, class_name: str) -> Tag | None:
    for cell in row.find_all("td", recursive=False):
        if isinstance(cell, Tag) and class_name in (cell.get("class") or []):
            return cell
    return None


def _direct_text(row: Tag, class_name: str) -> str | None:
    cell = _direct_cell(row, class_name)
    if cell is None:
        return None
    text = cell.get_text(" ", strip=True)
    return text or None


def _read_speedkeiba_horse(heading: Tag, year: int | None) -> Training:
    workouts: list[Workout] = []
    section: str | None = None
    summary = TrainingSummary()
    malformed = False
    for node in _until_next_h4(heading):
        if not isinstance(node, Tag):
            continue
        if node.name == "h5":
            text = node.get_text(" ", strip=True)
            if "前回連対時" in text:
                section = "前回連対時"
            elif "今回" in text:
                section = "今回"
            continue
        classes = set(node.get("class") or [])
        if node.name == "div" and "flexbox" in classes:
            if "average" in classes:
                average = _published_average(node)
                if average is None:
                    malformed = True
                elif summary.average is None:
                    summary = summary.model_copy(update={"average": average})
                continue
            workout = _speed_workout(node, section, year)
            if workout == "bad":
                malformed = True
            elif isinstance(workout, Workout):
                workouts.append(workout)
            continue
        if node.name == "span" and "choukyou-evaluation" in classes:
            text = node.get_text(" ", strip=True) or None
            if text and summary.evaluation is None:
                summary = summary.model_copy(update={"evaluation": text})
            continue
        if node.name == "table" and "choukyou-laptime" in classes:
            lap = _thisweek_lap(node)
            if lap == "bad":
                malformed = True
            elif isinstance(lap, TrainingLap):
                summary = summary.model_copy(update={"laps": [*summary.laps, lap]})
    if malformed:
        return build_training(
            workouts=[],
            summary=None,
            source=SOURCE_SPEEDKEIBA,
            warning=TRAINING_TABLE_MALFORMED,
        )
    return build_training(
        workouts=workouts,
        summary=summary,
        source=SOURCE_SPEEDKEIBA,
        warning=None,
    )


def _until_next_h4(heading: Tag) -> list[Tag]:
    nodes: list[Tag] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h4":
            break
        if isinstance(sibling, Tag):
            nodes.append(sibling)
            nodes.extend(tag for tag in sibling.find_all(True) if isinstance(tag, Tag))
    return nodes


def _published_average(node: Tag) -> PublishedAverage | None:
    label = _span_text(node, "date") or "(平均)"
    course = _basho_course(_span_text(node, "basho"))
    clock = _span_text(node, "time")
    if clock and _CLOCK_ROW.fullmatch(clock) is None:
        return None
    return PublishedAverage(label=label, course=course, time=clock)


def _speed_workout(node: Tag, section: str | None, year: int | None) -> Workout | str | None:
    label = _span_text(node, "date")
    basho = _span_text(node, "basho")
    clock = _span_text(node, "time")
    if clock and _CLOCK_ROW.fullmatch(clock) is None and not re.fullmatch(r"[–—\-\s]+", clock):
        return "bad"
    if not clock or re.fullmatch(r"[–—\-\s]+", clock):
        return None
    course, condition = _split_basho(basho)
    parsed: date | None = None
    long_gap: bool | None = None
    if section == "今回" and year is not None and label:
        match = _MONTH_DAY.match(label)
        if match:
            try:
                parsed = date(year, int(match.group(1)), int(match.group(2)))
                long_gap = False
            except ValueError:
                parsed = None
    return Workout(
        date=parsed,
        date_label=label,
        course=course,
        distance=None,
        time=clock,
        condition=condition,
        section=section,
        long_gap=long_gap,
    )


def _thisweek_lap(table: Tag) -> TrainingLap | str | None:
    header_row = table.find("tr")
    if not isinstance(header_row, Tag):
        return "bad"
    headers = [
        re.sub(r"\s+", "", cell.get_text())
        for cell in header_row.find_all(["th", "td"], recursive=False)
        if isinstance(cell, Tag)
    ]
    labels = headers[1:]
    if not labels or labels[-1] != "1F" or "4F" not in labels:
        return "bad"
    phase, course = _lap_caption(table)
    for row in table.find_all("tr"):
        if not isinstance(row, Tag) or row is header_row:
            continue
        cells = [
            cell.get_text(" ", strip=True)
            for cell in row.find_all(["td", "th"], recursive=False)
            if isinstance(cell, Tag)
        ]
        if not cells or cells[0] != "今走":
            continue
        if len(cells) != len(headers):
            return "bad"
        splits: list[TrainingSplit] = []
        for label, raw in zip(labels, cells[1:], strict=True):
            if not raw or raw in {"–", "—", "-"}:
                continue
            if _CLOCK.fullmatch(raw) is None:
                return "bad"
            splits.append(TrainingSplit(label=label, time=raw))
        if not splits:
            return None
        return TrainingLap(label="今走", phase=phase, course=course, splits=splits)
    return None


def _lap_caption(table: Tag) -> tuple[str | None, str | None]:
    for child in table.children:
        if not isinstance(child, Comment):
            continue
        match = re.search(r"caption>\s*([^<]+)", str(child))
        if match is None:
            continue
        text = match.group(1).strip()
        if "/" in text:
            course, phase = (part.strip() for part in text.split("/", 1))
            return phase or None, course or None
        return None, text or None
    return None, None


def _span_text(node: Tag, class_name: str) -> str | None:
    span = node.find("span", class_=class_name)
    if not isinstance(span, Tag):
        return None
    text = span.get_text(" ", strip=True)
    return text or None


def _split_basho(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    match = re.fullmatch(r"(.+?)\s*\(([^)]+)\)\s*", text)
    if match is None:
        return text.strip() or None, None
    return match.group(1).strip() or None, match.group(2).strip() or None


def _basho_course(text: str | None) -> str | None:
    course, _condition = _split_basho(text)
    return course


def _article_year(soup: BeautifulSoup) -> int | None:
    for tag in soup.find_all(["h2", "title"]):
        match = _YEAR.search(tag.get_text(" ", strip=True))
        if match:
            return int(match.group(0))
    return None


def _race_key(name: str) -> str:
    text = unicodedata.normalize("NFKC", name)
    text = re.sub(r"\s+", "", text)
    text = _KAI.sub("", text)
    text = re.sub(r"20\d{2}.*", "", text)
    text = text.replace("調教タイム・追い切り情報", "")
    text = text.replace("調教タイム", "")
    text = text.replace("追い切り情報", "")
    return text.replace("ステークス", "S")
