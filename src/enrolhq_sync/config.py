"""Typed configuration loaded from the external ``config/*.toml`` files.

The working predecessor baked the column map, enum tables, and the
(school-specific) religion list into Python source. Here they are *data*:
``config.py`` reads the TOML, validates it lightly, and hands the rest of the
package strongly-typed objects (:class:`ColumnRule`, :class:`MatchKeys`,
:class:`Config`). Swapping schools is then a matter of pointing at a different
``religion.<school>.toml`` — no code change.

TOML (not YAML) is used so there is no third-party parser dependency:
``tomllib`` is in the standard library on Python 3.11+.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# config/ sits at the repository root, two levels up from this file
# (src/enrolhq_sync/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"

# The nine destinations a value may be written to, expressed as the key path
# to walk into the application object. Section names are the contract between
# column_map.toml and the planner.
SECTION_PATHS: dict[str, tuple[str, ...]] = {
    "app": (),
    "addr_residential": ("residential_address",),
    "addr_mailing": ("mailing_address",),
    "parent1": ("user_parent",),
    "parent2": ("non_user_parent",),
    "parent1_addr_res": ("user_parent", "residential_address"),
    "parent1_addr_mail": ("user_parent", "mailing_address"),
    "parent2_addr_res": ("non_user_parent", "residential_address"),
    "parent2_addr_mail": ("non_user_parent", "mailing_address"),
}

# Which sections belong to which parent slot — used by the planner to gate and
# (when swapping) remap parent writes.
PARENT1_SECTIONS = frozenset({"parent1", "parent1_addr_res", "parent1_addr_mail"})
PARENT2_SECTIONS = frozenset({"parent2", "parent2_addr_res", "parent2_addr_mail"})

# Mirror tables for --allow-parent-swap: a Parent-One section -> its Parent-Two
# equivalent and vice versa.
SWAP_P1_TO_P2 = {
    "parent1": "parent2",
    "parent1_addr_res": "parent2_addr_res",
    "parent1_addr_mail": "parent2_addr_mail",
}
SWAP_P2_TO_P1 = {value: key for key, value in SWAP_P1_TO_P2.items()}


class ConfigError(ValueError):
    """Raised when a config file is missing or structurally invalid."""


@dataclass(frozen=True)
class ColumnRule:
    """One spreadsheet column's destination and optional transform."""

    header: str
    section: str
    field: str
    transform: str | None = None

    @property
    def is_key(self) -> bool:
        return self.section == "key"

    @property
    def path(self) -> tuple[str, ...]:
        """Key path into the application object for this rule's section."""
        return SECTION_PATHS[self.section]


@dataclass(frozen=True)
class MatchKeys:
    """Spreadsheet column names that drive matching (from matching.toml)."""

    student: dict[str, str]
    parent1: dict[str, str]
    parent2: dict[str, str]


@dataclass(frozen=True)
class ReligionConfig:
    """School-specific religion allowlist + alias table."""

    # canonical-lower -> canonical-spelling to send
    canonical: dict[str, str]
    # source-label-lower -> canonical-lower
    aliases: dict[str, str]

    def resolve(self, normalized_label: str) -> str | None:
        """Return the spelling to send, or ``None`` if not an allowed choice."""
        aliased = self.aliases.get(normalized_label, normalized_label)
        return self.canonical.get(aliased)


@dataclass(frozen=True)
class Config:
    """Everything the planner needs, assembled from the config directory."""

    columns: list[ColumnRule]
    match: MatchKeys
    enum_maps: dict[str, dict[str, Any]]
    religion: ReligionConfig
    by_header: dict[str, ColumnRule] = field(default_factory=dict)

    def rule_for(self, header: str) -> ColumnRule | None:
        return self.by_header.get(header)


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open("rb") as handle:
        return tomllib.load(handle)


def load_column_rules(path: Path) -> list[ColumnRule]:
    data = _load_toml(path)
    rules: list[ColumnRule] = []
    for entry in data.get("column", []):
        try:
            header = entry["header"]
            section = entry["section"]
            field_name = entry["field"]
        except KeyError as exc:
            raise ConfigError(
                f"{path.name}: a [[column]] entry is missing {exc}"
            ) from exc
        if section != "key" and section not in SECTION_PATHS:
            raise ConfigError(
                f"{path.name}: unknown section {section!r} for header {header!r}"
            )
        rules.append(
            ColumnRule(
                header=header,
                section=section,
                field=field_name,
                transform=entry.get("transform"),
            )
        )
    return rules


def load_match_keys(path: Path) -> MatchKeys:
    data = _load_toml(path)
    try:
        return MatchKeys(
            student=dict(data["student"]),
            parent1=dict(data["parent1"]),
            parent2=dict(data["parent2"]),
        )
    except KeyError as exc:
        raise ConfigError(f"{path.name}: missing table {exc}") from exc


def load_enum_maps(path: Path) -> dict[str, dict[str, Any]]:
    """Load enum tables, lower-casing every label key for case-insensitive lookup."""
    data = _load_toml(path)
    out: dict[str, dict[str, Any]] = {}
    for table_name, table in data.items():
        out[table_name] = {str(k).strip().lower(): v for k, v in table.items()}
    return out


def load_religion(path: Path) -> ReligionConfig:
    data = _load_toml(path)
    allowed = data.get("allowed", [])
    if not allowed:
        raise ConfigError(f"{path.name}: 'allowed' list is empty or missing")
    canonical = {str(choice).strip().lower(): str(choice) for choice in allowed}
    aliases = {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in data.get("aliases", {}).items()
    }
    return ReligionConfig(canonical=canonical, aliases=aliases)


def load_config(
    config_dir: Path = CONFIG_DIR,
    religion_file: str = "religion.sample.toml",
) -> Config:
    """Assemble the full :class:`Config` from the config directory."""
    columns = load_column_rules(config_dir / "column_map.toml")
    match = load_match_keys(config_dir / "matching.toml")
    enum_maps = load_enum_maps(config_dir / "enum_maps.toml")
    religion = load_religion(config_dir / religion_file)
    by_header = {rule.header: rule for rule in columns}
    return Config(
        columns=columns,
        match=match,
        enum_maps=enum_maps,
        religion=religion,
        by_header=by_header,
    )
