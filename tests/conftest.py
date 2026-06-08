"""Shared fixtures for the unit tests.

The tests are pure — they never touch the network or the SDK. They load the
real ``config/*.toml`` (it is data, so loading it also exercises the loaders)
and build fixture application/row dicts shaped like the EnrolHQ API.
"""

from __future__ import annotations

import pytest

from enrolhq_sync.config import load_config


@pytest.fixture(scope="session")
def config():
    return load_config()


@pytest.fixture
def dictionaries():
    """Reference FK tables (name-lower -> id), as the client would build them."""
    return {
        "countries": {"australia": "C-AU", "new zealand": "C-NZ"},
        "languages": {"spanish": "L-ES", "mandarin": "L-ZH"},
        "nationalities": {"australian": "N-AU", "spanish": "N-ES"},
    }


@pytest.fixture
def application():
    """A minimal but realistic application object with both parent slots."""
    return {
        "id": "app-123",
        "external_id": "S-001",
        "dob": "2010-05-01",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "middle_name": "Byron",
        "aboriginal_statuses": None,
        "home_language": None,
        "is_speak_other_language": None,
        "user_parent": {
            "external_id": "P1-001",
            "first_name": "Anna",
            "last_name": "Lovelace",
            "email": "anna@example.com",
            "mobile_phone": "0411 220662",
            "religion": None,
            "church_attended": None,
            "how_hear": ["Website"],
            "how_hear_other": None,
        },
        "non_user_parent": {
            "external_id": "P2-001",
            "first_name": "Ben",
            "last_name": "Lovelace",
            "email": "ben@example.com",
            "mobile_phone": "0422 330771",
            "religion": None,
        },
    }


@pytest.fixture
def base_row():
    """A sheet row whose key/name/email/mobile columns match ``application``."""
    return {
        "Student External Id 0A": "S-001",
        "Student Dob 5F": "2010-05-01",
        "Student First Name 1B": "Ada",
        "Student Middle Name 3D": "Byron",
        "Student Last Name 4E": "Lovelace",
        "Student Entry Year 11L": 2026,
        "Parent One External Id 22W": "P1-001",
        "Parent One Email 29AD": "anna@example.com",
        "Parent One Mobile 30AE": "0411 220662",
        "Parent One First Name 24Y": "Anna",
        "Parent One Last Name 26AA": "Lovelace",
        "Parent Two External Id 40AO": "P2-001",
        "Parent Two Email 47AV": "ben@example.com",
        "Parent Two Mobile 48AW": "0422 330771",
        "Parent Two First Name 42AQ": "Ben",
        "Parent Two Last Name 44AS": "Lovelace",
    }
