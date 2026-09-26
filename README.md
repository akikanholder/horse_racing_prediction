# horse_racing_prediction
競馬予測

## 概要

JRA（日本中央競馬会）の競馬データをスクレイピングして、レース情報、競走馬情報、コース情報、馬場情報を提供するバックエンドシステムです。パッケージは `backend` ディレクトリです。

## 機能

### スクレイピング対象

- **JRA公式サイト** (https://jra.jp/keiba/)
  - 今週のレーススケジュール
  - 競馬場スケジュール
  - コース情報
- **JRA出馬表** (https://www.jra.go.jp/JRADB/accessD.html)
  - レース詳細（レース条件、発走時刻、本賞金）
  - 出走馬（枠番、馬番、騎手、調教師、馬体重、単勝オッズ）
- **JRA馬場情報** (https://www.jra.go.jp/keiba/baba/index.html)
  - 馬場状態（芝/ダート）
  - クッション値
  - 含水率（ゴール前、4コーナー）
  - 週間天候・芝丈・使用コース
- **netkeiba** (https://race.netkeiba.com/top)
  - 詳細なレース情報
  - 出馬表（出走馬リスト）
  - レース結果
  - 競走馬の詳細情報（血統、過去成績など）
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
| `GET /api/races/{race_id}/result` | レース結果を取得 |
| `GET /api/races/{race_id}/horses` | 出走馬の詳細情報を取得 |
| `GET /api/races/{race_id}/odds` | 単勝・ワイドのオッズを取得 |
| `GET /api/horses/{horse_id}` | 競走馬の詳細情報を取得 |
| `GET /api/courses` | 全競馬場のコース情報を取得 |
| `GET /api/courses/{venue}` | 特定競馬場のコース情報を取得 |
| `GET /api/courses/{venue}/conditions` | 馬場状態を取得 |
| `GET /api/venues` | 今週の開催競馬場を取得 |
| `GET /api/tracks` | 馬場情報を取得（`venue`, `date`, `merge`） |

### CLI ツール

```bash
# 今週のレース一覧
python -m backend.cli races

# 出馬表を取得
python -m backend.cli card <race_id>

# 競走馬情報を取得
python -m backend.cli horse <horse_id>

# コース情報を取得
python -m backend.cli courses
python -m backend.cli courses --venue 東京

# 開催競馬場を取得
python -m backend.cli venues

# 馬場情報を取得
python -m backend.cli track
python -m backend.cli track --venue 中山 --merge

# オッズを取得（単勝が既定。ワイドは --bet-types wide）
python -m backend.cli odds <race_id> --bet-types wide --prefer live
python -m backend.cli odds <race_id> --bet-types win,wide --json

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

## オッズ

`GET /api/races/{race_id}/odds?bet_types=wide&prefer=live`

期待値は `RaceEntry.odds` ではなく、ここで返す `OddsQuote` を使う。

- `bet_types` は `win`（単勝）と `wide`（ワイド）。カンマ区切りで両方取れる。既定は `win`。
- 単勝の価格は `odds`（馬番）。ワイドの組み合わせは `3-7`（馬番の小さい順）で、価格は `odds_min` と `odds_max`。範囲の片側しか無い行は捨て、価格は作らない。
- `source` は `netkeiba_live` / `netkeiba_yoso` / `jradb`。`kind` は `live` または `yoso`。
- `fetched_at` は必須。市場の更新時刻があるときだけ `market_updated_at` を入れる。
- `prefer` は呼び出し側の希望であり、`kind` を書き換えない。`status=yoso` のペイロードは `prefer=live` でも `kind=yoso` のまま。
- `prefer=live` で市場オッズが取れないとき（`quotes=[]` / `status=unavailable`）、予想オッズへは自動で切り替えない。予想が必要ならクライアントが `prefer=yoso` を明示して取りにいく。
- 数値が無いレスポンスは `quotes=[]` と API の `reason`（例: `result odds empty`）。

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
    │   ├── course.py    # コースモデル
    │   └── track.py     # 馬場情報モデル
    ├── scrapers/        # スクレイパー
    │   ├── __init__.py
    │   ├── base_scraper.py # 基底クラス
    │   ├── jra_scraper.py  # JRA レース・コース・出馬表
    │   ├── netkeiba_scraper.py
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
