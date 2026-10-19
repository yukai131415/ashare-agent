from __future__ import annotations

import csv
import json
import math
import socket
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "runtime" / "research_backtests" / "daily_balanced_close"
CACHE_DIR = OUT_DIR / "kline_cache"
REPORT_PATH = OUT_DIR / "report.json"
TRADES_PATH = OUT_DIR / "trades.csv"


def read_current_names() -> dict[str, str]:
    path = ROOT / "runtime" / "agents" / "market" / "data" / "latest.csv"
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row.get("code", ""): row.get("name", "") for row in csv.DictReader(handle)}


def load_symbols() -> list[str]:
    payload = json.loads((ROOT / "runtime" / "agents" / "market" / "data" / "tencent_symbols.json").read_text(encoding="utf-8"))
    names = read_current_names()
    symbols: list[str] = []
    for raw in payload.get("list", []):
        symbol = str(raw).lower().strip()
        code = symbol[-6:]
        if not symbol.startswith(("sh", "sz")):
            continue
        if code.startswith(("300", "301", "8", "4", "920")):
            continue
        name = names.get(code, "")
        if "ST" in name.upper() or "退" in name:
            continue
        symbols.append(symbol)
    return sorted(set(symbols))


def secid(symbol: str) -> str:
    code = symbol[-6:]
    market = "1" if symbol.startswith("sh") else "0"
    return f"{market}.{code}"


def fetch_kline(symbol: str, beg: str, end: str, retries: int = 3) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{symbol}_{beg}_{end}.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid(symbol),
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "1",
        "beg": beg,
        "end": end,
    }
    request = urllib.request.Request(
        f"{url}?{urllib.parse.urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Referer": "https://quote.eastmoney.com/",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        except Exception as exc:
            last_error = exc
            time.sleep(0.4 * attempt)
    raise RuntimeError(f"{symbol} fetch failed: {last_error}")


def parse_rows(symbol: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    code = str(data.get("code") or symbol[-6:])
    name = str(data.get("name") or "")
    rows = []
    prev_volumes: list[float] = []
    for line in data.get("klines") or []:
        parts = str(line).split(",")
        if len(parts) < 11:
            continue
        try:
            volume = float(parts[5])
            volume_ratio = volume / statistics.fmean(prev_volumes[-5:]) if prev_volumes[-5:] else 1.0
            rows.append(
                {
                    "date": parts[0],
                    "code": code,
                    "name": name,
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": volume,
                    "amount": float(parts[6]),
                    "amplitude_pct": float(parts[7]),
                    "change_pct": float(parts[8]),
                    "change": float(parts[9]),
                    "turnover_pct": float(parts[10]),
                    "volume_ratio_z_source": volume_ratio,
                    "news_sentiment": 0.0,
                    "valuation_z": 0.0,
                }
            )
            prev_volumes.append(volume)
        except ValueError:
            continue
    return rows


def zscore(rows: list[dict[str, Any]], source: str, target: str, transform=lambda value: value) -> None:
    values = [transform(float(row[source])) for row in rows if row.get(source) not in (None, "")]
    mean = statistics.fmean(values) if values else 0.0
    deviation = statistics.pstdev(values) if len(values) > 1 else 1.0
    if deviation == 0:
        deviation = 1.0
    for row in rows:
        row[target] = round((transform(float(row[source])) - mean) / deviation, 6) if row.get(source) not in (None, "") else 0.0


def score(row: dict[str, Any]) -> float:
    return (
        0.28 * float(row.get("momentum_z") or 0)
        + 0.20 * float(row.get("volume_ratio_z") or 0)
        + 0.18 * float(row.get("liquidity_z") or 0)
        + 0.12 * float(row.get("turnover_z") or 0)
        + 0.05 * float(row.get("valuation_z") or 0)
        - 0.08 * max(float(row.get("amplitude_z") or 0), 0.0)
    )


def main() -> int:
    socket.setdefaulttimeout(15)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    symbols = load_symbols()
    beg, end = "20250101", datetime.now().strftime("%Y%m%d")
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    failures = []
    for index, symbol in enumerate(symbols, 1):
        if index == 1 or index % 25 == 0:
            print(f"fetching {index}/{len(symbols)} {symbol}", flush=True)
        try:
            for row in parse_rows(symbol, fetch_kline(symbol, beg, end, retries=1)):
                by_date[row["date"]].append(row)
        except Exception as exc:
            failures.append({"symbol": symbol, "error": str(exc)})
            print(f"failed {index}/{len(symbols)} {symbol}: {exc}", flush=True)
        if index % 100 == 0:
            print(f"fetched {index}/{len(symbols)}, failures={len(failures)}", flush=True)
        time.sleep(0.04)

    dates = sorted(day for day, rows in by_date.items() if len(rows) >= 2000)
    eval_dates = dates[-101:]
    trades = []
    for day, next_day in zip(eval_dates, eval_dates[1:]):
        rows = by_date[day]
        zscore(rows, "change_pct", "momentum_z")
        zscore(rows, "volume_ratio_z_source", "volume_ratio_z")
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
        next_row = next((row for row in by_date[next_day] if row["code"] == selected["code"]), None)
        if not next_row:
            continue
        return_pct = (float(next_row["close"]) - float(selected["close"])) / float(selected["close"]) * 100
        trades.append(
            {
                "buy_date": day,
                "sell_date": next_day,
                "code": selected["code"],
                "name": selected["name"],
                "buy_close": selected["close"],
                "sell_close": next_row["close"],
                "score": round(score(selected), 6),
                "return_pct": round(return_pct, 6),
            }
        )

    capital = 1.0
    for trade in trades[-100:]:
        capital *= 1 + float(trade["return_pct"]) / 100
    report = {
        "strategy": "balanced",
        "buy": "当日收盘价",
        "sell": "下一交易日收盘价",
        "requested_trades": 100,
        "actual_trades": len(trades[-100:]),
        "first_buy_date": trades[-100:][0]["buy_date"] if trades else None,
        "last_buy_date": trades[-100:][-1]["buy_date"] if trades else None,
        "compound_return_pct": round((capital - 1) * 100, 6),
        "win_rate_pct": round(sum(1 for trade in trades[-100:] if trade["return_pct"] > 0) / len(trades[-100:]) * 100, 2) if trades else None,
        "data_source": "东方财富 push2his 日 K",
        "symbols_requested": len(symbols),
        "symbol_failures": len(failures),
        "valid_market_days": len(dates),
        "factor_note": "历史日 K 无逐日新闻情绪和 PE，本复测将 news_sentiment 与 valuation_z 置为 0；volume_ratio 使用当日成交量/过去5日均量近似。",
        "failures_sample": failures[:20],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if trades:
        with TRADES_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades[-100:])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
