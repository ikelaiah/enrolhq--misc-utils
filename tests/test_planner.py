"""End-to-end tests for the pure planner on fixture dicts (no network)."""

from __future__ import annotations

from enrolhq_sync.planner import apply_changes


def test_no_changes_when_extras_blank(config, application, base_row, dictionaries):
    result = apply_changes(application, base_row, config, dictionaries)
    assert not result.has_changes
    assert result.warnings == []


def test_does_not_mutate_input_by_default(config, application, base_row, dictionaries):
    base_row["EXTRA_PARENT1_RELIGION"] = "Catholic"
    before = application["user_parent"]["religion"]
    apply_changes(application, base_row, config, dictionaries)  # in_place defaults False
    assert application["user_parent"]["religion"] == before  # original untouched


def test_in_place_applies_extra_fields(config, application, base_row, dictionaries):
    base_row["EXTRA_PARENT1_RELIGION"] = "catholic"
    base_row["EXTRA_PARENT1_PLACE_OF_WORSHIP"] = "St Mary's"
    base_row["EXTRA_STUDENT_COUNTRY_OF_BIRTH"] = "Australia"
    base_row["EXTRA_STUDENT_INDIGENOUS"] = "No"

    result = apply_changes(application, base_row, config, dictionaries, in_place=True)

    assert application["user_parent"]["religion"] == "Catholic"
    assert application["user_parent"]["church_attended"] == "St Mary's"
    assert application["born_country"] == {"id": "C-AU"}
    assert application["aboriginal_statuses"] == [0]
    assert result.has_changes


def test_parent_writes_dropped_when_slot_mismatch(config, application, base_row, dictionaries):
    base_row["EXTRA_PARENT1_RELIGION"] = "Catholic"
    for key in ("Parent One External Id 22W", "Parent One Email 29AD",
                "Parent One Mobile 30AE", "Parent One First Name 24Y",
                "Parent One Last Name 26AA"):
        base_row[key] = "zzz"

    result = apply_changes(application, base_row, config, dictionaries, in_place=True)
    assert application["user_parent"]["religion"] is None  # not written
    assert any("Parent One skipped" in w for w in result.warnings)


def test_swap_remap_writes_to_opposite_slot(config, application, base_row, dictionaries):
    # Reverse the identities on the application's slots.
    application["user_parent"].update(
        external_id="P2-001", first_name="Ben", email="ben@example.com",
        mobile_phone="0422 330771",
    )
    application["non_user_parent"].update(
        external_id="P1-001", first_name="Anna", email="anna@example.com",
        mobile_phone="0411 220662", religion=None,
    )
    base_row["EXTRA_PARENT1_RELIGION"] = "Catholic"  # sheet P1 -> Anna

    result = apply_changes(application, base_row, config, dictionaries,
                           allow_swap=True, in_place=True)
    # Anna is on non_user_parent after the swap, so the write lands there.
    assert application["non_user_parent"]["religion"] == "Catholic"
    assert application["user_parent"]["religion"] is None
    assert result.slots.swapped is True


def test_invalid_religion_does_not_block_other_writes(config, application, base_row, dictionaries):
    base_row["EXTRA_PARENT1_RELIGION"] = "Pastafarian"     # invalid -> skipped
    base_row["EXTRA_PARENT1_PLACE_OF_WORSHIP"] = "Noodle House"  # still applied

    result = apply_changes(application, base_row, config, dictionaries, in_place=True)
    assert application["user_parent"]["religion"] is None
    assert application["user_parent"]["church_attended"] == "Noodle House"
    assert any("not a valid EnrolHQ choice" in w for w in result.warnings)
