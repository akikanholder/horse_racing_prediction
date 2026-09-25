"""Models for daily race CSV export."""

from datetime import date

from pydantic import BaseModel, Field


class CsvExportFile(BaseModel):
    """A CSV file written for one information type."""

    name: str = Field(..., description="ファイル名")
    path: str = Field(..., description="出力パス")
    rows: int = Field(..., description="データ行数（ヘッダーを除く）")


class VenueCsvExport(BaseModel):
    """CSV files written for one racecourse on one date."""

    venue: str = Field(..., description="競馬場")
    directory: str = Field(..., description="競馬場ディレクトリ")
    race_count: int = Field(..., description="レース数")
    files: list[CsvExportFile] = Field(default_factory=list, description="出力ファイル")


class DailyRaceCsvExport(BaseModel):
    """Result of exporting one day's races to CSV."""

    race_date: date = Field(..., description="対象日")
    output_directory: str = Field(..., description="日付ディレクトリ")
    race_count: int = Field(..., description="レース数")
    venue_count: int = Field(..., description="競馬場数")
    venues: list[VenueCsvExport] = Field(default_factory=list, description="競馬場ごとの出力")
    warnings: list[str] = Field(default_factory=list, description="取得できなかった情報")
