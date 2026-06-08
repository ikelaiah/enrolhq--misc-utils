"""Read the input spreadsheet into a list of header->value dicts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Read every non-blank data row, keyed by the (stripped) header row.

    Empty rows (all cells blank) are dropped. Columns with a blank header are
    discarded, since they cannot be referenced by the column map.
    """
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    worksheet = workbook.active
    rows_iter = worksheet.iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]

    rows: list[dict[str, Any]] = []
    for raw in rows_iter:
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in raw):
            continue
        rows.append({h: v for h, v in zip(headers, raw) if h})
    return rows


def entry_years(rows: list[dict[str, Any]], year_column: str) -> set[int]:
    """Collect the distinct, integer-coercible entry years present in the sheet."""
    years: set[int] = set()
    for row in rows:
        value = row.get(year_column)
        if value is None:
            continue
        try:
            years.add(int(value))
        except (TypeError, ValueError):
            continue
    return years
