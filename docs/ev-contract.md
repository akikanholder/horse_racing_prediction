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
- 馬のトップレベル `warning` は近走の取得失敗だけ。血統・脚質・調教・バイアスの理由は各オブジェクトの `warning`。
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
| `odds` `result` `track` | JSON だけ |
| `races` `card` `horse` `courses` `venues` | 日本語の見出しのあと JSON |

混ざるコマンドは標準出力全体を `json.loads` しない。末尾の JSON 値を読む。見つからないとき（出馬表・馬・コース）は日本語の1行だけで、`--json` でも JSON は出ない。終了コードは 0。例外は標準エラーに出て終了コード 1。

HTTP は JSON だけ。見つからないレース・馬・コースは 404。サービス未初期化は 503。スクレイパ例外は 500。オッズと確定結果は欠測でも 200 で、本体の `status` / `warning` を見る。

## CLI と HTTP

`race_id` の例は netkeiba の12桁（年・場・回・日・レース）。調教の競馬ブックはその形のときだけ取りにいく。

| CLI | HTTP | 返すもの |
|---|---|---|
| `python -m backend.cli races --json` | `GET /api/races` | 今週の `RaceSchedule`。`track_bias` は null |
| `python -m backend.cli card <race_id> --json` | `GET /api/races/{race_id}/card` | `RaceCard`。`race.track_bias` あり |
| （card の `race` と同じ） | `GET /api/races/{race_id}` | `Race` だけ |
| `python -m backend.cli horse <horse_id> --json` | `GET /api/horses/{horse_id}` | `Horse`。近走は全走。当日バイアスは null |
| （CLI なし。HTTP のみ） | `GET /api/races/{race_id}/horses` | `HorseBasicInfo` の配列。近走は最大5走 |
| `python -m backend.cli result <race_id> --json` | `GET /api/races/{race_id}/result` | `SettledRaceResult`。予想の入力にしない |
| `python -m backend.cli odds <race_id> --bet-types win,wide,umaren,sanrenpuku --prefer live --json` | `GET /api/races/{race_id}/odds?bet_types=win,wide,umaren,sanrenpuku&prefer=live` | `RaceOdds` |
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

`Race` の主なフィールド: `race_id`, `race_name`, `race_number`, `race_date`, `post_time`, `venue`, `venue_code`, `round_number`, `day_number`, `distance`, `track_type`（芝/ダート/障害）, `direction`, `track_condition`（良/稍重/重/不良）, `weather`, `race_class`, `race_grade`, `age_condition`, `sex_condition`, `weight_condition`, `prize_money`（1着、万円）, `entry_count`。

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
| `odds` | 出馬表の単勝。期待値には使わない | 空なら null |
| `popularity` | 人気 | 空なら null |

出馬表に調教・血統・脚質は無い。それらは `horse` と `/horses`。

netkeiba の出馬表と accessD（`POST https://www.jra.go.jp/JRADB/accessD.html`）を重ねる。片方が落ちたときは残った方。accessD の HTTP 403 はデータセンター IP の遮断で、netkeiba の表は残る。認証変数は無い。

## 馬 `Horse` と出走馬 `HorseBasicInfo`

```bash
python -m backend.cli horse 2019104308 --json
```

`GET /api/horses/{horse_id}` は成績ページ `https://db.netkeiba.com/horse/result/{horse_id}/` の過去走。馬名と通算は `https://db.netkeiba.com/horse/{horse_id}/`。結果ページの見出しに名前があるときは、馬ページが無くても名前を埋める。

| フィールド | 意味 | null / 空 |
|---|---|---|
| `horse_id` / `horse_name` / `sex` | 識別。性別は牡/牝/セ | 必須 |
| `english_name` / `birth_date` / `birth_year` / `coat_color` | プロフィール | 無いとき null |
| `sire_name` `sire_id` `dam_name` `dam_id` `damsire_name` `damsire_id` | `pedigree` と同じ平坦な写し | 辺が無いとき null |
| `breeder_name` / `breeder_location` / `owner_name` | 生産・馬主 | 無いとき null |
| `trainer_name` / `trainer_id` / `training_center` | 調教師、美浦/栗東 | 無いとき null |
| `total_races` / `total_wins` | ページの「通算成績」（`16戦8勝`）。行数から勝ち数は作らない | 読めないとき null |
| `total_earnings` | 中央と地方の獲得賞金を万円で足した整数 | 無いとき null |
| `performances` | 過去走。今走の結果ではない | 失敗時は `[]` |
| `fetched_at` | 取得時刻 | 必須 |
| `pedigree` | 後述。検証後はオブジェクト | JSON では null にしない |
| `running_style` | 後述。検証後はオブジェクト | JSON では null にしない |
| `track_bias` | 馬単体は開催日が無い | 条件もラベルも null |
| `training` | 後述。検証後はオブジェクト | 未取得は `training not loaded` |
| `warning` | 近走が取れない理由 | 取れたら null |

`warning` の値:

- `horse result fetch failed`
- `horse result table not found`
- `horse result table malformed`
- `no performances on horse result page`

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

`GET /api/races/{race_id}/horses` は同じ成績ページの過去走で、最大5走。当該レースの確定結果ではない。`horse_id` が無い枠は落とす。`sex` が空のときは `不明`。`age` が空のときは `0`（null ではない。0 を実年齢にしない）。

| | `Horse` | `HorseBasicInfo` |
|---|---|---|
| 近走 | `performances`（全走） | `recent_performances`（最大5走） |
| 脚質の母数 | 全走 | その最大5走 |
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
| `fetched_at` | 取得時刻 | 必須 |
| `results` | `SettledFinisher`。結果表の並び | 失敗時 `[]` |
| `payouts` | `RacePayout`。金額がある馬券種だけ | 失敗時 `[]` |
| `payout_note` | 金額を作らなかった馬券種の注記 | 無ければ null |
| `warning` | 着順が取れない理由 | 取れたら null |

`SettledFinisher`: `rank`（取消・除外・中止・失格は null のまま行に残す）, `bracket_number`, `horse_number`, `horse_name`, `horse_id`, `weight`, `jockey_name`, `finish_time`, `margin`, `last_3f`（後3F）, `popularity`, `odds`, `passing_order`。空セルから数値は作らない。

`RacePayout`: `bet_type` はページの表記（単勝、複勝、枠連、馬連、ワイド、馬単、3連複、3連単）。`combination` はページの並び（馬単・3連単は着順どおり）。`payout` は円。`popularity` が無いときは null。ページに無い馬券種は要素にしない。

`warning`:

- `race result fetch failed`
- `race result not found`
- `race not yet settled`
- `race result table malformed`
- `no finishers on race result page`

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
| `fetched_at` | 取得時刻。必須 |

`race_phase` は API の `status` から決める。`yoso` と `middle` は `pre`（レース前）。`result` は `post`（レース後の確定オッズ）。

`OddsQuote`:

| フィールド | 単勝 `win` | ワイド `wide` | 馬連 `umaren` | 三連複 `sanrenpuku` |
|---|---|---|---|---|
| `combination` | 馬番 `1`（1–18） | `3-7`（小さい順） | `3-7`（小さい順） | `1-2-5`（小さい順、重複なし） |
| `odds` | 単一価格 | null | 単一価格 | 単一価格 |
| `odds_min` / `odds_max` | null | 両方必須。下限が上限を超えない | null | null |
| `source` | 下表 | 同左 | 同左 | 同左 |
| `kind` | `yoso` または `live` | 同左 | 同左 | 同左 |
| `fetched_at` | 必須 | 必須 | 必須 | 必須 |
| `market_updated_at` | API の `official_datetime` があるときだけ | 同左 | 同左 | 同左 |

価格が無い行、ワイドで範囲の片側しか無い行は捨てる。変動値は範囲ではないので馬連・三連複の `odds_min` / `odds_max` には入れない。HTTP 400 の本文に数値が載っていても価格にしない。

`source` と `kind`:

| API `status` | `source` | `kind` | スナップショット `status` | `race_phase` |
|---|---|---|---|---|
| `yoso` | `netkeiba_yoso` | `yoso` | `yoso` | `pre` |
| `middle` | `netkeiba_live` | `live` | `live` | `pre` |
| `result`（数値あり） | `netkeiba_live` | `live` | `live` | `post` |

型としての `source` は `netkeiba_yoso` | `netkeiba_live` | `jradb`。`kind=yoso` は `source=netkeiba_yoso` だけ。`kind=live` に `netkeiba_yoso` は入らない。`jradb` は `kind=live` のトークンとして許される。今のオッズ取得は netkeiba の API だけで、引用の `source` に `jradb` は書かない。accessD / JRADB は出馬表と当日バイアスであり、オッズ引用ではない。CLI という文字列を `source` に入れない。

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

ワイドの引用は `combination: "3-7"`、`odds: null`、`odds_min` と `odds_max` に価格。馬連は `combination: "3-7"` と単一の `odds`。三連複は `combination: "1-2-5"` と単一の `odds`。

オッズ API に無い馬券種（複勝、枠連、馬単、三連単）は取らない。確定結果の払戻表にそれらの名前が出ても、オッズ面の `bet_type` にはならない。

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

`inside_outside` と `pace_bias` は、公式の事前ページがバイアスの見出しとして印刷しているセルだけ。見出しは `内外` / `内外バイアス` / `枠バイアス`、または `ペース` / `ペースバイアス` / `展開バイアス`。セルは空白を畳んだ短いラベル（16字まで）。`傷み`、`内柵`、`コース`、`回り`、`メートル` を含む文は捨てる。同じ項目がページ内で食い違うときも捨てる。

ソース:

- accessD / JRADB の出馬表。ラベルがあれば `source` は `accessd`。
- JRA 馬場情報の当週タブ（`https://www.jra.go.jp/keiba/baba/index.html` と `index2.html`）。タイトルの競馬場がこのレースと一致したページだけ。ラベルがあれば `jra_baba`。両方に同じ文字があるときは `accessd+jra_baba`。
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

`TrackInfo`（`GET /api/tracks` の値）: `racecourse`, `date`, `weather`, `turf`, `dirt`, `grass_info`, `weekly_info`, `source`（`JRA` または `Netkeiba`。オッズの `source` とは別の語）, `scraped_at`。

芝/ダートの `condition` は 良/稍重/重/不良/不明。`moisture` は `goal_front` と `fourth_corner`。`cushion.value` が null のとき硬さは「不明」。`grass_info.course_name` は使用コース（A/B/C）。バイアスにはしない。`merge=true` のとき統合結果はキー `merged`。掲載が無い HTTP は 404。

## 対象外

- 組み合わせ確率、期待値、買い目
- 予想ブログ、netkeiba マスターコースのトラックバイアス
- オッズ API に無い馬券種の新規取得
- 競馬ブック Smart 画面の列位置からのハロン推定
- 認証が無いときの馬個別の調教履歴（時計は作らない）
- オッズ、人気、レース名、血統からの脚質・調教・バイアス
