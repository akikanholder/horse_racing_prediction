# horse_racing_prediction
競馬予測

## 概要

JRA（日本中央競馬会）の競馬データをスクレイピングして、レース情報、競走馬情報、コース情報を提供するバックエンドシステムです。

## 機能

### スクレイピング対象

- **JRA公式サイト** (https://jra.jp/keiba/)
  - 今週のレーススケジュール
  - 競馬場スケジュール
  - コース情報
  - 馬場状態

- **netkeiba** (https://race.netkeiba.com/top)
  - 詳細なレース情報
  - 出馬表（出走馬リスト）
  - レース結果
  - 競走馬の詳細情報（血統、過去成績など）

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

## インストール

```bash
# 依存パッケージのインストール
pip install -r requirements.txt
```

## 使用方法

### REST API サーバー

```bash
# サーバー起動
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
| `GET /api/horses/{horse_id}` | 競走馬の詳細情報を取得 |
| `GET /api/courses` | 全競馬場のコース情報を取得 |
| `GET /api/courses/{venue}` | 特定競馬場のコース情報を取得 |
| `GET /api/courses/{venue}/conditions` | 馬場状態を取得 |
| `GET /api/venues` | 今週の開催競馬場を取得 |

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

# JSON形式で出力
python -m backend.cli races --json

# 詳細ログ付きで実行
python -m backend.cli races -v
```

### Python から直接使用

```python
from backend.services.race_service import RaceService

# サービスの初期化
with RaceService() as service:
    # 今週のレース情報を取得
    schedule = service.get_weekly_races()
    for race in schedule.races:
        print(f"{race.race_date} {race.venue} {race.race_name}")

    # 競走馬情報を取得
    horse = service.get_horse_info("horse_id")
    if horse:
        print(f"{horse.horse_name} ({horse.sire_name} x {horse.dam_name})")

    # コース情報を取得
    course = service.get_course_info("東京")
    if course:
        print(f"芝直線: {course.turf_straight}m")
```

## プロジェクト構成

```
backend/
├── __init__.py
├── main.py              # FastAPI アプリケーション
├── cli.py               # コマンドラインツール
├── models/              # データモデル (Pydantic)
│   ├── __init__.py
│   ├── race.py         # レースモデル
│   ├── horse.py        # 競走馬モデル
│   └── course.py       # コースモデル
├── scrapers/            # スクレイパー
│   ├── __init__.py
│   ├── base_scraper.py # 基底クラス
│   ├── jra_scraper.py  # JRA公式サイト
│   └── netkeiba_scraper.py  # netkeiba
├── services/            # ビジネスロジック
│   ├── __init__.py
│   └── race_service.py # レースサービス
└── utils/               # ユーティリティ
    ├── __init__.py
    ├── http_client.py  # HTTPクライアント
    └── date_utils.py   # 日付ユーティリティ
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
