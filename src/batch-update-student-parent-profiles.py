"""Batch-update student + parent profiles in EnrolHQ from an Excel sheet.

API contract (verified against docs/EnrolHQ API (v2).yaml)
----------------------------------------------------------

  * Update is **PUT** on `/api/v2/applications/{id}/`, body is an
    `ApplicationV2` object. PUT is full-replacement, so the safe pattern is:
        app = client.applications.get(app_id)   # full current object
        app["first_name"] = "..."               # mutate in place
        client.applications.update(app_id, app) # PUT the whole thing back

  * List endpoint is `/api/v2/applications-list/`. Filters used here:
      - entry_year       (integer)
      - has_external_id  (bool; we only update records the school owns)
    NB: there is no `external_id=` query filter on this endpoint, so we
    fetch all records for each entry year and index them client-side.

  * Parent shape on the application:
      - `user_parent`     — Parent One   (AdminParentV1, never null in practice)
      - `non_user_parent` — Parent Two   (AdminParentV1, nullable)
      - `guardians[]`     — additional guardians (not handled by this script)
    There is NO flat `parent2_*` field set; the previous assumption was wrong.

  * Addresses on the application are nested objects of schema `Address`:
      - `residential_address` (nullable)
      - `mailing_address`     (nullable)
    Fields: apartment, street_address, city, suburb, state, postcode,
    country (object {id}).

  * Fields that look obvious but are NOT directly settable here:
      - `email`, `Parent One Email`, `Parent Two Email` are READ-ONLY in
        AdminParentV1. Email changes go through a separate flow.
      - `application_status` is an integer enum (-1..22), not a string —
        until we add a label→code mapping, sheet values won't translate.
      - `current_school`, `home_language`, `born_country` accept only
        `{id: <fk>}`, so free-text columns can't be mapped without a
        reference-data lookup.
      - `gender`, `entry_grade`, `residency_status` are integer enums.

References
----------
  * Local OpenAPI spec: docs/EnrolHQ API (v2).yaml
  * SDK update example: https://github.com/team-and-systems-hq/enrolhq-python/blob/main/examples/04_update_application.py
  * SDK search example: https://github.com/team-and-systems-hq/enrolhq-python/blob/main/examples/02_search_and_filter.py

Run with `--dry-run` (default) to preview the diff per row without hitting
the API. Pass `--live` to actually send updates.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl
from dotenv import load_dotenv
from enrolhq import EnrolHQClient


INPUT_EXCEL_FILE = Path(__file__).parent / "input_spreadsheet.xlsx"


# ---------------------------------------------------------------------------
# Column -> API field mapping
# ---------------------------------------------------------------------------
# Comment out any line to stop syncing that column. Cells for unmapped
# columns are silently ignored.
#
# Section meanings:
#   "key"               primary-key column, never written; used for matching
#   "app"               top-level field on the application object
#   "addr:residential"  nested under app["residential_address"] (Address)
#   "addr:mailing"      nested under app["mailing_address"]     (Address)
#   "user_parent"       nested under app["user_parent"]      (AdminParentV1)
#   "non_user_parent"   nested under app["non_user_parent"]  (AdminParentV1)
#   "up_addr_res"       app["user_parent"]["residential_address"]
#   "up_addr_mail"      app["user_parent"]["mailing_address"]
#   "nup_addr_res"      app["non_user_parent"]["residential_address"]
#   "nup_addr_mail"     app["non_user_parent"]["mailing_address"]
#
# Anything left commented out is either an unmappable / unverified field
# (see the API contract notes at the top of this file) or a field that
# requires FK / enum translation we have not implemented yet.

STUDENT_PK = ("Student External Id 0A", "Student Dob 5F")
PARENT1_PK = ("Parent One External Id 22W", "Parent One Email 29AD")
PARENT2_PK = ("Parent Two External Id 40AO", "Parent Two Email 47AV")
ENTRY_YEAR_COL = "Student Entry Year 11L"

COLUMN_MAP: dict[str, tuple[str, str]] = {
    # --- keys (matching only, never written) ---
    "Student External Id 0A":        ("key", "student_external_id"),
    "Student Dob 5F":                ("key", "student_dob"),
    "Parent One External Id 22W":    ("key", "parent1_external_id"),
    "Parent One Email 29AD":         ("key", "parent1_email"),  # email is RO
    "Parent Two External Id 40AO":   ("key", "parent2_external_id"),
    "Parent Two Email 47AV":         ("key", "parent2_email"),  # email is RO

    # --- student (top-level on the application) ---
    "Student First Name 1B":         ("app", "first_name"),
    "Student Preferred Name 2C":     ("app", "preferred_name"),
    "Student Middle Name 3D":        ("app", "middle_name"),
    "Student Last Name 4E":          ("app", "last_name"),
    # "Student Gender 6G":           ("app", "gender"),  # integer enum 1..4 — needs translation
    "Student House 13N":             ("app", "house"),
    "Student Code 58BG":             ("app", "student_code"),
    "Student Parish 72BU":           ("app", "parish"),
    # "Student Campus 75BX":         ("app", "campus"),  # campus is a UUID FK
    # "Student Religion 71BT":      ("app", "religion"),  # free-text label, OK
    # "Student Residency Status 70BS": ("app", "residency_details.residency_status"),  # integer enum 0..3

    # --- student residential address (nested) ---
    "Student Residential Unit 14O":         ("addr:residential", "apartment"),
    "Student Residential Street One 15P":   ("addr:residential", "street_address"),
    "Student Residential City 78CA":        ("addr:residential", "city"),
    "Student Residential Suburb 16Q":       ("addr:residential", "suburb"),
    "Student Residential State 17R":        ("addr:residential", "state"),
    "Student Residential Postcode 18S":     ("addr:residential", "postcode"),
    # "Student Residential Country 19T":    ("addr:residential", "country"),  # country is {id: <fk>}
    "Student Mailing City 79CB":            ("addr:mailing", "city"),

    # --- current school ---
    # The API's `current_school` is an FK ({id: ...}). The free-text
    # columns below cannot be set without a reference-data lookup.
    # "Student Current School Name 67BP":   ("app", "current_school_other"),
    # "Student Current School Suburb 68BQ": (...),
    # "Student Current School State 69BR":  (...),

    # --- application-level metadata ---
    # "Attendance Type 7H":                 ("app", "attendance_type"),  # FK string/UUID — verify
    # "Status 8I":                          ("app", "application_status"),  # integer enum -1..22
    # "Sub Status 9J":                      ("app", "custom_status"),  # closest free-text equivalent
    # "Entry Grade 10K":                    ("app", "entry_grade"),  # integer enum -5..13
    # "Student Entry Year 11L":             ("app", "entry_year"),  # don't update — used for matching
    "First Contacted Date 66BO":            ("app", "first_contacted_date"),
    "Start Date 65BN":                      ("app", "commencement_date"),
    # The following columns have no direct top-level field on ApplicationV2.
    # They may live under `manual_submission_dates` (read-only) or `progress`
    # (read-only) and are surfaced via dedicated POST endpoints.
    # "EOI Submission Date 12M":            (...),
    # "Application Date 59BH":              (...),
    # "Manual Enquiry Date 76BY":           (...),
    # "Enrolment Offer Overridden Expiry Date 60BI":  (...),
    # "Enrolment Offer Manual Made Date 61BJ":        (...),
    # "Enrolment Offer Manual Accepted Date 62BK":    (...),
    # "Event Booking Manual Made Date 63BL":          (...),
    # "Interview Manual Completed Date 64BM":         (...),
    # "Interview Category 20U":             (...),  # array of UUIDs
    # "Parents Relationship 21V":           (...),  # array of UUIDs
    # "How Did You Hear About Us 73BV":     (...),  # lives on parent (`how_hear`)
    # "Student Lives With 77BZ":            ("app", "student_resides_with"),  # integer enum

    # --- Parent One (user_parent) ---
    "Parent One Title 23X":              ("user_parent", "title"),
    "Parent One First Name 24Y":         ("user_parent", "first_name"),
    "Parent One Preferred Name 25Z":     ("user_parent", "preferred_name"),
    "Parent One Last Name 26AA":         ("user_parent", "last_name"),
    "Parent One Occupation 28AC":        ("user_parent", "occupation"),
    "Parent One Mobile 30AE":            ("user_parent", "mobile_phone"),
    "Parent One Home Phone 31AF":        ("user_parent", "home_phone"),
    "Parent One Business Phone 32AG":    ("user_parent", "business_phone"),
    # "Parent One Email Preference 33AH":  ("user_parent", "email_preference"),  # UUID FK, not a label
    # "Parent One Relationship 27AB":    ("user_parent", "relationship_to_student"),  # integer enum

    # Parent One residential address
    "Parent One Apartment 34AI":         ("up_addr_res", "apartment"),
    "Parent One Street 35AJ":            ("up_addr_res", "street_address"),
    "Parent One City 80CC":              ("up_addr_res", "city"),
    "Parent One Suburb 36AK":            ("up_addr_res", "suburb"),
    "Parent One State 37AL":             ("up_addr_res", "state"),
    "Parent One Postcode 38AM":          ("up_addr_res", "postcode"),
    # "Parent One Country 39AN":         ("up_addr_res", "country"),  # FK object
    "Parent One Mailing City 81CD":      ("up_addr_mail", "city"),

    # Parent One EXTRA_* — school-required fields, mapped to standard
    # AdminParentV1 properties. (The EXTRA_ prefix is a source-side tag,
    # not an indicator of a custom field on EnrolHQ.)
    #
    # Enabled (safe free-text / date):
    "EXTRA_PARENT1_RELIGION":            ("user_parent", "religion"),
    "EXTRA_PARENT1_PLACE_OF_WORSHIP":    ("user_parent", "church_attended"),
    "EXTRA_PARENT1_BIRTHDATE":           ("user_parent", "dob"),
    #
    # Disabled until the source values can be translated to EnrolHQ's
    # enums / FKs (Edumate codes won't match EnrolHQ codes):
    # "EXTRA_PARENT1_HEARD_ABOUT_SCHOOL": ("user_parent", "how_hear_other"),     # free-text fallback if source is a label
    # "EXTRA_PARENT1_EDUCATION":          ("user_parent", "highest_school_year"), # int enum 1..4
    # "EXTRA_PARENT1_OCCUPATION_GROUP":   ("user_parent", "occupational_group"),  # int enum [1,2,3,4,8,9]
    # "EXTRA_PARENT1_LANG_AT_HOME":       ("user_parent", "home_language_other"), # free-text fallback (home_language is FK)
    # "EXTRA_PARENT1_NON_EDUCATION":      (...),                                  # ambiguous — confirm with school

    # --- Parent Two (non_user_parent) ---
    "Parent Two Title 41AP":             ("non_user_parent", "title"),
    "Parent Two First Name 42AQ":        ("non_user_parent", "first_name"),
    "Parent Two Preferred Name 43AR":    ("non_user_parent", "preferred_name"),
    "Parent Two Last Name 44AS":         ("non_user_parent", "last_name"),
    "Parent Two Occupation 46AU":        ("non_user_parent", "occupation"),
    "Parent Two Mobile 48AW":            ("non_user_parent", "mobile_phone"),
    "Parent Two Home Phone 49AX":        ("non_user_parent", "home_phone"),
    "Parent Two Business Phone 50AY":    ("non_user_parent", "business_phone"),
    # "Parent Two Email Preference 51AZ":  ("non_user_parent", "email_preference"),  # UUID FK, not a label
    # "Parent Two Relationship 45AT":    ("non_user_parent", "relationship_to_student"),

    # Parent Two residential address
    "Parent Two Apartment 52BA":         ("nup_addr_res", "apartment"),
    "Parent Two Street 53BB":            ("nup_addr_res", "street_address"),
    "Parent Two City 82CE":              ("nup_addr_res", "city"),
    "Parent Two Suburb 54BC":            ("nup_addr_res", "suburb"),
    "Parent Two State 55BD":             ("nup_addr_res", "state"),
    "Parent Two Postcode 56BE":          ("nup_addr_res", "postcode"),
    # "Parent Two Country 57BF":         ("nup_addr_res", "country"),  # FK object
    "Parent Two Mailing City 83CF":      ("nup_addr_mail", "city"),

    # Parent Two EXTRA_* — mirrors Parent One.
    "EXTRA_PARENT2_RELIGION":            ("non_user_parent", "religion"),
    "EXTRA_PARENT2_PLACE_OF_WORSHIP":    ("non_user_parent", "church_attended"),
    "EXTRA_PARENT2_BIRTHDATE":           ("non_user_parent", "dob"),
    # "EXTRA_PARENT2_HEARD_ABOUT_SCHOOL": ("non_user_parent", "how_hear_other"),
    # "EXTRA_PARENT2_EDUCATION":          ("non_user_parent", "highest_school_year"),
    # "EXTRA_PARENT2_OCCUPATION_GROUP":   ("non_user_parent", "occupational_group"),
    # "EXTRA_PARENT2_LANG_AT_HOME":       ("non_user_parent", "home_language_other"),
    # "EXTRA_PARENT2_NON_EDUCATION":      (...),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def _norm_dob(v: Any) -> str | None:
    n = _norm(v)
    return str(n) if n is not None else None


def read_rows(path: Path) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    headers = [str(h).strip() if h is not None else "" for h in next(rows_iter)]
    rows: list[dict[str, Any]] = []
    for row in rows_iter:
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        rows.append({h: v for h, v in zip(headers, row) if h})
    return rows


# ---------------------------------------------------------------------------
# EnrolHQ — fetch + index
# ---------------------------------------------------------------------------

def fetch_apps_by_year(
    client: EnrolHQClient, entry_years: set[int]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Fetch applications per entry year, indexed by (external_id, dob).

    list() returns lightweight summaries; we re-GET each record before
    PUT because the API does full-replacement and we need the entire
    object.
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for year in sorted(entry_years):
        print(f"  Fetching applications for entry year {year}...")
        for app in client.applications.list(entry_year=year, has_external_id=True):
            ext_id = app.get("external_id")
            dob = app.get("dob")
            if not ext_id or not dob:
                continue
            index[(str(ext_id).strip(), _norm_dob(dob) or "")] = app
    return index


# ---------------------------------------------------------------------------
# Update planner
# ---------------------------------------------------------------------------

# Section -> path inside the application object, expressed as a list of keys.
# A trailing "*" marks an intermediate dict that may be None on the existing
# application and should be created on demand.
SECTION_PATHS: dict[str, list[str]] = {
    "app":              [],
    "addr:residential": ["residential_address"],
    "addr:mailing":     ["mailing_address"],
    "user_parent":      ["user_parent"],
    "non_user_parent":  ["non_user_parent"],
    "up_addr_res":      ["user_parent", "residential_address"],
    "up_addr_mail":     ["user_parent", "mailing_address"],
    "nup_addr_res":     ["non_user_parent", "residential_address"],
    "nup_addr_mail":    ["non_user_parent", "mailing_address"],
}


def _get_container(app: dict[str, Any], path: list[str]) -> dict[str, Any] | None:
    """Walk `path` into `app`, creating empty dicts as needed.

    Returns the leaf dict, or None if a parent in the path is missing AND
    we shouldn't auto-create (currently always creates — change here if
    you want to refuse to materialise a missing nested object).
    """
    cur = app
    for key in path:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    return cur


def _check_parent_pk(
    sheet_ext_id: Any, app_ext_id: Any, slot_label: str
) -> tuple[bool, str | None]:
    sheet = _norm(sheet_ext_id)
    if sheet is None:
        return True, None
    app_id = _norm(app_ext_id)
    if app_id is None:
        return True, None
    if str(sheet).strip() == str(app_id).strip():
        return True, None
    return False, (
        f"{slot_label} external_id mismatch: "
        f"sheet={sheet!r} vs application={app_id!r} — skipping {slot_label}"
    )


def apply_row_to_app(
    app: dict[str, Any], row: dict[str, Any]
) -> tuple[dict[str, str], list[str]]:
    """Mutate `app` in place with non-empty values from `row`.

    Returns (diff, warnings).
    """
    diff: dict[str, str] = {}
    warnings: list[str] = []

    # Parent-PK guard. Both parent slots are AdminParentV1, with the
    # external id at app["user_parent"]["external_id"] and
    # app["non_user_parent"]["external_id"] respectively (per the
    # AdminParentV1 schema in docs/EnrolHQ API (v2).yaml).
    up_existing = app.get("user_parent") if isinstance(app.get("user_parent"), dict) else {}
    nup_existing = app.get("non_user_parent") if isinstance(app.get("non_user_parent"), dict) else {}
    p1_ok, p1_warn = _check_parent_pk(
        row.get(PARENT1_PK[0]),
        (up_existing or {}).get("external_id"),
        "Parent One",
    )
    if p1_warn:
        warnings.append(p1_warn)
    p2_ok, p2_warn = _check_parent_pk(
        row.get(PARENT2_PK[0]),
        (nup_existing or {}).get("external_id"),
        "Parent Two",
    )
    if p2_warn:
        warnings.append(p2_warn)

    parent_one_sections = {"user_parent", "up_addr_res", "up_addr_mail"}
    parent_two_sections = {"non_user_parent", "nup_addr_res", "nup_addr_mail"}

    for col, value in row.items():
        mapping = COLUMN_MAP.get(col)
        if not mapping:
            continue
        section, field = mapping
        if section == "key":
            continue
        new_val = _norm(value)
        if new_val is None:
            continue
        if section in parent_one_sections and not p1_ok:
            continue
        if section in parent_two_sections and not p2_ok:
            continue
        path = SECTION_PATHS.get(section)
        if path is None:
            warnings.append(f"unknown section {section!r} for column {col!r}")
            continue
        container = _get_container(app, path)
        if container is None:
            continue
        path_label = ".".join(path + [field]) if path else field
        if container.get(field) != new_val:
            diff[path_label] = f"{container.get(field)!r} -> {new_val!r}"
        container[field] = new_val

    return diff, warnings


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Actually call the API. Default is dry-run.")
    parser.add_argument("--input", type=Path, default=INPUT_EXCEL_FILE)
    args = parser.parse_args()

    load_dotenv()
    if not os.getenv("ENROLHQ_API_TOKEN"):
        print("ERROR: ENROLHQ_API_TOKEN not set in environment / .env", file=sys.stderr)
        return 2

    print(f"Reading {args.input}...")
    rows = read_rows(args.input)
    print(f"  {len(rows)} data rows")

    entry_years: set[int] = set()
    for row in rows:
        y = row.get(ENTRY_YEAR_COL)
        if y is None:
            continue
        try:
            entry_years.add(int(y))
        except (TypeError, ValueError):
            pass
    print(f"  entry years in sheet: {sorted(entry_years)}")

    client = EnrolHQClient()
    print("Fetching existing applications...")
    index = fetch_apps_by_year(client, entry_years)
    print(f"  indexed {len(index)} applications")

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"\n=== {mode} ===\n")

    updated = skipped = missing = nochange = errors = 0
    for i, row in enumerate(rows, start=2):  # row 1 is header
        ext_id = _norm(row.get(STUDENT_PK[0]))
        dob = _norm_dob(row.get(STUDENT_PK[1]))
        if not ext_id or not dob:
            print(f"row {i}: SKIP — missing student PK")
            skipped += 1
            continue

        summary = index.get((str(ext_id), dob))
        if summary is None:
            print(f"row {i}: NOT FOUND — student_external_id={ext_id} dob={dob}")
            missing += 1
            continue

        app_id = summary.get("id")
        if not app_id:
            print(f"row {i}: NOT FOUND — list result has no 'id'")
            missing += 1
            continue

        try:
            app = client.applications.get(app_id)
        except Exception as exc:  # noqa: BLE001
            print(f"row {i}: ERROR fetching {app_id}: {exc}")
            errors += 1
            continue

        diff, warnings = apply_row_to_app(app, row)
        for w in warnings:
            print(f"row {i}: WARNING — {w}")
        if not diff:
            print(f"row {i}: app_id={app_id} no changes")
            nochange += 1
            continue

        print(f"row {i}: app_id={app_id} student={ext_id}")
        for fld, change in diff.items():
            print(f"    {fld}: {change}")

        if args.live:
            try:
                client.applications.update(app_id, app)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR on PUT: {exc}")
                errors += 1
        else:
            updated += 1

    print(
        f"\nDone. updated={updated} no_change={nochange} "
        f"missing={missing} skipped={skipped} errors={errors}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
