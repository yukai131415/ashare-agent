from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class AgentResult:
    agent: str
    ok: bool
    started_at: str
    finished_at: str
    records: int = 0
    output_files: list[str] = field(default_factory=list)
    message: str = ""
    error_type: str | None = None
    repair_action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class MarketRow:
    code: str
    name: str
    price: float
    change_pct: float
    change: float | None = None
    change_speed_pct: float | None = None
    turnover_pct: float | None = None
    volume_ratio: float | None = None
    amplitude_pct: float | None = None
    amount: float | None = None
    circulating_shares: float | None = None
    circulating_market_cap: float | None = None
    pe_ratio: float | None = None
    previous_close: float | None = None
    high_price: float | None = None
    quote_time: str | None = None


@dataclass(slots=True)
class NewsItem:
    news_id: str
    title: str
    url: str
    published_at: str
    source: str = "东方财富快讯"
    channel: str = "股票"
    summary: str = ""
    region: str = "domestic"
    feed: str = "eastmoney_724"
    relevance_score: float = 1.0
    relevance_tags: list[str] = field(default_factory=list)
    stock_codes: list[str] = field(default_factory=list)
