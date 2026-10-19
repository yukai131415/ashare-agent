from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

from ..contracts import AgentResult, iso_now
from ..errors import DataUnavailableError
from ..storage import atomic_write_json, read_csv, read_json
from .base import BaseAgent


POSITIVE_TERMS = ("利好", "增长", "增持", "回购", "突破", "中标", "预增", "创新高", "上调", "盈利", "涨停")
NEGATIVE_TERMS = ("利空", "亏损", "减持", "处罚", "调查", "下调", "暴跌", "风险", "退市", "违约", "跌停")


def sentiment(text: str) -> float:
    positive = sum(text.count(word) for word in POSITIVE_TERMS)
    negative = sum(text.count(word) for word in NEGATIVE_TERMS)
    total = positive + negative
    return (positive - negative) / total if total else 0.0


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _zscore(rows: list[dict[str, Any]], source: str, target: str, transform=lambda value: value) -> None:
    values = [transform(value) for row in rows if (value := _float(row.get(source))) is not None]
    mean = statistics.fmean(values) if values else 0.0
    deviation = statistics.pstdev(values) if len(values) > 1 else 1.0
    if deviation == 0:
        deviation = 1.0
    for row in rows:
        value = _float(row.get(source))
        row[target] = round((transform(value) - mean) / deviation, 6) if value is not None else 0.0


def build_features(market: list[dict[str, Any]], news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    global_news_score = statistics.fmean([sentiment(f"{item.get('title', '')} {item.get('summary', '')}") for item in news]) if news else 0.0
    rows: list[dict[str, Any]] = []
    for item in market:
        code, name = str(item.get("code", "")), str(item.get("name", ""))
        related = []
        for entry in news:
            text = f"{entry.get('title', '')} {entry.get('summary', '')}"
            stock_codes = {str(value) for value in (entry.get("stock_codes") or [])}
            if code in stock_codes or code in text or (len(name) >= 2 and name in text):
                related.append(entry)
        related_score = statistics.fmean([sentiment(f"{entry.get('title', '')} {entry.get('summary', '')}") for entry in related]) if related else global_news_score * 0.25
        row = dict(item)
        row["news_count"] = len(related)
        row["news_sentiment"] = round(related_score, 6)
        rows.append(row)
    _zscore(rows, "change_pct", "momentum_z")
    _zscore(rows, "volume_ratio", "volume_ratio_z")
    _zscore(rows, "turnover_pct", "turnover_z")
    _zscore(rows, "change_speed_pct", "change_speed_z")
    _zscore(rows, "amplitude_pct", "amplitude_z")
    _zscore(rows, "amount", "liquidity_z", lambda value: math.log1p(max(value, 0.0)))
    # 正且较低的市盈率得分更高；亏损/缺失公司赋中性值，避免错误奖励。
    positive_pes = [value for row in rows if (value := _float(row.get("pe_ratio"))) is not None and value > 0]
    median_pe = statistics.median(positive_pes) if positive_pes else 30.0
    for row in rows:
        pe = _float(row.get("pe_ratio"))
        row["valuation_raw"] = 1.0 / max(pe if pe and pe > 0 else median_pe, 1.0)
    _zscore(rows, "valuation_raw", "valuation_z")
    return rows


class AnalysisAgent(BaseAgent):
    name = "analysis"

    def run(self) -> AgentResult:
        started = iso_now()
        market_path = self.settings.runtime / "agents" / "market" / "data" / "latest.csv"
        news_path = self.settings.runtime / "agents" / "news" / "data" / "latest.json"
        if not market_path.exists():
            raise DataUnavailableError("缺少行情 Agent 的 latest.csv")
        market = read_csv(market_path)
        news_payload = read_json(news_path, {"items": []})
        news = news_payload.get("items", []) if isinstance(news_payload, dict) else []
        if not market:
            raise DataUnavailableError("行情文件为空")
        features = build_features(market, news)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        output_dir = self.settings.runtime / "agents" / "analysis" / "data"
        snapshot = output_dir / f"features_{stamp}.json"
        latest = output_dir / "latest.json"
        payload = {
            "generated_at": iso_now(), "market_source": str(market_path), "news_source": str(news_path),
            "count": len(features), "features": features,
        }
        atomic_write_json(snapshot, payload)
        atomic_write_json(latest, payload)
        return AgentResult(
            agent=self.name, ok=True, started_at=started, finished_at=iso_now(), records=len(features),
            output_files=[str(snapshot), str(latest)], message="多因子特征与新闻情绪分析完成",
        )
