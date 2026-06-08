"""Pure planner: turn one sheet row into a set of mutations on an application.

This is the testable core. :func:`apply_changes` mutates a deep copy of the
application by default and returns a :class:`ChangeSet` describing what it did
(diff, warnings, slot decision). It performs no I/O and no logging — the CLI
owns all of that — so tests assert on the returned value with no network and
no mocks.

The flow mirrors the predecessor's ``apply_row_to_app`` but the per-field
branching is gone: every field goes through the transform registry, with plain
free-text/date fields handled by an implicit passthrough transform.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field as dc_field
from typing import Any

from . import transforms
from .config import (
    Config,
    PARENT1_SECTIONS,
    PARENT2_SECTIONS,
    SECTION_PATHS,
    SWAP_P1_TO_P2,
    SWAP_P2_TO_P1,
)
from .matching import SlotDecision, resolve_slots
from . import normalize


@dataclass
class ChangeSet:
    """Result of planning one row against one application."""

    diff: dict[str, str] = dc_field(default_factory=dict)
    warnings: list[str] = dc_field(default_factory=list)
    slots: SlotDecision | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.diff)


def _container(app: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    """Walk ``path`` into ``app``, materializing empty dicts as needed."""
    cur = app
    for key in path:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    return cur


def _columns_with_data(config: Config, row: dict[str, Any], sections: frozenset[str]) -> list[str]:
    """Headers whose section is in ``sections`` and whose cell is non-empty."""
    return [
        rule.header
        for rule in config.columns
        if rule.section in sections and normalize.cell(row.get(rule.header)) is not None
    ]


def apply_changes(
    app: dict[str, Any],
    row: dict[str, Any],
    config: Config,
    dictionaries: dict[str, dict[str, Any]] | None = None,
    allow_swap: bool = False,
    in_place: bool = False,
) -> ChangeSet:
    """Plan and apply a row's writes onto ``app``.

    By default operates on a deep copy (``in_place=False``); pass
    ``in_place=True`` from the CLI when ``app`` is the object you intend to PUT.
    """
    target = app if in_place else copy.deepcopy(app)
    dictionaries = dictionaries or {}
    result = ChangeSet()

    slots = resolve_slots(target, row, config.match, allow_swap=allow_swap)
    result.slots = slots
    result.warnings.extend(slots.warnings)

    # Explain dropped parent columns (unless a swap warning already covers it).
    if not slots.swapped and not any("swap" in w.lower() for w in slots.warnings):
        _warn_dropped_parent(result, target, row, config, slots)

    for rule in config.columns:
        if rule.is_key:
            continue
        raw = row.get(rule.header)

        section = rule.section
        # Gate + remap parent sections per the slot decision.
        if section in PARENT1_SECTIONS:
            if slots.parent1_target is None:
                continue
            if slots.parent1_target == "non_user_parent":
                section = SWAP_P1_TO_P2[section]
        elif section in PARENT2_SECTIONS:
            if slots.parent2_target is None:
                continue
            if slots.parent2_target == "user_parent":
                section = SWAP_P2_TO_P1[section]

        path = SECTION_PATHS[section]
        container = _container(target, path)
        path_label = ".".join((*path, rule.field)) if path else rule.field

        if rule.transform:
            kind, arg = transforms.parse_name(rule.transform)
            func = transforms.get(kind)
            if func is None:
                result.warnings.append(
                    f"{rule.header}: unknown transform {rule.transform!r}"
                )
                continue
            ctx = transforms.TransformContext(
                raw=raw,
                container=container,
                field=rule.field,
                arg=arg,
                config=config,
                dictionaries=dictionaries,
                path_label=path_label,
            )
            outcome = func(ctx)
            if outcome.warning:
                result.warnings.append(f"{rule.header}: {outcome.warning}")
            for label, change in outcome.changes:
                result.diff[label] = change
            continue

        # Implicit passthrough: plain free-text / date.
        value = normalize.cell(raw)
        if value is None:
            continue
        existing = container.get(rule.field)
        if not transforms.values_equivalent(existing, value):
            result.diff[path_label] = f"{existing!r} -> {value!r}"
        container[rule.field] = value

    return result


def _warn_dropped_parent(
    result: ChangeSet,
    app: dict[str, Any],
    row: dict[str, Any],
    config: Config,
    slots: SlotDecision,
) -> None:
    if slots.parent1_target is None:
        cols = _columns_with_data(config, row, PARENT1_SECTIONS)
        if cols:
            reason = (
                "user_parent slot null/missing on the application"
                if not isinstance(app.get("user_parent"), dict)
                else "sheet's Parent One does not match app's user_parent by "
                     "external_id, email, mobile, or name"
            )
            result.warnings.append(
                f"Parent One skipped ({reason}). Dropped: {', '.join(cols)}"
            )
    if slots.parent2_target is None:
        cols = _columns_with_data(config, row, PARENT2_SECTIONS)
        if cols:
            reason = (
                "non_user_parent slot null/missing on the application"
                if not isinstance(app.get("non_user_parent"), dict)
                else "sheet's Parent Two does not match app's non_user_parent by "
                     "external_id, email, mobile, or name"
            )
            result.warnings.append(
                f"Parent Two skipped ({reason}). Dropped: {', '.join(cols)}"
            )
