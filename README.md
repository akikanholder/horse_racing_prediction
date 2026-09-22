# horse_racing_prediction
競馬予測

## 機能

### 馬場情報スクレイピング

競馬場の馬場情報（馬場状態、含水率、クッション値など）を取得するスクレイパーを提供します。

#### 対応データソース

- **JRA（日本中央競馬会）**: https://www.jra.go.jp/keiba/baba/index.html
- **Netkeiba**: https://race.netkeiba.com/race/track.html

#### 取得可能な情報

- 馬場状態（良/稍重/重/不良）
- クッション値（芝コース）
- 含水率（ゴール前、4コーナー）
- 天候
- 週間天候情報（JRA）
- 芝丈・使用コース情報（JRA）

## インストール

```bash
pip install -r requirements.txt
```

または

```bash
pip install -e .
```

## 使用方法

### JRAスクレイパー

```python
from src.scrapers import JRAScraper

with JRAScraper() as scraper:
    # デフォルトページ（現在開催中の競馬場）をスクレイピング
    track_info = scraper.scrape()
    
    # 特定の競馬場を指定
    track_info = scraper.scrape("nakayama")  # 中山
    
    print(f"競馬場: {track_info.racecourse}")
    print(f"芝馬場状態: {track_info.turf.condition.value}")
    print(f"ダート馬場状態: {track_info.dirt.condition.value}")
```

### Netkeibaスクレイパー

```python
from src.scrapers import NetkeibaScraper

with NetkeibaScraper() as scraper:
    # デフォルトページをスクレイピング
    track_info = scraper.scrape()
    
    # 競馬場IDを指定（中山=06）
    track_info = scraper.scrape("06")
    
    # 日付も指定
    track_info = scraper.scrape("06", "20260922")
    
    print(f"競馬場: {track_info.racecourse}")
    print(f"クッション値: {track_info.turf.cushion.value}")
```

### TrackInfoService（統合サービス）

複数のデータソースからデータを取得し、統合することができます。

```python
from src.scrapers import TrackInfoService

with TrackInfoService() as service:
    # 両方のソースからデータを取得
    results = service.get_track_info()
    
    # 特定の競馬場を指定
    results = service.get_track_info(racecourse="中山")
    
    # データをマージして取得
    results = service.get_track_info(merge=True)
    
    if "merged" in results:
        merged_info = results["merged"]
        print(f"統合データ - 芝: {merged_info.turf.condition.value}")
```

### JSON出力

```python
track_info = scraper.scrape()
json_output = track_info.model_dump_json(indent=2, exclude_none=True)
print(json_output)
```

## 競馬場コード

| 競馬場 | JRAコード | NetkeibaID |
|--------|-----------|------------|
| 札幌   | sapporo   | 01         |
| 函館   | hakodate  | 02         |
| 福島   | fukushima | 03         |
| 新潟   | niigata   | 04         |
| 東京   | tokyo     | 05         |
| 中山   | nakayama  | 06         |
| 中京   | chukyo    | 07         |
| 京都   | kyoto     | 08         |
| 阪神   | hanshin   | 09         |
| 小倉   | kokura    | 10         |

## テスト

```bash
# 全テスト実行
pytest

# 単体テストのみ
pytest -m "not integration"

# 統合テストのみ（ネットワークアクセスが必要）
pytest -m "integration"
```

## プロジェクト構成

```
horse_racing_prediction/
├── src/
│   ├── __init__.py
│   └── scrapers/
│       ├── __init__.py
│       ├── models.py              # データモデル
│       ├── base.py                # 基底スクレイパークラス
│       ├── jra_scraper.py         # JRAスクレイパー
│       ├── netkeiba_scraper.py    # Netkeibaスクレイパー
│       └── track_info_service.py  # 統合サービス
├── tests/
│   └── test_scrapers.py
├── examples/
│   └── demo_scraper.py
├── requirements.txt
├── pyproject.toml
└── pytest.ini
```

## ライセンス

MIT License
