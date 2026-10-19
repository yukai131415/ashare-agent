from __future__ import annotations

import csv
import json
import math
import socket
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import baostock as bs


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runtime" / "research_backtests" / "daily_balanced_close_baostock"
CACHE_DIR = OUT_DIR / "symbol_cache"
REPORT_PATH = OUT_DIR / "report.json"
TRADES_PATH = OUT_DIR / "trades.csv"
RAW_PATH = OUT_DIR / "daily_rows.jsonl"


def query_rows(rs: Any) -> list[dict[str, str]]:
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(dict(zip(rs.fields, rs.get_row_data())))
    if rs.error_code != "0":
        raise RuntimeError(f"{rs.error_code}: {rs.error_msg}")
    return rows


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
        + 0.17 * 0.0
        + 0.05 * 0.0
        - 0.08 * max(float(row.get("amplitude_z") or 0), 0.0)
    )


def load_names() -> dict[str, str]:
    path = ROOT / "runtime" / "agents" / "market" / "data" / "latest.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get("code", ""): row.get("name", "") for row in csv.DictReader(handle)}


def main() -> int:
    socket.setdefaulttimeout(20)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start = (date.today() - timedelta(days=220)).isoformat()
    end = date.today().isoformat()
    names = load_names()
    print(f"baostock login...", flush=True)
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"login failed {login.error_code}: {login.error_msg}")
    try:
        index_rows = query_rows(bs.query_history_k_data_plus("sh.000001", "date,close", start_date=start, end_date=end, frequency="d", adjustflag="2"))
        trading_days = [row["date"] for row in index_rows]
        needed_days = trading_days[-130:]
        if len(needed_days) < 101:
            raise RuntimeError(f"可用交易日不足 101 天，只有 {len(needed_days)} 天")
        universe_rows = query_rows(bs.query_all_stock(day=needed_days[-1]))
        codes = [
            row["code"] for row in universe_rows
            if row.get("tradeStatus") == "1" and row.get("code_name") and allowed_code(row["code"])
        ]
        print(f"trading_days={len(trading_days)}, backtest_window={needed_days[0]}..{needed_days[-1]}, symbols={len(codes)}", flush=True)
        fields = "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST"
        by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
        failures = []
        RAW_PATH.write_text("", encoding="utf-8")
        for index, code in enumerate(codes, 1):
            cache_path = CACHE_DIR / f"{code.replace('.', '_')}_{needed_days[0]}_{needed_days[-1]}.json"
            try:
                if cache_path.exists():
                    rows = json.loads(cache_path.read_text(encoding="utf-8"))
                else:
                    if index == 1 or index % 25 == 0:
                        print(f"fetching {index}/{len(codes)} {code}", flush=True)
                    rows = query_rows(bs.query_history_k_data_plus(code, fields, start_date=needed_days[0], end_date=needed_days[-1], frequency="d", adjustflag="2"))
                    cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
                prev_volumes: list[float] = []
                with RAW_PATH.open("a", encoding="utf-8") as handle:
                    for row in rows:
                        pure = normalize_code(row["code"])
                        if row.get("isST") == "1" or not allowed_code(row["code"]):
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
                            "name": names.get(pure) or row.get("code_name") or "",
                            "close": close,
                            "amount": amount,
                            "change_pct": pct_chg,
                            "turnover_pct": turn,
                            "amplitude_pct": amplitude,
                            "volume_ratio": volume_ratio,
                        }
                        by_date[row["date"]].append(parsed)
                        handle.write(json.dumps(parsed, ensure_ascii=False) + "\n")
                        prev_volumes.append(volume)
            except Exception as exc:
                failures.append({"code": code, "error": str(exc)})
                print(f"failed {index}/{len(codes)} {code}: {exc}", flush=True)
            if index % 200 == 0:
                print(f"fetched {index}/{len(codes)}, failures={len(failures)}", flush=True)

        valid_days = [day for day in needed_days if len(by_date.get(day, [])) >= 2000]
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
        trades = trades[-100:]
        if len(trades) < 100:
            raise RuntimeError(f"有效交易不足 100 笔，只有 {len(trades)} 笔；不输出伪造结论")
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
            "data_source": "BaoStock 历史日线",
            "valid_market_days": len(valid_days),
            "symbols_requested": len(codes),
            "symbol_failures": len(failures),
            "factor_note": "使用真实日线 close/amount/turn/pctChg/high/low/isST；历史日线不含新闻情绪和逐日PE，news_sentiment 与 valuation_z 按中性0处理；volume_ratio 用当日成交量/过去5日均量计算。",
            "failures_sample": failures[:20],
        }
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        with TRADES_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
