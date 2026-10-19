from __future__ import annotations

from datetime import date, datetime, timedelta


# 上交所2026年休市公告（2025-12-22）：
# https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
HOLIDAY_RANGES = {
    2026: [('01-01', '01-03'), ('02-15', '02-23'), ('04-04', '04-06'),
           ('05-01', '05-05'), ('06-19', '06-21'), ('09-25', '09-27'),
           ('10-01', '10-07')],
}


def is_trading_weekday(day: date | None = None) -> bool:
    """兼容旧函数名：排除周末和交易所休市日，未知年份拒绝猜测。"""

    selected = day or datetime.now().astimezone().date()
    if selected.year not in HOLIDAY_RANGES:
        raise ValueError(f'{selected.year}年交易所日历尚未配置，请更新后运行')
    md = selected.strftime('%m-%d')
    return selected.weekday() < 5 and not any(start <= md <= end for start, end in HOLIDAY_RANGES[selected.year])


def next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while not is_trading_weekday(candidate):
        candidate += timedelta(days=1)
    return candidate
