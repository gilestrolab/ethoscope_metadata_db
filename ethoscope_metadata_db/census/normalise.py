"""Value normalisation for the ethoscope census.

Every function here is pure: it takes a raw cell value (string, number,
datetime, ``None``...) and returns a normalised value, or ``None`` when the
value cannot be interpreted. Callers always keep the raw value next to the
normalised one.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, NamedTuple

import pandas as pd

# Excel serial day numbers for 2000-01-01 and 2100-01-01. Numbers outside this
# range are not treated as dates (they are more likely ids or counts).
_EXCEL_EPOCH = dt.date(1899, 12, 30)
_EXCEL_MIN, _EXCEL_MAX = 36526, 73051

_ISO_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$")
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
        status: One of ``ok``, ``invalid`` or ``missing``.
    """

    date: dt.date | None
    status: str


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


def parse_date(value: Any) -> ParsedDate:
    """Parse a metadata date cell.

    The lab convention is dd/mm/yyyy: day/month strings are always read day
    first (``05/01/2024`` is 5 January; ``01/25/2024`` is invalid). Also
    accepted: ``datetime``/``date`` objects (including pandas Timestamps),
    Excel serial numbers, ISO strings (``2024-01-05``, ``2024/01/05``,
    ``2024-01-05 00:00:00``) and compact ``20240105``. Separators may be
    ``/``, ``-`` or ``.``; two-digit years are 20yy.

    Args:
        value: The raw cell value.

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
    for pattern, order in ((_COMPACT_RE, (0, 1, 2)), (_ISO_RE, (0, 1, 2)), (_DMY_RE, (2, 1, 0))):
        match = pattern.match(text)
        if match:
            parts = [int(g) for g in match.groups()]
            parsed = _safe_date(*(parts[i] for i in order))
            return ParsedDate(parsed, "ok" if parsed else "invalid")
    if number is not None and _EXCEL_MIN <= number < _EXCEL_MAX:
        return ParsedDate(_EXCEL_EPOCH + dt.timedelta(days=int(number)), "ok")
    return ParsedDate(None, "invalid")


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
