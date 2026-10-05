"""演练排期日历：自然周期口径与滚动口径。

合规定的是"每个自然周期内至少演练一次"，所以系统正式计划只按自然周期排：

* ``monthly``    每年 12 个自然月；
* ``quarterly``  Q1..Q4（1-3、4-6、7-9、10-12 月）；
* ``half_year``  上半年 / 下半年（1-6、7-12 月）；
* ``yearly``     一个自然年。

另一种口径是"距上次实际执行不超过一个周期长度"（见 :func:`rolling_due_after`）。
固定步长下它最终也能覆盖每个月份，但演练周期与自然边界不重合：从 11 月锚点
排起，次年 2 月那次演练负责覆盖上年 12 月，而**本年 12 月要等后年 2 月那次才
产生闭合证据**——年末按自然年检查时，12 月在台账上无法在本年闭合。
:func:`compare_calendars` 把两种口径在同一时间窗内排出来，列出每个自然年里
"年末检查时滚动口径尚不能闭合的月份"与跨年拼接关系，供选型与检查时说清口径，
不作为正式台账。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

FREQUENCIES = ("monthly", "quarterly", "half_year", "yearly")

# 每种频率一个自然周期包含的月数。
_FREQUENCY_MONTHS = {"monthly": 1, "quarterly": 3, "half_year": 6, "yearly": 12}


def today(now_ts: float) -> date:
    """把 Unix 时间戳按 UTC 折成日期（台账一律用 UTC，避免现场时区漂移）。"""

    return datetime.utcfromtimestamp(float(now_ts)).date()


def parse_date(value: str) -> date:
    """解析 ``YYYY-MM-DD`` 日期串，非法时抛 ValueError。"""

    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def to_iso(day: date) -> str:
    """日期转 ISO 串。"""

    return day.isoformat()


def _month_end(day: date) -> date:
    if day.month == 12:
        return date(day.year, 12, 31)
    return date(day.year, day.month + 1, 1) - timedelta(days=1)


def add_months(day: date, months: int) -> date:
    """月份加减：目标月没有对应日（如 3 月 31 日加 1 个月）时取目标月最后一天。"""

    total = day.month - 1 + int(months)
    year = day.year + total // 12
    month = total % 12 + 1
    return _month_end(date(year, month, 1)).replace(day=min(day.day, _month_end(date(year, month, 1)).day))


def period_label(frequency: str, year: int, index: int) -> str:
    """自然周期标识：``2025``、``2025-H1``、``2025-Q4``、``2025-12``。"""

    if frequency == "yearly":
        return f"{year:04d}"
    if frequency == "half_year":
        return f"{year:04d}-H{index}"
    if frequency == "quarterly":
        return f"{year:04d}-Q{index}"
    return f"{year:04d}-{index:02d}"


def parse_period_label(frequency: str, label: str) -> tuple[int, int]:
    """把周期标识解析成 ``(年份, 年内序号, 从 1 起)``。"""

    try:
        if frequency == "yearly":
            return int(label), 1
        year_text, suffix = label.split("-", 1)
        year = int(year_text)
        if frequency == "monthly":
            index = int(suffix)
        else:
            index = int(suffix[1:])
    except (ValueError, IndexError) as error:
        raise ValueError(f"非法周期标识: {label}") from error
    periods = 12 // _FREQUENCY_MONTHS[frequency]
    if not 1 <= index <= periods:
        raise ValueError(f"周期序号越界: {label}")
    return year, index


def period_window(frequency: str, year: int, index: int) -> tuple[date, date]:
    """返回某自然周期的起止日期（闭区间）。"""

    if frequency not in _FREQUENCY_MONTHS:
        raise ValueError(f"未知频率: {frequency}")
    months = _FREQUENCY_MONTHS[frequency]
    periods = 12 // months
    if not 1 <= index <= periods:
        raise ValueError(f"周期序号越界: {index}")
    start_month = (index - 1) * months + 1
    start = date(year, start_month, 1)
    return start, _month_end(add_months(start, months - 1))


def iter_period_keys(frequency: str, start: date, end: date) -> list[str]:
    """列出与 ``[start, end]`` 相交的全部自然周期标识，按时间升序。"""

    if end < start:
        raise ValueError("结束日期不能早于开始日期")
    keys: list[str] = []
    year = start.year
    periods = 12 // _FREQUENCY_MONTHS[frequency]
    for index in range(1, periods + 1):
        window_start, window_end = period_window(frequency, year, index)
        if window_end >= start:
            keys.append(period_label(frequency, year, index))
        if window_end >= end:
            return keys
    year += 1
    while True:
        for index in range(1, periods + 1):
            window_start, window_end = period_window(frequency, year, index)
            if window_start > end:
                return keys
            keys.append(period_label(frequency, year, index))
            if window_end >= end:
                return keys
        year += 1


def natural_schedule(frequency: str, start: date, end: date, day_of_period: int | None = None) -> list[dict]:
    """按自然周期排出正式计划，默认到期日取周期最后一天。

    ``day_of_period`` 给定时取周期内第 N 天（超过周期天数则夹到最后一天），
    用于"每季度首月 15 日演练"这类内部约定；覆盖判定仍以自然周期为准。
    """

    schedule: list[dict] = []
    for key in iter_period_keys(frequency, start, end):
        year, index = parse_period_label(frequency, key)
        window_start, window_end = period_window(frequency, year, index)
        due = window_end
        if day_of_period is not None:
            offset = min(int(day_of_period) - 1, (window_end - window_start).days)
            due = window_start + timedelta(days=max(0, offset))
        schedule.append(
            {
                "period": key,
                "period_start": to_iso(window_start),
                "period_end": to_iso(window_end),
                "due": to_iso(due),
            }
        )
    return schedule


def rolling_due_after(anchor: date, frequency: str) -> date:
    """滚动口径：从锚点（上次实际执行日）往后推一个周期长度。"""

    return add_months(anchor, _FREQUENCY_MONTHS[frequency])


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = month - 1 + delta
    return year + total // 12, total % 12 + 1


def _covered_months(previous_due: date, due: date) -> list[str]:
    """两次计划之间滚动覆盖到的月份标签：上次到期次月到本次到期当月。"""

    year, month = _shift_month(previous_due.year, previous_due.month, 1)
    labels: list[str] = []
    while (year, month) <= (due.year, due.month):
        labels.append(f"{year:04d}-{month:02d}")
        year, month = _shift_month(year, month, 1)
    return labels


def compare_calendars(frequency: str, anchor: date, years: list[int]) -> dict:
    """对比两种口径在给定自然年内的覆盖与跨年闭合差异（不写台账）。

    返回每个自然年的对照行：

    * ``natural_months``        自然口径在该年 1-12 月均有自己的计划项；
    * ``rolling_months``        滚动口径最终覆盖到的该年月份；
    * ``rolling_straddles_from_prior`` 本年到期的演练跨回去覆盖了去年哪个月；
    * ``rolling_unproven_at_year_end`` 到 12 月 31 日为止，滚动口径尚无本年
      演练为之闭合、要等次年演练的月份（自然口径不存在此问题）。
    """

    months = _FREQUENCY_MONTHS[frequency]
    natural: list[dict] = []
    for year in years:
        natural.extend(
            natural_schedule(frequency, date(year, 1, 1), date(year, 12, 31))
        )

    # 从锚点按同一节奏回推到首年 1 月之前再正序排列，保证被对比的每个自然年
    # 都有完整滚动链，差异只来自跨年结构而不是链条起点。
    first_year, last_year = min(years), max(years)
    first_due = anchor
    while first_due > date(first_year, 1, 1):
        first_due = add_months(first_due, -months)
    rolling: list[dict] = []
    previous, due = add_months(first_due, -months), first_due
    while due.year <= last_year + 1:
        rolling.append(
            {
                "due": to_iso(due),
                "due_year": due.year,
                "covered_months": _covered_months(previous, due),
            }
        )
        previous, due = due, add_months(due, months)

    natural_by_year: dict[int, list[str]] = {year: [f"{year:04d}-{m:02d}" for m in range(1, 13)] for year in years}

    rolling_by_year: dict[int, list[str]] = {}
    straddles_from_prior: dict[int, list[str]] = {}
    for item in rolling:
        due_day = parse_date(item["due"])
        for label in item["covered_months"]:
            covered_year = int(label[:4])
            if covered_year in years:
                rolling_by_year.setdefault(covered_year, []).append(label)
                if due_day.year in years and covered_year == due_day.year - 1:
                    straddles_from_prior.setdefault(due_day.year, []).append(label)

    per_year: list[dict] = []
    for year in years:
        rolling_months = sorted(set(rolling_by_year.get(year, [])))
        # 年末检查口径：只有"本年已有演练闭合"的月份才算数；跨到次年才闭合的 12 月不算。
        proven_within_year = [
            label
            for label in rolling_months
            if any(
                int(item["due"][:4]) == year and label in item["covered_months"]
                for item in rolling
            )
        ]
        unproven = sorted(set(natural_by_year[year]) - set(proven_within_year))
        per_year.append(
            {
                "year": year,
                "natural_periods": [item["period"] for item in natural if parse_date(item["period_end"]).year == year],
                "natural_months": natural_by_year[year],
                "rolling_months": rolling_months,
                "rolling_straddles_from_prior": sorted(set(straddles_from_prior.get(year, []))),
                "rolling_unproven_at_year_end": unproven,
                "natural_only_months": sorted(set(natural_by_year[year]) - set(rolling_months)),
            }
        )

    return {
        "frequency": frequency,
        "anchor": to_iso(anchor),
        "selected_calendar": "natural",
        "years": per_year,
        "natural": natural,
        "rolling": rolling,
    }
