"""Tests for student indexing and parent-slot resolution."""

from __future__ import annotations

import pytest

from enrolhq_sync.matching import (
    ApplicationIndex,
    MatchPath,
    StudentLookupError,
    resolve_slots,
)


def _summary(**over):
    base = {
        "id": "app-1",
        "external_id": "S-001",
        "dob": "2010-05-01",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "middle_name": "Byron",
    }
    base.update(over)
    return base


# -- student index ----------------------------------------------------------

def test_match_by_external_id(config, base_row):
    index = ApplicationIndex.build([_summary()])
    match = index.find(base_row, config.match.student)
    assert match.path is MatchPath.EXTERNAL_ID
    assert match.summary["id"] == "app-1"


def test_name_fallback_when_external_id_rewritten(config, base_row):
    # App's external_id has been rewritten; sheet still carries the old one.
    index = ApplicationIndex.build([_summary(external_id="S-REWRITTEN")])
    match = index.find(base_row, config.match.student)
    assert match.path is MatchPath.NAME_FALLBACK


def test_ambiguous_name_fallback_raises(config, base_row):
    twins = [_summary(external_id="A"), _summary(external_id="B")]
    index = ApplicationIndex.build(twins)
    # Force external-id miss so the fallback runs.
    base_row["Student External Id 0A"] = "S-NONE"
    with pytest.raises(StudentLookupError) as exc:
        index.find(base_row, config.match.student)
    assert exc.value.kind == "ambiguous"


def test_missing_dob_raises(config, base_row):
    index = ApplicationIndex.build([_summary()])
    base_row["Student Dob 5F"] = None
    with pytest.raises(StudentLookupError) as exc:
        index.find(base_row, config.match.student)
    assert exc.value.kind == "missing_dob"


def test_not_found_raises(config, base_row):
    index = ApplicationIndex.build([_summary(external_id="OTHER", first_name="Zed")])
    base_row["Student External Id 0A"] = "S-NONE"
    base_row["Student First Name 1B"] = "Nobody"
    with pytest.raises(StudentLookupError) as exc:
        index.find(base_row, config.match.student)
    assert exc.value.kind == "not_found"


# -- parent slots -----------------------------------------------------------

def test_natural_slots_match(config, application, base_row):
    decision = resolve_slots(application, base_row, config.match)
    assert decision.parent1_target == "user_parent"
    assert decision.parent2_target == "non_user_parent"
    assert decision.warnings == []


def test_parent_match_by_email_when_extid_stale(config, application, base_row):
    base_row["Parent One External Id 22W"] = "STALE"  # email still matches
    decision = resolve_slots(application, base_row, config.match)
    assert decision.parent1_target == "user_parent"


def test_parent_mismatch_yields_none(config, application, base_row):
    for key in ("Parent One External Id 22W", "Parent One Email 29AD",
                "Parent One Mobile 30AE", "Parent One First Name 24Y",
                "Parent One Last Name 26AA"):
        base_row[key] = "zzz"
    decision = resolve_slots(application, base_row, config.match)
    assert decision.parent1_target is None


def test_slot_swap_refused_by_default(config, application, base_row):
    # Sheet P1 identifies the app's non_user_parent and vice versa.
    application["user_parent"]["external_id"] = "P2-001"
    application["user_parent"]["first_name"] = "Ben"
    application["user_parent"]["email"] = "ben@example.com"
    application["user_parent"]["mobile_phone"] = "0422 330771"
    application["non_user_parent"]["external_id"] = "P1-001"
    application["non_user_parent"]["first_name"] = "Anna"
    application["non_user_parent"]["email"] = "anna@example.com"
    application["non_user_parent"]["mobile_phone"] = "0411 220662"

    decision = resolve_slots(application, base_row, config.match, allow_swap=False)
    assert decision.parent1_target is None and decision.parent2_target is None
    assert any("REFUSED" in w for w in decision.warnings)


def test_slot_swap_remapped_with_flag(config, application, base_row):
    application["user_parent"]["external_id"] = "P2-001"
    application["user_parent"]["first_name"] = "Ben"
    application["user_parent"]["email"] = "ben@example.com"
    application["user_parent"]["mobile_phone"] = "0422 330771"
    application["non_user_parent"]["external_id"] = "P1-001"
    application["non_user_parent"]["first_name"] = "Anna"
    application["non_user_parent"]["email"] = "anna@example.com"
    application["non_user_parent"]["mobile_phone"] = "0411 220662"

    decision = resolve_slots(application, base_row, config.match, allow_swap=True)
    assert decision.parent1_target == "non_user_parent"
    assert decision.parent2_target == "user_parent"
    assert decision.swapped is True
