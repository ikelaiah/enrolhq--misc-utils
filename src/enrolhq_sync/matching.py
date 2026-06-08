"""Match spreadsheet rows to EnrolHQ applications, and sheet parents to slots.

External IDs in EnrolHQ are not stable — integration jobs (e.g. the TASC sync)
rewrite them — so matching never trusts a single identifier:

  * **Students** match on ``(external_id, dob)`` first, then fall back to
    ``(last, first, middle, dob)``. A fallback that hits more than one record
    is reported AMBIGUOUS and the row is skipped.

  * **Parents** match their natural slot (Parent One -> ``user_parent``,
    Parent Two -> ``non_user_parent``) by ANY of external_id / email / mobile /
    (first, last). OR semantics let one stale identifier coexist with good
    ones. A detected cross-slot swap is refused by default, or cross-mapped
    under ``allow_swap``.

The :class:`ApplicationIndex` is built once per run from the lightweight
list-endpoint summaries; the full object is fetched separately before the PUT.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import normalize
from .config import MatchKeys

StudentKey = tuple[str, str]  # (external_id, dob)
NameKey = tuple[str, str, str, str]  # (last, first, middle, dob)


class MatchPath(Enum):
    EXTERNAL_ID = "external_id"
    NAME_FALLBACK = "name_fallback"


@dataclass
class StudentMatch:
    summary: dict[str, Any]
    path: MatchPath


class StudentLookupError(Exception):
    """Raised when a row cannot be resolved to exactly one application."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind  # "not_found" | "ambiguous" | "missing_dob"


@dataclass
class ApplicationIndex:
    """Two indexes over the fetched application summaries."""

    by_external_id: dict[StudentKey, dict[str, Any]]
    by_name: dict[NameKey, list[dict[str, Any]]]

    @classmethod
    def build(cls, summaries: list[dict[str, Any]]) -> "ApplicationIndex":
        by_external_id: dict[StudentKey, dict[str, Any]] = {}
        by_name: dict[NameKey, list[dict[str, Any]]] = {}
        for app in summaries:
            dob = normalize.cell_date(app.get("dob"))
            if not dob:
                continue
            ext_id = app.get("external_id")
            if ext_id:
                by_external_id[(str(ext_id).strip(), dob)] = app
            last = normalize.name(app.get("last_name"))
            first = normalize.name(app.get("first_name"))
            middle = normalize.name(app.get("middle_name"))
            if last and first:
                by_name.setdefault((last, first, middle, dob), []).append(app)
        return cls(by_external_id=by_external_id, by_name=by_name)

    def __len__(self) -> int:
        return len(self.by_external_id)

    def find(self, row: dict[str, Any], keys: dict[str, str]) -> StudentMatch:
        """Resolve a sheet row to one application or raise :class:`StudentLookupError`."""
        ext_id = normalize.cell(row.get(keys["external_id"]))
        dob = normalize.cell_date(row.get(keys["dob"]))
        if not dob:
            raise StudentLookupError("missing_dob", "missing student DOB")

        if ext_id is not None:
            summary = self.by_external_id.get((str(ext_id), dob))
            if summary is not None:
                return StudentMatch(summary, MatchPath.EXTERNAL_ID)

        last = normalize.name(row.get(keys["last_name"]))
        first = normalize.name(row.get(keys["first_name"]))
        middle = normalize.name(row.get(keys["middle_name"]))
        if not (last and first):
            raise StudentLookupError(
                "not_found",
                f"external_id={ext_id} dob={dob} missed and no first/last name "
                f"in sheet for fallback",
            )

        candidates = self.by_name.get((last, first, middle, dob), [])
        if not candidates:
            raise StudentLookupError(
                "not_found",
                f"neither external_id={ext_id} dob={dob} nor name+dob "
                f"({last!r}, {first!r}, {middle!r}) matches any application",
            )
        if len(candidates) > 1:
            ids = ", ".join(str(c.get("external_id")) for c in candidates)
            raise StudentLookupError(
                "ambiguous",
                f"name+dob ({last!r}, {first!r}, {middle!r}, {dob!r}) matches "
                f"{len(candidates)} records (external_ids: {ids})",
            )
        return StudentMatch(candidates[0], MatchPath.NAME_FALLBACK)


def parent_matches(
    parent: dict[str, Any] | None,
    *,
    external_id: Any,
    first: str,
    last: str,
    email: str = "",
    mobile: str = "",
) -> bool:
    """True if ``parent`` matches the sheet by ANY of external_id / email /
    mobile / (first, last)."""
    if not isinstance(parent, dict):
        return False
    if external_id is not None:
        p_ext = normalize.cell(parent.get("external_id"))
        if p_ext is not None and str(external_id).strip() == str(p_ext).strip():
            return True
    if email:
        p_email = normalize.email(parent.get("email"))
        if p_email and p_email == email:
            return True
    if mobile:
        p_mobile = normalize.phone(parent.get("mobile_phone"))
        if p_mobile and p_mobile == mobile:
            return True
    if first and last:
        if normalize.name(parent.get("first_name")) == first and \
                normalize.name(parent.get("last_name")) == last:
            return True
    return False


@dataclass
class SlotDecision:
    """Resolved write targets for a row's two sheet parents.

    Each target is the app slot to write to (``"user_parent"`` /
    ``"non_user_parent"``) or ``None`` when that parent's writes are refused.
    ``swapped`` is True when --allow-parent-swap cross-mapped the slots.
    """

    parent1_target: str | None
    parent2_target: str | None
    warnings: list[str]
    swapped: bool = False


def _sheet_parent(row: dict[str, Any], keys: dict[str, str]) -> dict[str, Any]:
    return {
        "external_id": normalize.cell(row.get(keys["external_id"])),
        "first": normalize.name(row.get(keys["first_name"])),
        "last": normalize.name(row.get(keys["last_name"])),
        "email": normalize.email(row.get(keys["email"])),
        "mobile": normalize.phone(row.get(keys["mobile"])),
    }


def resolve_slots(
    app: dict[str, Any],
    row: dict[str, Any],
    match: MatchKeys,
    allow_swap: bool = False,
) -> SlotDecision:
    """Decide which app slot each sheet parent writes to."""
    user_parent = app.get("user_parent") if isinstance(app.get("user_parent"), dict) else None
    non_user_parent = (
        app.get("non_user_parent") if isinstance(app.get("non_user_parent"), dict) else None
    )

    p1 = _sheet_parent(row, match.parent1)
    p2 = _sheet_parent(row, match.parent2)

    p1_to_up = parent_matches(user_parent, external_id=p1["external_id"], first=p1["first"],
                              last=p1["last"], email=p1["email"], mobile=p1["mobile"])
    p1_to_nup = parent_matches(non_user_parent, external_id=p1["external_id"], first=p1["first"],
                               last=p1["last"], email=p1["email"], mobile=p1["mobile"])
    p2_to_up = parent_matches(user_parent, external_id=p2["external_id"], first=p2["first"],
                              last=p2["last"], email=p2["email"], mobile=p2["mobile"])
    p2_to_nup = parent_matches(non_user_parent, external_id=p2["external_id"], first=p2["first"],
                               last=p2["last"], email=p2["email"], mobile=p2["mobile"])

    # Cross-slot swap: P1 looks like the app's non_user_parent and P2 like the
    # user_parent, without also matching the natural slots.
    if p1_to_nup and p2_to_up and not p1_to_up and not p2_to_nup:
        if allow_swap:
            return SlotDecision(
                parent1_target="non_user_parent",
                parent2_target="user_parent",
                swapped=True,
                warnings=[
                    "Parent slot swap REMAPPED (--allow-parent-swap): sheet's "
                    "Parent One -> non_user_parent; sheet's Parent Two -> user_parent."
                ],
            )
        return SlotDecision(
            parent1_target=None,
            parent2_target=None,
            warnings=[
                "Parent slot swap REFUSED: sheet's Parent One matches app's "
                "non_user_parent and Parent Two matches app's user_parent. Pass "
                "--allow-parent-swap to cross-map, or reorder the sheet."
            ],
        )

    return SlotDecision(
        parent1_target="user_parent" if p1_to_up else None,
        parent2_target="non_user_parent" if p2_to_nup else None,
        warnings=[],
    )
