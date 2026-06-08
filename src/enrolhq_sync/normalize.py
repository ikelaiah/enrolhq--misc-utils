"""Pure value normalizers shared across the package.

Nothing here touches the network, the filesystem, or any config. Every
function is a deterministic transform on a single cell value, which makes
this the easiest module in the package to unit-test exhaustively.

The distinction between the normalizers matters:

  * ``cell`` / ``cell_date`` produce the value we actually *write back* to
    EnrolHQ (date objects become ISO strings, blanks become ``None``).
  * ``label`` / ``name`` / ``email`` / ``phone`` produce *comparison keys*
    used for matching and lookups; they are lower-cased and collapse blanks
    to ``""`` so a missing field on one side equals a missing field on the
    other.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = ["cell", "cell_date", "label", "name", "email", "phone"]


def cell(value: Any) -> Any:
    """Normalize a raw spreadsheet cell into a writable value.

    Strings are stripped (blank -> ``None``); dates/datetimes become ISO
    strings; everything else passes through unchanged.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def cell_date(value: Any) -> str | None:
    """Like :func:`cell` but coerces the result to a string (or ``None``).

    Used for the date-of-birth match key, where an int/serial date would
    otherwise compare unequal to its string form.
    """
    normalized = cell(value)
    return None if normalized is None else str(normalized)


def label(value: Any) -> str | None:
    """Comparison key for choice/enum lookups: stripped + lower-cased.

    Returns ``None`` for blank input so callers can short-circuit empty
    cells before attempting a translation.
    """
    normalized = cell(value)
    if normalized is None:
        return None
    return str(normalized).strip().lower()


def name(value: Any) -> str:
    """Comparison key for name matching: stripped + lower-cased, blank -> ``""``.

    Unlike :func:`label`, a missing name collapses to the empty string so
    that an absent middle name on either side forms the same key.
    """
    normalized = cell(value)
    return "" if normalized is None else str(normalized).strip().lower()


def email(value: Any) -> str:
    """Comparison key for email matching: stripped + lower-cased, blank -> ``""``."""
    normalized = cell(value)
    return "" if normalized is None else str(normalized).strip().lower()


def phone(value: Any) -> str:
    """Digit-only phone key with the AU country prefix folded away.

    Folds so that the common Australian formats compare equal::

        "0411 220662"    -> "411220662"
        "+61 411 220662" -> "411220662"
        "61411220662"    -> "411220662"
        "0411220662"     -> "411220662"

    Strategy: keep digits only, then strip a leading ``61`` (country code)
    and/or a leading ``0`` (trunk prefix). Blank input -> ``""``.
    """
    if value is None:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if digits.startswith("61"):
        digits = digits[2:]
    if digits.startswith("0"):
        digits = digits[1:]
    return digits
