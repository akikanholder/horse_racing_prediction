# EV 向け取得契約

競馬予想（EV）がこのツールの CLI と HTTP JSON を読むときの契約。取得と正規化はツール。組み合わせの確率と買うかどうかは EV。

OpenAPI（起動中の `http://localhost:8000/openapi.json` と `/docs`）は型の一覧。null の意味、`warning` の文、環境変数はここ。パーサが列をどう捨てるかの長文は [README](../README.md) の同名節。モデル（`backend/src/models`）とこの文書が食い違ったらモデルを優先し、この文書を直す。

スクレイパの挙動はこの文書では変えない。

## 役割

| 持つもの | 担当 |
|---|---|
| 出馬表、近走、血統、脚質の集計、調教の時計、当日バイアスのラベル、オッズ引用、確定着順と払戻の取得と正規化 | ツール |
| 欠測を `null` と `warning` / `data_sufficiency` / `reason` のまま返す | ツール |
| 単勝・ワイド・馬連・三連複の確率 | EV |
| 期待値、買うかどうか、金額 | EV |
| `short_distance_front_under_specified` や `thin` をどう感度に使うか | EV |
| `prefer=live` が空のとき `prefer=yoso` を明示して取り直すか | EV |

`RaceEntry.odds` は出馬表に印刷された単勝であり、期待値の価格ではない。価格は `OddsQuote`。

確定結果（`result`）はレース後の精算。レース前の入力に混ぜない。`horse` の `performances` は過去走であり、今走の着順ではない。

## 欠測

ページに無い値は `null`。レース名、オッズ、人気、血統から脚質・時計・バイアス・価格を作らない。

- 成績の空セル、`**`、`取`、`計不` は数値にしない。距離が読めない行の `distance` は `null`（0 にしない）。
- 配列が空でもオブジェクトは残ることがある。理由は隣の `warning` または `reason`。
- `data_sufficiency` の語は面ごとに違う。脚質は `insufficient` / `thin` / `sufficient`。バイアスと調教は `unavailable` / `thin` / `sufficient`。オッズは `data_sufficiency` を持たず、`status` が `live` / `yoso` / `unavailable`。
- 馬のトップレベル `warning` は近走の取得失敗と、要求した走数に公開分が足りないとき。血統・脚質・調教・バイアスの理由は各オブジェクトの `warning`。
- JSON は欠測フィールドを省略しない。Pydantic の既定どおり `null` を出す。日付と時刻は ISO 8601（タイムゾーン無しのナイーブ時刻を含む）。

EV は生 HTML を読まない。文字コード（オッズ API の UTF-8、馬成績の EUC-JP、馬場情報の cp932）はツールが JSON にする前に解く。

## 起動

```bash
pip install -r backend/requirements.txt
pip install -e backend

python -m backend.cli --help
python -m backend.main
# または
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

ログは標準エラー。`-v` はログレベルだけを上げる。

CLI の `--json` はサブコマンドの後ろに付ける。

```bash
python -m backend.cli odds 202605030811 --bet-types wide --prefer live --json
```

| サブコマンド | `--json` の標準出力 |
|---|---|
| `odds` `result` `history` `trends` `track` | JSON だけ |
| `races` `card` `horse` `courses` `venues` | 日本語の見出しのあと JSON |

混ざるコマンドは標準出力全体を `json.loads` しない。末尾の JSON 値を読む。見つからないとき（出馬表・馬・コース）は日本語の1行だけで、`--json` でも JSON は出ない。終了コードは 0。例外は標準エラーに出て終了コード 1。

HTTP は JSON だけ。見つからないレース・馬・コースは 404。サービス未初期化は 503。スクレイパ例外は 500。オッズ、確定結果、レース傾向は欠測でも 200 で、本体の `status` / `warning` を見る。傾向で日付も recent meeting も無いときは 400。

## CLI と HTTP

`race_id` の例は netkeiba の12桁（年・場・回・日・レース）。調教の競馬ブックはその形のときだけ取りにいく。

| CLI | HTTP | 返すもの |
|---|---|---|
| `python -m backend.cli races --json` | `GET /api/races` | 今週の `RaceSchedule`。`track_bias` は null |
| `python -m backend.cli card <race_id> --json` | `GET /api/races/{race_id}/card` | `RaceCard`。`race.track_bias` あり |
| （card の `race` と同じ） | `GET /api/races/{race_id}` | `Race` だけ |
| `python -m backend.cli horse <horse_id> --json` | `GET /api/horses/{horse_id}` | `Horse`。近走はログイン無しで見える全走。9走未満は warning。当日バイアスは null |
| （CLI なし。HTTP のみ） | `GET /api/races/{race_id}/horses` | `HorseBasicInfo` の配列。近走はログイン無しで最大5走 |
| `python -m backend.cli result <race_id> --json` | `GET /api/races/{race_id}/result` | `SettledRaceResult`。予想の入力にしない |
| `python -m backend.cli history <race_id> --json` | `GET /api/races/{race_id}/history` | `HistoricalBatch`。2025年以降の平地のバックテスト。予想の入力にしない |
| `python -m backend.cli history --date 2025-01-05 --venue 東京 --json` | `GET /api/races/history?date=2025-01-05&venue=東京` | その日の範囲内レース。日付範囲は `--from` `--to` と `date_from` `date_to` |
| `python -m backend.cli history --date 2025-01-05 --export csv` | `POST /api/races/history/export?date=2025-01-05&format=csv` | 芝・ダートで分けられる CSV。`format=jsonl` は1レース1行 |
| `python -m backend.cli trends --today --venue 東京 --json` | `GET /api/races/trends?today=true&venue=東京` | 今日（Asia/Tokyo）の確定かつ complete なレースだけ。未確定は `races_pending` |
| `python -m backend.cli trends --date 20261018 --venue 東京 --surface 芝 --json` | `GET /api/races/trends?date=20261018&venue=東京&surface=芝` | その開催日だけ。`YYYY-MM-DD` も同じ。確定レースの人気・枠・脚質・騎手・父の集計。予想の入力にしない |
| `python -m backend.cli trends --recent-meeting --venue 東京 --json` | `GET /api/races/trends?recent_meeting=true&venue=東京` | 当日（Asia/Tokyo）の確定と、同じ競馬場の前開催日 |
| `python -m backend.cli odds <race_id> --bet-types win,wide,umaren,sanrenpuku --prefer live --json` | `GET /api/races/{race_id}/odds?bet_types=win,wide,umaren,sanrenpuku&prefer=live` | `RaceOdds` |
| `python -m backend.cli final-odds <race_id> --bet-types wide,umaren,sanrenpuku --json` | `GET /api/races/{race_id}/final-odds?bet_types=wide,umaren,sanrenpuku` | `FinalRaceOdds`。過去の確定ワイド・馬連・三連複 |
| `python -m backend.cli courses --json` | `GET /api/courses` | `Course` の配列 |
| `python -m backend.cli courses --venue 東京 --json` | `GET /api/courses/{venue}` | `Course` |
| （CLI なし） | `GET /api/courses/{venue}/conditions` | `CourseCondition` |
| `python -m backend.cli venues --json` | `GET /api/venues` | `WeeklyVenueSchedule` の配列 |
| `python -m backend.cli track --venue 中山 --merge --json` | `GET /api/tracks?venue=中山&merge=true` | `jra` / `netkeiba` / `merged` をキーにした `TrackInfo` |
| （CLI なし） | `POST /api/races/export?date=YYYY-MM-DD` | CSV 書き出し。予想の入力ではない |

オッズの例:

```bash
python -m backend.cli odds 202605030811 --bet-types wide --prefer live --json
python -m backend.cli odds 202605030811 --bet-types umaren --prefer live --json
python -m backend.cli odds 202605030811 --bet-types sanrenpuku --prefer live --json
python -m backend.cli odds 202605030811 --bet-types win --prefer yoso --json
```

`track` の `--date` は netkeiba 用の `YYYYMMDD`。`--merge` は JRA と netkeiba を統合し、キー `merged` を足す。

## 今週のレース `RaceSchedule`

| フィールド | 意味 | null |
|---|---|---|
| `week_start` / `week_end` | 週の開始日・終了日 | 必須 |
| `races` | `Race` の配列 | 空配列があり得る |
| `fetched_at` | 取得時刻 | 必須 |

`Race` の主なフィールド: `race_id`, `race_name`, `race_number`, `race_date`, `post_time`, `post_time_source`, `post_time_fetched_at`, `venue`, `venue_code`, `round_number`, `day_number`, `distance`, `track_type`（芝/ダート/障害）, `direction`, `track_condition`（良/稍重/重/不良）, `weather`, `race_class`, `race_grade`, `age_condition`, `sex_condition`, `weight_condition`, `prize_money`（1着、万円）, `entry_count`。

コース欄が `芝→ダート` でも、レース名かカテゴリに `障` があるときは `track_type` は `障害`。距離文字列の `障` は、同じ文字列の `芝` / `ダート` より先に読む。

`post_time` は印字された発走時刻。`15時30分` や `15:45発走` は `15:30+09:00` のように Asia/Tokyo のオフセット付きで持つ。発走の語が無い時計（測定の `7時00分` など）は入れない。無ければ null。`post_time_source` は `jra_html` または `netkeiba_html`。`post_time_fetched_at` はその値を得た時刻。

週次一覧の `track_bias` は null。そこでは当日バイアスを判定しない。

## 出馬表 `RaceCard`

```bash
python -m backend.cli card 202605030811 --json
```

`race` は上の `Race`。この面では `track_bias` が入る（後述）。`entries` は `RaceEntry`。

| フィールド | 意味 | null |
|---|---|---|
| `bracket_number` | 枠番 1–8 | 必須 |
| `horse_number` | 馬番 | 必須 |
| `horse_name` | 馬名 | 必須 |
| `horse_id` | netkeiba の馬ID | リンクが無いとき null。この馬は `/horses` に出ない |
| `sex` | 牡/牝/セ | ページに無いとき null |
| `age` | 馬齢 | ページに無いとき null |
| `weight` | 斤量 | 空なら null |
| `jockey_name` / `jockey_id` | 騎手 | 無いとき null |
| `trainer_name` / `trainer_id` | 調教師 | 無いとき null |
| `owner_name` | 馬主 | 無いとき null |
| `horse_weight` / `horse_weight_diff` | 馬体重と増減 | 空なら null |
| `odds` | 出馬表の単勝。期待値には使わない | 空なら null。`----` や `取消` から価格は作らない |
| `popularity` | 人気 | 空なら null |
| `status` | `entered`（出走）/ `scratched`（取消・出走取消）/ `excluded`（除外・競走除外）/ `unknown` | ページから区分が読めないとき null。`finished` はレース前には使わない |
| `status_label` | ページに印字された区分のまま（`取消`、`出走取消`、`除外`、`競走除外`） | 出走は null |
| `source` | `jra_html` / `jra_js` / `netkeiba_html` / `netkeiba_js` / `headless` | 読めないとき null |
| `fetched_at` | その区分を得た時刻 | 読めないとき null |

印字が無い出走馬は `entered`。`status_label` は null。`unknown` は、区分の欄に `取消` 以外の短い文字があるとき。その文字が `status_label`。JRA の印字を優先し、JRA が無いときは netkeiba。両方に印字があるときも JRA。馬番は作らない。過去走の着順（`td.past` の `取消` / `除外`）と `非当選・非抽選馬情報` は今走の印字ではない。馬番の欄自体が `取消` の行は残す。その馬番は、並びの中で欠けている整数（4 の次が `取消` でその次が 6 なら 5）。その整数が隣の馬番と重なるときは作らない。

出馬表の表が空、または区分が埋まっていないときは、ページ内の JSON、ページ自身が呼ぶ同一サイトの URL、入っていればヘッドレス描画の順で足す。403 とログイン画面は止め、User-Agent の差し替え、captcha、ログインはしない。ブラウザが無いときは `headless browser is not installed`。区分がそれでも無い馬は `status` が null で、`warning` に `scratch status was not on the page`。

`warning` の例:

- `scratch status was not on the page`
- `JRA shutuba returned 403`
- `JRA shutuba requires login`
- `netkeiba shutuba returned 403`
- `netkeiba shutuba requires login`
- `headless browser is not installed`

出馬表に調教・血統・脚質は無い。それらは `horse` と `/horses`。

netkeiba の出馬表と accessD（`POST https://www.jra.go.jp/JRADB/accessD.html`）を重ねる。片方が落ちたときは残った方。accessD の HTTP 403 はデータセンター IP の遮断で、netkeiba の表は残る。認証変数は無い。

## 馬 `Horse` と出走馬 `HorseBasicInfo`

```bash
python -m backend.cli horse 2019104308 --json
```

`GET /api/horses/{horse_id}` は成績ページ `https://db.netkeiba.com/horse/result/{horse_id}/` の、ログイン無しで見える過去走。馬名と通算は `https://db.netkeiba.com/horse/{horse_id}/`。結果ページの見出しに名前があるときは、馬ページが無くても名前を埋める。

netkeiba のログイン、Cookie、資格情報の環境変数は無い。403、captcha、ログイン画面はそこで止め、User-Agent の差し替えや別の身元では取りにいかない。表が要求走数より短く、ページ自身が呼ぶ同一サイトの成績 URL がある、または本文に「読み込み中」があるときは、その HTML に埋め込まれた JSON、その URL、入っていればヘッドレス描画の順で足す。ページが続きはログインと示しているときは、その先へ進まない。ブラウザが無いときは `headless browser is not installed`。すでに表に書いてある行は残す。

| フィールド | 意味 | null / 空 |
|---|---|---|
| `horse_id` / `horse_name` / `sex` | 識別。性別は牡/牝/セ | 必須 |
| `english_name` / `birth_date` / `birth_year` / `coat_color` | プロフィール | 無いとき null |
| `sire_name` `sire_id` `dam_name` `dam_id` `damsire_name` `damsire_id` | `pedigree` と同じ平坦な写し | 辺が無いとき null |
| `breeder_name` / `breeder_location` / `owner_name` | 生産・馬主 | 無いとき null |
| `trainer_name` / `trainer_id` / `training_center` | 調教師、美浦/栗東 | 無いとき null |
| `total_races` / `total_wins` | ページの「通算成績」（`16戦8勝`）。行数から勝ち数は作らない | 読めないとき null |
| `total_earnings` | 中央と地方の獲得賞金を万円で足した整数 | 無いとき null |
| `performances` | ログイン無しで見える過去走。今走の結果ではない。各行のフィールドは従来どおり | 失敗時は `[]` |
| `runs_fetched` | 返した走数。`performances` の長さ | 失敗時は 0 |
| `login_required_for_more` | ページが、返した走より先はログインが必要と示しているとき true。全成績でログイン不要と示しているとき false | どちらとも示していなければ null |
| `source` | 近走を得た方法。`netkeiba_html` / `netkeiba_js` / `headless`。混ざるときは `netkeiba_html+netkeiba_js` | 1走も無いとき null |
| `fetched_at` | 取得時刻 | 必須 |
| `pedigree` | 後述。検証後はオブジェクト | JSON では null にしない |
| `running_style` | 後述。検証後はオブジェクト | JSON では null にしない |
| `track_bias` | 馬単体は開催日が無い | 条件もラベルも null |
| `training` | 後述。検証後はオブジェクト | 未取得は `training not loaded` |
| `warning` | 近走が取れない、または公開分が要求走数より少ない理由 | 要求を満たせば null |

`warning` の値:

- `horse result fetch failed`
- `horse result table not found`
- `horse result table malformed`
- `no performances on horse result page`
- `horse result returned 403`
- `horse result requires login`
- `horse result blocked by captcha`
- `fewer than N recent runs available without login`（N は要求走数。`Horse` は 9、出走馬一覧は 5）
- `headless browser is not installed`（まだ足りず、Playwright が無いときだけ。ヘッドレスを切ったテストでは出さない）

1走も取れない失敗は上の取得失敗の文のまま。公開分が要求より少ないときは足りない走数の文。途中の 403 や、ブラウザが無いことは `; ` でつなぐ。ログイン画面で 1 走も無いときは `horse result requires login` と `login_required_for_more: true`。403 は `login_required_for_more` を true にしない（null のまま）。続きがログインと分かっていても、見えている行は返す。

`HorsePerformance`（`performances` と、出走馬の `recent_performances`）:

| フィールド | 意味 |
|---|---|
| `race_date` | その走の日付。必須 |
| `race_id` / `race_name` / `venue` | レース。`race_name` と `venue` は必須 |
| `distance` | メートル。読めなければ null |
| `track_type` | 芝/ダート/障害 |
| `track_condition` | 馬場。`稍` は `稍重` |
| `weather` | 天候 |
| `entry_count` | 頭数。脚質の三等分に使う |
| `horse_number` / `rank` | 馬番、着順 |
| `odds` / `popularity` | その走の単勝と人気。脚質には使わない |
| `jockey_name` / `weight` | 騎手、斤量 |
| `horse_weight` / `horse_weight_diff` | 馬体重 |
| `finish_time` / `margin` / `passing_order` / `last_3f` | タイム、着差、通過、上がり3F |
| `prize_money` | 万円。小数を残す（例: `50298.2`） |

水分量や馬場指数の列はタイムにも上がりにも入れない。

`GET /api/races/{race_id}/horses` は同じ成績ページの、ログイン無しで見える過去走で、最大5走。当該レースの確定結果ではない。`horse_id` が無い枠は落とす。`sex` が空のときは `不明`。`age` が空のときは `0`（null ではない。0 を実年齢にしない）。各馬に `runs_fetched`、`login_required_for_more`、`source`、`fetched_at`、`warning` を載せる。意味は `Horse` と同じ。要求は 5 走なので、公開分が 5 未満の `warning` は `fewer than 5 recent runs available without login`。

| | `Horse` | `HorseBasicInfo` |
|---|---|---|
| 近走 | `performances`（ログイン無しの全走） | `recent_performances`（最大5走） |
| 足りないときの warning | 公開分が 9 走未満 | 公開分が 5 走未満 |
| 脚質の母数 | 返した全走 | その最大5走 |
| 血統 | プロフィール表と血統表 | 血統表だけ |
| 調教 | 馬名検索（ログイン時）と speedkeiba | その `race_id` の調教ページ、履歴、speedkeiba |
| `track_bias` | 開催日が無くすべて null | そのレースの出馬表と同じオブジェクト |

## 確定結果 `SettledRaceResult`

```bash
python -m backend.cli result 202605030811 --json
```

ページは `https://race.netkeiba.com/race/result.html?race_id={race_id}`。UTF-8。出馬表でも `/horse/result/{horse_id}/` でもない。

| フィールド | 意味 | 欠測 |
|---|---|---|
| `race_id` | レースID | 必須 |
| `race_name` / `race_date` / `venue` | ページの見出しと `kaisai_date`。日付はレースIDの桁からは作らない | 無いとき null |
| `distance` / `track_type` / `track_condition` / `weather` | 結果ページに書いてあるときだけ | 無いとき null。`稍` は `稍重` |
| `race_class` | `RaceData02` のクラスの span（`オープン`、`未勝利`、`1勝クラス` など） | 印字が無ければ null。レース名からは作らない |
| `entry_count` | ページの `N頭` | 印字が無ければ null |
| `fetched_at` | 取得時刻。呼び出し側が渡さないときは Asia/Tokyo のオフセット付き（`+09:00`） | 必須 |
| `http_status` | 結果ページか公式払戻の取得が HTTP 401 / 403 で止まったときのステータス | 拒否以外は null。払戻金の「403円」では付けない |
| `login_required` | ログイン画面で止まったとき true | 既定 false |
| `source` | 着順を得た方法。`netkeiba_html` / `netkeiba_js` / `headless`。混ざるときは `netkeiba_html+netkeiba_js` | 失敗時 null |
| `complete` | 走った頭数（取消・除外を除く）が `N頭` から取消・除外を引いた数と一致し、各馬に `status` があり、速報の上位表だけではないとき true | 失敗時 false |
| `result_phase` | `confirmed`（確定）か `provisional`（確定前・暫定・未確定）。印字、払戻表と全着順、または JRA の払戻表 | 決められなければ null。払戻が無く局面も null なら警告を出す。最終結果は `result_phase` が `confirmed` かつ `complete` が true のときだけ |
| `results` | `SettledFinisher`。結果表の並び。未入着も含む | 失敗時 `[]` |
| `payouts` | `RacePayout`。金額がある馬券種だけ | 失敗時 `[]` |
| `payout_note` | 金額を作らなかった馬券種の注記 | 無ければ null |
| `warning` | 着順が取れない、または頭数・区分・確定印・公式払戻が揃わない理由。複数は `; ` でつなぐ | 揃っていれば null |

`results` はカードの全頭。着順表がページの `N頭` より短い、速報の `FirstDisplay` だけの表（`Table_Show_All` が無く、`5着まで` / `全着順を見る` かちょうど5行）、空、壊れているときは、そのページに埋め込まれた JSON、ページ自身が呼ぶ同一サイトの結果 JSON、入っていればヘッドレス描画の順で足す。着順表が無い（未確定）ときはこの順に進まない。403 とログイン画面は止め、別の身元では取りにいかない。ログインは環境変数の資格情報を渡したセッションだけ。

`complete` の頭数は、返した行から取消（`scratched`）と除外（`excluded`）を引いた数と、`N頭` から同じ取消・除外を引いた数を比べる。中止と失格は走った馬として数える。速報が `N頭` を見えている5頭に書き換えていても、`Table_Show_All` の無い `FirstDisplay` だけの表は `complete: false` と `race result incomplete: N runners on the early result, full order not on the page`。`N頭` の方が大きいときは `race result incomplete: 5 of 16 runners` のように走った頭数で書く。

ヘッドレスは結果 URL で、`domcontentloaded` のあと着順表の行数が `N頭` 未満なら待ち、速報の5行ではないところまで待つ（15秒）。行数の比較は `rows.length < starters`。タイムアウトはその短い HTML をパースし、`complete: false` のまま返す。もう一度取りにはいかない。401/403 はこの待ちの前に止め、再試行しない。

速報の `FirstDisplay` だけの `result.html`（`Table_Show_All` が無い、または走った頭数が `N頭` より短い）はページキャッシュに入れない。入れたあとに同じ URL を読んでも、その短い表は返さない。着順表の無い未確定ページと、`result_phase` が `provisional` のページも保存しない。すでに保存されていても再生しない。`Table_Show_All` の無い5行だけの表は、本当の5頭立てと速報が同じ印字なので `complete: false` のままにする。普通の `HorseList` の5頭立ては complete。

`--wait-complete`（API は `wait_complete=true`）は `complete` になるまで同じ結果ページを取り直す。`--retry-interval` の既定は 60 秒で、それより短い指定は 60 秒に上げる。`--max-wait` の既定は 600 秒。どちらも 0 以上の有限値だけ受け付ける。`nan` と `inf` と負数は CLI が拒否し、API は 422。次の間隔が上限を超えるときは待たず、その時点の結果を返す。待ちを止めるのは取得側の `http_status`（401 / 403）か `login_required` で、警告文に `403` という数字があるだけでは止めない。払戻が `403円` や `1403円` でも待ちは続く。公式払戻の取得そのものが HTTP 403 のときは `http_status: 403` と `JRA payout returned 403` で、待たずに止める。既定の `result` は1回だけ取る。

`result_phase` は、まず結果ヘッダの印字（`.RaceResultState`、`[data-result-phase]`、`.RaceList_Item02` 内の `.Race_Fixed` / `.Race_Provisional`、またはそのノード自身の短い文字が `確定` / `確定前` / `結果確定` / `結果確定前` / `未確定` / `暫定`）。タイトルの「結果・払戻」、meta の「速報」、ナビの「ゴール後 30秒速報」は局面にしない。印字が `確定前` のときは、払戻表があっても `provisional` のまま。出典は着順と同じ `source` と `fetched_at`。

印字が無いとき、払戻表に円の金額があり、着順が全頭（速報の `FirstDisplay` だけの表ではなく、行数がページの `N頭` と一致する）なら `confirmed`。出典は同じく着順の `source` と `fetched_at`。

結果ページで決まらないとき、JRA の払戻ページがそのレースの払戻表（金額のある行）を出していれば `confirmed`。そのレース塊に `確定` / `確定前` の短い印字があるときはその印字。このときだけ `source` に `jra_html` または `headless` を足す。`fetched_at` は結果取得の時刻。`レース結果` という見出しや上位3頭の表だけでは決めない。

どちらでも決められないときは `result_phase` を null のままにし、`result phase was not on the page` を出す。着順が揃って `complete` が true でも、払戻が無く局面が null ならこの警告は残す。警告を出さないのは、印字、払戻表と全着順、または JRA の払戻表で局面が決まったときだけ。利用者は `result_phase` が `confirmed` で、かつ `complete` が true のときだけ、そのレースを最終結果として扱う。`complete` が true で `result_phase` が null の結果は確定ではない。複数の結果文書が食い違うときは null と `result phase sources disagree`。

`SettledFinisher`:

| フィールド | 意味 | 欠測 |
|---|---|---|
| `rank` | 公式の着順 | 取消・除外・中止・競走中止・失格は null。着順は作らない |
| `status` | `finished` / `scratched`（取消・出走取消）/ `excluded`（除外・競走除外）/ `did_not_finish`（中止・競走中止）/ `disqualified`（失格）/ `unknown` | 着順欄が空なら null。その行があると `complete` は false |
| `status_label` | ページに印字された区分のまま（`取消`、`除外`、`中止`、`競走中止`、`失格`） | 入着は null |
| `demoted` | その行に `降着` と印字があるとき true。着順は公式の順位のまま | 印字が無ければ false。ページ末尾の「降着・失格のルール」は対象外 |
| `bracket_number` から `passing_order` | 従来どおり。`last_3f` は後3F | 空セルから数値は作らない |
| `source` | その行を得た方法。`netkeiba_html` / `netkeiba_js` / `headless` | 失敗時 null |
| `fetched_at` | その行を得た時刻 | 失敗時 null |

`unknown` は区分がある（`status` は null ではない）。印字が `取消` など以外で数字でもないとき、`status_label` はその文字、`rank` は null。

同着は同じ `rank` を複数行に残す。着順欄の `1` と `1同着`、着差の `同着` は入着。着順はページの並びのまま。馬番や着順で並べ直さない。次の着順が飛んでも（1着同着の次が3着）、`complete` は頭数と `status` だけを見る。

`refunds` は `payouts` の隣。各払戻行には混ぜない。結果ページか JRA の払戻ページに返還の印字があるときだけ入れる。印字が無いときは null。空のリストにはしない。金額は作らない。

| フィールド | 意味 | 欠測 |
|---|---|---|
| `horse_numbers` | 返還馬番。ページの並び | 馬番が無く枠だけなら `[]` |
| `bet_types` | 印字された馬券種（単勝、複勝、枠連など） | 書いていなければ null |
| `bracket_numbers` | `返還同枠` の枠番 | 書いていなければ null |
| `source` | `netkeiba_html` / `netkeiba_js` / `headless` / `jra_html`。両方なら `netkeiba_html+jra_html` | |
| `fetched_at` | その返還を得た時刻 | |

馬番が取消（`scratched`）か除外（`excluded`）と一致するときは、返還があること自体では `warning` を足さない。入着馬や中止・失格の馬番が返還に出たら `refund horses are not scratched or excluded: 2`。netkeiba と JRA の馬番が違うときは netkeiba を残し、`JRA refund mismatch: netkeibaのみ: 2番; JRAのみ: 5番`。JRA にしか印字が無いときは `source` は `jra_html`。ページに無い馬番は作らない。

`RacePayout`: `bet_type` はページの表記（単勝、複勝、枠連、馬連、ワイド、馬単、3連複、3連単）。`combination` はページの並び（馬単・3連単は着順どおり）。`payout` は円。`popularity` が両方のページに無いときは null。ページに無い馬券種は要素にしない。各要素に `source`（`netkeiba_html` / `netkeiba_js` / `headless`。人気だけ JRA を記録したときは `netkeiba_html+jra_html`、ブラウザなら `+headless`）と `fetched_at`（結果取得の時刻。人気の採用もこの時刻）。

払戻があるとき、結果ページに印字された競馬場・回・日・レース番号・開催日から JRA の公開払戻（`accessH`、メニューに印刷された cname）を突き合わせる。金額は置き換えない。同じ馬券種の行は全部残す（1着同着の単勝が2行、複勝やワイドが増える）。行の順番が違うこと、片方だけ人気が空であることは不一致にしない。人気だけが違うときも不一致にしない。記録する人気は JRA の数字で、金額と組み合わせは結果ページのまま。`payout_note` に `JRA popularity: 複勝 2 140円 netkeiba 3人気, JRA 4人気` のように違う行だけ書く。片方の人気が空のときは注記しない。金額か組み合わせが違うときは `JRA payout mismatch: netkeibaのみ: …; JRAのみ: …`。警告に載せるのは違う馬券だけ。一致した馬券種は書かない。ページが取れない、開催が一つに決まらないときは `JRA official payout was not reachable`。401/403 は `JRA payout returned 403` で止め、次のページは開かない。金額は作らない。

`warning`（取得そのものが失敗したときは、この文だけ。確定印や払戻の注記は足さない）:

- `race result fetch failed`
- `race result not found`
- `race not yet settled`
- `race result table malformed`
- `no finishers on race result page`
- `race result returned 403`
- `race result requires login`

揃い方の注記（上と `; ` でつなぐことがある）:

- `race result incomplete: card runner count was not on the page`
- `race result incomplete: 5 of 8 runners`（取消・除外を引いたあと。速報で `N頭` が残っているとき）
- `race result incomplete: 5 runners on the early result, full order not on the page`（速報が頭数まで5にしているとき）
- `race result incomplete: runner missing status: 2、7`
- `result phase was not on the page`（印字も払戻表と全着順も JRA の払戻表も局面を示さないとき。着順が揃っていても払戻が無ければ出す。出さないのは局面が決まったときだけ）
- `result phase sources disagree`
- `headless browser is not installed`（表がまだ短く、Playwright が無いときだけ。`headless` を切ったテストでは出さない）
- `JRA official payout was not reachable`
- `JRA payout returned 403`
- `JRA payout mismatch: …`
- `refund horses are not scratched or excluded: …`
- `JRA refund mismatch: …`

ヘッドレスは任意。`pip install -e ".[headless]"` と `playwright install chromium`。未インストールでもコマンドは動く。ブラウザの UA はそのまま。403 を避けるための UA 変更、captcha、資格情報の回避はしない。同じ取得順は馬場・調教でも使う。馬場の測定時刻とキャッシュは下の「コース・開催・馬場」にある。

### JRA の取得モード

CLI の `--fetch-mode` と API の `fetch_mode` は `html`（既定）、`headless`、`auto`。対象は JRA 公式（accessD の出馬表、accessO のオッズ、accessH の払戻突き合わせ、馬場情報）。netkeiba の近走、競馬ブックの調教、netkeiba へのフォールバックは変えない。

`html` は現行の HTTP。`headless` は Playwright で描画し、ページ内の JSON を数える。値は描画後の DOM を現行のパーサで読む。`source` はブラウザが文書を返したときだけ `headless`。`fetched_at` は今までどおり。JRA だけの返還は `source` が `headless`。netkeiba と足したときは `netkeiba_html+headless`。`auto` はブラウザがあれば headless、無ければ html で、未インストールの警告は出さない。

`headless` でブラウザが無い、または描画に失敗したときは HTTP に戻り、`headless browser is not installed` または `headless render failed` を付ける。401/403、captcha、ログイン画面は HTTP に戻さず、null と今までの警告で止める。UA はブラウザ自身。差し替え、プロキシ、回避のための再試行はしない。間隔は 1.5 秒以上。

確定オッズのパーサは、見出しか表（`title`、`h1`〜`h4`、`caption`、`th`）にある `確定オッズ` と `最終オッズ` を確定とする。メニュー、ボタン、ナビの同じ文言は確定にしない。過去レースの確定オッズも同じ判定を使う。JRA の確定後ページが見出しに `最終オッズ` と出すとき、そのページの価格は確定引用になる。発売中の数字をこのラベル無しで確定にはしない。

オッズの cname はページに印刷されたものを使う。メニューの `doAction` が先にあり、レースの `151ou` があるときはレース側を使う。`Z/` は最終オッズのボタン。チェックサムは計算しない。

Playwright を入れる CI ワークフローはこのリポジトリに無い。ローカルページを描く4件（馬場、確定オッズ、取消、着順）は、`pip install -e ".[headless]"` と `playwright install chromium` が済んだ環境では実行する。無い環境では skip のまま。skip 条件は外さない。

レース前の `Race` と `Horse` に `status`、`status_label`、`demoted`、`complete`、`result_phase` は無い。`RaceEntry` にはレース前の `status`（`entered` / `scratched` / `excluded` / `unknown`）と `status_label` がある。`demoted`、`complete`、`result_phase` は確定結果だけ。

`payout_note` の文:

- `払戻が結果ページに無い`
- `払戻に無い馬券種は空のまま: …`
- `払戻の対応が取れない馬券種は空のまま: …`
- `価格が無い払戻行は空のまま: …`

この面の馬券種はオッズ API の `bet_type`（`win` など）とは別名。予想の価格には使わない。

## オッズ `RaceOdds`

期待値はここ。既定は単勝、`prefer=live`。

| クエリ / CLI | 値 |
|---|---|
| `bet_types` / `--bet-types` | `win` `wide` `umaren` `sanrenpuku`。カンマ区切り。未知の名前は捨てる。全部未知なら `reason` は `win odds were not requested` |
| `prefer` / `--prefer` | `live` または `yoso`。`kind` は書き換えない |

取得は netkeiba の `GET https://race.netkeiba.com/api/api_get_jra_odds.html`（`compress=0`、`action=init`）。type は単勝 1、馬連 4、ワイド 5、三連複 7。JSON は UTF-8。

`RaceOdds`:

| フィールド | 意味 |
|---|---|
| `race_id` | レースID |
| `bet_types` | 要求して対応した馬券種。並びは要求順 |
| `prefer` | 呼び出し側の希望。実データの `kind` ではない |
| `status` | `live` / `yoso` / `unavailable`。引用が全部 `yoso` のときだけ `yoso`。ひとつでも `live` なら `live` |
| `reason` | 全部空のときの理由。1件でも引用があれば null。馬券種が混ざる失敗は `wide: …; umaren: …` |
| `encoding` | 常に `utf-8` |
| `race_phase` | `pre` または `post`。馬券種で食い違うとき、判別できない失敗は null |
| `quotes` | `OddsQuote`。失敗時は `[]` |
| `non_runners` | 取消・除外の馬。`horse_number`、`status`（`scratched` / `excluded` / `unknown`）、`status_label`、`source`、`fetched_at`。ページを読んで無いときは `[]`。読めないときは null |
| `warning` | 取消・除外が読めない理由。価格の失敗は `reason`。読めれば null |
| `fetched_at` | 取得時刻。必須 |

`race_phase` は API の `status` から決める。`yoso` と `middle` は `pre`（レース前）。`result` は `post`（レース後の確定オッズ）。

`OddsQuote`:

| フィールド | 単勝 `win` | ワイド `wide` | 馬連 `umaren` | 三連複 `sanrenpuku` |
|---|---|---|---|---|
| `combination` | 馬番 `1`（1–18） | `3-7`（小さい順） | `3-7`（小さい順） | `1-2-5`（小さい順、重複なし） |
| `odds` | 単一価格。`----` や `取消` のときは null | null | 単一価格。`----` や `取消` のときは null | 同左 |
| `odds_min` / `odds_max` | null | 両方必須。下限が上限を超えない。`----` や `取消` のときは両方 null | null | null |
| `source` | 下表 | 同左 | 同左 | 同左 |
| `kind` | `yoso` または `live` | 同左 | 同左 | 同左 |
| `fetched_at` | 必須 | 必須 | 必須 | 必須 |
| `market_updated_at` | API の `official_datetime` があるときだけ | 同左 | 同左 | 同左 |
| `non_runner` | その馬が取消・除外のとき true | 組のどれかが取消・除外のとき true | 同左 | 同左 |
| `withheld` | ページが `----` または `取消` のとき true | 同左 | 同左 | 同左 |
| `popularity` | ページか API の人気。無いときは null | 同左。配列の3列目 | 同左 | 同左 |

価格が無い行、ワイドで範囲の片側しか無い行は捨てる。ページが `----` または `取消` / `除外` / `出走取消` / `競走除外` と出している行は残す。価格は null、`withheld` は true。数値は作らない。組のページが行自体を出していないときは、その組は作らない。取消馬を含む組でページが価格を出しているときは、その価格を残し `non_runner` を true。変動値は範囲ではないので馬連・三連複の `odds_min` / `odds_max` には入れない。HTTP 400 の本文に数値が載っていても価格にしない。ワイドの掲載上限 `9,999.9` で 2 列目が `0` または負の変動のときは、両端がその上限の点範囲。`2.9` / `0.0` のような片側だけの行は捨てる。

`non_runners` はレース単位の取消・除外。JRA のオッズページ（無ければ出馬表の印字）を優先し、JRA が 403 またはログインのときはそこで止め、netkeiba の印字を残す。`warning` は `JRA odds returned 403`、`JRA odds requires login`、`netkeiba odds returned 403`、`netkeiba odds requires login`、`JRA odds fetch failed`、`JRA odds page was not on the card`、`headless browser is not installed`。403 本文から馬番は作らない。価格の欠測は従来どおり `reason`。

`source` と `kind`:

| API `status` | `source` | `kind` | スナップショット `status` | `race_phase` |
|---|---|---|---|---|
| `yoso` | `netkeiba_yoso` | `yoso` | `yoso` | `pre` |
| `middle` | `netkeiba_live` | `live` | `live` | `pre` |
| `result`（数値あり） | `netkeiba_live` | `live` | `live` | `post` |

型としての引用 `source` は `netkeiba_yoso` | `netkeiba_live` | `jradb`。`kind=yoso` は `source=netkeiba_yoso` だけ。`kind=live` に `netkeiba_yoso` は入らない。`jradb` は `kind=live` のトークンとして許される。価格の引用は netkeiba の API で、引用の `source` に `jradb` は書かない。取消・除外の `source` は別で、`jra_html` / `jra_js` / `netkeiba_html` / `netkeiba_js` / `headless`。accessD / JRADB は出馬表と当日バイアスと、取消・除外の印字。オッズの数値にはしない。CLI という文字列を `source` に入れない。

`prefer=live` で市場が無いとき（`quotes=[]`、`status=unavailable`）、予想へは切り替えない。予想が要るなら `prefer=yoso` を明示する。`status=yoso` の本体は `prefer=live` でも `kind=yoso` のまま。複数馬券種で一部だけ空のときは、空の馬券種は `quotes` に現れず、スナップショットの `reason` は null。要求した `bet_types` と引用の `bet_type` を突き合わせる。

`reason` の例:

- API の文（`result odds empty`、`odds unavailable`）
- `http 400`、本文に理由があれば `http 400: result odds empty`
- `malformed odds payload`
- `invalid odds payload`
- `fetch failed`
- `unsupported odds status: …`
- `win odds were not requested`

レース後で空、HTTP 400 でも `quotes` は空のまま。

形の例（単勝が発売前、ワイドが欠測のときの別リクエスト）:

```json
{
  "race_id": "202605030811",
  "bet_types": ["win"],
  "prefer": "yoso",
  "status": "yoso",
  "reason": null,
  "encoding": "utf-8",
  "race_phase": "pre",
  "quotes": [
    {
      "race_id": "202605030811",
      "bet_type": "win",
      "combination": "7",
      "odds": 4.2,
      "odds_min": null,
      "odds_max": null,
      "source": "netkeiba_yoso",
      "kind": "yoso",
      "fetched_at": "2026-09-28T09:00:00",
      "market_updated_at": null
    }
  ],
  "fetched_at": "2026-09-28T09:00:00"
}
```

```json
{
  "race_id": "202605030811",
  "bet_types": ["wide"],
  "prefer": "live",
  "status": "unavailable",
  "reason": "result odds empty",
  "encoding": "utf-8",
  "race_phase": "post",
  "quotes": [],
  "fetched_at": "2026-09-28T09:00:00"
}
```

ワイドの引用は `combination: "3-7"`、`odds: null`、`odds_min` と `odds_max` に価格。馬連は `combination: "3-7"` と単一の `odds`。三連複は `combination: "1-2-5"` と単一の `odds`。`popularity` はページか API にあるときだけ。無いときは null。

### 過去の確定組み合わせオッズ `FinalRaceOdds`

```bash
python -m backend.cli final-odds 202605040711 --bet-types wide --json
python -m backend.cli final-odds 202605040711 --bet-types umaren --json
python -m backend.cli final-odds 202605040711 --bet-types sanrenpuku --fetch-mode auto --json
```

`GET /api/races/{race_id}/final-odds?bet_types=wide,umaren,sanrenpuku`。既定の馬券種はワイド、馬連、三連複。`win` はこのコマンドでは捨てる。

`FinalRaceOdds` は `RaceOdds` に `result_phase`、`complete`、`odds_final` を足したもの。`encoding` は `utf-8`。`fetched_at` は Asia/Tokyo の `+09:00`。

価格を載せるのは、着順の `result_phase` が `confirmed` かつ `complete` が true のときだけ。そのとき `odds_final` は true、`race_phase` は `post`。それ以外は `quotes` が空、`odds_final` は false、`race_phase` は null、`warning` に理由。払戻からも、発売中の数字からも価格は作らない。

| 局面 | `warning` |
|---|---|
| `result_phase` が `provisional` | `result phase is provisional` |
| `complete` が false、または結果が無い | 着順の警告をそのまま出す（`race result not found`、`race result incomplete: …`）。警告が無いときだけ `race result is not complete` |
| 局面が null で警告も無い | `result phase was not on the page` |
| 結果ページが 403 / ログイン | 着順の警告のまま。オッズのページは開かない |

取り方は、静的 HTML、ページに埋め込まれた JSON、ページ自身が呼ぶ同一サイトの URL、それから `odds` と同じ netkeiba オッズ API（`/api/api_get_jra_odds.html`、`race_id`、`type`、`action=init`、`compress=0`）。価格がまだ無く、発売中の数字でもないときだけヘッドレス。`--fetch-mode html` はヘッドレスを開かない。`headless` はブラウザを使う。JRA が先に開いたブラウザがあればそれを使い、もう一つは起動しない。ブラウザが本当に無いときは HTTP に戻して `headless browser is not installed`。`auto` はブラウザがあれば使い、無ければ警告しない。403、captcha、`CAPTCHA認証`、タイトルが `Just a moment...` そのものか `cf-challenge` などのチャレンジ印、ログイン画面はそこで止める。タイトルに `just a moment` という語が含まれるだけではチャレンジにしない。User-Agent の差し替えはしない。JRA が 403 のあとは netkeiba に一度だけ進む。JRA の次のページは開かない。netkeiba が 403 のあとは API もヘッドレスも残りの馬券種も開かない。

ページの見出しか表にある `確定オッズ` と `最終オッズ` はどちらも確定。メニューやボタンの同じ文言は確定にしない。オッズ API は `status` が `result` で組み合わせが空でないときだけ確定価格にする。着順が `confirmed` かつ `complete` なら、ページに見出しが無くてもこの API の価格を確定にする。`middle` と `yoso` は捨てる。`---.-` のような空欄は価格にしない。

ワイドは範囲。払戻が `odds_min × 100` から `odds_max × 100` のあいだ（10円単位の端数）なら一致。馬連と三連複は `払戻 ≈ odds × 100`（10円以内）。違うときは価格を置き換えず、`warning` に `final odds and payout disagree: umaren 1-3`。ページに無い組は作らない。`combination odds were not on the page: wide` のように欠測の馬券種を書く。

過去の確定オッズは `backend/.cache/pages` に残す。オッズ API は `status` が `result` かつオッズが空でないときだけ。`official_datetime` が今日（Asia/Tokyo）以降のときは保存しない。`odds/index.html` は価格を読めたときだけ保存する。`---.-` だけの文書は保存しないし、保存済みでも読み返さない。開催日が今日以降のレースは、結果ページの日付を見てオッズ本文を保存しない。今日以降の開催一覧と JRA の当日ページは従来どおり保存しない。

オッズ API のこのコマンドに無い馬券種（複勝、枠連、馬単、三連単）は、発売中の `odds` では取らない。複勝の確定オッズだけは過去レースが、ページに印字があるとき `bet_type=place` で持つ。確定結果の払戻表にそれらの名前が出ても、発売中のオッズ面の `bet_type` にはならない。

## 過去レース `HistoricalBatch`

バックテスト用。予想の入力に混ぜない。着順・払戻・返還は「確定結果」と同じパーサ（`status`、`status_label`、`complete`、`result_phase`、`refunds`）。確定オッズは「オッズ」の `OddsQuote`（`withheld`、`non_runner`）と `non_runners`。同じ形をここには書き直さない。

```bash
python -m backend.cli history 202505030811 --json
python -m backend.cli history --date 2025-01-05 --venue 東京 --priority-venues --json
python -m backend.cli history --from 2025-01-05 --to 2025-01-06 --flat-only --export jsonl
python -m backend.cli history --date 2025-01-05 --export csv --output backend/csv_output/historical
python -m backend.cli history --from 2025-01-05 --to 2025-01-06 --resume-from 202505010201 --json
```

対象は 2025-01-01 以降。それより前の日は取りにいかない。`warning` は `historical races start in 2025`。`race_id` を直接指定してページの日付が 2025 より前のときは、レコードは返るが `in_scope` は false。日付はレースIDの桁からは作らない。

平地だけが既定（`--flat-only` / `flat_only=true`）。`track_type` が障害のレースは日付の一覧から落とす。`race_id` で障害を直接開いたときは `flat` が false、`in_scope` が false、`warning` に `jump races are excluded`。`--no-flat-only` で残す。

`--venue` は繰り返す。`--priority-venues` は東京、京都、中山、阪神。指定が無い日付の一覧は全場。場が違う `race_id` は `in_scope` false と `venue is outside the filter`。

1レースのフィールド:

| フィールド | 意味 | null |
|---|---|---|
| `surface` | 芝 / ダート | 障害、または印字が無いとき null。障害は騎手・父の芝/ダート集計に入れない |
| `distance` | メートル | 印字が無いとき null |
| `race_class` | 結果ページのクラス | 印字が無いとき null。`race class was not on the page` |
| `field_size` | `N頭`。印字が無く着順があるときはその頭数 | 両方無いとき null |
| `going` | 良 / 稍重 / 重 / 不良 | 印字が無いとき null |
| `flat` | 芝・ダートは true、障害は false | 面が読めないとき null |
| `in_scope` | 2025年以降、平地（指定時）、指定場 | 範囲外は false |
| `result` | `SettledRaceResult`。全着順、払戻、返還 | 失敗時は空の着順と `warning` |
| `final_odds` | 確定の `OddsQuote`。単勝・複勝、ページにあれば馬連・ワイド（`odds_min` / `odds_max`）・三連複 | 確定と言えないときは null。空配列にはしない |
| `non_runners` | 取消・除外 | 結果ページが読めないときは null。読んで無いときは `[]` |
| `odds_final` | `final_odds` を確定として載せたときだけ true | |
| `source` / `fetched_at` | 着順とオッズの取得元。各馬・各引用・各払戻・各返還も自分の `source` と `fetched_at` を持つ | |

確定オッズにするのは次だけ。

- ページの見出しか表（`title`、`h1`〜`h4`、`caption`、`th`）に `確定オッズ` または `最終オッズ` と印字がある。メニューやボタンは含めない。
- またはレース後のページ（着順表がある。`result_phase` が `provisional` のときは含めない）。netkeiba オッズ API は `race_phase=post`（API の `status=result`）のときだけ。

発売中（`middle` / `yoso` / `race_phase=pre`）の数字は確定オッズにしない。`final_odds` は null、`warning` は `final odds were not confirmed`。結果ページに単勝が印字されていれば、それはレース後のページなので単勝の確定オッズにしてよい。複勝と組み合わせは、そのページに価格が無いときは作らない。払戻からオッズは作らない。

優先は JRA の公開オッズ、次が netkeiba。取り方は HTML、ページ内の JSON、ページ自身が呼ぶ同一サイトの URL、任意のヘッドレス。403 とログイン画面は止め、User-Agent の差し替え、captcha、ログインはしない。ブラウザが無いヘッドレス段は `headless browser is not installed`。JRA が 403 のあとは netkeiba に進んでよい。JRA の次のページは開かない。

複勝は `bet_type=place`。範囲なら `odds_min` / `odds_max`、単一価格なら `odds`。発売中の `odds` コマンドは複勝を取りにいかない。ワイドは従来どおり範囲。馬連・三連複は単一価格。`----` と `取消` は残し、価格は null、`withheld` は true。取消・除外は `non_runner` と `non_runners`。

ページに無い市場は null ではなく、その引用が無い。`warning` の例:

- `final odds were not confirmed`
- `final odds were not on the page`
- `final win odds were not on the page`
- `place odds were not on the page`
- `combination odds were not on the page: umaren, wide, sanrenpuku`
- `race class was not on the page`
- `field size was not on the page`
- `going was not on the page`
- `surface was not on the page`
- `jump races are excluded`
- `venue is outside the filter`
- `historical races start in 2025`
- `JRA odds returned 403`
- `headless browser is not installed`
- `stopped at {race_id}: race result returned 403`
- `stopped at {race_id}: race result requires login`
- `race list returned 403`
- `race list requires login`

単勝の確定オッズと、単勝・ワイド・馬連・三連複の払戻が揃えばバックテストに足りる。組み合わせの確定オッズはページにあるときだけ。

日付範囲は、先に範囲内のレースIDを並べ、その順に1レースずつ取る。レースとレースのあいだは既定 1.5 秒（`--delay`）。キャッシュにあるページのあいだは待たない。HTTP もその間隔を空ける。取ったページは `backend/.cache/pages` に残す。保存時に `replayable` をバージョン（現行は 2）で付ける。現行の版が付いた結果以外のページは、読み出し時にログイン画面や 403 の本文を解析しない。`replayable: true` は以前の印で、拒否の判定が厳しくなったあとも拒否チェックを飛ばしていた。その古い印と、印の無いファイルは、拒否かどうかを調べ直す。調べ直しはパスと mtime ごとに一度だけ覚え、同じファイルを読むたびに解析し直さない。`result.html`、netkeiba のオッズ API、`odds/index.html` は印の版に関わらず本文を見る。その完全性の判断もパスと mtime ごとに一度。キャッシュの `fetched_at` にタイムゾーンが無いときは、Asia/Tokyo の時計として `+09:00` を付けて返す。オフセット付きの値はそのまま返す。確定かつ complete の `result.html` は開き直さない。速報、着順が短いページ、着順表の無い未確定ページ、`result_phase` が `provisional` のページは保存しない。すでに保存されていても再生しない。次の実行が取り直す。開催日が今日（Asia/Tokyo）以降の `race_list.html` / `race_list_get_date_list.html` / `race_list_sub.html` も保存しない。日付の無い開催一覧も保存しない。過去の `kaisai_date` はキャッシュしてよい。今日以降の JRA `accessH`（払戻）、`accessO`（オッズ）、`accessD`（出馬表）と、日付の無いそれらのメニューも保存しない。cname の `ymd` が今日より前のページは保存してよい。cname から日付が読めないページは、リクエストの `race_date`（`YYYYMMDD` または `YYYY-MM-DD`）か、プロジェクト形式の `race_id`（`YYYYMMDD` のあとに場コードとレース番号）が今日より前のときだけ保存する。netkeiba の12桁（年・場・回・日・レース）からは日付を作らない。今日以降と、日付がまだ分からないメニューは保存しない。オッズ API（`api_get_jra_odds.html`）は `status` が `result` で、`data` が空でなく、組み合わせが一つ以上あるときだけ保存する。`data` が `""`、null、空のオブジェクト、空の配列の `result` は保存しない。`middle` や `yoso` は保存せず、次の実行が取り直す。`official_datetime` が今日（Asia/Tokyo）以降の結果も保存しない。開催日が今日以降と分かっている確定オッズの取得は、その本文を保存しない。`odds/index.html` は価格を読めたときだけ保存する。`---.-` だけの文書は保存しないし、保存済みでも読み返さない。書き込み時にログイン画面、`403 Forbidden`、`Access Denied`、captcha、`CAPTCHA認証` や `キャプチャ` などの日本語の captcha 見出し、Cloudflare のチャレンジは保存しない。タイトルが `Just a moment...` そのもの、または `cf-challenge` などのチャレンジ印があるページは拒否で、そこで止め、別の身元では取りにいかない。タイトルに `just a moment` という語が含まれるだけではチャレンジにしない。`第403回` や `403万円`、払戻の `403円` は拒否ではない。


結果ページが `race result returned 403` または `race result requires login` のとき、そのレースのオッズも、それより後のレースも開かない。同じ実行ではその URL を再試行しない。User-Agent の差し替え、captcha、ログインはしない。開催一覧が 403 またはログイン画面のときは、その日以降の一覧を開かない。すでに並んだレースだけ取り、`warning` は `race list returned 403` または `race list requires login`。JRA のオッズだけが 403 のときは、そのレースの netkeiba に一度だけ進み、日付範囲は止めない。JRA の次のページは開かない。

止めたときの `HistoricalBatch` と書き出し:

| フィールド | 意味 |
|---|---|
| `stopped_at_race_id` | 403 またはログイン画面だったレースID。開催一覧で止まりレースIDが無いときは null |
| `completed_race_ids` | この実行で取り終えた範囲内のレースID。止めたレースは含めない |
| `remaining_race_ids` | まだ取っていないID。止めたレースを先頭に、一覧の順 |
| `warning` | `stopped at {race_id}: race result returned 403` のように止めた理由 |

同じ日付範囲をもう一度実行すると、`completed_race_ids` の結果ページはキャッシュから読む。ネットワークは止めたレースからで、完了済みは取り直さない。`--resume-from`（`resume_from`）は、そのIDより前をキャッシュだけにし、キャッシュが無いIDは開かない。

書き出しは範囲内（`in_scope`）だけ。`progress.json` に上の4つを書く。JSON の `HistoricalBatch` と、export の応答も同じフィールドを持つ。

| 形式 | 中身 |
|---|---|
| `jsonl` | `historical.jsonl`。1レース1行。`surface` で分ける。`progress.json` に停止位置 |
| `csv` | `runners.csv` は1頭1行。`runners_芝.csv` と `runners_ダート.csv` は面ごと。列は `surface`、`distance`、`race_class`、`field_size`、`going`、馬番、馬名、着順、`status`、`final_win_odds`、`popularity`、単勝・ワイド・馬連・三連複の払戻。`progress.json` に停止位置 |

## レース傾向 `RaceTrendReport`

確定したレースの集計。予想の入力に混ぜない。行は `history` と同じ確定着順・確定オッズ・払戻。ページは `backend/.cache/pages` を使う。確定かつ complete の `result.html` は開き直さない。未確定の `result.html` と、今日（Asia/Tokyo）以降の開催一覧、今日以降の JRA 払戻・オッズ・出馬表、`status` が `result` でないか `data` が空か `official_datetime` が今日以降のオッズ API、価格の無い `odds/index.html` は開き直す。日付範囲の終了日は含む。今日を落とす past-only は無い。`progress.json` は history の書き出しが作るだけで、trends は読まない。開催一覧が空の日は `no meeting on YYYY-MM-DD`。一覧の取得に失敗した日は `race list failed for YYYY-MM-DD`。403 とログイン画面は今までどおり止める。

```bash
python -m backend.cli trends --today --venue 東京 --json
python -m backend.cli trends --date 20261018 --venue 東京 --surface 芝 --json
python -m backend.cli trends --from 2025-06-01 --to 2025-06-08 --venue 東京 --surface 芝 --distance 1600 --class 未勝利 --going 良 --json
python -m backend.cli trends --recent-meeting --venue 東京 --as-of 2025-06-08 --json
```

`GET /api/races/trends`。`today` も `date` も `date_from` も `recent_meeting` も無いときは 400。`today=true` と別の日付を一緒に渡したときも 400。欠測の集計は 200 で、rate は null、`warning` を見る。

| クエリ | 意味 |
|---|---|
| `venue` | 競馬場。繰り返す。無くてもよい |
| `surface` | `芝` または `ダート` |
| `distance` | メートル。印字と一致 |
| `class` | クラス。印字と一致 |
| `going` | 良 / 稍重 / 重 / 不良 |
| `today` | true のとき、開催日は今日（Asia/Tokyo）の1日。`date` や `date_from` とは一緒に使えない |
| `date` | その開催日だけ。`YYYYMMDD` または `YYYY-MM-DD`。読めない文字列は 400 |
| `date_from` / `date_to` | 開催日。含む。`YYYYMMDD` または `YYYY-MM-DD`。読めない文字列は 400。`date_to` の当日も範囲に入る |
| `recent_meeting` | true のとき、当日の確定とその競馬場の前開催日だけ。当日の既定は今日（Asia/Tokyo） |
| `as_of` | recent meeting の当日。無ければ今日（Asia/Tokyo） |

`--today` と `--date` は、その1日のレースだけを見る。競馬場は任意。開催一覧が `li.RaceList_DataItem` を持たず、ページの script が `race_list_get_date_list.html` を呼ぶときは、その先の `race_list_sub.html` を開催一覧にする。拾いの `race_id` リンクだけではその日のカードにしない。日付一覧が 403 またはログイン画面のときは、その拾いリンクで代えない。

`--recent-meeting` は競馬場が一つ必要。前開催日は、当日の前日以前でその競馬場のレースが並んだ最も近い日。空の日は飛ばす。一覧が 403 またはログイン画面のときは、それより前の日も、レースページも開かない。User-Agent の差し替え、captcha、ログインはしない。120日見ても無いときは `previous_meeting_date` は null。

集計に入れるのは `result_phase` が `confirmed` かつ `complete` が true で、出走馬ごとに単勝の確定オッズがあるレース。最終結果でないレースは `excluded` に理由を書き、`races_pending` に数える。着順やオッズは作らない。理由の例: `race not yet settled`、`result phase is provisional`、`result phase was not on the page`、`race result is not complete`、`race result incomplete: …`。`warning` は `1 race is not final and was left out: {race_id}` または `N races are not final and were left out: {race_id}, …`。件数のあとにレースIDを書く。403 とログイン画面は `excluded` に書くが `races_pending` には入れない。確定かつ complete でも単勝の確定オッズが無いレースは `excluded`（`final win odds were not on the page`）で、pending ではない。フィルタと印字が違うレースは除外一覧に入れない。印字が無くてフィルタを適用できないレースは入れる（`distance was not on the page` など）。

`included_races` は入れたレースごとに `race_id`、`source`、`fetched_at`。`fetched_at` は Asia/Tokyo のオフセット `+09:00` を持つ。`included_race_ids` はその `race_id` だけで、同じ順。`source` と `fetched_at`（レポート全体）は入れたレースをまとめたもの。時刻が違うときは `fetched_at` は null。

各表の `sample_size` は出走馬数。取消・除外は入れない。`sample_size` が 0 の rate と ROI は null。0.0 にはしない。1頭の区分も落とさない。

| 表 | 区分 |
|---|---|
| `by_popularity` | 1 / 2 / 3 / 4-6 / 7-9 / 10+。空の帯も残す。`surface` を付けないときは芝とダートを合わせた数 |
| `by_gate` | 馬番。出た番だけ。合わせた数 |
| `by_frame` | 枠番。出た枠だけ。合わせた数 |
| `by_running_style` | 逃げ / 先行 / 差し / 追込。0頭でも残す。合わせた数 |
| `by_surface` | `surface` が `芝` または `ダート`。中に `by_popularity` / `by_gate` / `by_frame` / `by_running_style`。`surface` クエリが無いときは芝とダートの両方。指定したときはその面だけ。障害は入れない。0頭の面も残す |
| `by_jockey` | 騎手 × 芝/ダート。面は混ぜない |
| `by_sire` | 父 × 芝/ダート。父が取れない馬は行にしない |

`win_rate` / `top2_rate` / `top3_rate` は頭数で割った占有率（小数4桁）。

`win_roi` と `place_roi` は `(回収円 / (100円 × 価格が付いた頭数)) - 1`。0 が元返し。単勝の回収は、その馬番の単勝払戻。払戻が無く確定オッズがある勝ち馬はオッズ×100。両方があって1円でも違うときは、その馬を ROI に入れず `win payout and final win odds disagree`。平均は作らない。

複勝は、出走が8頭以上なら3着まで、5頭以上7頭以下なら2着まで。4頭以下は `place_roi` を null にし、`place betting is not sold when fewer than 5 horses start`。入着馬の回収は複勝払戻。単一の確定複勝オッズは、払戻が無いときだけオッズ×100。範囲（`odds_min` / `odds_max`）の中央値は作らず、`place odds are a range and were not used`。

脚質は1レースの通過順で、4区分（逃げ・先行・差し・追込）。定義は `running_style_definition`。`basis` は `passing_order_first_call`。使うのは最初の数字（`13-12-12-7` の 13）。4コーナー（最後の数字）は使わない。最初の通過順位が 1 の馬は逃げで、三等分に入れない。それ以外の馬は `ceil(field_size / 3)` までが先行、`ceil(field_size * 2 / 3)` までが差し、それより後ろが追込。`ceil(field_size / 3)` が 1 のときは先行は 0 頭。頭数はページの `field_size`。読めない走はラベルにしない。0頭や1頭の区分も `sample_size` 付きで残す。

父は履歴の着順に無い。馬IDがある馬だけ、既存の血統（`/horse/ped/{horse_id}/`）を1回読む。キャッシュにあれば開き直さない。父が無いときはその行を作らず、`runner_omissions` に血統の `warning`。403（`pedigree returned 403`）かログイン画面のあとは、残りの馬の血統を開かない。

レポートの `source` は入れたレースの source を `+` でつないだもの。無ければ null。各レースの source は `included_races` にある。`fetched_at` は入れたレースの時刻が全て同じときだけ。違うときは null と `fetched_at differs across included races`。`fetched_at_min` と `fetched_at_max` は観測した端。`computed_at` は集計した時刻で、Asia/Tokyo のオフセット `+09:00` を持つ。もう一度実行すると、その日に確定したレースが `included_races` に足される。未確定のページを確定済みとして返さない。

## 血統 `pedigree`

`horse` と `/horses` に載る。出走馬一覧は `https://db.netkeiba.com/horse/ped/{horse_id}/` の `table.blood_table` だけ。馬単体はプロフィール表（`父` / `母` / `母父`）が先で、名前と ID が揃わないとき血統表。`class="blood"` の表は先頭2リンクが父と母。3番目を母父にはしない。

| フィールド | 意味 |
|---|---|
| `sire` / `dam` / `damsire` | `{ "name", "horse_id" }`。その辺が無いときは null |
| `source` | `profile` / `netkeiba_ped` / `profile+netkeiba_ped`。取れないときは null |
| `warning` | 父と母が揃えば null。母父が表に無ければ `damsire` だけ null で warning は null |

名前はリンク文字のまま。`(米)` のような国表記は残す。すでに入っている名前は上書きしない。ID は同じ名前のときだけ足す。行数が奇数、または母のセルが父と同じ rowspan のときは `pedigree table malformed`。

`warning`:

- `pedigree fetch failed`
- `pedigree returned 403`
- `pedigree requires login`
- `pedigree table not found`
- `pedigree table malformed`
- `pedigree not on profile or ped page`
- `pedigree incomplete`

## 脚質 `running_style`

馬ページに脚質の項目は無い。成績の「ペース」列（`37.1-33.4` のようなラップ）は逃げ・先行でもハイ・スローでもない。通過順の最初の数字だけを使う。`13-12-12-7` の初角は 13。数字とハイフン以外は捨てる。頭数が無い、順位が頭数を超える走はラベルにしない。

- 初角が 1 なら逃げ。
- それ以外は頭数を三等分する。`front_end = ceil(頭数 / 3)`、`mid_end = ceil(頭数 * 2 / 3)`。先行は `front_end` まで、差しは `mid_end` まで、残りは追込。
- `label` は判定できた走が 3 走以上で、最多が一つだけのとき。未満や同数は null。内訳は `counts`（逃げ・先行・差し・追込）。
- `confidence` は `label` の占有率（小数4桁）。`label` が null なら null。
- `sample_size` はラベルを付けた走数。`unusable_runs` は付けなかった走数。
- `data_sufficiency` は `insufficient`（3走未満、または1走も判定できない）、`thin`（3走以上だが5走未満、信頼度 0.6 未満、または同数）、`sufficient`（5走以上かつ信頼度 0.6 以上）。
- `basis` は `passing_order_first_call`。1走も判定できないときは null。
- 1400m 以下を短距離とする（`short_distance_m`）。障害は数えない。短距離の逃げ・先行が 1〜2 走なら `short_distance_front_under_specified` が true。全体の `label` は消さない。感度を上げるかは EV。

`warning`:

- `passing order unavailable`
- `running style field size unavailable`
- `running style sample too small`
- `running style tie`

`label` が付いたとき `warning` は null。

## 当日バイアス `track_bias`

馬場状態・芝/ダート/障害・競馬場・距離は、そのページに書いてあるときだけ `going` / `surface` / `venue` / `distance`。`不明` や距離 0 は null。`going` は 良/稍重/重/不良。

`venue` は出馬表の開催ヘッダ（`RaceData02` の競馬場名）が、その `race_id` の場と一致するときだけ。netkeiba の ID は `年 + 場 + 回 + 日 + レース`。日次（2日目の `02`）は場コードではない。`02` は函館の場コードだが、東京2日・京都2日の ID からは函館にしない。ナビや 403 本文に場名が出ていても写さない。ヘッダが別の場なら `venue` は null。

`inside_outside` と `pace_bias` は、公式の事前ページがバイアスの見出しとして印刷しているセルだけ。見出しは `内外` / `内外バイアス` / `枠バイアス`、または `ペース` / `ペースバイアス` / `展開バイアス`。セルは空白を畳んだ短いラベル（16字まで）。`傷み`、`内柵`、`コース`、`回り`、`メートル` を含む文は捨てる。同じ項目がページ内で食い違うときも捨てる。

ソース:

- accessD / JRADB の出馬表。ラベルがあれば `source` は `accessd`。
- JRA 馬場情報の当週タブ（`https://www.jra.go.jp/keiba/baba/index.html` と `index2.html`。そのページの `.kaisai_tab` が指す `index3.html` 以降も）。タイトルの競馬場がこのレースと一致したページだけ。ラベルがあれば `jra_baba`。両方に同じ文字があるときは `accessd+jra_baba`。
- 二つのページの同じ項目が違う文字なら、両方 null。`warning` は `day-of track bias sources disagree`。

写さないもの: 出馬表の `（芝・右 外）`（外回りであり内外バイアスではない）、使用コース（A/B/C）、内柵、芝の傷み、含水率、クッション値、馬場状態そのもの（`going` には写すがバイアスラベルにはしない。馬場の数値は `track`）、オッズ、人気、レース名、結果の通過順、予想ブログ、netkeiba のトラックバイアス（匿名 HTML に値が無く、公式の事前ラベルではない）。

`data_sufficiency`: 内外とペースが両方あるとき `sufficient`（`warning` は null）、片方だけ `thin`、無いとき `unavailable`。

ライブの出馬表に見出しが無いことは多い。そのときラベルは null。欠測であり、取得エラーとは別。`warning` が `not published` を含んでも HTTP は 200。

`warning` の本体:

- `day-of track bias unavailable without a race card`（`horse`。accessD は開かない）
- `day-of track bias not loaded from accessD/JRADB`（条件だけ写し、まだ見出しを見ていない内部値。出馬表の応答では取得後の文に置き換わる）
- `day-of inside/outside and pace bias not published on accessD or JRA baba`
- `day-of inside/outside and pace bias not published on accessD; JRA baba has no page for this venue`
- `day-of inside/outside and pace bias not published on accessD`
- `day-of track bias unavailable: JRA accessD returned 403`
- `day-of track bias unavailable: accessD card not on the current menu`
- `day-of track bias unavailable: race id cannot be mapped to accessD`
- `day-of track bias fetch failed`
- `day-of pace bias not published`（内外だけある）
- `day-of inside/outside bias not published`（ペースだけある）
- `day-of track bias sources disagree`

馬場タブの結果が理由を変えるとき、上の文のあとに次が続く。

- `; JRA baba did not publish inside/outside or pace bias`
- `; JRA baba has no page for this venue`
- `; JRA baba returned 403`
- `; JRA baba fetch failed`

403 は資格情報不足ではない。個別の `/keiba/baba/{venue}.html` は見ない。認証用の環境変数は無い。

見出しがあるときの形:

```json
{
  "venue": "中山",
  "surface": "芝",
  "distance": 1600,
  "going": "稍重",
  "inside_outside": "内有利",
  "pace_bias": "先行有利",
  "source": "accessd",
  "data_sufficiency": "sufficient",
  "warning": null
}
```

`horse` の形:

```json
{
  "venue": null,
  "surface": null,
  "distance": null,
  "going": null,
  "inside_outside": null,
  "pace_bias": null,
  "source": null,
  "data_sufficiency": "unavailable",
  "warning": "day-of track bias unavailable without a race card"
}
```

近走の馬場・距離・芝ダートは `performances` の各行。当日の内外とペースは出馬表の `race.track_bias` と、`/horses` の各馬に載る同じオブジェクト。

## 調教 `training`

`horse` と `/horses` の各馬。出馬表の `RaceEntry` には無い。ページに書いてある時計・短評・矢印・平均だけ。レース名は speedkeiba の記事を選ぶ鍵にだけ使い、時計には入れない。

### ソースと環境変数

| `source` | ページ | いつ |
|---|---|---|
| `keibabook_smart` | `https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}` | `race_id` が12桁。列名（`5F`、`半哩`、坂路の `4F`）がある表。匿名 HTML に載っている馬だけ |
| `keibabook_crireki` | `https://p.keibabook.co.jp/db/uma/{umacd}/crireki` | ログインできたとき、まだ時計が無い馬 |
| `speedkeiba` | 今週の重賞記事（見出しに「調教」） | 競馬ブックに時計が無いとき。馬名は `h4` と空白を除いて一致 |
| null | | 取れない、壊れている、ログインが要る、記事が曖昧。要約だけ残るときは source を残すことがある |

`https://s.keibabook.co.jp/...`（列名が無い Smart 画面）と中間時計 `/cyukan` は読まない。位置からハロンは作らない。

環境変数は調教だけ。

| 変数 | 役割 |
|---|---|
| `KEIBABOOK_LOGIN_ID` | `KEIBABOOK_PASSWORD` と両方あるとき `POST /login/login`（フォームの `login_id` / `pswd` / `_token`） |
| `KEIBABOOK_PASSWORD` | 上と組。パスワードがある経路では Cookie よりこちら |
| `KEIBABOOK_COOKIE` | パスワードが無いときだけ送り、`POST /logincheck/logincheck` で確認 |

両方のログイン項目が空、かつ Cookie も空なら匿名。ログインしたことにはしない。ログインチェックが「ログアウト」または `/mypage/` を含み「ログインしていない」を含まないときだけ成功。それ以外は `training keibabook login failed`。

ログイン失敗でも、匿名の調教ページ（`keibabook_smart`）に既にある時計は残す。speedkeiba に時計があればそれも残す。失敗で `training keibabook login failed` になるのは、まだ時計が無く履歴を読めなかった馬だけ。`horse` はレースの調教ページを開かないので、匿名のときに残るのは speedkeiba の時計だけ。

`umacd` は netkeiba の馬IDから作らない。レースでは出馬表 `https://p.keibabook.co.jp/cyuou/syutuba/{race_id}` の馬名リンク `/db/uma/{umacd}` だけ。同じ馬名が別の umacd を指すときは捨てる。血統の `/sanku` は使わない。

`horse` はレースIDが無い。ログインできたとき、`https://p.keibabook.co.jp/db/search/horse` に馬名を送る。netkeiba プロフィールの生年月日（馬ページに日付があるときはその日付、検索行は生年）と性別が一致し、候補が1頭のときだけ履歴を読む。生年月日が無い、複数一致、不一致は `training unavailable: keibabook umacd was not on a source page`。認証が無い `horse` は speedkeiba の今週の記事を最大12本見て、馬名が一つの記事だけにあるとき埋める。二つ以上は `training ambiguous on speedkeiba`。

時計の優先は、調教ページ、調教履歴、speedkeiba。壊れた競馬ブックの時計は捨て、speedkeiba に時計があればそちら。

### `workouts` の行

| フィールド | 意味 | null |
|---|---|---|
| `date` | 年月日がページから決まるときだけ | 年が無い月日、`■`、`◇`、speedkeiba の「前回連対時」は null |
| `date_label` | セルの文字のまま | |
| `long_gap` | 日付の先頭が `*` または `＊` のとき true | 記号だけの日付は null |
| `course` | `美Ｗ`、`美坂`、`美浦 W`、`栗東 坂` | |
| `condition` | 略記のまま（`不` を不良にしない。`稍` も略記のまま） | |
| `distance` | 列名があるとき一番長い時計の列 | 列名が無い行は null |
| `time` | その列の時計、または列名の無い行の文字列（`51.9 – 37.1 – 11.5` は分解しない） | |
| `splits` | `{ "label", "time" }`。空セルは入れない | 無ければ `[]` |
| `rank_in_group` | 回り位置 `［８］` / `[8]`。全角も読む | 空なら null |
| `rider` / `gait` / `comment` / `mark` | 騎乗者、脚色、短評、`☆` | 空なら null |
| `partner_note` | 併せの行の全文。`0.9秒` のような差は時計にしない | |
| `final_workout` | `tr.oikiri` または `☆` のときだけ true | それ以外は null |
| `slope_reps` | 坂路の `N回` | 無ければ null |
| `section` | speedkeiba の `今回` / `前回連対時` | 競馬ブックでは null |

坂路、または `6F(坂路)` のセルが `N回` のときは、注記「坂路は4F・3F・2F・1F」に合わせ、`5F(4F)` を `4F`、`半哩(3F)` を `3F`、`3F(2F)` を `2F`、`1F(1F)` を `1F`。平地の `半哩` はメートルにしない。月日だけの日付は見出しの `YYYY年M月D日` を基準に、それより後の月日は前年。年がページに無い月日は `date` も `long_gap` も null。時計セルに数値以外が入っている馬は、その馬の行を全部捨て `training table malformed`。

### `summary`

ページが印刷しているものだけ。平均も矢印もこちらでは計算しない。

| フィールド | 意味 |
|---|---|
| `comment` / `arrow` | 競馬ブックの追い切り短評と矢印。矢印は評価点にしない |
| `evaluation` | speedkeiba の評価文 |
| `average` | `{ "label", "course", "time" }`。`(平均)` 行。こちらでは計算しない |
| `laps` | ラップ表の `今走` だけ。`label` / `phase`（一週前・本追切）/ `course` / `splits`。`好調` と `凡走` は人気・着順・着差から作られた行なのでコピーしない |

### 十分さ

- `sufficient`: 日付・時計・コースが揃った行が2本以上。`warning` は null。
- `thin`: 時計かページの要約はあるが、日付つきの時計が2本未満。`warning` は `training sample thin`。
- `unavailable`: 取れない、壊れている、ログインが要る、記事が曖昧。

`warning`:

- `training fetch failed`
- `training table not found`
- `training table malformed`
- `training not on keibabook page without login`
- `training not on keibabook page without login, and not on the speedkeiba article`
- `training unavailable: not on speedkeiba weekly index; keibabook training history requires login`
- `training ambiguous on speedkeiba`
- `training sample thin`
- `training not loaded`
- `training keibabook login failed`
- `training history empty on keibabook`（「調教履歴データがありません」）
- `training history requires keibabook login`（本文が会員案内のまま）
- `training unavailable: keibabook umacd was not on a source page`

匿名ページにその馬が無いときの形:

```json
{
  "workouts": [],
  "summary": null,
  "source": null,
  "data_sufficiency": "unavailable",
  "warning": "training not on keibabook page without login, and not on the speedkeiba article"
}
```

時計がある行の形は README の「調教」節（フィクスチャ `keibabook_cyokyo.html` と `keibabook_crireki.html`）と同じ。

## コース・開催・馬場

予想の本体は上の面。こちらは条件の補助。

`Course`: `venue_code`, `venue_name`, `location`, `established`, `capacity`, `turf_circumference`, `dirt_circumference`, `turf_straight`, `dirt_straight`, `layouts`, `conditions`。長さがページに無いときは null。`layouts` の `inner_outer` はコースの内回り/外回りであり、`track_bias.inside_outside` ではない。

`CourseCondition`: `venue`, `condition_date`, `turf_condition`, `turf_moisture`, `dirt_condition`, `dirt_moisture`, `weather`, `temperature`, `wind_direction`, `wind_speed`。

`WeeklyVenueSchedule`: `venue`, `venue_code`, `dates`, `round_number`, `conditions`, `fetched_at`。

`TrackInfo`（`GET /api/tracks` の値、`cli track --json` の各値）: `racecourse`, `date`, `weather`, `weather_source`, `weather_fetched_at`, `turf`, `dirt`, `grass_info`, `weekly_info`, `source`（`JRA` / `Netkeiba` / `JRA+Netkeiba`。オッズの `source` とは別の語）, `measured_at`, `is_today`, `stale`, `http_status`, `login_required`, `warning`, `post_time`, `post_time_source`, `post_time_fetched_at`, `scraped_at`。

`http_status` と `login_required` は馬場ページの取得が HTTP 401 / 403 またはログイン画面で止まったときだけ付く。本文に `403` という数字があっても付けない。JRA が 403 のとき、このフラグが付いた JRA の値は null のまま、同じ場の netkeiba 公開ページへ戻る。その netkeiba のページに測定時刻が無ければ `stale` は null。`stale` が null なことは待ちの理由にしない。

`measured_at` はページに印字された測定時刻（Asia/Tokyo の ISO、例 `2026-10-16T12:00:00+09:00`）。JRA では馬場状態・クッション値・含水率の各節のうち最も新しい印字。週間情報の時刻（`10月18日（日曜） 8時現在` など）は測定にしない。節を開いた見出しより下の段の見出しは、知らない名前なら節の中に残す。h2 の中の h4 `測定時刻` は節を終わらせない。その下のドロップダウンの時刻は測定に入る。`週間情報`、`使用コース`、`芝の状態`、`散水情報` は、どの段の見出しでも、見出しでない行やラベルでも、その節を終わらせる。h2 の下の h3 `週間情報` も、行の `散水情報` も、含水率の節を終わらせ、その時刻は `measured_at` に入らない。データ行の `計測情報` のような語では終わらない。年が印字に無いときは null。`fetched_at` からは作らない。`発走` の時刻に加え、`発表予定` など発表・予定と印字された時刻も測定時刻にしない。除外は、その時刻から同じ行の次の時刻まで（次が無ければ行末まで）の、その時刻に属する文字だけを見る。`15時40分 ブラジルC 発走` のように、その時刻の直後にある `発走` は測定にしない。`5時30分 測定 発走は15時40分` の 5:30 は測定で、`発走は` は次の時刻側である。`7時00分 測定（次回発表 12時）` の 7:00 は測定で、括弧内の次回発表は 12時側である。前の行や次の行の `発表予定` は別の行の測定を消さない。分の無い `8時現在` は `08:00`、`8時半` は `08:30`、`正午現在` は `12:00`（Asia/Tokyo）。同じ行の `発走`・`発表`・`予定` は、分の無い時刻でも、その時刻に属するときだけ測定にしない。各クッション値・含水率は自分の `measured_at` と `source` と `fetched_at` を持つ。時刻の無い数値は現在の測定ではない。`measured_at` は null、`is_today` と `stale` も null のまま、`warning` に `moisture reading has no measurement time and is not current` または `cushion reading has no measurement time and is not current` を付ける。`is_today` は測定日が今日（Asia/Tokyo）のとき true。測定時刻が無ければ null。`stale` は測定日がレース日と違うとき true。レース日は `--date`（`YYYYMMDD` または `YYYY-MM-DD`）。省略したときは今日。日付として読めない文字列は `stale` を null のまま `track race date was not a date`。馬場ページに印字された別の日をレース日には使わない。netkeiba の開催日は、選択中のタブの `kaisai_date` を優先する。`script` や `style` の中の日付（例 `2026/04/02`）は開催日にしない。

測定日がレース日でないときは `warning` に `track measurement is not the race date: YYYY-MM-DD`。応答ヘッダ（`x-cache` / `x-cache-status` / `cf-cache-status` / `x-akamai-cache`）が `HIT` のときは `track response was a cached copy`。`Age` だけでは HIT にしない。数値は HIT の本文のまま返す。

JRA の `/keiba/baba/` は開催のたびに同じ URL なのでページキャッシュに入れない。netkeiba の `track.html` は `kaisai_date` が無い、読めない、今日かそれ以降のときだけキャッシュしない。過去の `kaisai_date` はキャッシュしてよい。リクエストは `Cache-Control: no-cache` と `Pragma: no-cache` を送る。クエリでキャッシュを壊すこと、UA の差し替えはしない。ヘッドレスの馬場 URL も同じヘッダを付ける。馬場ページは、ページ自身が `#cushion_list` または `#moist_list` に測定時刻を入れるまで待つ（12秒）。時間切れはその時点の HTML をパースし、もう一度取りにはいかない。401/403 はこの待ちの前に止める。

`--wait-fresh`（API は `wait_fresh=true`）は、依頼したレース日が今日（Asia/Tokyo）で、かつ印字された測定日がその日より前のときだけ取り直す。`--date` を省略したとき、過去の日、未来の日は、`stale: true` と今までの警告を付けたまますぐ返す。測定日が今日かそれより後のとき、測定時刻が無いとき（`stale` が null）も待たない。間隔は `--retry-interval`（既定 60 秒、それ未満は 60 秒）、上限は `--max-wait`（既定 600 秒）。どちらも 0 以上の有限値だけ。`nan` / `inf` / 負数は CLI が拒否し、API は 422。JRA が HTTP 403 またはログインのときは `http_status` / `login_required` が付くので待たない。そのとき netkeiba へ戻っても、測定時刻が無ければ `stale` は null のままで、`--wait-fresh` は待たずに返す。警告文に `403` という数字があるだけでは止めない。統合しても JRA の数値を netkeiba の新しい数値で置き換えない。`stale` は残ったクッション値・含水率の印字から付け直す。

芝/ダートの `condition` は 良/稍重/重/不良/不明。印字された馬場状態には `condition_source` と `condition_fetched_at` が付く。`moisture.goal_front` と `fourth_corner` は印字された含水率のまま。凡例や馬場状態からは作らない。`cushion.value` は印字されたクッション値の数値。硬度目盛の 12 / 10 / 8 / 7 や「硬め」は入れない。`cushion.grade` は同じ欄に印字された区分だけ。数値や目盛から区分は計算しない。印字が無ければ null。数値がページに無いときは `warning` に `cushion value was not on the page` を足す。`grass_info.course_name` は使用コース（A/B/C）。バイアスにはしない。`merge=true` のとき、場が取れたソースがある場合だけ統合結果はキー `merged`。

クッション値、含水率、馬場状態、天候、発走時刻の各値は、得た方法を `source`（`jra_html` / `jra_js` / `netkeiba_html` / `netkeiba_js` / `headless`）と `fetched_at` で持つ。`headless` は Playwright。ブラウザが無いときは `headless browser is not installed` を警告して、その段で止める。403 とログイン画面もそこで止め、User-Agent の差し替え、captcha、ログインはしない。

JRA は個別の `/keiba/baba/{venue}.html` を見ない。当週の `index.html` と `index2.html`（cp932）を読み、依頼した場のタイトルがどちらにも無いときは、そのページの `.kaisai_tab` が指す `indexN.html` だけを続ける。施設ページは見ない。タイトル `馬場情報（○○競馬場）` が依頼した場のタブだけを読む。HTML が取れて `#cushion_num` が空のとき、またはクッション値・含水率の数値に測定時刻が無くページ自身が `_data_cushion.html` / `_data_moist.html`（またはそれを読む `/keiba/baba/_js/`）を指しているときは、その同一サイトのファイルを次に読む。測定時刻が付いた数値を優先し、静的 HTML の時刻の無い数値を置き換える。`measured_at` を付ける。データファイルを読んでも時刻が無いときだけ、任意のヘッドレス描画を使う。静的な数値しか無いときはその数値を返し、`measured_at` は null、上の warning を付ける。現在の測定としては扱わない。参照が無く時刻も無い数値ではヘッドレスを開かない。目盛表 `#cushion_relation` は使わない。403 とログイン画面では止め、その先のファイルもヘッドレスも開かない。`/JRADB/accessJ.html` は追わない。403、タイトルの無い本文、別場のタブでは、その JRA オブジェクトの `racecourse` も `date` も null。`turf.cushion` と `turf.moisture` / `dirt.moisture` も null。`warning` は `JRA baba returned 403`、`JRA baba requires login`、`JRA baba page is not the requested meeting`、`JRA baba page could not be parsed`、`JRA baba fetch failed` のいずれか。

JRA が 403、またはクッション値の数値が無いときは、同じ場・同じ日の netkeiba 公開ページへ戻る。`sources` が `jra` だけのときも `netkeiba` キーを足す。JRA 側のオブジェクトは 403 の null のまま。netkeiba も、選択中のタブが依頼した場のときだけ数値を写す。HTML、ページ内の JSON、ページ自身が呼ぶ同一サイトの URL、任意のヘッドレス描画の順。403 とログインは止め、`netkeiba track returned 403` または `netkeiba track requires login`。失敗しても HTTP は 200 で、この形の JSON を返す。`cli track --json` は見出しを混ぜず、このオブジェクトだけを出す。クッション値・含水率・馬場状態は 403 本文やレース名からは作らない。

`post_time` は出馬表と同じく、印字された発走時刻を `15:40+09:00`（Asia/Tokyo）で持つ。`post_time_source` と `post_time_fetched_at` が付く。発走が印字されていなければ null。測定時刻は発走時刻にしない。

掲載が無くソース自体が空のときの HTTP は 404。

## 対象外

- 組み合わせ確率、期待値、買い目
- 予想ブログ、netkeiba マスターコースのトラックバイアス
- オッズ API に無い馬券種の新規取得
- 競馬ブック Smart 画面の列位置からのハロン推定
- 認証が無いときの馬個別の調教履歴（時計は作らない）
- オッズ、人気、レース名、血統からの脚質・調教・バイアス
