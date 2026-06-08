"""Tests for the transform registry."""

from __future__ import annotations

import pytest

from enrolhq_sync import transforms
from enrolhq_sync.transforms import TransformContext, values_equivalent


def _ctx(config, dictionaries, container, field, raw, arg=None, path_label=None):
    return TransformContext(
        raw=raw,
        container=container,
        field=field,
        arg=arg,
        config=config,
        dictionaries=dictionaries,
        path_label=path_label or field,
    )


def test_parse_name():
    assert transforms.parse_name("enum:degree") == ("enum", "degree")
    assert transforms.parse_name("religion") == ("religion", None)


# -- equivalence ------------------------------------------------------------

def test_values_equivalent_fk_partial_dict():
    assert values_equivalent({"id": 7, "name": "X"}, {"id": 7})
    assert not values_equivalent({"id": 7}, {"id": 8})


def test_values_equivalent_arrays():
    assert values_equivalent([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}])
    assert values_equivalent([0, 2], [2, 0])
    assert not values_equivalent([1], [2])


# -- enum -------------------------------------------------------------------

def test_enum_maps_label_to_code(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "degree", "Bachelor Degree", arg="degree")
    result = transforms.get("enum")(ctx)
    assert result.warning is None
    assert container["degree"] == 7
    assert result.changes


def test_enum_unmapped_label_warns_and_leaves_field(config, dictionaries):
    container = {"degree": 5}
    ctx = _ctx(config, dictionaries, container, "degree", "PhD in Wizardry", arg="degree")
    result = transforms.get("enum")(ctx)
    assert "unmapped" in result.warning
    assert container["degree"] == 5  # untouched


def test_enum_array_indigenous(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "aboriginal_statuses",
               "Aboriginal and Torres Strait Islander", arg="indigenous")
    transforms.get("enum_array")(ctx)
    assert container["aboriginal_statuses"] == [1, 2]


# -- fk ---------------------------------------------------------------------

def test_fk_resolves_to_id_object(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "born_country", "Australia", arg="countries")
    transforms.get("fk")(ctx)
    assert container["born_country"] == {"id": "C-AU"}


def test_fk_miss_warns(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "born_country", "Atlantis", arg="countries")
    result = transforms.get("fk")(ctx)
    assert "no countries dictionary entry" in result.warning
    assert "born_country" not in container


def test_fk_array_wraps_in_list(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "nationalities", "Australian", arg="nationalities")
    transforms.get("fk_array")(ctx)
    assert container["nationalities"] == [{"id": "N-AU"}]


# -- religion ---------------------------------------------------------------

def test_religion_allowed_choice(config, dictionaries):
    container = {"religion": None}
    ctx = _ctx(config, dictionaries, container, "religion", "catholic")
    result = transforms.get("religion")(ctx)
    assert container["religion"] == "Catholic"  # canonical spelling
    assert result.warning is None


def test_religion_alias_none_to_nil(config, dictionaries):
    container = {"religion": None}
    ctx = _ctx(config, dictionaries, container, "religion", "No Religion")
    transforms.get("religion")(ctx)
    assert container["religion"] == "Nil"


def test_religion_invalid_skipped_with_warning(config, dictionaries):
    container = {"religion": "Catholic"}
    ctx = _ctx(config, dictionaries, container, "religion", "Pastafarian")
    result = transforms.get("religion")(ctx)
    assert "not a valid EnrolHQ choice" in result.warning
    assert container["religion"] == "Catholic"  # untouched


def test_religion_already_set_no_change(config, dictionaries):
    container = {"religion": "Catholic"}
    ctx = _ctx(config, dictionaries, container, "religion", "CATHOLIC")
    result = transforms.get("religion")(ctx)
    assert result.changes == []


# -- home language ----------------------------------------------------------

def test_home_language_english_disables_gate(config, dictionaries):
    container = {"home_language": {"id": "L-ES"}, "is_speak_other_language": True}
    ctx = _ctx(config, dictionaries, container, "home_language", "English",
               path_label="home_language")
    transforms.get("home_language")(ctx)
    assert container["is_speak_other_language"] is False
    assert container["home_language"] is None


def test_home_language_other_sets_fk_and_gate(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "home_language", "Spanish",
               path_label="user_parent.home_language")
    transforms.get("home_language")(ctx)
    assert container["is_speak_other_language"] is True
    assert container["home_language"] == {"id": "L-ES"}


def test_home_language_unknown_warns(config, dictionaries):
    container = {}
    ctx = _ctx(config, dictionaries, container, "home_language", "Dothraki",
               path_label="home_language")
    result = transforms.get("home_language")(ctx)
    assert "no languages dictionary entry" in result.warning


# -- how hear ---------------------------------------------------------------

def test_how_hear_records_free_text_when_not_structured(config, dictionaries):
    container = {"how_hear": ["Website"], "how_hear_other": None}
    ctx = _ctx(config, dictionaries, container, "how_hear_other", "Parish bulletin",
               path_label="user_parent.how_hear_other")
    transforms.get("how_hear")(ctx)
    assert container["how_hear_other"] == "Parish bulletin"


def test_how_hear_suppresses_duplicate_of_structured(config, dictionaries):
    container = {"how_hear": ["Website"], "how_hear_other": None}
    ctx = _ctx(config, dictionaries, container, "how_hear_other", "Website",
               path_label="user_parent.how_hear_other")
    result = transforms.get("how_hear")(ctx)
    assert result.changes == []
    assert container["how_hear_other"] is None


def test_how_hear_clears_previously_redundant_value(config, dictionaries):
    container = {"how_hear": ["Website"], "how_hear_other": "Website"}
    ctx = _ctx(config, dictionaries, container, "how_hear_other", "Website",
               path_label="user_parent.how_hear_other")
    transforms.get("how_hear")(ctx)
    assert container["how_hear_other"] == ""
