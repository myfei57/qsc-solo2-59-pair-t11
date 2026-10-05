"""演练周期与两种排期口径。

合规台账只认日期，不依赖第三方日期库。这里同时实现两种排期口径：

* **自然周期**（``calendar``）：按年度／上下半年／季度／月度的固定边界切窗，
  跨年位置固定，是合规判定的唯一口径；
* **滚动间隔**（``rolling``）：以上一次实际合格演练日为锚点，每满 N 个月排下
  一次。锚点随执行日漂移，跨年会落在与自然窗口不同的月份集合上，只用于内部
  加严预警。

支持的档级为 12 的约数：1（月）、2、3（季）、4、6（半年）、12（年）个月，
窗口都能对齐到自然月边界。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

SUPPORTED_CADENCES = (1, 2, 3, 4, 6, 12)

_WINDOW_KINDS = {
    1: "month",
    2: "block",
    3: "quarter",
    4: "block",
    6: "half_year",
    12: "year",
}


def validate_cadence(months: int) -> int:
    """校验周期档级，返回规整后的整数月数。"""

    value = int(months)
    if value not in SUPPORTED_CADENCES:
        raise ValueError(f"不支持的周期档级: {months} 个月，仅支持 {SUPPORTED_CADENCES}")
    return value


def parse_iso_date(text: str) -> date:
    """解析 ``YYYY-MM-DD``，拒绝非 ISO 格式。"""

    return date.fromisoformat(str(text).strip())


def parse_year_month(text: str) -> tuple[int, int]:
    """解析 ``YYYY-MM`` 形式的起始周期。"""

    parts = str(text).strip().split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"起始周期必须是 YYYY-MM: {text}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"月份越界: {text}")
    return year, month


def add_months(day: date, months: int) -> date:
    """月份平移；目标月没有对应日时落到月末（如 3/31 + 1 月 = 4/30）。"""

    index = day.year * 12 + (day.month - 1) + int(months)
    year, month = index // 12, index % 12 + 1
    longest = date(year, month, 1) + timedelta(days=32)
    last_day = (longest - timedelta(days=longest.day)).day
    return date(year, month, min(day.day, last_day))


def months_between(start: date, end: date) -> int:
    """两个日期之间相隔的整月数（按年月差，忽略日）。"""

    return (end.year - start.year) * 12 + (end.month - start.month)


@dataclass(frozen=True)
class NaturalWindow:
    """一个自然周期窗口。"""

    key: str
    label: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        """某日是否落在窗口内。"""

        return self.start <= day <= self.end

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
        }


def _window_labels(cadence: int, year: int, segment: int) -> tuple[str, str]:
    kind = _WINDOW_KINDS[cadence]
    if kind == "year":
        return f"{year}", f"{year}年度"
    if kind == "half_year":
        return f"{year}-H{segment}", f"{year}年{'上' if segment == 1 else '下'}半年"
    if kind == "quarter":
        return f"{year}-Q{segment}", f"{year}年第{segment}季度"
    if kind == "month":
        return f"{year}-{segment:02d}", f"{year}年{segment}月"
    return f"{year}-B{segment}", f"{year}年第{segment}时段"


def window_for_date(cadence_months: int, day: date) -> NaturalWindow:
    """返回某日所属的自然窗口。"""

    cadence = validate_cadence(cadence_months)
    segment = (day.month - 1) // cadence + 1
    first_month = (segment - 1) * cadence + 1
    start = date(day.year, first_month, 1)
    end = add_months(start, cadence) - timedelta(days=1)
    key, label = _window_labels(cadence, day.year, segment)
    return NaturalWindow(key=key, label=label, start=start, end=end)


def iter_natural_windows(cadence_months: int, start_period: str, as_of: date) -> list[NaturalWindow]:
    """枚举从起始周期到基准日为止的全部自然窗口（含进行中的当前窗口）。"""

    cadence = validate_cadence(cadence_months)
    year, month = parse_year_month(start_period)
    cursor = date(year, month, 1)
    windows: list[NaturalWindow] = []
    for _ in range(1200):
        window = window_for_date(cadence, cursor)
        if window.end >= date(year, month, 1) and window.start <= as_of:
            windows.append(window)
        if window.start > as_of:
            break
        cursor = add_months(window.start, cadence)
    return windows


def rolling_due_dates(anchor: date, cadence_months: int, count: int) -> list[date]:
    """从锚点日出发，按滚动间隔排出接下来 ``count`` 个到期日。"""

    cadence = validate_cadence(cadence_months)
    if count < 1:
        raise ValueError("预览数量必须为正")
    return [add_months(anchor, cadence * step) for step in range(1, count + 1)]
