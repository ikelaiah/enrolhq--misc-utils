"""Run output: human console/file logging plus a machine-readable JSON report.

Two audiences are served from one place:

  * The **console/log file** mirrors the predecessor's readable readout — clean
    INFO lines, ``[WARNING]``/``[ERROR]`` prefixes, optional ``logs/run-*.log``.
  * The **JSON report** (``logs/run-*.json``) records every row's structured
    outcome plus the run summary, so a run is diffable and a future step can
    re-drive only the failures.

:class:`RunReport` is an accumulator the CLI feeds as it processes rows; at the
end it both prints the summary and (unless suppressed) writes the JSON.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("enrolhq_sync")


class _ConsoleFormatter(logging.Formatter):
    """No prefix for INFO (keeps the diff readout clean); level tag above it."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.levelno >= logging.WARNING:
            return f"[{record.levelname}] {msg}"
        return msg


def setup_logging(log_file: Path | None = None) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_ConsoleFormatter())
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root.addHandler(file_handler)


@dataclass
class RowResult:
    """Structured outcome for one spreadsheet row."""

    row_number: int
    outcome: str  # updated | no_change | not_found | ambiguous | skipped | error
    application_id: Any = None
    student_external_id: Any = None
    match_path: str | None = None
    diff: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# Warning -> summary category. A row contributes at most once per category.
def categorize(warnings: list[str]) -> set[str]:
    cats: set[str] = set()
    for warning in warnings:
        low = warning.lower()
        if "slot swap refused" in low:
            cats.add("slot_swap_refused")
        elif "slot swap remapped" in low:
            cats.add("slot_swap_remapped")
        elif warning.startswith("Parent One skipped"):
            cats.add("p1_missing_slot" if "null/missing" in warning else "p1_mismatch")
        elif warning.startswith("Parent Two skipped"):
            cats.add("p2_missing_slot" if "null/missing" in warning else "p2_mismatch")
        elif "unmapped" in low and "label" in low:
            cats.add("unmapped_enum")
        elif "dictionary entry for" in low:
            cats.add("fk_miss")
        elif "is not a valid enrolhq choice" in low:
            cats.add("religion_invalid")
    return cats


_CATEGORY_LABELS = [
    ("slot_swap_refused", "Slot swap REFUSED (both skipped)"),
    ("slot_swap_remapped", "Slot swap REMAPPED (cross-mapped)"),
    ("p1_missing_slot", "Parent One skipped: slot missing"),
    ("p1_mismatch", "Parent One skipped: identity miss"),
    ("p2_missing_slot", "Parent Two skipped: slot missing"),
    ("p2_mismatch", "Parent Two skipped: identity miss"),
    ("unmapped_enum", "Rows with unmapped enum labels"),
    ("fk_miss", "Rows with FK dictionary miss"),
    ("religion_invalid", "Rows with invalid religion (skipped)"),
]


@dataclass
class RunReport:
    """Accumulates per-row results and emits the summary + JSON report."""

    mode: str  # "LIVE" | "DRY-RUN"
    input_file: str
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    rows: list[RowResult] = field(default_factory=list)
    category_counts: dict[str, int] = field(
        default_factory=lambda: {key: 0 for key, _ in _CATEGORY_LABELS}
    )
    limit_hit: bool = False
    total_rows: int = 0

    def add(self, result: RowResult) -> None:
        self.rows.append(result)
        for cat in categorize(result.warnings):
            self.category_counts[cat] += 1

    # -- derived counters ---------------------------------------------------

    def _count(self, outcome: str) -> int:
        return sum(1 for r in self.rows if r.outcome == outcome)

    def _match_count(self, path: str) -> int:
        return sum(1 for r in self.rows if r.match_path == path)

    # -- output -------------------------------------------------------------

    def log_summary(self) -> None:
        live = self.mode == "LIVE"
        verb = "Updated" if live else "Would update"
        _log.info("")
        _log.info("=== SUMMARY ===")
        _log.info("Mode: %s", self.mode)
        if self.limit_hit:
            _log.info("Rows examined: %d of %d (stopped early: --limit)",
                      len(self.rows), self.total_rows)
        else:
            _log.info("Rows processed: %d", len(self.rows))
        _log.info("")
        _log.info("Row outcomes:")
        _log.info("  %-33s %d", verb, self._count("updated"))
        _log.info("  %-33s %d", "No changes needed", self._count("no_change"))
        _log.info("  %-33s %d", "Not found", self._count("not_found"))
        _log.info("  %-33s %d", "Ambiguous (fallback collision)", self._count("ambiguous"))
        _log.info("  %-33s %d", "Skipped (missing student PK)", self._count("skipped"))
        _log.info("  %-33s %d", "Errors (fetch or PUT)", self._count("error"))
        _log.info("")
        _log.info("Student match path:")
        _log.info("  %-33s %d", "Matched by external_id", self._match_count("external_id"))
        _log.info("  %-33s %d", "Matched by name+dob fallback", self._match_count("name_fallback"))
        _log.info("")
        _log.info("Parent-slot & translation issues (row counts):")
        for key, label in _CATEGORY_LABELS:
            _log.info("  %-37s %d", label, self.category_counts[key])

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": self.mode,
            "input_file": self.input_file,
            "started_at": self.started_at,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "total_rows": self.total_rows,
            "rows_examined": len(self.rows),
            "limit_hit": self.limit_hit,
            "outcomes": {
                name: self._count(name)
                for name in ("updated", "no_change", "not_found", "ambiguous", "skipped", "error")
            },
            "match_path": {
                "external_id": self._match_count("external_id"),
                "name_fallback": self._match_count("name_fallback"),
            },
            "category_counts": dict(self.category_counts),
            "rows": [asdict(r) for r in self.rows],
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        _log.info("JSON report: %s", path)
