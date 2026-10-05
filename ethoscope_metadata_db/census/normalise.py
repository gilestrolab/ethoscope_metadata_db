"""Value normalisation for the ethoscope census.

Every function here is pure: it takes a raw cell value (string, number,
datetime, ``None``...) and returns a normalised value, or ``None`` when the
value cannot be interpreted. Callers always keep the raw value next to the
normalised one.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Iterable, NamedTuple

import pandas as pd

# Excel serial day numbers for 2000-01-01 and 2100-01-01. Numbers outside this
# range are not treated as dates (they are more likely ids or counts).
_EXCEL_EPOCH = dt.date(1899, 12, 30)
_EXCEL_MIN, _EXCEL_MAX = 36526, 73051
# POSIX seconds for 2000-01-01 and 2100-01-01 (UTC): R writes POSIXct datetimes this way.
_EPOCH_MIN, _EPOCH_MAX = 946684800, 4102444800

_ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T_].*)?$")
# A results-tree stamp (``2024-01-05_10-00-00``) written as a date: one run's start.
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_(\d{2}-\d{2}-\d{2})$")
# A full date-time (rethomics ``datetime``): 2024-01-05 10:00:00, ...T10:00:00 or the stamp.
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[ T_](\d{2}[:\-]\d{2}[:\-]\d{2})")
_COMPACT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DMY_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})(?:[ T].*)?$")
_TIME_RE = re.compile(r"^(\d{1,2})[:.\-h](\d{2})(?:[:.\-m](\d{2}))?(?:\.\d+)?s?$")
_MACHINE_RE = re.compile(r"^(?:ETHOSCOPE|ETHO|E)?[\s_\-#]*0*(\d{1,4})$")

_SEX_MAP = {
    "m": "M",
    "male": "M",
    "males": "M",
    "♂": "M",
    "f": "F",
    "female": "F",
    "females": "F",
    "♀": "F",
    "mixed": "mixed",
    "both": "mixed",
    "m+f": "mixed",
    "mf": "mixed",
}


class ParsedDate(NamedTuple):
    """Result of :func:`parse_date`.

    Attributes:
        date: The date, or ``None`` when missing or invalid.
        status: ``ok``, ``invalid``, ``missing``, or ``ambiguous`` for a
            day/month string that reads as two different dates.
        alt: For ``ambiguous``: the month-first reading (``date`` is day-first).
    """

    date: dt.date | None
    status: str
    alt: dt.date | None = None


def is_blank(value: Any) -> bool:
    """Return True for ``None``, NaN/NaT/NA and whitespace-only strings."""
    if isinstance(value, str):
        return not value.strip()
    return bool(pd.api.types.is_scalar(value) and pd.isna(value))


def _as_number(value: Any) -> float | None:
    """Return ``value`` as a float if it is numeric or a numeric string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def day_month_parts(value: Any) -> tuple[int, int, int] | None:
    """Return ``(first, second, year)`` of a day/month string, in written order.

    ``05/01/2024`` gives ``(5, 1, 2024)`` whichever of day or month comes
    first; anything else (ISO strings, numbers, datetimes) gives ``None``.
    """
    match = _DMY_RE.match(value.strip()) if isinstance(value, str) else None
    return tuple(int(g) for g in match.groups()) if match else None


def date_order(values: Iterable[Any]) -> str | None:
    """Tell from a column's unambiguous day/month strings which order it uses.

    A part above 12 can only be a day: ``25/03/2024`` shows day-first,
    ``03/25/2024`` month-first.

    Args:
        values: The raw cells of one ``date`` column.

    Returns:
        ``"dmy"`` or ``"mdy"`` when the evidence points one way, ``"mixed"``
        when it points both ways, ``"unknown"`` when every part is <= 12, and
        ``None`` when the column has no day/month strings at all.
    """
    seen = day_first = month_first = False
    for value in values:
        parts = day_month_parts(value)
        if parts:
            seen = True
            # Only a value readable one way is evidence (13/13/2024 is no date at all).
            day_first |= parts[0] > 12 >= parts[1]
            month_first |= parts[1] > 12 >= parts[0]
    if not seen:
        return None
    return {(True, False): "dmy", (False, True): "mdy", (True, True): "mixed"}.get(
        (day_first, month_first), "unknown"
    )


def parse_date(value: Any, order: str = "dmy") -> ParsedDate:
    """Parse a metadata date cell.

    Day/month strings (``05/01/2024``) are read in ``order``: ``"dmy"`` (the
    lab convention), ``"mdy"``, or ``"either"`` when the order is not known:
    then a string valid one way only is read that way, and one valid both ways
    (``05/01/2024``) is ``ambiguous``, with the day-first reading in ``date``
    and the month-first one in ``alt``. Also accepted: ``datetime``/``date``
    objects (including pandas Timestamps), Excel serial numbers, ISO strings
    (``2024-01-05``, ``2024/01/05``, ``2024-01-05 00:00:00``, the
    results-tree stamp ``2024-01-05_10-00-00``) and compact ``20240105``.
    Separators may be ``/``, ``-`` or ``.``; two-digit years are 20yy.

    Args:
        value: The raw cell value.
        order: How to read day/month strings: ``"dmy"``, ``"mdy"`` or ``"either"``.

    Returns:
        A :class:`ParsedDate`.
    """
    if is_blank(value):
        return ParsedDate(None, "missing")
    if isinstance(value, dt.datetime):
        return ParsedDate(value.date(), "ok")
    if isinstance(value, dt.date):
        return ParsedDate(value, "ok")

    number = _as_number(value)
    text = str(value).strip()
    for pattern in (_COMPACT_RE, _ISO_RE):
        match = pattern.match(text)
        if match:
            parsed = _safe_date(*(int(g) for g in match.groups()))
            return ParsedDate(parsed, "ok" if parsed else "invalid")
    parts = day_month_parts(text)
    if parts:
        first, second, year = parts
        day_first = _safe_date(year, second, first)
        month_first = _safe_date(year, first, second)
        if order == "either" and day_first and month_first and day_first != month_first:
            return ParsedDate(day_first, "ambiguous", month_first)
        if order == "mdy" or (order == "either" and not day_first):
            parsed = month_first
        else:
            parsed = day_first
        return ParsedDate(parsed, "ok" if parsed else "invalid")
    if number is not None and _EXCEL_MIN <= number < _EXCEL_MAX:
        return ParsedDate(_EXCEL_EPOCH + dt.timedelta(days=int(number)), "ok")
    return ParsedDate(None, "invalid")


def date_from_parts(day: Any, month: Any, year: Any) -> ParsedDate:
    """Build a date from separate ``day``, ``month`` and ``year`` cells.

    This is the unambiguous way to record a date: unlike a single date cell,
    it survives spreadsheets that rewrite dates in the computer's locale.

    Args:
        day: Day of the month (1-31).
        month: Month number (1-12).
        year: Four-digit year (two digits are read as 20yy).

    Returns:
        A :class:`ParsedDate`: ``missing`` when all three are blank,
        ``invalid`` when any is not a whole number or they form no date.
    """
    cells = (day, month, year)
    if all(is_blank(v) for v in cells):
        return ParsedDate(None, "missing")
    numbers = [_as_number(v) for v in cells]
    if any(n is None or not float(n).is_integer() for n in numbers):
        return ParsedDate(None, "invalid")
    d, m, y = (int(n) for n in numbers)
    parsed = _safe_date(y, m, d)
    return ParsedDate(parsed, "ok" if parsed else "invalid")


def parse_time(value: Any) -> str | None:
    """Parse a start-time cell into ``HH:MM:SS``.

    Accepts ``time``/``datetime`` objects, Excel day fractions (0 <= x < 1)
    and strings such as ``09:30``, ``9.30``, ``09-30-00`` or ``9h30``.

    Args:
        value: The raw cell value.

    Returns:
        The time as ``HH:MM:SS``, or ``None`` if it cannot be read.
    """
    if is_blank(value):
        return None
    if isinstance(value, (dt.datetime, dt.time)):
        return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    number = _as_number(value)
    if number is not None and 0 <= number < 1 and not isinstance(value, str):
        seconds = round(number * 86400)
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    match = _TIME_RE.match(str(value).strip().lower())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def stamp_time(value: Any) -> str | None:
    """Return the start time of a date cell written as a results-tree stamp.

    ``2024-01-05_10-00-00`` names one run exactly, so its time can pick that
    run when the sheet has no ``time`` column.

    Args:
        value: The raw ``date`` cell.

    Returns:
        ``HH:MM:SS``, or ``None`` when the cell is not a stamp.
    """
    match = _STAMP_RE.match(value.strip()) if isinstance(value, str) else None
    return parse_time(match.group(1)) if match else None


def datetime_time(value: Any) -> str | None:
    """Return the time of a full date-time cell, as rethomics' ``datetime`` holds.

    Unlike :func:`stamp_time` it also reads datetime objects: a ``datetime``
    column always carries the run's start time, whereas a ``date`` cell
    stored as a datetime only means a day.

    Args:
        value: The raw ``datetime`` cell.

    Returns:
        ``HH:MM:SS``, or ``None`` when the cell has no time.
    """
    if isinstance(value, dt.datetime):
        return f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
    match = _DATETIME_RE.match(value.strip()) if isinstance(value, str) else None
    return parse_time(match.group(1)) if match else None


def parse_datetime(value: Any, order: str = "dmy") -> tuple[ParsedDate, str | None]:
    """Read a rethomics ``datetime`` cell: the run's start date and time.

    Accepts what :func:`parse_date` accepts, plus POSIX seconds as R writes a
    ``POSIXct`` column, read as UTC (scopr parses result-tree stamps as UTC).

    Args:
        value: The raw ``datetime`` cell.
        order: How to read day/month strings (see :func:`parse_date`).

    Returns:
        ``(parsed date, time as HH:MM:SS or None)``.
    """
    number = _as_number(value)
    if number is not None and _EPOCH_MIN <= number < _EPOCH_MAX:
        moment = dt.datetime.fromtimestamp(number, tz=dt.timezone.utc)
        return ParsedDate(moment.date(), "ok"), f"{moment:%H:%M:%S}"
    return parse_date(value, order), datetime_time(value)


def normalise_machine_name(value: Any) -> str | None:
    """Return the canonical ``ETHOSCOPE_NNN`` form of a machine name.

    ``ETHOSCOPE_007``, ``ETHOSCOPE_7``, ``ethoscope 7``, ``007``, ``7`` and
    ``7.0`` all become ``ETHOSCOPE_007``. Names that are not numbered
    ethoscopes (for example ``ETHOSCOPE_DEV``) are returned upper-cased.

    Args:
        value: The raw cell value.

    Returns:
        The canonical name, or ``None`` for blank values.
    """
    if is_blank(value):
        return None
    number = _as_number(value)
    if number is not None and not isinstance(value, str):
        if not float(number).is_integer() or number < 0:
            return None
        return f"ETHOSCOPE_{int(number):03d}"
    text = str(value).strip().upper()
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    match = _MACHINE_RE.match(text)
    if match:
        return f"ETHOSCOPE_{int(match.group(1)):03d}"
    return re.sub(r"[\s\-]+", "_", text)


def normalise_region_id(value: Any) -> int | None:
    """Return a region (ROI) id as a positive integer.

    Accepts ``3``, ``3.0``, ``"3"``, ``"ROI_3"``, ``"roi 3"`` and ``"tube3"``.

    Args:
        value: The raw cell value.

    Returns:
        The region id, or ``None`` if blank or not a positive integer.
    """
    if is_blank(value):
        return None
    number = _as_number(value)
    if number is None:
        match = re.fullmatch(r"(?:roi|tube|region)?[\s_\-#]*(\d+)", str(value).strip().lower())
        number = float(match.group(1)) if match else None
    if number is None or not float(number).is_integer() or number < 1:
        return None
    return int(number)


def normalise_sex(value: Any) -> str | None:
    """Return ``M``, ``F`` or ``mixed``; ``None`` when unknown or blank."""
    if is_blank(value):
        return None
    return _SEX_MAP.get(str(value).strip().lower())


def clean_text(value: Any) -> str | None:
    """Return a stripped string for free-text fields, ``None`` when blank."""
    if is_blank(value):
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def kernel_stack(kernel: str | None) -> str | None:
    """Classify the camera software stack from a kernel release string.

    Raspberry Pi OS kernels for the picamera2 era carry ``+rpt`` or ``-v8``;
    anything else is the legacy picamera stack.

    Args:
        kernel: Kernel release, e.g. ``6.1.0-rpi7-rpi-v8`` or ``4.19.66-1-ARCH``.

    Returns:
        ``"picamera2"``, ``"legacy"`` or ``None`` when the kernel is unknown.
    """
    if not kernel:
        return None
    return "picamera2" if ("+rpt" in kernel or "-v8" in kernel) else "legacy"
