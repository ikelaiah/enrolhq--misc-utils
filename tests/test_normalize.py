"""Tests for the pure value normalizers."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from enrolhq_sync import normalize


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("  hi  ", "hi"),
        (42, 42),
        (datetime(2020, 1, 2, 3, 4), "2020-01-02"),
        (date(2020, 1, 2), "2020-01-02"),
    ],
)
def test_cell(raw, expected):
    assert normalize.cell(raw) == expected


def test_cell_date_coerces_to_string():
    assert normalize.cell_date(20100501) == "20100501"
    assert normalize.cell_date(None) is None
    assert normalize.cell_date(date(2010, 5, 1)) == "2010-05-01"


def test_label_lowercases_or_none():
    assert normalize.label("  Catholic ") == "catholic"
    assert normalize.label("") is None
    assert normalize.label(None) is None


def test_name_blank_collapses_to_empty():
    assert normalize.name(None) == ""
    assert normalize.name("  ") == ""
    assert normalize.name(" Lovelace ") == "lovelace"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0411 220662", "411220662"),
        ("+61 411 220662", "411220662"),
        ("61411220662", "411220662"),
        ("0411220662", "411220662"),
        (None, ""),
        ("", ""),
    ],
)
def test_phone_folds_au_prefixes(raw, expected):
    assert normalize.phone(raw) == expected
