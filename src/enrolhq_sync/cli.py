"""Command-line entry point: argument parsing, wiring, and the per-row loop.

The CLI is the only place that does I/O — it reads the sheet, builds the
resilient client, fetches and indexes applications, then for each row asks the
pure planner what to change and (in ``--live`` mode) PUTs it back. All
domain decisions live in the other modules; this file is glue plus
presentation.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from . import excel
from .client import ResilientClient, build_default_client
from .config import CONFIG_DIR, Config, ConfigError, load_config
from .matching import ApplicationIndex, MatchPath, StudentLookupError
from .planner import apply_changes
from .reporting import RowResult, RunReport, setup_logging

_log = logging.getLogger("enrolhq_sync")

DEFAULT_INPUT = Path(__file__).resolve().parents[1] / "input_spreadsheet.xlsx"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="enrolhq_sync",
        description="Batch-update EnrolHQ student/parent EXTRA_* fields from an Excel sheet.",
    )
    parser.add_argument("--live", action="store_true",
                        help="Actually PUT updates. Default is dry-run (no writes).")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help="Input spreadsheet (default: src/input_spreadsheet.xlsx).")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Stop after N rows that have changes. Use --live --limit 1 "
                             "for a single-record canary.")
    parser.add_argument("--allow-parent-swap", action="store_true",
                        help="Cross-map writes when a parent slot swap is detected, "
                             "instead of refusing the row's parent writes.")
    parser.add_argument("--config-dir", type=Path, default=CONFIG_DIR,
                        help="Directory holding the *.toml config files.")
    parser.add_argument("--religion-config", default="religion.sample.toml",
                        help="School-specific religion config filename within --config-dir.")

    log_group = parser.add_mutually_exclusive_group()
    log_group.add_argument("--log-file", type=Path, default=None,
                           help="Write the run log here. Default: logs/run-<ts>.log.")
    log_group.add_argument("--no-log-file", action="store_true",
                           help="Do not write a log file (stdout only).")
    parser.add_argument("--no-report", action="store_true",
                        help="Do not write the JSON run report.")
    return parser


def _resolve_log_file(args: argparse.Namespace, timestamp: str) -> Path | None:
    if args.no_log_file:
        return None
    if args.log_file is not None:
        return args.log_file
    return Path("logs") / f"run-{timestamp}.log"


def _process_row(
    row_number: int,
    row: dict,
    index: ApplicationIndex,
    client: ResilientClient,
    config: Config,
    dictionaries: dict,
    args: argparse.Namespace,
) -> RowResult:
    student_ext = row.get(config.match.student["external_id"])

    try:
        match = index.find(row, config.match.student)
    except StudentLookupError as exc:
        outcome = {"missing_dob": "skipped", "ambiguous": "ambiguous"}.get(exc.kind, "not_found")
        level = logging.WARNING
        _log.log(level, "row %d: %s — %s", row_number, outcome.upper(), exc)
        return RowResult(row_number, outcome, student_external_id=student_ext, warnings=[str(exc)])

    app_id = match.summary.get("id")
    if not app_id:
        _log.warning("row %d: NOT FOUND — list result has no 'id'", row_number)
        return RowResult(row_number, "not_found", student_external_id=student_ext)

    if match.path is MatchPath.NAME_FALLBACK:
        _log.info("row %d: matched by name+dob fallback (sheet ext_id=%r -> app ext_id=%r)",
                  row_number, student_ext, match.summary.get("external_id"))

    try:
        app = client.get_application(app_id)
    except Exception as exc:  # noqa: BLE001
        _log.error("row %d: ERROR fetching %s: %s", row_number, app_id, exc)
        return RowResult(row_number, "error", application_id=app_id,
                         student_external_id=student_ext, warnings=[f"fetch failed: {exc}"])

    changes = apply_changes(
        app, row, config, dictionaries,
        allow_swap=args.allow_parent_swap, in_place=True,
    )
    for warning in changes.warnings:
        _log.warning("row %d: %s", row_number, warning)

    result = RowResult(
        row_number=row_number,
        outcome="no_change",
        application_id=app_id,
        student_external_id=student_ext,
        match_path=match.path.value,
        diff=dict(changes.diff),
        warnings=list(changes.warnings),
    )

    if not changes.has_changes:
        _log.info("row %d: app_id=%s no changes", row_number, app_id)
        return result

    _log.info("row %d: app_id=%s student=%s", row_number, app_id, student_ext)
    for field_label, change in changes.diff.items():
        _log.info("    %s: %s", field_label, change)

    if args.live:
        try:
            client.update_application(app_id, app)
            result.outcome = "updated"
        except Exception as exc:  # noqa: BLE001
            _log.error("    ERROR on PUT: %s", exc)
            result.outcome = "error"
            result.warnings.append(f"PUT failed: {exc}")
    else:
        result.outcome = "updated"  # "would update" in dry-run
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")

    log_file = _resolve_log_file(args, timestamp)
    setup_logging(log_file)
    if log_file is not None:
        _log.info("Log file: %s", log_file)

    try:
        config = load_config(args.config_dir, religion_file=args.religion_config)
    except ConfigError as exc:
        _log.error("config error: %s", exc)
        return 2

    load_dotenv()
    if not os.getenv("ENROLHQ_API_TOKEN"):
        _log.error("ENROLHQ_API_TOKEN not set in environment / .env")
        return 2

    _log.info("Reading %s...", args.input)
    rows = excel.read_rows(args.input)
    _log.info("  %d data rows", len(rows))

    years = excel.entry_years(rows, config.match.student["entry_year"])
    _log.info("  entry years in sheet: %s", sorted(years))

    client = build_default_client()
    dictionaries = client.dictionaries()

    _log.info("Fetching existing applications...")
    summaries: list = []
    for year in sorted(years):
        _log.info("  Fetching applications for entry year %d...", year)
        summaries.extend(client.list_applications(entry_year=year))
    index = ApplicationIndex.build(summaries)
    _log.info("  indexed %d by external_id, %d by name+dob",
              len(index.by_external_id), len(index.by_name))

    mode = "LIVE" if args.live else "DRY-RUN"
    _log.info("\n=== %s ===\n", mode)

    report = RunReport(mode=mode, input_file=str(args.input))
    report.total_rows = len(rows)
    writes_attempted = 0

    for offset, row in enumerate(rows):
        row_number = offset + 2  # row 1 is the header
        result = _process_row(row_number, row, index, client, config, dictionaries, args)
        report.add(result)

        if result.outcome == "updated" and result.diff:
            writes_attempted += 1
            if args.limit is not None and writes_attempted >= args.limit:
                _log.info("Reached --limit %d (attempted %d write(s)); stopping.",
                          args.limit, writes_attempted)
                report.limit_hit = True
                break

    report.log_summary()
    if not args.no_report:
        report.write_json(Path("logs") / f"run-{timestamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
