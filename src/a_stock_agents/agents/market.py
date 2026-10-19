from __future__ import annotations

import re
import time
import hashlib
import json
import random
from dataclasses import asdict
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..contracts import AgentResult, MarketRow, iso_now
from ..errors import DataUnavailableError, SchemaChangedError, SourceBlockedError, StateCorruptedError
from ..http import HttpClient
from ..storage import atomic_write_json, read_json, write_csv
from .base import BaseAgent


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _number(value: str, *, percent: bool = False) -> float | None:
    text = value.strip().replace(",", "").replace("%", "")
    if not text or text in {"--", "-", "亏损", "N/A"}:
        return None
    multiplier = 1.0
    if text.endswith("万"):
        multiplier, text = 10_000.0, text[:-1]
    elif text.endswith("亿"):
        multiplier, text = 100_000_000.0, text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return None


HEADER_ALIASES = {
    "code": {"代码", "股票代码"},
    "name": {"名称", "股票简称", "股票名称"},
    "price": {"现价", "最新价"},
    "change_pct": {"涨跌幅(%)", "涨跌幅", "涨跌幅（%）"},
    "change": {"涨跌"},
    "change_speed_pct": {"涨速(%)", "涨速", "涨速（%）"},
    "turnover_pct": {"换手(%)", "换手率", "换手（%）"},
    "volume_ratio": {"量比"},
    "amplitude_pct": {"振幅(%)", "振幅", "振幅（%）"},
    "amount": {"成交额"},
    "circulating_shares": {"流通股"},
    "circulating_market_cap": {"流通市值"},
    "pe_ratio": {"市盈率"},
}


def parse_market_html(html: str) -> list[MarketRow]:
    parser = _TableParser()
    parser.feed(html)
    header_index = next((index for index, row in enumerate(parser.rows) if any(cell in HEADER_ALIASES["code"] for cell in row)), None)
    if header_index is None:
        if not parser.rows:
            return []
        raise SchemaChangedError("同花顺行情页中未找到股票表头")
    headers = parser.rows[header_index]
    indexes: dict[str, int] = {}
    for canonical, aliases in HEADER_ALIASES.items():
        for idx, header in enumerate(headers):
            if header in aliases:
                indexes[canonical] = idx
                break
    required = {"code", "name", "price", "change_pct"}
    if not required.issubset(indexes):
        raise SchemaChangedError(f"同花顺字段变化，缺少: {sorted(required - indexes.keys())}; 实际字段: {headers}")

    def cell(row: list[str], name: str) -> str:
        idx = indexes.get(name)
        return row[idx] if idx is not None and idx < len(row) else ""

    result: list[MarketRow] = []
    for row in parser.rows[header_index + 1 :]:
        code = re.sub(r"\D", "", cell(row, "code"))
        price = _number(cell(row, "price"))
        change_pct = _number(cell(row, "change_pct"), percent=True)
        if len(code) != 6 or price is None or change_pct is None:
            continue
        result.append(
            MarketRow(
                code=code,
                name=cell(row, "name"),
                price=price,
                change_pct=change_pct,
                change=_number(cell(row, "change")),
                change_speed_pct=_number(cell(row, "change_speed_pct"), percent=True),
                turnover_pct=_number(cell(row, "turnover_pct"), percent=True),
                volume_ratio=_number(cell(row, "volume_ratio")),
                amplitude_pct=_number(cell(row, "amplitude_pct"), percent=True),
                amount=_number(cell(row, "amount")),
                circulating_shares=_number(cell(row, "circulating_shares")),
                circulating_market_cap=_number(cell(row, "circulating_market_cap")),
                pe_ratio=_number(cell(row, "pe_ratio")),
            )
        )
    return result


def is_allowed_stock(row: MarketRow, config: dict[str, Any]) -> bool:
    name_upper = row.name.upper().replace(" ", "")
    if config.get("exclude_st", True) and "ST" in name_upper:
        return False
    if config.get("exclude_gem", True) and row.code.startswith(("300", "301")):
        return False
    if config.get("exclude_bse", True) and row.code.startswith(("4", "8", "92")):
        return False
    if config.get("exclude_star", True) and row.code.startswith(("688", "689")):
        return False
    # 沪深 A 股代码范围；科创板默认排除。
    return row.code.startswith(("000", "001", "002", "003", "600", "601", "603", "605", "688", "689"))


MARKET_FIELDS = list(MarketRow.__dataclass_fields__)


def _json_number(value: Any) -> float | None:
    if value in (None, "", "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_eastmoney_payload(payload: dict[str, Any]) -> list[MarketRow]:
    """将东方财富 push2 行情列表映射为系统内部的统一行情格式。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SchemaChangedError("东方财富行情响应缺少 data 对象")
    diff = data.get("diff")
    if diff is None:
        return []
    source_rows = list(diff.values()) if isinstance(diff, dict) else diff
    if not isinstance(source_rows, list):
        raise SchemaChangedError("东方财富行情 data.diff 不再是列表")
    result: list[MarketRow] = []
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        code = str(source.get("f12") or "").strip()
        name = str(source.get("f14") or "").strip()
        price = _json_number(source.get("f2"))
        change_pct = _json_number(source.get("f3"))
        if len(code) != 6 or not name or price is None or change_pct is None:
            continue
        result.append(
            MarketRow(
                code=code,
                name=name,
                price=price,
                change_pct=change_pct,
                change=_json_number(source.get("f4")),
                change_speed_pct=_json_number(source.get("f22")),
                turnover_pct=_json_number(source.get("f8")),
                volume_ratio=_json_number(source.get("f10")),
                amplitude_pct=_json_number(source.get("f7")),
                amount=_json_number(source.get("f6")),
                circulating_shares=None,
                circulating_market_cap=_json_number(source.get("f21")),
                pe_ratio=_json_number(source.get("f9")),
            )
        )
    return result


def parse_tencent_quotes(text: str) -> list[MarketRow]:
    """解析腾讯财经 qt.gtimg.cn 的 GBK 波浪线分隔行情。"""
    result: list[MarketRow] = []
    for match in re.finditer(r'v_[^=]+="([^"]*)";', text):
        fields = match.group(1).split("~")
        if len(fields) < 46:
            continue
        code = fields[2].strip()
        name = fields[1].strip()
        price = _json_number(fields[3])
        change_pct = _json_number(fields[32])
        if len(code) != 6 or not name or price is None or change_pct is None or price <= 0:
            continue
        amount_10k = _json_number(fields[37])
        float_cap_100m = _json_number(fields[44])
        float_cap = float_cap_100m * 100_000_000 if float_cap_100m is not None else None
        result.append(
            MarketRow(
                code=code,
                name=name,
                price=price,
                change_pct=change_pct,
                change=_json_number(fields[31]),
                previous_close=_json_number(fields[4]),
                high_price=_json_number(fields[33]),
                quote_time=fields[30].strip(),
                change_speed_pct=None,
                turnover_pct=_json_number(fields[38]),
                volume_ratio=None,
                amplitude_pct=_json_number(fields[43]),
                amount=amount_10k * 10_000 if amount_10k is not None else None,
                circulating_shares=float_cap / price if float_cap is not None else None,
                circulating_market_cap=float_cap,
                pe_ratio=_json_number(fields[39]),
            )
        )
    return result


class MarketDataAgent(BaseAgent):
    name = "market"

    @staticmethod
    def _config_signature(config: dict[str, Any]) -> str:
        serialized = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]

    def _fetch_page(self, client: HttpClient, config: dict[str, Any], page: int) -> tuple[dict[str, Any], str]:
        urls = [str(url) for url in (config.get("base_urls") or [config["base_url"]])]
        params = {
            "pn": page,
            "pz": config["page_size"],
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": config["sort_field"],
            "fs": config["market_filter"],
            "fields": config["fields"],
            "ut": config["public_token"],
            # 避免边缘节点错误复用已失效的缓存响应。
            "_": int(time.time() * 1000),
        }
        errors: list[str] = []
        retries = max(1, int(config.get("page_retries", 2)))
        for attempt in range(1, retries + 1):
            # 每轮改变起始节点，避免主节点故障时每次都先等待同一个超时。
            offset = (page + attempt - 2) % len(urls)
            ordered_urls = urls[offset:] + urls[:offset]
            for url in ordered_urls:
                try:
                    return client.get_json(str(url), params), str(url)
                except SourceBlockedError as exc:
                    errors.append(f"{url}: {exc}")
            if attempt < retries:
                backoff = float(config.get("page_retry_backoff_seconds", 1.0)) * attempt
                time.sleep(backoff + random.uniform(0.1, 0.5))
        nodes = ", ".join(urls)
        last_reason = errors[-1] if errors else "未知连接错误"
        raise SourceBlockedError(
            f"东方财富第 {page} 页的 {len(urls)} 个节点均不可用（{nodes}）；最后错误：{last_reason}"
        )

    def run(self) -> AgentResult:
        config = self.settings.section("market")
        if config.get("provider") == "tencent":
            return self._run_tencent(config)
        return self._run_eastmoney(config)

    def _run_tencent(self, config: dict[str, Any]) -> AgentResult:
        started = iso_now()
        http_config = self.settings.section("http")
        client = HttpClient(
            timeout=float(config.get("timeout_seconds", http_config["timeout_seconds"])),
            user_agent=str(http_config["user_agent"]),
            interval=float(config.get("request_interval_seconds", http_config["request_interval_seconds"])),
        )
        data_dir = self.settings.runtime / "agents" / "market" / "data"
        symbols_path = data_dir / "tencent_symbols.json"
        try:
            symbols_payload = client.get_json(str(config["symbol_list_url"]))
            symbols = symbols_payload.get("list", []) if isinstance(symbols_payload, dict) else []
            if not isinstance(symbols, list) or not symbols:
                raise SchemaChangedError("腾讯备用代码清单缺少 list")
            atomic_write_json(symbols_path, symbols_payload)
        except Exception as exc:
            try:
                cached_symbols = read_json(symbols_path, {})
            except StateCorruptedError:
                cached_symbols = {}
            symbols = cached_symbols.get("list", []) if isinstance(cached_symbols, dict) else []
            if not symbols:
                raise SourceBlockedError(f"无法取得腾讯 A 股代码清单且本地无缓存：{exc}") from exc

        # 在请求报价前先排除创业板和北交所，减少约三分之一的网络数据量。
        requested_symbols = []
        for symbol in symbols:
            normalized = str(symbol).lower().strip()
            code = normalized[-6:]
            if not normalized.startswith(("sh", "sz")):
                continue
            probe = MarketRow(code=code, name="待获取", price=1.0, change_pct=0.0)
            if is_allowed_stock(probe, config):
                requested_symbols.append(normalized)
        requested_symbols = list(dict.fromkeys(requested_symbols))
        if not requested_symbols:
            raise DataUnavailableError("腾讯 A 股代码清单过滤后为空")

        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        raw_dir = self.settings.runtime / "agents" / "market" / "raw" / stamp
        raw_dir.mkdir(parents=True, exist_ok=True)
        batch_size = max(1, int(config.get("batch_size", 180)))
        retries = max(1, int(config.get("page_retries", 2)))
        all_rows: list[MarketRow] = []
        seen_codes: set[str] = set()
        batch_count = (len(requested_symbols) + batch_size - 1) // batch_size
        for batch_index, start in enumerate(range(0, len(requested_symbols), batch_size), 1):
            batch = requested_symbols[start : start + batch_size]
            last_error: Exception | None = None
            text = ""
            for attempt in range(1, retries + 1):
                try:
                    text = client.get_text(str(config["quote_base_url"]), {"q": ",".join(batch)})
                    if not text.strip():
                        raise SourceBlockedError("腾讯行情返回空响应")
                    break
                except SourceBlockedError as exc:
                    last_error = exc
                    if attempt < retries:
                        time.sleep(float(config.get("page_retry_backoff_seconds", 1.0)) * attempt)
            else:
                raise SourceBlockedError(f"腾讯行情第 {batch_index}/{batch_count} 批失败：{last_error}") from last_error
            atomic_write_json(raw_dir / f"batch_{batch_index:03d}.json", {"symbols": batch, "payload": text})
            for row in parse_tencent_quotes(text):
                if row.code not in seen_codes and is_allowed_stock(row, config):
                    seen_codes.add(row.code)
                    all_rows.append(row)

        coverage = len(seen_codes) / len(requested_symbols)
        min_coverage = float(config.get("min_quote_coverage", 0.95))
        if coverage < min_coverage:
            raise DataUnavailableError(
                f"腾讯行情仅返回 {len(seen_codes)}/{len(requested_symbols)} 个有效标的"
                f"（{coverage:.1%}），低于完整性阈值 {min_coverage:.0%}"
            )
        records = [asdict(row) for row in all_rows]
        snapshot = data_dir / f"market_{stamp}.csv"
        latest = data_dir / "latest.csv"
        write_csv(snapshot, records, MARKET_FIELDS)
        write_csv(latest, records, MARKET_FIELDS)
        return AgentResult(
            agent=self.name,
            ok=True,
            started_at=started,
            finished_at=iso_now(),
            records=len(records),
            output_files=[str(snapshot), str(latest), str(raw_dir), str(symbols_path)],
            message="腾讯财经 A 股实时行情快照抓取完成",
            metadata={
                "provider": "tencent",
                "requested_symbols": len(requested_symbols),
                "quote_coverage": coverage,
                "batches": batch_count,
                "filters": {
                    "exclude_st": config.get("exclude_st", True),
                    "exclude_gem": config.get("exclude_gem", True),
                    "exclude_bse": config.get("exclude_bse", True),
                    "exclude_star": config.get("exclude_star", True),
                },
            },
        )

    def _run_eastmoney(self, config: dict[str, Any]) -> AgentResult:
        started = iso_now()
        http_config = self.settings.section("http")
        client = HttpClient(
            timeout=float(config.get("timeout_seconds", http_config["timeout_seconds"])),
            user_agent=str(http_config["user_agent"]),
            interval=float(config.get("request_interval_seconds", http_config["request_interval_seconds"])),
        )
        data_dir = self.settings.runtime / "agents" / "market" / "data"
        checkpoint_path = data_dir / "checkpoint.json"
        today = datetime.now().astimezone().strftime("%Y%m%d")
        signature = self._config_signature(config)
        try:
            checkpoint = read_json(checkpoint_path, {})
        except StateCorruptedError:
            # checkpoint 只是可丢弃的运行缓存，损坏时从头重建，不阻断行情 Agent。
            checkpoint = {}
        try:
            checkpoint_next_page = int(checkpoint.get("next_page", 1)) if isinstance(checkpoint, dict) else 1
        except (TypeError, ValueError):
            checkpoint_next_page = 1
        can_resume = (
            isinstance(checkpoint, dict)
            and checkpoint.get("day") == today
            and checkpoint.get("config_signature") == signature
            and not checkpoint.get("completed")
            and checkpoint_next_page > 1
        )
        if can_resume:
            stamp = str(checkpoint["stamp"])
            start_page = checkpoint_next_page
            try:
                all_rows = [MarketRow(**row) for row in checkpoint.get("all_rows", [])]
            except (TypeError, ValueError):
                all_rows = []
                start_page = 1
            seen_codes = set(checkpoint.get("seen_codes", []))
            reported_total = checkpoint.get("reported_total")
            if not isinstance(reported_total, int):
                reported_total = None
            message_prefix = f"从第 {start_page} 页断点续抓；"
        else:
            stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
            start_page = 1
            all_rows = []
            seen_codes = set()
            reported_total = None
            message_prefix = ""
        raw_dir = self.settings.runtime / "agents" / "market" / "raw" / stamp
        raw_dir.mkdir(parents=True, exist_ok=True)
        empty_pages = 0
        endpoint_usage: dict[str, int] = {}
        for page in range(start_page, int(config["max_pages"]) + 1):
            payload, endpoint = self._fetch_page(client, config, page)
            endpoint_usage[endpoint] = endpoint_usage.get(endpoint, 0) + 1
            atomic_write_json(raw_dir / f"page_{page:03d}.json", payload)
            rows = parse_eastmoney_payload(payload)
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("total"), int):
                reported_total = data["total"]
            new_rows = [row for row in rows if row.code not in seen_codes]
            if not new_rows:
                empty_pages += 1
                if empty_pages >= int(config["stop_after_empty_pages"]):
                    break
                continue
            empty_pages = 0
            for row in new_rows:
                seen_codes.add(row.code)
                if is_allowed_stock(row, config):
                    all_rows.append(row)
            atomic_write_json(
                checkpoint_path,
                {
                    "day": today,
                    "stamp": stamp,
                    "config_signature": signature,
                    "next_page": page + 1,
                    "reported_total": reported_total,
                    "seen_codes": sorted(seen_codes),
                    "all_rows": [asdict(row) for row in all_rows],
                    "completed": False,
                },
            )
            # 某些边缘节点会无视 pz 并将单页截断到 100 条，因此以实际唯一代码数判断完成。
            if reported_total is not None and len(seen_codes) >= reported_total:
                break
        if not all_rows:
            raise DataUnavailableError("东方财富抓取完成但筛选后没有可用的沪深 A 股数据")
        if reported_total is not None and len(seen_codes) < reported_total:
            raise DataUnavailableError(
                f"东方财富仅抓到 {len(seen_codes)}/{reported_total} 个原始标的，拒绝用不完整行情覆盖有效快照"
            )
        records = [asdict(row) for row in all_rows]
        snapshot = data_dir / f"market_{stamp}.csv"
        latest = data_dir / "latest.csv"
        write_csv(snapshot, records, MARKET_FIELDS)
        write_csv(latest, records, MARKET_FIELDS)
        atomic_write_json(
            checkpoint_path,
            {
                "day": today,
                "stamp": stamp,
                "config_signature": signature,
                "next_page": page + 1,
                "reported_total": reported_total,
                "completed": True,
                "records": len(records),
            },
        )
        return AgentResult(
            agent=self.name,
            ok=True,
            started_at=started,
            finished_at=iso_now(),
            records=len(records),
            output_files=[str(snapshot), str(latest), str(raw_dir)],
            message=f"{message_prefix}东方财富公开行情快照抓取完成",
            metadata={
                "provider": config["provider"],
                "pages_saved": len(list(raw_dir.glob("*.json"))),
                "reported_total": reported_total,
                "endpoint_usage": endpoint_usage,
                "filters": config,
            },
        )
