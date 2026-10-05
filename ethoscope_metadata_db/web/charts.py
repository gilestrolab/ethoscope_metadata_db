"""Server-side SVG charts: no chart library, every mark is a link.

Colours are CSS custom properties (``--series-N``, defined in ``style.css`` for
light and dark), so the SVG is written against roles, not hex values. The
five series use the validated categorical slots 1-5 in fixed order, stacked
in that same order so adjacent segments are the validated adjacent pairs.
"""

from __future__ import annotations

import datetime as dt
import math
from html import escape
from typing import Callable

from markupsafe import Markup

# (key, census categories folded into it, legend label); order = slot order = stack order.
SERIES = (
    ("claimed", ("single", "multiple"), "Claimed"),
    ("orphan", ("orphan",), "Orphan"),
    ("failed", ("misfire", "empty"), "Misfire or empty"),
    ("copies", ("superseded", "duplicate"), "Earlier run or copy"),
    ("tentative", ("tentative",), "Tentative"),
)
GAP = 2  # surface gap between stacked segments and adjacent columns (px)
CATEGORY_SERIES = {category: key for key, categories, _ in SERIES for category in categories}


def _months(first: str, last: str) -> list[str]:
    """Every YYYY-MM from ``first`` to ``last`` inclusive."""
    year, month = int(first[:4]), int(first[5:7])
    out = []
    while f"{year:04d}-{month:02d}" <= last:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def nice_ticks(top: float, count: int = 4) -> list[int]:
    """Clean y-axis ticks (multiples of 1, 2 or 5 x 10^k) from 0 to at least ``top``."""
    if top <= 0:
        return [0, 1]
    raw = top / count
    power = 10 ** math.floor(math.log10(raw))
    step = next(m * power for m in (1, 2, 5, 10) if m * power >= raw)
    return [int(step * i) for i in range(int(math.ceil(top / step)) + 1)]


def fold(counts: dict[str, int]) -> dict[str, int]:
    """Census category counts folded into the chart's series."""
    return {key: sum(counts.get(c, 0) for c in cats) for key, cats, _ in SERIES}


FIRST_PLAUSIBLE_MONTH = "2010-01"  # earlier starts are clocks that were never set (epoch 0)


def monthly_columns(
    monthly: dict[str, dict[str, int]],
    link: Callable[[str], str],
    height: int = 260,
    min_width: int = 1200,
    first: str = FIRST_PLAUSIBLE_MONTH,
) -> Markup:
    """Stacked columns of databases per month (start month on the lab clock).

    Args:
        monthly: ``{"YYYY-MM": {category: count}}``.
        link: URL of the Runs page for one month.
        height: Plot height in px (the SVG scales to its container's width).
        min_width: Smallest drawing width in px.
        first: Months before this are left out (count them with :func:`implausible`).

    Returns:
        SVG markup, with one ``<title>`` tooltip and link per month.
    """
    months = sorted(m for m in monthly if m and len(m) == 7 and m >= first)
    if not months:
        return Markup('<p class="muted">No dated runs yet.</p>')
    months = _months(months[0], months[-1])
    folded = {m: fold(monthly.get(m, {})) for m in months}
    totals = {m: sum(v.values()) for m, v in folded.items()}
    ticks = nice_ticks(max(totals.values()))
    top = ticks[-1]
    left, bottom, right, pad_top = 44, 22, 8, 8
    slot = max(3.0, (min_width - left - right) / len(months))
    width = left + right + slot * len(months)
    bar = max(1.0, min(24.0, slot - GAP))
    plot_h = height - bottom - pad_top

    def y(value: float) -> float:
        return pad_top + plot_h * (1 - value / top)

    parts = [
        f'<svg class="chart" viewBox="0 0 {width:.0f} {height}" role="img" '
        f'aria-label="Databases per month by claim category" preserveAspectRatio="xMinYMin meet">'
    ]
    for tick in ticks:
        parts.append(
            f'<line class="grid" x1="{left}" x2="{width - right:.0f}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
            f'<text class="tick" x="{left - 6}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick:,}</text>'
        )
    for i, month in enumerate(months):
        x = left + i * slot + (slot - bar) / 2
        tip = [f"{_month_label(month)}: {totals[month]:,} databases"]
        tip += [
            f"{label}: {folded[month][key]:,}" for key, _, label in SERIES if folded[month][key]
        ]
        parts.append(
            f'<a href="{escape(link(month))}" class="col"><title>{escape(chr(10).join(tip))}</title>'
        )
        parts.append(
            f'<rect class="hit" x="{left + i * slot:.1f}" y="{pad_top}" width="{slot:.1f}" height="{plot_h}"/>'
        )
        base = 0
        stacked = [(key, folded[month][key]) for key, _, _ in SERIES if folded[month][key]]
        for j, (key, value) in enumerate(stacked):
            y_top, y_bottom = y(base + value), y(base)
            h = y_bottom - y_top - (GAP if j else 0)
            if h > 0.3:
                r = min(4.0, bar / 2, h) if j == len(stacked) - 1 else 0
                parts.append(_segment(x, y_top, bar, h, r, key))
            base += value
        parts.append("</a>")
        # Year labels at each January; the first partial year only if it has room.
        if month.endswith("-01") or (i == 0 and int(month[5:7]) <= 9):
            parts.append(
                f'<text class="tick" x="{left + i * slot:.1f}" y="{height - 6}">{month[:4]}</text>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))


def _segment(x: float, y: float, w: float, h: float, r: float, key: str) -> str:
    """A stacked segment; the top one gets a rounded data-end, square at the base."""
    if r <= 0:
        return f'<rect class="s-{key}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}"/>'
    return (
        f'<path class="s-{key}" d="M{x:.1f},{y + h:.1f} V{y + r:.1f} '
        f"Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} H{x + w - r:.1f} "
        f'Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"/>'
    )


def _month_label(month: str) -> str:
    return dt.date(int(month[:4]), int(month[5:7]), 1).strftime("%b %Y")


def legend() -> Markup:
    """The legend for :func:`monthly_columns` (always shown: five series)."""
    items = "".join(
        f'<li><span class="swatch s-{key}"></span>{escape(label)}</li>' for key, _, label in SERIES
    )
    return Markup(f'<ul class="legend">{items}</ul>')


def implausible(monthly: dict[str, dict[str, int]], first: str = FIRST_PLAUSIBLE_MONTH) -> int:
    """How many runs :func:`monthly_columns` leaves out for starting before ``first``."""
    return sum(sum(v.values()) for m, v in monthly.items() if m and m < first)
