from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..contracts import AgentResult, NewsItem, iso_now
from ..errors import AgentError, DataUnavailableError, SchemaChangedError
from ..http import HttpClient
from ..storage import append_jsonl_deduplicated, atomic_write_json
from .base import BaseAgent


A_SHARE_TERMS = (
    "A股", "沪指", "深成指", "上证", "深交所", "上交所", "沪深", "两市",
    "涨停", "跌停", "北向资金", "主力资金",
    "龙虎榜", "科创板", "创业板", "中证", "证监会", "券商", "公募", "私募",
)
DOMESTIC_MARKET_TERMS = ("股市", "股票", "证券", "个股", "上市公司", "板块", "概念股")
DOMESTIC_CORPORATE_TERMS = (
    "公司公告", "业绩预告", "净利润", "营业收入", "并购", "重组", "回购", "增持",
    "减持", "中标", "定增", "分红", "股东", "董事会", "实际控制人",
)
FOREIGN_MARKERS = (
    "美股", "港股", "日股", "欧股", "纳斯达克", "道琼斯", "标普", "海外", "全球",
    "美国", "欧洲", "日本", "韩国", "英国", "德国", "法国", "华尔街",
    "印度", "印尼", "澳大利亚", "加拿大", "俄罗斯", "乌克兰", "新加坡", "中东",
)
FOREIGN_URL_MARKERS = ("/usstock/", "/hkstock/", "/forex/", "/global/", "/world/")
GLOBAL_IMPACT_GROUPS: dict[str, tuple[str, ...]] = {
    "global_monetary": (
        "美联储", "fomc", "降息", "加息", "利率决议", "美国非农", "美国cpi",
        "美国通胀", "美元指数", "美债收益率", "欧洲央行", "日本央行",
    ),
    "fx_china": (
        "人民币", "离岸人民币", "美元兑人民币", "中国资产", "富时中国a50", "msci中国",
        "北向资金", "外资", "中概股",
    ),
    "trade_policy": (
        "对华", "中美", "关税", "出口管制", "实体清单", "贸易摩擦", "制裁", "反倾销",
    ),
    "commodities": (
        "原油", "布伦特", "wti", "欧佩克", "opec", "黄金", "白银", "铜价", "铁矿石",
        "锂价", "天然气", "大宗商品",
    ),
    "global_supply_chain": (
        "英伟达", "台积电", "费城半导体", "半导体", "芯片", "人工智能", "ai算力",
        "苹果供应链", "新能源汽车", "光伏", "航运", "集运指数",
    ),
    "geopolitics": (
        "地缘冲突", "俄乌", "中东局势", "红海", "霍尔木兹", "停火", "战争升级",
    ),
}
STOCK_CODE_RE = re.compile(r"(?<!\d)(?:00[0-3]|30[01]|60[0135]|68[89])\d{3}(?!\d)")
EASTMONEY_STOCK_REF_RE = re.compile(r"^(?:0|1)\.((?:00[0-3]|30[01]|60[0135]|68[89])\d{3})$")


def classify_a_share_relevance(
    title: str,
    summary: str = "",
    url: str = "",
    feed_region: str = "mixed",
) -> tuple[float, list[str], str]:
    """返回 A 股相关性分数、传导标签和新闻地域。"""
    text = f"{title} {summary}"
    lowered = text.lower()
    foreign = (
        any(term in text for term in FOREIGN_MARKERS)
        or any(marker in url.lower() for marker in FOREIGN_URL_MARKERS)
        or feed_region == "global"
    )
    region = "global" if foreign else "domestic"
    tags: list[str] = []
    score = 0.0
    if STOCK_CODE_RE.search(text):
        tags.append("a_share_code")
        score = 1.0
    direct_terms = [term for term in A_SHARE_TERMS if term.lower() in lowered]
    if direct_terms:
        tags.append("a_share_direct")
        score = max(score, 0.9)
    if not foreign and any(term in text for term in DOMESTIC_MARKET_TERMS):
        tags.append("domestic_market")
        score = max(score, 0.68)
    domestic_stock_url = "/stock/" in url or "eastmoney.com/a/" in url
    if any(term in text for term in DOMESTIC_CORPORATE_TERMS) and domestic_stock_url:
        tags.append("listed_company")
        score = max(score, 0.72 if not foreign else 0.45)
    for tag, terms in GLOBAL_IMPACT_GROUPS.items():
        if any(term.lower() in lowered for term in terms):
            tags.append(tag)
            score = max(score, 0.72)
    if domestic_stock_url and not foreign:
        tags.append("domestic_stock_channel")
        score = max(score, 0.6)
    if foreign and not any(tag in GLOBAL_IMPACT_GROUPS for tag in tags) and score < 0.9:
        # 单纯海外股指或个股涨跌并不足以构成 A 股实时信号。
        score = min(score, 0.4)
    return round(score, 2), list(dict.fromkeys(tags)), region


def is_a_share_news(title: str, summary: str = "", url: str = "", feed_region: str = "mixed") -> bool:
    score, _, _ = classify_a_share_relevance(title, summary, url, feed_region)
    return score >= 0.55


def _published(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.isdigit():
        timestamp = int(text)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone().isoformat(timespec="seconds")


def _eastmoney_stock_codes(raw: dict[str, Any]) -> list[str]:
    values = raw.get("stockList") or []
    if not isinstance(values, list):
        return []
    codes: list[str] = []
    for value in values:
        if isinstance(value, dict):
            value = value.get("quoteId") or value.get("securityCode") or value.get("code") or ""
        matched = EASTMONEY_STOCK_REF_RE.fullmatch(str(value).strip())
        if matched:
            codes.append(matched.group(1))
    return list(dict.fromkeys(codes))


def _eastmoney_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if str(payload.get("code", "")) != "1":
        raise SchemaChangedError(
            f"东方财富实时新闻接口返回失败：{payload.get('message') or payload.get('code') or '未知错误'}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SchemaChangedError("东方财富实时新闻响应缺少 data 对象")
    rows = data.get("fastNewsList")
    if not isinstance(rows, list):
        raise SchemaChangedError("东方财富实时新闻 data.fastNewsList 不再是列表")
    return [row for row in rows if isinstance(row, dict)]


def parse_eastmoney_payload(
    payload: dict[str, Any],
    *,
    feed: str = "eastmoney_724",
    channel: str = "7×24快讯",
    feed_region: str = "mixed",
    min_relevance_score: float = 0.55,
) -> list[NewsItem]:
    items: list[NewsItem] = []
    for raw in _eastmoney_rows(payload):
        title = re.sub(r"<[^>]+>", "", str(raw.get("title") or "")).strip()
        summary = re.sub(
            r"<[^>]+>", "", str(raw.get("summary") or raw.get("digest") or raw.get("content") or "")
        ).strip()
        raw_id = str(raw.get("code") or raw.get("id") or "").strip()
        url = str(raw.get("uniqueUrl") or raw.get("url") or "").strip()
        if not url and raw_id:
            url = f"https://finance.eastmoney.com/a/{raw_id}.html"
        stock_codes = _eastmoney_stock_codes(raw)
        relevance_score, relevance_tags, region = classify_a_share_relevance(
            title, summary, url, feed_region
        )
        if stock_codes:
            relevance_score = 1.0
            relevance_tags = list(dict.fromkeys(["a_share_code", *relevance_tags]))
        if not title or not url or relevance_score < min_relevance_score:
            continue
        news_id = f"eastmoney:{raw_id}" if raw_id else hashlib.sha256(url.encode()).hexdigest()[:24]
        media_name = str(raw.get("mediaName") or raw.get("source") or "").strip()
        source = f"东方财富 · {media_name}" if media_name else "东方财富快讯"
        items.append(
            NewsItem(
                news_id=news_id,
                title=title,
                url=url,
                published_at=_published(
                    raw.get("showTime") or raw.get("pubTime") or raw.get("datetime") or raw.get("time")
                ),
                source=source,
                channel=channel,
                summary=summary,
                region=region,
                feed=feed,
                relevance_score=relevance_score,
                relevance_tags=relevance_tags,
                stock_codes=stock_codes,
            )
        )
    return items


class NewsAgent(BaseAgent):
    name = "news"

    def run(self) -> AgentResult:
        started = iso_now()
        config = self.settings.section("news")
        http_config = self.settings.section("http")
        client = HttpClient(
            timeout=float(http_config["timeout_seconds"]),
            user_agent=str(http_config["user_agent"]),
            interval=float(config.get("request_interval_seconds", http_config["request_interval_seconds"])),
            default_headers={"Referer": "https://kuaixun.eastmoney.com/"},
        )
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        raw_dir = self.settings.runtime / "agents" / "news" / "raw" / stamp
        data_dir = self.settings.runtime / "agents" / "news" / "data"
        raw_dir.mkdir(parents=True, exist_ok=True)
        cutoff = datetime.now().astimezone() - timedelta(hours=float(config["lookback_hours"]))
        items: list[NewsItem] = []
        seen: set[str] = set()
        source_stats: dict[str, dict[str, Any]] = {}
        source_failures: list[dict[str, str]] = []
        if str(config.get("provider", "eastmoney")).lower() != "eastmoney":
            raise DataUnavailableError("新闻 provider 必须配置为 eastmoney")
        sources = config.get("sources") or [
            {
                "name": "eastmoney_724", "base_url": config["base_url"],
                "fast_column": "102", "channel": "7×24快讯", "region": "mixed",
            }
        ]
        for source in sources:
            name = str(source.get("name") or f"source_{len(source_stats) + 1}")
            source_dir = raw_dir / name
            source_dir.mkdir(parents=True, exist_ok=True)
            accepted = 0
            pages_fetched = 0
            sort_end = ""
            seen_cursors: set[str] = set()
            try:
                for page in range(1, int(source.get("max_pages", config["max_pages"])) + 1):
                    payload = client.get_json(
                        str(source.get("base_url") or config["base_url"]),
                        {
                            "client": "web",
                            "biz": "web_724",
                            "fastColumn": str(source.get("fast_column") or "102"),
                            "sortEnd": sort_end,
                            "pageSize": source.get("page_size", config["page_size"]),
                            "req_trace": f"{int(datetime.now().timestamp() * 1000)}{page:02d}",
                        },
                    )
                    pages_fetched += 1
                    atomic_write_json(source_dir / f"page_{page:03d}.json", payload)
                    raw_rows = _eastmoney_rows(payload)
                    if not raw_rows:
                        break
                    page_items = parse_eastmoney_payload(
                        payload,
                        feed=name,
                        channel=str(source.get("channel") or "东方财富快讯"),
                        feed_region=str(source.get("region", "mixed")),
                        min_relevance_score=float(config.get("min_relevance_score", 0.55)),
                    )
                    raw_oldest = _oldest_published(raw_rows)
                    for item in page_items:
                        published = _parse_published(item.published_at)
                        if published is not None and published < cutoff:
                            continue
                        if item.news_id not in seen:
                            seen.add(item.news_id)
                            items.append(item)
                            accepted += 1
                    if raw_oldest is not None and raw_oldest < cutoff:
                        break
                    data = payload.get("data", {})
                    next_sort_end = str(
                        data.get("sortEnd")
                        or raw_rows[-1].get("realSort")
                        or ""
                    ).strip()
                    if not next_sort_end or next_sort_end == sort_end or next_sort_end in seen_cursors:
                        break
                    seen_cursors.add(next_sort_end)
                    sort_end = next_sort_end
                source_stats[name] = {
                    "ok": True,
                    "pages": pages_fetched,
                    "accepted": accepted,
                    "fast_column": str(source.get("fast_column") or "102"),
                }
            except (AgentError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                source_stats[name] = {
                    "ok": False,
                    "pages": pages_fetched,
                    "accepted": accepted,
                    "fast_column": str(source.get("fast_column") or "102"),
                }
                source_failures.append({"source": name, "error": str(exc)[:500]})
        if not items:
            detail = f"；来源错误：{source_failures}" if source_failures else ""
            raise DataUnavailableError(f"东方财富实时新闻中未取得符合 A 股传导规则的新闻{detail}")
        items.sort(key=lambda item: item.published_at, reverse=True)
        rows = [asdict(item) for item in items]
        archive = data_dir / "news_archive.jsonl"
        added = append_jsonl_deduplicated(archive, rows, "news_id")
        latest = data_dir / "latest.json"
        domestic_count = sum(1 for item in items if item.region == "domestic")
        global_count = len(items) - domestic_count
        atomic_write_json(
            latest,
            {
                "provider": "eastmoney", "fetched_at": iso_now(), "count": len(rows),
                "domestic_count": domestic_count, "global_count": global_count,
                "source_stats": source_stats, "source_failures": source_failures, "items": rows,
            },
        )
        return AgentResult(
            agent=self.name,
            ok=True,
            started_at=started,
            finished_at=iso_now(),
            records=len(rows),
            output_files=[str(archive), str(latest), str(raw_dir)],
            message=(
                f"东方财富实时 A 股新闻抓取完成：国内 {domestic_count} 条、"
                f"海外传导 {global_count} 条，新增 {added} 条"
            ),
            metadata={
                "provider": "eastmoney",
                "new_records": added, "lookback_hours": config["lookback_hours"],
                "domestic_records": domestic_count, "global_records": global_count,
                "source_stats": source_stats, "source_failures": source_failures,
            },
        )


def _parse_published(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_published(value))
    except (TypeError, ValueError, OSError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone()


def _oldest_published(rows: list[Any]) -> datetime | None:
    values = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _parse_published(
            row.get("showTime") or row.get("pubTime") or row.get("ctime")
            or row.get("datetime") or row.get("time")
        )
        if parsed is not None:
            values.append(parsed)
    return min(values) if values else None
