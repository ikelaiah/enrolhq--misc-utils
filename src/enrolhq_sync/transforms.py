"""Registry of field transforms — the heart of the redesign.

The predecessor resolved each field shape with a long ``if/elif`` ladder in
``apply_row_to_app`` plus a parallel ``COLUMN_TRANSFORMS`` dict. Here every
transform is a small registered function with one uniform signature, so the
planner has no per-field branching and each transform is trivially unit
testable in isolation.

A transform name is ``"<kind>"`` or ``"<kind>:<table>"`` (e.g. ``"enum:degree"``,
``"fk:countries"``). The planner looks up the kind, the transform reads the
table argument from the context. Each transform:

  * reads ``ctx.raw`` (the spreadsheet cell),
  * mutates ``ctx.container`` in place (so compound writes touching several
    sibling fields — home language, how-hear — are expressed naturally),
  * returns a :class:`TransformResult` listing the human-readable changes and
    at most one warning.

A blank cell yields an empty result (no change, no warning). A value that
cannot be translated yields ``changes=[]`` plus a ``warning`` — the planner
logs it and leaves the field untouched, so one bad cell never sinks the whole
record's PUT.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Callable

from . import normalize
from .config import Config

# Language EnrolHQ treats as the implicit default ("no other language spoken").
HOME_LANGUAGE_DEFAULT = "english"


@dataclass
class TransformContext:
    """Everything a transform needs to resolve one cell."""

    raw: Any
    container: dict[str, Any]  # the dict the field lives on (mutated in place)
    field: str  # destination attribute name
    arg: str | None  # the part after ':' in the transform name, if any
    config: Config
    dictionaries: dict[str, dict[str, Any]]  # reference FK tables (name->id)
    path_label: str  # dotted path for diff messages, e.g. "user_parent.religion"


@dataclass
class TransformResult:
    """Outcome of applying one transform."""

    changes: list[tuple[str, str]] = dc_field(default_factory=list)
    warning: str | None = None


Transform = Callable[[TransformContext], TransformResult]

_REGISTRY: dict[str, Transform] = {}


def register(kind: str) -> Callable[[Transform], Transform]:
    """Decorator registering a transform under ``kind``."""

    def wrap(func: Transform) -> Transform:
        _REGISTRY[kind] = func
        return func

    return wrap


def get(kind: str) -> Transform | None:
    return _REGISTRY.get(kind)


def parse_name(name: str) -> tuple[str, str | None]:
    """Split ``"enum:degree"`` into ``("enum", "degree")``; bare name -> arg None."""
    if ":" in name:
        kind, arg = name.split(":", 1)
        return kind, arg
    return name, None


# ---------------------------------------------------------------------------
# Equality + assignment helpers
# ---------------------------------------------------------------------------

def values_equivalent(existing: Any, candidate: Any) -> bool:
    """FK-aware equality.

    ``{"id": X, "name": "Australia"}`` compares equal to ``{"id": X}``, and the
    same holds element-wise for arrays of such objects. Falls back to ``==``.
    """
    if existing == candidate:
        return True
    if isinstance(existing, dict) and isinstance(candidate, dict):
        e_id, c_id = existing.get("id"), candidate.get("id")
        if e_id is not None and c_id is not None:
            return str(e_id) == str(c_id)
    if isinstance(existing, list) and isinstance(candidate, list):
        e_ids = _id_set(existing)
        c_ids = _id_set(candidate)
        if e_ids is not None and c_ids is not None:
            return e_ids == c_ids
        try:  # primitive arrays, e.g. aboriginal_statuses [0]/[1]/[2]
            return sorted(existing) == sorted(candidate)
        except TypeError:
            return False
    return False


def _id_set(items: list[Any]) -> list[str] | None:
    ids = [
        str(it["id"]) for it in items if isinstance(it, dict) and it.get("id") is not None
    ]
    return sorted(ids) if ids else None


def _assign(ctx: TransformContext, field: str, new: Any, label: str) -> list[tuple[str, str]]:
    """Set ``container[field] = new``, returning a change record if it differs."""
    existing = ctx.container.get(field)
    changes: list[tuple[str, str]] = []
    if not values_equivalent(existing, new):
        changes.append((label, f"{existing!r} -> {new!r}"))
    ctx.container[field] = new
    return changes


# ---------------------------------------------------------------------------
# Simple value transforms (enum / fk)
# ---------------------------------------------------------------------------

@register("enum")
def _enum(ctx: TransformContext) -> TransformResult:
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()
    table = ctx.config.enum_maps.get(ctx.arg or "", {})
    code = table.get(label)
    if code is None:
        return TransformResult(warning=f"unmapped {ctx.arg} label: {ctx.raw!r}")
    return TransformResult(changes=_assign(ctx, ctx.field, code, ctx.path_label))


@register("enum_array")
def _enum_array(ctx: TransformContext) -> TransformResult:
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()
    table = ctx.config.enum_maps.get(ctx.arg or "", {})
    codes = table.get(label)
    if codes is None:
        return TransformResult(warning=f"unmapped {ctx.arg} label: {ctx.raw!r}")
    return TransformResult(changes=_assign(ctx, ctx.field, list(codes), ctx.path_label))


@register("fk")
def _fk(ctx: TransformContext) -> TransformResult:
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()
    table = ctx.dictionaries.get(ctx.arg or "", {})
    fk_id = table.get(label)
    if fk_id is None:
        return TransformResult(warning=f"no {ctx.arg} dictionary entry for {ctx.raw!r}")
    return TransformResult(changes=_assign(ctx, ctx.field, {"id": fk_id}, ctx.path_label))


@register("fk_array")
def _fk_array(ctx: TransformContext) -> TransformResult:
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()
    table = ctx.dictionaries.get(ctx.arg or "", {})
    fk_id = table.get(label)
    if fk_id is None:
        return TransformResult(warning=f"no {ctx.arg} dictionary entry for {ctx.raw!r}")
    return TransformResult(changes=_assign(ctx, ctx.field, [{"id": fk_id}], ctx.path_label))


# ---------------------------------------------------------------------------
# Compound / validated transforms
# ---------------------------------------------------------------------------

@register("religion")
def _religion(ctx: TransformContext) -> TransformResult:
    """Set ``religion`` to a school-allowlisted choice.

    An invalid value 400s the whole PUT, so unknown values are skipped with a
    warning rather than sent. Comparison is case-insensitive.
    """
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()
    canonical = ctx.config.religion.resolve(label)
    if canonical is None:
        return TransformResult(
            warning=(
                f"religion {ctx.raw!r} is not a valid EnrolHQ choice for this "
                f"school; skipping (leaving existing value)"
            )
        )
    existing = ctx.container.get(ctx.field)
    if normalize.label(existing) == canonical.strip().lower():
        return TransformResult()  # already set (case-insensitive)
    return TransformResult(changes=_assign(ctx, ctx.field, canonical, ctx.path_label))


@register("home_language")
def _home_language(ctx: TransformContext) -> TransformResult:
    """Compound write of ``home_language`` + ``is_speak_other_language``.

    EnrolHQ gates ``home_language`` behind the boolean and treats English as
    the implicit default, so a bare home_language write is silently dropped
    when the flag is False. The sheet is the source of truth:

      * "English"  -> is_speak_other_language=False, home_language=None
      * other lang -> is_speak_other_language=True,  home_language={id}
    """
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()

    prefix = ctx.path_label.rsplit(".", 1)[0] + "." if "." in ctx.path_label else ""
    changes: list[tuple[str, str]] = []

    if label == HOME_LANGUAGE_DEFAULT:
        changes += _assign(ctx, "is_speak_other_language", False, f"{prefix}is_speak_other_language")
        changes += _assign(ctx, "home_language", None, f"{prefix}home_language")
        return TransformResult(changes=changes)

    table = ctx.dictionaries.get("languages", {})
    fk_id = table.get(label)
    if fk_id is None:
        return TransformResult(warning=f"no languages dictionary entry for {ctx.raw!r}")
    changes += _assign(ctx, "is_speak_other_language", True, f"{prefix}is_speak_other_language")
    changes += _assign(ctx, "home_language", {"id": fk_id}, f"{prefix}home_language")
    return TransformResult(changes=changes)


@register("how_hear")
def _how_hear(ctx: TransformContext) -> TransformResult:
    """Write HEARD_ABOUT_SCHOOL into the free-text ``how_hear_other`` box, but
    suppress it when the value already appears in the read-only structured
    ``how_hear`` multi-select (otherwise it just duplicates a checkbox).

    A previously-written redundant value is cleared back to ``""``.
    """
    label = normalize.label(ctx.raw)
    if label is None:
        return TransformResult()

    structured = ctx.container.get("how_hear")
    structured_norm = (
        {normalize.name(x) for x in structured} if isinstance(structured, list) else set()
    )
    current = ctx.container.get(ctx.field)

    if label in structured_norm:
        if current and normalize.name(current) == label:
            return TransformResult(changes=_assign(ctx, ctx.field, "", ctx.path_label))
        return TransformResult()

    return TransformResult(changes=_assign(ctx, ctx.field, normalize.cell(ctx.raw), ctx.path_label))
