from __future__ import annotations

import json
import math
from datetime import date, datetime, time, timedelta
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any

from ..contracts import AgentResult, iso_now
from ..model_state import ModelState
from ..recommendation_store import recommendation_store
from ..storage import atomic_write_json, read_csv, upsert_jsonl_by_key
from ..trading_calendar import is_trading_weekday
from .base import BaseAgent


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    rows.append(value)
            except json.JSONDecodeError:
                continue
    return rows


def negative_streak(rows: list[dict[str, Any]]) -> int:
    """按目标日从新到旧计算最近连续亏损条数。"""
    completed = [row for row in rows if row.get("profitable") is not None]
    completed.sort(
        key=lambda row: (
            str(row.get("target_date", "")),
            str(row.get("evaluated_at", "")),
            str(row.get("recommendation_id", "")),
        ),
        reverse=True,
    )
    streak = 0
    for row in completed:
        if row.get("profitable") is False:
            streak += 1
            continue
        break
    return streak


def is_close_based_result(row: dict[str, Any]) -> bool:
    return (
        row.get("calculation_version") == "target-high-prev-close-minus-1-v1"
        and row.get("target_high_price") is not None
        and row.get("previous_close_price") is not None
    )


def high_return_pct(high: float, previous_close: float) -> float:
    if not all(math.isfinite(value) and value > 0 for value in (high, previous_close)):
        raise ValueError("最高价和昨收价必须是有效正数")
    return (high / previous_close - 1.0) * 100.0 - 1.0


def parse_daily_high(payload: dict, symbol: str, target_date: date) -> tuple[float, float]:
    if payload.get("code") != 0:
        raise ValueError("历史行情接口返回错误")
    # 只用不复权日线，不用实时 qt 字段或复权价格替代。
    rows = sorted(payload.get("data", {}).get(symbol, {}).get("day", []), key=lambda row: row[0])
    for index, row in enumerate(rows):
        if row[0] == target_date.isoformat() and index > 0:
            if rows[index - 1][0] >= row[0]:
                raise ValueError("历史日期重复")
            high, previous_close = float(row[3]), float(rows[index - 1][2])
            high_return_pct(high, previous_close)
            if high < max(float(row[1]), float(row[2]), float(row[4])):
                raise ValueError("历史最高价不合法")
            return high, previous_close
    raise ValueError("缺少目标日日线或前一交易日收盘价")


def fetch_daily_high(data_dir: Path, code: str, target_date: date):
    symbol = ("sh" if code.startswith("6") else "sz") + code
    start = (target_date - timedelta(days=40)).isoformat()
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start},{target_date.isoformat()},60,"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    high, previous_close = parse_daily_high(payload, symbol, target_date)
    source = data_dir.parent / "raw" / "historical_daily" / f"{code}_{target_date}.json"
    atomic_write_json(source, {"url": url, "fetched_at": iso_now(), "adjustment": "none", "payload": payload})
    return high, previous_close, str(source), ""


def daily_high_prices(data_dir: Path, target_date: date, close_at: time):
    """使用行情自身日期及时间校验，禁止盘中或其他日期数据兜底。"""
    from .market import parse_tencent_quotes
    values = {}
    prefix = target_date.strftime("%Y%m%d")
    sources = sorted((data_dir.parent / "raw").glob(prefix + "*"))
    for directory in sources:
        for path in sorted(directory.glob("batch_*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = parse_tencent_quotes(payload.get("payload", ""))
            except (OSError, ValueError, TypeError):
                continue
            for row in rows:
                stamp = row.quote_time or ""
                try:
                    quoted_at = datetime.strptime(stamp, "%Y%m%d%H%M%S")
                    high_return_pct(row.high_price, row.previous_close)
                except (ValueError, TypeError):
                    continue
                if quoted_at.date() != target_date or quoted_at.time() < close_at:
                    continue
                if row.code not in values or stamp > values[row.code][3]:
                    values[row.code] = (row.high_price, row.previous_close, str(path), stamp)
    return values


def parse_close_time(value: Any) -> time:
    text = str(value or "15:00").strip()
    try:
        hour, minute = text.split(":", 1)
        return time(hour=int(hour), minute=int(minute[:2]))
    except (TypeError, ValueError):
        return time(hour=15, minute=0)


def target_close_reached(target_date: date, now: datetime, close_at: time) -> bool:
    today = now.date()
    if target_date < today:
        return True
    if target_date > today:
        return False
    return now.time() >= close_at


def market_snapshot_for_close(data_dir: Path, target_date: date, close_at: time) -> tuple[Path | None, bool]:
    """返回目标日行情快照；优先收盘后快照，否则用当天最后一份快照兜底。"""
    prefix = target_date.strftime("market_%Y%m%d_")
    all_candidates: list[tuple[time, Path]] = []
    for path in data_dir.glob(f"{prefix}*.csv"):
        stamp = path.stem.removeprefix(prefix)
        try:
            snapshot_time = time(
                hour=int(stamp[0:2]),
                minute=int(stamp[2:4]),
                second=int(stamp[4:6] or "0"),
            )
        except (ValueError, IndexError):
            continue
        all_candidates.append((snapshot_time, path))
    if not all_candidates:
        return None, False
    close_candidates = [item for item in all_candidates if item[0] >= close_at]
    if close_candidates:
        return sorted(close_candidates, key=lambda item: item[0], reverse=True)[0][1], True
    return sorted(all_candidates, key=lambda item: item[0], reverse=True)[0][1], False


def close_prices(data_dir: Path, target_date: date, close_at: time) -> tuple[dict[str, float], Path | None, bool]:
    snapshot, is_after_close = market_snapshot_for_close(data_dir, target_date, close_at)
    if snapshot is None:
        return {}, None, False
    return {row["code"]: float(row["price"]) for row in read_csv(snapshot)}, snapshot, is_after_close


class BacktestAgent(BaseAgent):
    name = "backtest"

    def run(self) -> AgentResult:
        started = iso_now()
        recommendation_config = self.settings.section("recommendation")
        backtest_config = self.settings.section("backtest")
        results_path = self.settings.runtime / "agents" / "backtest" / "data" / "results.jsonl"
        market_data_dir = self.settings.runtime / "agents" / "market" / "data"
        recommendations = [
            recommendation
            for recommendation in recommendation_store(self.settings.runtime).history()
            if _is_valid_trading_day_recommendation(recommendation)
        ]
        valid_recommendation_ids = {
            str(recommendation.get("recommendation_id"))
            for recommendation in recommendations
            if recommendation.get("recommendation_id")
        }
        existing_rows = [
            row for row in read_jsonl(results_path)
            if is_close_based_result(row)
            and str(row.get("recommendation_id", "")) in valid_recommendation_ids
        ]
        existing_by_rec = {str(row.get("recommendation_id")): row for row in existing_rows if row.get("recommendation_id")}
        now = datetime.now().astimezone()
        today = now.date()
        close_at = parse_close_time(backtest_config.get("close_time", "15:00"))
        price_cache: dict[str, dict] = {}
        results: list[dict[str, Any]] = []
        skipped_without_close = 0
        rotations: list[dict[str, str]] = []
        history_errors = []
        model_state = ModelState(self.settings.runtime / "state" / "model_state.json", list(recommendation_config["models"]))
        for recommendation in recommendations:
            rec_id = recommendation.get("recommendation_id")
            try:
                target_date = date.fromisoformat(str(recommendation["target_date"]))
            except (KeyError, ValueError):
                continue
            if not rec_id or not target_close_reached(target_date, now, close_at):
                continue
            code = str(recommendation.get("code", ""))
            cache_key = target_date.isoformat()
            if cache_key not in price_cache:
                price_cache[cache_key] = daily_high_prices(market_data_dir, target_date, close_at)
            prices = price_cache[cache_key]
            if code not in prices:
                try:
                    prices[code] = fetch_daily_high(market_data_dir, code, target_date)
                except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
                    skipped_without_close += 1
                    history_errors.append({"code": code, "target_date": cache_key, "error_type": type(exc).__name__})
                    continue
            entry = float(recommendation["reference_price"])
            high, previous_close, source, quote_time = prices[code]
            return_pct = high_return_pct(high, previous_close)
            result = {
                "backtest_id": f"{rec_id}:{today.isoformat()}",
                "recommendation_id": rec_id,
                "evaluated_at": iso_now(),
                "target_date": recommendation["target_date"],
                "code": code,
                "name": recommendation.get("name", ""),
                "model": recommendation.get("model", ""),
                "entry_reference_price": entry,
                "target_high_price": high,
                "previous_close_price": previous_close,
                "max_change_pct": round(return_pct + 1.0, 6),
                "deduction_percentage_points": 1.0,
                "source": source,
                "quote_time": quote_time,
                "calculation_version": "target-high-prev-close-minus-1-v1",
                "return_pct": round(return_pct, 6),
                "profitable": return_pct >= 0,
                "method": "目标日最高价相对前一交易日收盘价的涨幅减1个百分点（模拟收益，非实际成交）",
            }
            results.append(result)
        added = sum(1 for result in results if str(result.get("recommendation_id")) not in existing_by_rec)
        updated = len(results) - added
        if results:
            upsert_jsonl_by_key(results_path, results, lambda row: row.get("recommendation_id") or row.get("backtest_id"))
            merged_by_rec = dict(existing_by_rec)
            merged_by_rec.update({str(row.get("recommendation_id")): row for row in results if row.get("recommendation_id")})
            latest_loss_is_new = any(
                row.get("profitable") is False
                and not (
                    isinstance(existing_by_rec.get(str(row.get("recommendation_id"))), dict)
                    and existing_by_rec[str(row.get("recommendation_id"))].get("profitable") is False
                )
                for row in results
            )
            streak = negative_streak(list(merged_by_rec.values()))
            threshold = max(1, int(backtest_config.get("negative_streak_to_switch", 5)))
            if (
                latest_loss_is_new
                and streak >= threshold
                and bool(backtest_config["switch_model_on_negative"])
            ):
                latest_negative = sorted(
                    [row for row in results if row.get("profitable") is False],
                    key=lambda row: (str(row.get("target_date", "")), str(row.get("evaluated_at", ""))),
                    reverse=True,
                )[0]
                rec_id = str(latest_negative.get("recommendation_id", ""))
                old, new = model_state.rotate(f"最近连续 {streak} 条推荐回测为负，达到阈值 {threshold}", rec_id)
                rotations.append({"from": old, "to": new, "recommendation_id": rec_id})
        else:
            streak = negative_streak(existing_rows)
            threshold = max(1, int(backtest_config.get("negative_streak_to_switch", 5)))
        message = f"回测新增 {added} 条，刷新 {updated} 条"
        if skipped_without_close:
            message += f"，{skipped_without_close} 条缺少目标日收盘后最高价或昨收价暂不判定"
        if rotations:
            message += f"，负收益触发 {len(rotations)} 次模型轮换"
        return AgentResult(
            agent=self.name, ok=True, started_at=started, finished_at=iso_now(), records=len(results),
            output_files=[str(results_path)] if results_path.exists() else [], message=message,
            metadata={
                "model_rotations": rotations,
                "pending_or_total": len(recommendations),
                "negative_streak": streak,
                "negative_streak_to_switch": threshold,
                "close_time": close_at.strftime("%H:%M"),
                "skipped_without_close": skipped_without_close,
                "history_errors": history_errors,
            },
        )


def _is_valid_trading_day_recommendation(recommendation: dict[str, Any]) -> bool:
    recommendation_date = str(
        recommendation.get("recommendation_date") or recommendation.get("created_at", "")
    )[:10]
    try:
        return is_trading_weekday(date.fromisoformat(recommendation_date))
    except ValueError:
        return False
