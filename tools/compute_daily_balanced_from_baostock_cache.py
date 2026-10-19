from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "runtime" / "research_backtests" / "daily_balanced_close_baostock"
CACHE_DIR = BASE / "symbol_cache"
REPORT_PATH = BASE / "report_99_from_cache.json"
TRADES_PATH = BASE / "trades_99_from_cache.csv"
WINDOW_SUFFIX = "2026-04-09_2026-09-02.json"


def normalize_code(code: str) -> str:
    return code.split(".")[-1]


def allowed_code(code: str) -> bool:
    pure = normalize_code(code)
    if code.startswith("sh."):
        return pure.startswith(("600", "601", "603", "605"))
    if code.startswith("sz."):
        return pure.startswith(("000", "001", "002"))
    return False


def f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except ValueError:
        return None


def zscore(rows: list[dict[str, Any]], source: str, target: str, transform=lambda value: value) -> None:
    values = [transform(float(row[source])) for row in rows if f(row.get(source)) is not None]
    mean = statistics.fmean(values) if values else 0.0
    deviation = statistics.pstdev(values) if len(values) > 1 else 1.0
    if deviation == 0:
        deviation = 1.0
    for row in rows:
        value = f(row.get(source))
        row[target] = round((transform(value) - mean) / deviation, 6) if value is not None else 0.0


def score(row: dict[str, Any]) -> float:
    return (
        0.28 * float(row.get("momentum_z") or 0)
        + 0.20 * float(row.get("volume_ratio_z") or 0)
        + 0.18 * float(row.get("liquidity_z") or 0)
        + 0.12 * float(row.get("turnover_z") or 0)
        - 0.08 * max(float(row.get("amplitude_z") or 0), 0.0)
    )


def load_names() -> dict[str, str]:
    path = ROOT / "runtime" / "agents" / "market" / "data" / "latest.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get("code", ""): row.get("name", "") for row in csv.DictReader(handle)}


def main() -> int:
    names = load_names()
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cache_files = sorted(CACHE_DIR.glob(f"*_{WINDOW_SUFFIX}"))
    for cache_path in cache_files:
        code = cache_path.name.split("_", 1)[0] + "." + cache_path.name.split("_", 2)[1]
        code = code.replace("sh.", "sh.", 1).replace("sz.", "sz.", 1)
        if not (code.startswith("sh.") or code.startswith("sz.")):
            parts = cache_path.name.split("_")
            code = f"{parts[0]}.{parts[1]}"
        if not allowed_code(code):
            continue
        rows = json.loads(cache_path.read_text(encoding="utf-8"))
        prev_volumes: list[float] = []
        for row in rows:
            pure = normalize_code(row.get("code", code))
            full_code = row.get("code", code)
            if row.get("isST") == "1" or not allowed_code(full_code):
                continue
            close = f(row.get("close"))
            amount = f(row.get("amount"))
            pct_chg = f(row.get("pctChg"))
            turn = f(row.get("turn"))
            high = f(row.get("high"))
            low = f(row.get("low"))
            volume = f(row.get("volume"))
            if None in (close, amount, pct_chg, turn, high, low, volume) or close == 0:
                continue
            amplitude = ((high - low) / close * 100) if close else 0.0
            volume_ratio = volume / statistics.fmean(prev_volumes[-5:]) if prev_volumes[-5:] else 1.0
            parsed = {
                "date": row["date"],
                "code": pure,
                "name": names.get(pure) or "",
                "close": close,
                "amount": amount,
                "change_pct": pct_chg,
                "turnover_pct": turn,
                "amplitude_pct": amplitude,
                "volume_ratio": volume_ratio,
            }
            by_date[row["date"]].append(parsed)
            prev_volumes.append(volume)

    valid_days = sorted(day for day, rows in by_date.items() if len(rows) >= 2000)
    trades = []
    for buy_day, sell_day in zip(valid_days, valid_days[1:]):
        rows = by_date[buy_day]
        zscore(rows, "change_pct", "momentum_z")
        zscore(rows, "volume_ratio", "volume_ratio_z")
        zscore(rows, "turnover_pct", "turnover_z")
        zscore(rows, "amplitude_pct", "amplitude_z")
        zscore(rows, "amount", "liquidity_z", lambda value: math.log1p(max(value, 0.0)))
        candidates = [
            row for row in rows
            if float(row.get("amount") or 0) >= 100_000_000 and abs(float(row.get("change_pct") or 0)) < 9.8
        ]
        if not candidates:
            continue
        selected = max(candidates, key=score)
        sell_row = next((row for row in by_date[sell_day] if row["code"] == selected["code"]), None)
        if not sell_row:
            continue
        return_pct = (float(sell_row["close"]) - float(selected["close"])) / float(selected["close"]) * 100
        trades.append(
            {
                "buy_date": buy_day,
                "sell_date": sell_day,
                "code": selected["code"],
                "name": selected["name"],
                "buy_close": selected["close"],
                "sell_close": sell_row["close"],
                "score": round(score(selected), 6),
                "return_pct": round(return_pct, 6),
            }
        )

    trades = trades[-99:]
    if len(trades) != 99:
        raise RuntimeError(f"缓存只能形成 {len(trades)} 笔交易，不能输出 99 天结论")
    capital = 1.0
    for trade in trades:
        capital *= 1 + float(trade["return_pct"]) / 100
    report = {
        "strategy": "平衡多因子 balanced",
        "buy": "当天收盘买入",
        "sell": "第二个交易日收盘卖出",
        "trade_count": len(trades),
        "first_buy_date": trades[0]["buy_date"],
        "last_buy_date": trades[-1]["buy_date"],
        "compound_return_pct": round((capital - 1) * 100, 6),
        "simple_sum_return_pct": round(sum(float(trade["return_pct"]) for trade in trades), 6),
        "win_rate_pct": round(sum(1 for trade in trades if float(trade["return_pct"]) > 0) / len(trades) * 100, 2),
        "data_source": "BaoStock 历史日线缓存",
        "cache_files": len(cache_files),
        "valid_market_days": len(valid_days),
        "factor_note": "使用真实日线 close/amount/turn/pctChg/high/low/isST；历史日线不含新闻情绪和逐日PE，news_sentiment 与 valuation_z 按中性0处理；volume_ratio 用当日成交量/过去5日均量计算。",
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with TRADES_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
        writer.writeheader()
        writer.writerows(trades)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
