# horse_racing_prediction
競馬予測

## 概要

JRA（日本中央競馬会）の競馬データをスクレイピングして、レース情報、競走馬情報、コース情報、馬場情報を提供するバックエンドシステムです。パッケージは `backend` ディレクトリです。

EV（競馬予想）が CLI と HTTP JSON を読む契約は [docs/ev-contract.md](docs/ev-contract.md)。取得と欠測の意味、環境変数。組み合わせの確率と買うかどうかは EV 側。

## 機能

### スクレイピング対象

- **JRA公式サイト** (https://jra.jp/keiba/)
  - 今週のレーススケジュール
  - 競馬場スケジュール
  - コース情報
- **JRA出馬表** (https://www.jra.go.jp/JRADB/accessD.html)
  - レース詳細（レース条件、発走時刻、本賞金）
  - 出走馬（枠番、馬番、騎手、調教師、馬体重、単勝オッズ、取消・除外）
- **JRA馬場情報** (https://www.jra.go.jp/keiba/baba/index.html)
  - 馬場状態（芝/ダート）
  - クッション値
  - 含水率（ゴール前、4コーナー）
  - 週間天候・芝丈・使用コース
- **netkeiba** (https://race.netkeiba.com/top)
  - 詳細なレース情報
  - 出馬表（出走馬リスト）
  - レース結果
  - 競走馬の詳細情報（血統、過去成績、調教など）
- **netkeiba馬場情報** (https://race.netkeiba.com/race/track.html)
  - 馬場状態、クッション値、含水率

### 取得できる情報

1. **レース情報** (Race)
   - レース名、レース番号
   - 開催日、発走時刻
   - 競馬場、距離、コース種別（芝/ダート/障害）
   - 馬場状態、天候
   - クラス、グレード

2. **競走馬情報** (Horse)
   - 馬名、性別、年齢
   - 血統（父、母、母父）
   - 調教師、馬主
   - 過去の成績
   - 調教（競馬ブックの調教ページ、または speedkeiba の重賞記事）

3. **コース情報** (Course)
   - 競馬場データ（周回距離、直線距離）
   - 現在の馬場状態

4. **馬場情報** (TrackInfo)
   - 芝/ダートの馬場状態（良/稍重/重/不良）
   - クッション値と硬さレベル
   - 含水率（ゴール前、4コーナー）
   - 天候、週間天候、芝丈、使用コース

## インストール

```bash
# 依存パッケージのインストール
pip install -r backend/requirements.txt
pip install -e backend
```

## 使用方法

コマンド、JSON のフィールド、`null` と `warning` の一覧は [docs/ev-contract.md](docs/ev-contract.md)。

### REST API サーバー

```bash
python -m backend.main

# または uvicorn で起動
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

API ドキュメント: http://localhost:8000/docs

### API エンドポイント

| エンドポイント | 説明 |
|---------------|------|
| `GET /api/races` | 今週のレース一覧を取得 |
| `GET /api/races/{race_id}` | 特定のレース情報を取得 |
| `GET /api/races/{race_id}/card` | 出馬表を取得 |
| `GET /api/races/{race_id}/result` | 確定した1レースの着順と払戻（近走ではない） |
| `GET /api/races/{race_id}/horses` | 出走馬の詳細情報を取得（血統・脚質・調教を含む） |
| `GET /api/races/{race_id}/odds` | 単勝・ワイド・馬連のオッズを取得 |
| `GET /api/horses/{horse_id}` | 競走馬の近走・血統・脚質・調教を取得（今走の結果ではない） |
| `GET /api/courses` | 全競馬場のコース情報を取得 |
| `GET /api/courses/{venue}` | 特定競馬場のコース情報を取得 |
| `GET /api/courses/{venue}/conditions` | 馬場状態を取得 |
| `GET /api/venues` | 今週の開催競馬場を取得 |
| `GET /api/tracks` | 馬場情報を取得（`venue`, `date`, `merge`） |

### CLI ツール

```bash
# 今週のレース一覧
python -m backend.cli races

# 出馬表を取得（取消・除外は status と印字どおりの status_label。JRA を優先）
python -m backend.cli card <race_id>

# 競走馬の近走・血統・脚質・調教（確定したレース結果ではない。当日バイアスは null）
python -m backend.cli horse <horse_id>

# 確定した1レースの着順と払戻（馬の近走成績ではない。予測入力に混ぜない）
python -m backend.cli result <race_id>
python -m backend.cli result <race_id> --json

# コース情報を取得
python -m backend.cli courses
python -m backend.cli courses --venue 東京

# 開催競馬場を取得
python -m backend.cli venues

# 馬場情報を取得（--json は TrackInfo だけ。JRA が 403 のとき jra の cushion / moisture は null。同じ場・日は netkeiba の公開ページへ戻る）
python -m backend.cli track
python -m backend.cli track --venue 中山 --merge
python -m backend.cli track --venue 東京 --json

# オッズを取得（単勝が既定。ワイド・馬連・三連複は --bet-types）
python -m backend.cli odds <race_id> --bet-types wide --prefer live
python -m backend.cli odds <race_id> --bet-types umaren --prefer live
python -m backend.cli odds <race_id> --bet-types sanrenpuku --prefer live
python -m backend.cli odds <race_id> --bet-types win,wide,umaren,sanrenpuku --json

# JSON形式で出力
python -m backend.cli races --json

# 詳細ログ付きで実行
python -m backend.cli races -v
```

### Python から直接使用

```python
from backend.services.race_service import RaceService
from backend.services.track_info_service import TrackInfoService

with RaceService() as service:
    schedule = service.get_weekly_races()
    for race in schedule.races:
        print(f"{race.race_date} {race.venue} {race.race_name}")

with TrackInfoService() as service:
    results = service.get_track_info(racecourse="中山", merge=True)
    merged = results.get("merged") or results.get("jra")
    if merged and merged.turf:
        print(f"芝: {merged.turf.condition.value}")
```

## 競走馬の近走

`GET /api/horses/{horse_id}` と `python -m backend.cli horse <horse_id>` はその馬の過去走を返す。今走の確定結果ではない。着順の精算や `cli result` には使わず、近走の能力評価にだけ使う。血統・脚質・トラックバイアス・調教の契約は後述。

- 成績の行は `https://db.netkeiba.com/horse/result/{horse_id}/` の表。列名で読む。水分量や馬場指数の列をタイムや上がりには使わない。
- 馬名と通算成績は馬ページ `https://db.netkeiba.com/horse/{horse_id}/`。結果ページの見出しに名前があるときは、馬ページが取れなくても名前を埋める。
- 通算成績はページの「通算成績」（`16戦8勝`）。走った行数から勝ち数は作らない。獲得賞金は中央と地方を万円で足す。
- セルが空、`**`、`取`、`計不` のときは数値を作らず null。距離が読めない行も `distance: null`（0 にはしない）。
- 取れないときは `performances: []` と `warning`。値は `horse result fetch failed`、`horse result table not found`、`horse result table malformed`、`no performances on horse result page`。
- `fetched_at` は取得時刻。フィールド名は従来どおり。追加は `fetched_at` と `warning`。`prize_money` は万円の小数を残すため数値（例: `50298.2`）。
- `GET /api/races/{race_id}/horses` の `recent_performances` も同じ成績ページの過去走であり、そのレースの結果ではない。
- 着順の精算は `cli result` / `GET /api/races/{race_id}/result`。近走の行を今走の結果として使わない。

## 確定結果

`GET /api/races/{race_id}/result` と `python -m backend.cli result <race_id>` は、確定した1レースの着順と払戻を返す。マーク×着順の事後ログ用。レース前の期待値の入力に混ぜない。

- ページは `https://race.netkeiba.com/race/result.html?race_id={race_id}`。出馬表や `cli horse` の `/horse/result/{horse_id}/` ではない。
- このページは UTF-8。馬の成績ページ（EUC-JP）とは別。UA はオッズ・馬ページと同じ Chrome。
- `results` はこのレースの出走馬（結果表の並び）。取消・除外・中止・競走中止・失格も残す。`rank` は入着の公式順位だけ。未入着は null のまま。着順は作らない。
- 各馬の `status` は `finished` / `scratched` / `excluded` / `did_not_finish` / `disqualified` / `unknown`。`status_label` はページの文字のまま（`取消`、`除外`、`中止`、`競走中止`、`失格`）。行に `降着` があるときだけ `demoted: true`。着順はその公式順位。
- 同着は同じ着順の行をページの順で残す。`1同着` も着順は数字。並べ替えない。
- `refunds` は払戻の隣。返還の印字（馬番、印字されていれば馬券種、JRA の返還同枠）があるときだけ。無いときは null。取消・除外と一致する返還に警告は足さない。金額は作らない。
- `complete` は返した頭数がページの `N頭` と一致し、各馬に `status` があるときだけ true。足りないときは false と、何が足りないかの `warning`。
- `result_phase` はページが `確定` か `確定前`（暫定・未確定）と印字しているときだけ `confirmed` / `provisional`。タイトルや「速報」からは決めない。印字が無ければ null と `result phase was not on the page`。
- 着順表が `N頭` より短い、空、壊れているときは、ページ内の JSON、ページ自身が呼ぶ同一サイトの結果 JSON、入っていればヘッドレス描画の順で足す。各値の `source` は `netkeiba_html` / `netkeiba_js` / `headless`。`fetched_at` はその値を得た時刻。ヘッドレスは任意（`pip install -e ".[headless]"`）。未インストールなら警告だけ。403 とログイン画面は止め、回避しない。
- 開催日はページの `kaisai_date`（無ければタイトルの年月日）。レースIDの桁からは日付を作らない。
- 競馬場、距離、芝/ダート、馬場、天候は結果ページに書いてあるときだけ入る。馬場の `稍` は馬成績と同じく `稍重`。
- 払戻はページの単勝・複勝・枠連・馬連・ワイド・馬単・3連複・3連単。同じ馬券種の行も全部残す。金額が無い馬券種は入れず、`payout_note` に残す。ページに無い価格は作らない。JRA の公開払戻に届くときは突き合わせ、不一致は `warning`。行の順番と、片方だけ空の人気は不一致にしない。届かないときも金額は作らず `JRA official payout was not reachable`。
- 取れないときは `results: []`、`payouts: []` と `warning`。値は `race result fetch failed`、`race result not found`、`race not yet settled`、`race result table malformed`、`no finishers on race result page`、`race result returned 403`、`race result requires login`。
- `fetched_at` は取得時刻。フィールドの対応は `docs/ev-contract.md`。

## オッズ

`GET /api/races/{race_id}/odds?bet_types=sanrenpuku&prefer=live`

期待値は `RaceEntry.odds` ではなく、ここで返す `OddsQuote` を使う。

- `bet_types` は `win`（単勝）、`wide`（ワイド）、`umaren`（馬連）、`sanrenpuku`（三連複）。カンマ区切りで同時に取れる。既定は `win`。
- 単勝の価格は `odds`（馬番）。ワイドの組み合わせは `3-7`（馬番の小さい順）で、価格は `odds_min` と `odds_max`。範囲の片側しか無い行は捨て、価格は作らない。ページが `----` または `取消` の行は残し、価格は null、`withheld` を true。
- 馬連の組み合わせも `3-7`（馬番の小さい順）。価格は単勝と同じ単一の `odds`。netkeiba の変動値は範囲ではないので `odds_min` / `odds_max` は空。価格が無い行は捨て、数値は作らない。`----` と `取消` は残して価格は null。
- 三連複の組み合わせは `1-2-5`（馬番3つの小さい順）。価格は馬連と同じ単一の `odds`。netkeiba の変動値は範囲ではないので `odds_min` / `odds_max` は空。価格が無い行は捨て、数値は作らない。`----` と `取消` は残して価格は null。
- `non_runners` は取消・除外の馬番。`status` は `scratched` / `excluded` / `unknown`。`status_label` はページの文字。JRA を優先し、403 のときは netkeiba。馬番は作らない。組がページに無いときはその組を作らない。
- `source` は `netkeiba_live` / `netkeiba_yoso` / `jradb`。`kind` は `live` または `yoso`。CLI を `source` には入れない。
- `fetched_at` は必須。市場の更新時刻があるときだけ `market_updated_at` を入れる。
- `encoding` は常に `utf-8`。オッズ API（`api_get_jra_odds.html`、`compress=0`）の JSON を UTF-8 で読む。`combination` は ASCII の馬番（単勝 `1`、ワイド・馬連 `3-7`、三連複 `1-2-5`）。馬成績ページの EUC-JP では読まない。呼び出し側で文字コードを推測しない。
- `race_phase` はレース前かレース後。API の `status` からの対応は次のとおり。`yoso` と `middle` は `pre`。`result` は `post`（レース後の確定オッズ）。馬券種で局面が食い違うときは `null`。判別できない失敗も `null`。
- `prefer` は呼び出し側の希望であり、`kind` を書き換えない。`status=yoso` のペイロードは `prefer=live` でも `kind=yoso` のまま。
- `prefer=live` で市場オッズが取れないとき（`quotes=[]` / `status=unavailable`）、予想オッズへは自動で切り替えない。予想が必要ならクライアントが `prefer=yoso` を明示して取りにいく。単勝・ワイド・馬連・三連複とも同じ。
- 失敗は `quotes=[]` と `reason`。着順の `warning` に相当し、オッズ側では別名を置かない。API が理由を返すときはそれを使う（例: `result odds empty`）。HTTP 400 は `http 400`、本文に API の理由があれば `http 400: result odds empty`。400 の本文に数値が載っていても価格にはしない。壊れた JSON は `malformed odds payload`。オブジェクトでない JSON は `invalid odds payload`。通信失敗は `fetch failed`。レース後（`post`）で空、または HTTP 400 のときも `quotes` は空のまま。価格は作らない。

## 血統・脚質・トラックバイアス

EV がコース適性、ペース、血統を見るための構造。無い値は null のまま。レース名、オッズ、人気からは作らない。

`cli horse` / `GET /api/horses/{horse_id}` と `GET /api/races/{race_id}/horses` に載せる。当日バイアスの枠は出馬表の `race.track_bias` にもある。週次一覧の `track_bias` は null（そこでは判定しない）。

### 血統 `pedigree`

- 父・母・母父。各々 `name` と `horse_id`。ページに無い側は null。
- 馬ページのプロフィール表（`父` / `母` / `母父`）と、表が `class="blood"` のときの先頭2リンク（父、母。3番目は母父にしない）。
- プロフィールに父・母・母父の名前と ID が揃わないときは `https://db.netkeiba.com/horse/ped/{horse_id}/` の `table.blood_table`。父は1行目の1列、母は表の半分の1列、母父はその右。行数が奇数、または母のセルが父と同じ rowspan のときは名前を使わず `pedigree table malformed`。
- 名前は血統表のリンク文字そのもの。外国馬の `(米)` のようにリンクに国が書いてあれば残す。削らない。
- すでに入っている名前は上書きしない。ID は同じ名前のときだけ足す。
- 出走馬一覧はプロフィールを開かず、血統表だけを見る。
- `source` は `profile` / `netkeiba_ped` / `profile+netkeiba_ped`。取れないときは null。
- 父と母が揃えば `warning` は null（母父が表に無ければ `damsire` だけ null）。揃わないときの `warning` は `pedigree fetch failed`、`pedigree table not found`、`pedigree table malformed`、`pedigree not on profile or ped page`、`pedigree incomplete`。
- 平坦な `sire_name` / `sire_id` / `dam_name` / `dam_id` / `damsire_name` / `damsire_id` は `pedigree` と同じ値。

### 脚質 `running_style`

netkeiba の馬ページに脚質の項目は無い。成績の「ペース」は `37.1-33.4` のようなラップで、逃げ・先行・差し・追込でもハイ/スローでもない。分類しない。

通過順の最初の数字だけを使う。`13-12-12-7` の初角は 13。数字とハイフン以外は捨てる。頭数が無い、または順位が頭数を超える走はラベルにしない。

- 初角が 1 なら逃げ。
- それ以外は頭数を三等分する。`front_end = ceil(頭数 / 3)`、`mid_end = ceil(頭数 * 2 / 3)`。先行は `front_end` まで、差しは `mid_end` まで、残りは追込。
- `label` は判定できた走が 3 走以上で、最多が一つだけのとき。未満や同数は null。内訳は `counts`（逃げ・先行・差し・追込）。
- `confidence` は `label` の占有率。`label` が null なら null。
- `data_sufficiency` は `insufficient`（3走未満、または1走も判定できない）、`thin`（3走以上だが5走未満、信頼度 0.6 未満、または同数）、`sufficient`（5走以上かつ信頼度 0.6 以上）。
- `basis` は `passing_order_first_call`。1走も判定できないときは null。
- `warning` は `passing order unavailable`、`running style field size unavailable`、`running style sample too small`、`running style tie`。
- 1400m 以下を短距離とする。障害は短距離に数えない。短距離の逃げ・先行が 1〜2 走なら `short_distance_front_under_specified` が true。全体の `label` は消さない。EV はここで感度を上げる。
- `cli horse` は成績ページの全走。出走馬一覧の脚質は近走最大5走だけ。

### トラックバイアス `track_bias`

馬場状態（良/稍重/重/不良）、芝/ダート/障害、競馬場、距離は、そのページに書いてあるときだけ `going` / `surface` / `venue` / `distance` に写す。`不明` や距離 0 は null。

`venue` は出馬表の開催ヘッダが `race_id` の場と一致するときだけ。netkeiba ID の日次（東京2日・京都2日の `02`）は函館の場コードではない。ナビや 403 本文の場名は写さない。

`inside_outside` と `pace_bias` は、公式の事前ページが**バイアスの見出し**として印刷しているセルだけを写す。見出しは `内外` / `内外バイアス` / `枠バイアス`、または `ペース` / `ペースバイアス` / `展開バイアス`。セルは空白を畳んだ短いラベル（16字まで）。`傷み`、`内柵`、`コース`、`回り`、`メートル` を含む文は捨てる。

- accessD / JRADB の出馬表（`POST https://www.jra.go.jp/JRADB/accessD.html`、`cname`）。ラベルがあれば `source` は `accessd`。
- JRA 馬場情報の当週タブ（`https://www.jra.go.jp/keiba/baba/index.html` と `index2.html`）。タイトルの競馬場がこのレースと一致したページだけ。ラベルがあれば `source` は `jra_baba`。両方に同じ文字があるときは `accessd+jra_baba`。
- 二つのページの同じ項目が違う文字なら、両方 null。`warning` は `day-of track bias sources disagree`。

写さないもの:

- 出馬表の `（芝・右 外）`。外回りであり、内外バイアスではない。
- 使用コース（A/B/C）、内柵の位置、芝の傷み、含水率、クッション値、馬場状態。`cli track` のまま。バイアスにはしない。
- オッズ、人気、レース名、結果の通過順、予想ブログ。
- netkeiba のトラックバイアス。マスターコースのログインの向こうにあり、匿名 HTML には値が無い。公式の事前ラベルではない。

`data_sufficiency` は、内外とペースが両方あるとき `sufficient`（`warning` は null）、片方だけ `thin`、無いとき `unavailable`。

`warning`:

- 馬単体（開催日が無い）: `day-of track bias unavailable without a race card`
- まだ取りにいっていない出馬表オブジェクト: `day-of track bias not loaded from accessD/JRADB`
- accessD と馬場情報を見てラベルが無い: `day-of inside/outside and pace bias not published on accessD or JRA baba`
- accessD には無く、馬場情報にその競馬場が無い: `day-of inside/outside and pace bias not published on accessD; JRA baba has no page for this venue`
- accessD が HTTP 403: `day-of track bias unavailable: JRA accessD returned 403`（馬場情報を見たときはその結果を続ける）
- 通信失敗: `day-of track bias fetch failed`
- 片方だけ: `day-of pace bias not published` または `day-of inside/outside bias not published`

認証用の環境変数は無い。accessD も馬場情報も公開ページ。403 は資格情報不足ではなく、Akamai がデータセンターの IP を止めたとき。個別の `/keiba/baba/{venue}.html` は 403 になることがあるので、当週の `index.html` / `index2.html` だけを見る。馬場ページが 403 のときは、その JRA の値は null のまま、同じ場・日の netkeiba 公開ページ（クッション値、含水率、馬場、天候、発走時刻）へ戻る。403 の回避、captcha、ログインはしない。

- 出馬表と `GET /api/races/{race_id}` の `race.track_bias` は、条件を写したうえで上記を取りにいく。
- 出走馬一覧の各馬にも同じオブジェクトを載せる。
- `cli horse` の `track_bias` は開催日が無いので条件もバイアスも null。accessD は開かない。近走の馬場・距離・芝ダートは `performances` の各行。
- 週次一覧の `track_bias` は null（そこでは判定しない）。

見出しがあるページ（パーサ契約。2026-09-28 に取得した東京の馬場情報と、9月26日の秋風ステークス出馬表にはこの見出しは無い）:

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

```json
{
  "pedigree": {
    "sire": {"name": "ハーツクライ", "horse_id": "2001103038"},
    "dam": {"name": "ダストアンドダイヤモンズ", "horse_id": "000a0137a4"},
    "damsire": {"name": "Vindication", "horse_id": "000a010544"},
    "source": "netkeiba_ped",
    "warning": null
  },
  "running_style": {
    "label": null,
    "counts": {"逃げ": 0, "先行": 0, "差し": 0, "追込": 2},
    "sample_size": 2,
    "unusable_runs": 1,
    "confidence": null,
    "data_sufficiency": "insufficient",
    "basis": "passing_order_first_call",
    "short_distance_m": 1400,
    "short_distance_runs": 0,
    "short_distance_front_runs": 0,
    "short_distance_front_under_specified": false,
    "warning": "running style sample too small"
  },
  "track_bias": {
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
}
```

上の脚質は、通過が空の1走と追込2走しか無い例。ラベルは出さない。

## 調教 `training`

`cli horse` / `GET /api/horses/{horse_id}` と `GET /api/races/{race_id}/horses` の各馬に載せる。出馬表（`cli card` / `RaceEntry`）には載せない。ページに書いてある時計・短評・矢印・平均だけを写す。レース名、オッズ、人気、血統からは時計も評価も作らない。レース名は speedkeiba の記事を選ぶ鍵にだけ使い、値には入れない。

### ソース

- レース単位は `https://p.keibabook.co.jp/cyuou/cyokyo/0/0/{race_id}`。`race_id` が12桁のときだけ取りにいく。列名（`5F`、`半哩`、坂路の `4F`）が HTML にある。`source` は `keibabook_smart`。
- Smart の `https://s.keibabook.co.jp/...` は同じ表でも列名が無い。位置からハロンは作らないので、その URL は読まない。
- 匿名の競馬ブック応答に載っている馬だけを埋める。観察したページは出走馬のうち1頭だけが静的 HTML に入っていた。載っていない馬は `workouts: []` と warning。環境変数が無いときはこのまま。ログインしたことにはしない。
- ログインできるときだけ、同じ調教 URL をセッション付きで読み、まだ時計が無い馬は出馬表 `https://p.keibabook.co.jp/cyuou/syutuba/{race_id}` の馬名リンク `/db/uma/{umacd}` から `https://p.keibabook.co.jp/db/uma/{umacd}/crireki` を読む。`source` は `keibabook_crireki`。中間時計ページ（`/cyukan`）は時計が匿名 HTML に無いので読まない。
- `umacd` は netkeiba の馬IDから作らない。レースでは出馬表のリンクだけ。同じ馬名が別の umacd を指すときは捨てる。血統の `/sanku` リンクは使わない。
- `cli horse` はレースIDが無い。ログインできたとき、`https://p.keibabook.co.jp/db/search/horse` に馬名を送り、結果行か馬ページのリンクを使う。netkeiba プロフィールの生年月日（馬ページに日付があるときはその日付、検索行は生年）と性別がページの印字と一致し、候補が1頭のときだけ。生年月日が無い、複数一致、不一致は `training unavailable: keibabook umacd was not on a source page`。
- 環境変数は次のとおり。両方のログイン項目が空、かつ Cookie も空なら匿名。`KEIBABOOK_LOGIN_ID` と `KEIBABOOK_PASSWORD` が両方あるときは `POST /login/login`（フォームの `login_id` / `pswd` / `_token`）。パスワードが無いときだけ `KEIBABOOK_COOKIE` を送り、`POST /logincheck/logincheck` が「ログアウト」または `/mypage/` を含み「ログインしていない」を含まないときだけ成功。それ以外は `training keibabook login failed`。匿名の調教ページに既にある時計は残す。
- 競馬ブックにその馬の時計が無いときは speedkeiba の今週の重賞記事（`https://speedkeiba.com/今週のレース/` の見出しに「調教」があるリンク）。馬名は記事の `h4` と、空白を除いて一致したときだけ。`source` は `speedkeiba`。
- 時計の優先は、調教ページ、調教履歴、speedkeiba。壊れた競馬ブックの時計は捨て、speedkeiba に時計があればそちらを使う。
- `cli horse` は認証が無いとき speedkeiba の今週の記事を最大12本見て、馬名が一つの記事だけにあるときだけ埋める。二つ以上の記事にあるときは混ぜず `training ambiguous on speedkeiba`。

### 行 `workouts`

- `date` は年月日がページから決まるときだけ。セルが `2026/5/29` や `2026年5月29日` のときはその年月日。月日だけのときは見出しの `YYYY年M月D日` を基準に、それより後の月日は前年。年がページに無い月日は `date` も `long_gap` も null。`■` と `◇` は日付ではない。`date_label` はセルの文字のまま。
- 日付の先頭が `*` または `＊` のときだけ `long_gap: true`。
- speedkeiba の「今回」は記事見出しの年を使う。「前回連対時」は年が書いていないので `date` は null。
- `course` はページのコース（`美Ｗ`、`美坂`、`美浦 W`、`栗東 坂`）。`condition` は略記のまま（`不` を不良にはしない）。
- `distance` と `time` は、列名があるときは一番長い時計の列（左端の数値）。speedkeiba の `51.9 – 37.1 – 11.5` は区切りが無く、`distance` は null、`time` は文字列のまま。
- `splits` は列名つきの時計。空セルは入れない。坂路、または `6F(坂路)` のセルが `N回` のときは、ページの注記「坂路は4F・3F・2F・1F」に合わせ、`5F(4F)` を `4F`、`半哩(3F)` を `3F`、`3F(2F)` を `2F`、`1F(1F)` を `1F` とする。回数は `slope_reps`。平地の `半哩` はメートルにしない。
- 時計セルに数値以外が入っている馬は、その馬の行を全部捨て `training table malformed`。一部だけ残さない。
- `rank_in_group` は回り位置 `［８］` / `[8]`。全角数字も読む。空なら null。
- 併せの行は `partner_note` に全文。`0.9秒` のような差は時計に分解しない。
- `final_workout` は `tr.oikiri` または印 `☆` のときだけ true。それ以外は null。
- speedkeiba の `section` は `今回` / `前回連対時`。

### 要約 `summary`

ページが印刷しているものだけ。こちらでは平均も矢印も計算しない。

- 競馬ブックの追い切り短評と矢印（`comment` / `arrow`）。
- speedkeiba の評価文（`evaluation`）と `(平均)` 行（`average`）。
- ラップ表は `今走` の行だけ。`好調` と `凡走` はページ自身が人気・着順・着差から作った行なのでコピーしない。キャプションのコメントにある `坂路 / 本追切` を `course` / `phase` にする。

### 十分さ

- `sufficient`: 日付・時計・コースが揃った行が2本以上。`warning` は null。
- `thin`: 時計か、ページの要約はあるが、日付つきの時計が2本未満。`warning` は `training sample thin`。
- `unavailable`: 取れない、壊れている、ログインが要る、記事が曖昧。`source` は、要約だけ残るとき以外 null。

`warning` は次のいずれか。

- `training fetch failed`
- `training table not found`
- `training table malformed`
- `training not on keibabook page without login`
- `training not on keibabook page without login, and not on the speedkeiba article`
- `training unavailable: not on speedkeiba weekly index; keibabook training history requires login`
- `training ambiguous on speedkeiba`
- `training sample thin`
- `training not loaded`（まだ取得していない初期値）
- `training keibabook login failed`（環境変数はあるがログインチェックがログイン中ではない）
- `training history empty on keibabook`（履歴ページが「調教履歴データがありません」）
- `training history requires keibabook login`（履歴の本文がまだ会員案内のまま）
- `training unavailable: keibabook umacd was not on a source page`（リンクと生年月日で1頭に決まらない）

```json
{
  "workouts": [
    {
      "date": "2026-05-31",
      "date_label": "5/31(日)",
      "course": "美坂",
      "distance": "4F",
      "time": "59.3",
      "condition": "良",
      "rank_in_group": null,
      "rider": "助手",
      "gait": "馬なり余力",
      "comment": null,
      "partner_note": null,
      "mark": null,
      "final_workout": null,
      "long_gap": false,
      "slope_reps": 1,
      "section": null,
      "splits": [
        {"label": "4F", "time": "59.3"},
        {"label": "3F", "time": "43.9"},
        {"label": "2F", "time": "28.8"},
        {"label": "1F", "time": "14.3"}
      ]
    },
    {
      "date": "2026-06-03",
      "date_label": "6/3(水)",
      "course": "美Ｗ",
      "distance": "5F",
      "time": "68.2",
      "condition": "不",
      "rank_in_group": 8,
      "rider": "助手",
      "gait": "馬なり余力",
      "comment": "乗り込み入念",
      "partner_note": null,
      "mark": "☆",
      "final_workout": true,
      "long_gap": false,
      "slope_reps": null,
      "section": null,
      "splits": [
        {"label": "5F", "time": "68.2"},
        {"label": "半哩", "time": "52.7"},
        {"label": "3F", "time": "38.0"},
        {"label": "1F", "time": "12.1"}
      ]
    }
  ],
  "summary": {
    "comment": "乗り込み入念",
    "arrow": "→",
    "evaluation": null,
    "average": null,
    "laps": []
  },
  "source": "keibabook_smart",
  "data_sufficiency": "sufficient",
  "warning": null
}
```

上はフィクスチャ `keibabook_cyokyo.html` の坂路1本と本追切。実レスポンスはページの行を全部持つ（`■` は `date: null`、併せ馬は `partner_note`）。speedkeiba のフィクスチャでは `time` が `"51.9 – 37.1 – 11.5"`、`distance` は null、`summary.average` はページの `(平均)`、`summary.laps` は `今走` だけ。

ログイン後の調教履歴（フィクスチャ `keibabook_crireki.html`。匿名の調教ページにいなかった馬）:

```json
{
  "workouts": [
    {
      "date": "2026-05-29",
      "date_label": "2026/5/29(金)",
      "course": "美Ｗ",
      "distance": "5F",
      "time": "67.4",
      "condition": "良",
      "rank_in_group": 3,
      "final_workout": null,
      "long_gap": false,
      "slope_reps": null,
      "splits": [
        {"label": "5F", "time": "67.4"},
        {"label": "半哩", "time": "52.4"},
        {"label": "3F", "time": "37.6"},
        {"label": "1F", "time": "12.3"}
      ]
    },
    {
      "date": "2026-06-01",
      "date_label": "2026/6/1(月)",
      "course": "美坂",
      "distance": "4F",
      "time": "58.1",
      "condition": "稍",
      "rank_in_group": null,
      "slope_reps": 1,
      "long_gap": false,
      "splits": [
        {"label": "4F", "time": "58.1"},
        {"label": "3F", "time": "42.6"},
        {"label": "2F", "time": "27.9"},
        {"label": "1F", "time": "13.8"}
      ]
    }
  ],
  "summary": {"comment": "動き良い", "arrow": "↗", "evaluation": null, "average": null, "laps": []},
  "source": "keibabook_crireki",
  "data_sufficiency": "sufficient",
  "warning": null
}
```

`■` の行は上の2本の前にあり、`date` は null。ページにその馬が無いとき:

```json
{
  "workouts": [],
  "summary": null,
  "source": null,
  "data_sufficiency": "unavailable",
  "warning": "training not on keibabook page without login, and not on the speedkeiba article"
}
```

### 対象外

- 予想ブログ、netkeiba マスターコースのトラックバイアス（匿名ページに値は無い）
- 新しい馬券種
- オッズ・確定結果パーサの書き換え
- 成績表の列追加（通過順は既存の `passing_order` を読むだけ。ペース列は脚質にしない）
- オッズ、人気、レース名からの脚質・血統・調教
- 競馬ブック Smart 画面の列位置からのハロン推定
- 認証が無いときの馬個別の調教履歴（本文が空のまま。時計は作らない）

## プロジェクト構成

```
backend/
├── pyproject.toml       # パッケージ定義
├── requirements.txt     # 依存パッケージ
├── pytest.ini           # pytest 設定
├── examples/            # 使用例
│   └── demo_scraper.py
├── tests/               # テスト
│   ├── __init__.py
│   └── test_scrapers.py
└── src/                 # backend パッケージのソース
    ├── __init__.py
    ├── main.py          # FastAPI アプリケーション
    ├── cli.py           # コマンドラインツール
    ├── models/          # データモデル (Pydantic)
    │   ├── __init__.py
    │   ├── race.py      # レースモデル
    │   ├── horse.py     # 競走馬モデル
    │   ├── training.py  # 調教
    │   ├── course.py    # コースモデル
    │   └── track.py     # 馬場情報モデル
    ├── scrapers/        # スクレイパー
    │   ├── __init__.py
    │   ├── base_scraper.py # 基底クラス
    │   ├── jra_scraper.py  # JRA レース・コース・出馬表
    │   ├── netkeiba_scraper.py
    │   ├── training_scraper.py       # 競馬ブック・speedkeiba の調教
    │   ├── jra_track_scraper.py      # JRA 馬場情報
    │   └── netkeiba_track_scraper.py
    ├── services/        # ビジネスロジック
    │   ├── __init__.py
    │   ├── race_service.py
    │   └── track_info_service.py
    └── utils/           # ユーティリティ
        ├── __init__.py
        ├── http_client.py  # HTTPクライアント
        └── date_utils.py   # 日付ユーティリティ
```

## テスト

```bash
cd backend
pytest
pytest -m "not integration"
pytest -m "integration"
```

## 技術スタック

- **Python 3.11+**
- **FastAPI** - REST API フレームワーク
- **Pydantic** - データバリデーション
- **BeautifulSoup4** - HTML パーシング
- **requests / httpx** - HTTP クライアント
- **lxml** - 高速 HTML パーサー

## 注意事項

- スクレイピングはサイトの利用規約に従って行ってください
- リクエスト間隔を適切に設定し、サーバーに負荷をかけないようにしてください
- 取得したデータの商用利用には注意が必要です

## ライセンス

MIT License
