"""Batch-update student + parent profiles in EnrolHQ from an Excel sheet.

Lookup strategy:
  1. Read all rows from the Excel file.
  2. For each unique `Student Entry Year 11L` in the sheet, call
     `client.applications.list(entry_year=YYYY)` once and index the results
     by (Student External Id, Student DOB) and by parent (External Id, Email).
  3. For each row, find the matching application and PATCH only the non-empty
     fields.

Run with `--dry-run` (default) to preview the payloads without hitting the API.
Pass `--live` to actually send updates.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
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
# Keys are the exact Excel column headers (row 1 of the sheet).
# Values are the API field paths to update. The path is split into a
# "section" (student / parent1 / parent2) and a "field" name. Section
# `key` marks primary-key columns used only for matching, not for update.

STUDENT_PK = ("Student External Id 0A", "Student Dob 5F")
PARENT1_PK = ("Parent One External Id 22W", "Parent One Email 29AD")
PARENT2_PK = ("Parent Two External Id 40AO", "Parent Two Email 47AV")
ENTRY_YEAR_COL = "Student Entry Year 11L"

# section, api_field. section in {"student", "parent1", "parent2", "application", "key"}
COLUMN_MAP: dict[str, tuple[str, str]] = {
    # --- keys (not updated, used for matching) ---
    "Student External Id 0A":        ("key", "student_external_id"),
    "Student Dob 5F":                ("key", "student_dob"),
    "Parent One External Id 22W":    ("key", "parent1_external_id"),
    "Parent One Email 29AD":         ("key", "parent1_email"),
    "Parent Two External Id 40AO":   ("key", "parent2_external_id"),
    "Parent Two Email 47AV":         ("key", "parent2_email"),

    # --- student ---
    "Student First Name 1B":         ("student", "first_name"),
    "Student Preferred Name 2C":     ("student", "preferred_name"),
    "Student Middle Name 3D":        ("student", "middle_name"),
    "Student Last Name 4E":          ("student", "last_name"),
    "Student Gender 6G":             ("student", "gender"),
    "Student House 13N":             ("student", "house"),
    "Student Residential Unit 14O":           ("student", "residential_unit"),
    "Student Residential Street One 15P":     ("student", "residential_street"),
    "Student Residential Suburb 16Q":         ("student", "residential_suburb"),
    "Student Residential State 17R":          ("student", "residential_state"),
    "Student Residential Postcode 18S":       ("student", "residential_postcode"),
    "Student Residential Country 19T":        ("student", "residential_country"),
    "Student Residential City 78CA":          ("student", "residential_city"),
    "Student Mailing City 79CB":              ("student", "mailing_city"),
    "Student Current School Name 67BP":       ("student", "current_school_name"),
    "Student Current School Suburb 68BQ":     ("student", "current_school_suburb"),
    "Student Current School State 69BR":      ("student", "current_school_state"),
    "Student Residency Status 70BS":          ("student", "residency_status"),
    "Student Religion 71BT":                  ("student", "religion"),
    "Student Parish 72BU":                    ("student", "parish"),
    "Student Lives With 77BZ":                ("student", "lives_with"),
    "Student Code 58BG":                      ("student", "student_code"),
    "Student Campus 75BX":                    ("student", "campus"),
    "EXTRA_STUDENT_LANG_AT_HOME":             ("student", "language_at_home"),
    "EXTRA_STUDENT_COUNTRY_OF_BIRTH":         ("student", "country_of_birth"),
    "EXTRA_STUDENT_NATIONALITY":              ("student", "nationality"),
    "EXTRA_STUDENT_INDIGENOUS":               ("student", "indigenous"),

    # --- application-level ---
    "Attendance Type 7H":                 ("application", "attendance_type"),
    "Status 8I":                          ("application", "status"),
    "Sub Status 9J":                      ("application", "sub_status"),
    "Entry Grade 10K":                    ("application", "entry_grade"),
    "Student Entry Year 11L":             ("application", "entry_year"),
    "EOI Submission Date 12M":            ("application", "eoi_submission_date"),
    "Interview Category 20U":             ("application", "interview_category"),
    "Parents Relationship 21V":           ("application", "parents_relationship"),
    "Application Date 59BH":              ("application", "application_date"),
    "Start Date 65BN":                    ("application", "start_date"),
    "First Contacted Date 66BO":          ("application", "first_contacted_date"),
    "Manual Enquiry Date 76BY":           ("application", "manual_enquiry_date"),
    "How Did You Hear About Us 73BV":     ("application", "how_did_you_hear"),
    "Enrolment Offer Overridden Expiry Date 60BI": ("application", "enrolment_offer_expiry_date"),
    "Enrolment Offer Manual Made Date 61BJ":       ("application", "enrolment_offer_made_date"),
    "Enrolment Offer Manual Accepted Date 62BK":   ("application", "enrolment_offer_accepted_date"),
    "Event Booking Manual Made Date 63BL":         ("application", "event_booking_made_date"),
    "Interview Manual Completed Date 64BM":        ("application", "interview_completed_date"),

    # --- parent 1 ---
    "Parent One Title 23X":              ("parent1", "title"),
    "Parent One First Name 24Y":         ("parent1", "first_name"),
    "Parent One Preferred Name 25Z":     ("parent1", "preferred_name"),
    "Parent One Last Name 26AA":         ("parent1", "last_name"),
    "Parent One Relationship 27AB":      ("parent1", "relationship"),
    "Parent One Occupation 28AC":        ("parent1", "occupation"),
    "Parent One Mobile 30AE":            ("parent1", "mobile"),
    "Parent One Home Phone 31AF":        ("parent1", "home_phone"),
    "Parent One Business Phone 32AG":    ("parent1", "business_phone"),
    "Parent One Email Preference 33AH":  ("parent1", "email_preference"),
    "Parent One Apartment 34AI":         ("parent1", "apartment"),
    "Parent One Street 35AJ":            ("parent1", "street"),
    "Parent One Suburb 36AK":            ("parent1", "suburb"),
    "Parent One State 37AL":             ("parent1", "state"),
    "Parent One Postcode 38AM":          ("parent1", "postcode"),
    "Parent One Country 39AN":           ("parent1", "country"),
    "Parent One City 80CC":              ("parent1", "city"),
    "Parent One Mailing City 81CD":      ("parent1", "mailing_city"),
    "EXTRA_PARENT1_RELIGION":            ("parent1", "religion"),
    "EXTRA_PARENT1_PLACE_OF_WORSHIP":    ("parent1", "place_of_worship"),
    "EXTRA_PARENT1_BIRTHDATE":           ("parent1", "birthdate"),
    "EXTRA_PARENT1_HEARD_ABOUT_SCHOOL":  ("parent1", "heard_about_school"),
    "EXTRA_PARENT1_EDUCATION":           ("parent1", "education"),
    "EXTRA_PARENT1_NON_EDUCATION":       ("parent1", "non_education"),
    "EXTRA_PARENT1_OCCUPATION_GROUP":    ("parent1", "occupation_group"),
    "EXTRA_PARENT1_LANG_AT_HOME":        ("parent1", "language_at_home"),

    # --- parent 2 ---
    "Parent Two Title 41AP":             ("parent2", "title"),
    "Parent Two First Name 42AQ":        ("parent2", "first_name"),
    "Parent Two Preferred Name 43AR":    ("parent2", "preferred_name"),
    "Parent Two Last Name 44AS":         ("parent2", "last_name"),
    "Parent Two Relationship 45AT":      ("parent2", "relationship"),
    "Parent Two Occupation 46AU":        ("parent2", "occupation"),
    "Parent Two Mobile 48AW":            ("parent2", "mobile"),
    "Parent Two Home Phone 49AX":        ("parent2", "home_phone"),
    "Parent Two Business Phone 50AY":    ("parent2", "business_phone"),
    "Parent Two Email Preference 51AZ":  ("parent2", "email_preference"),
    "Parent Two Apartment 52BA":         ("parent2", "apartment"),
    "Parent Two Street 53BB":            ("parent2", "street"),
    "Parent Two Suburb 54BC":            ("parent2", "suburb"),
    "Parent Two State 55BD":             ("parent2", "state"),
    "Parent Two Postcode 56BE":          ("parent2", "postcode"),
    "Parent Two Country 57BF":           ("parent2", "country"),
    "Parent Two City 82CE":              ("parent2", "city"),
    "Parent Two Mailing City 83CF":      ("parent2", "mailing_city"),
    "EXTRA_PARENT2_RELIGION":            ("parent2", "religion"),
    "EXTRA_PARENT2_PLACE_OF_WORSHIP":    ("parent2", "place_of_worship"),
    "EXTRA_PARENT2_BIRTHDATE":           ("parent2", "birthdate"),
    "EXTRA_PARENT2_HEARD_ABOUT_SCHOOL":  ("parent2", "heard_about_school"),
    "EXTRA_PARENT2_EDUCATION":           ("parent2", "education"),
    "EXTRA_PARENT2_NON_EDUCATION":       ("parent2", "non_education"),
    "EXTRA_PARENT2_OCCUPATION_GROUP":    ("parent2", "occupation_group"),
    "EXTRA_PARENT2_LANG_AT_HOME":        ("parent2", "language_at_home"),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    """Normalise an Excel cell value for comparison or transport."""
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
    """Normalise a DOB for matching — always ISO YYYY-MM-DD string."""
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


def build_payload(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Convert a row into a section-bucketed payload, skipping empty cells."""
    payload: dict[str, dict[str, Any]] = defaultdict(dict)
    for col, value in row.items():
        mapping = COLUMN_MAP.get(col)
        if not mapping:
            continue
        section, field = mapping
        if section == "key":
            continue
        norm = _norm(value)
        if norm is None:
            continue
        payload[section][field] = norm
    return dict(payload)


# ---------------------------------------------------------------------------
# EnrolHQ lookup
# ---------------------------------------------------------------------------

def fetch_apps_by_year(client: EnrolHQClient, entry_years: set[int]) -> dict[tuple[str, str], Any]:
    """Fetch applications for each entry year and index by (student_external_id, dob).

    The exact attribute names on the returned objects depend on the SDK; we try
    a few common candidates so this works whether the SDK returns plain dicts
    or model objects.
    """
    index: dict[tuple[str, str], Any] = {}
    for year in entry_years:
        print(f"  Fetching applications for entry year {year}...")
        apps = client.applications.list(entry_year=year)
        for app in apps:
            ext_id = _get(app, "external_id", "student_external_id")
            dob = _get(app, "dob", "date_of_birth", "student_dob")
            if ext_id is None or dob is None:
                continue
            key = (str(ext_id).strip(), _norm_dob(dob) or "")
            index[key] = app
    return index


def _get(obj: Any, *names: str) -> Any:
    """Attribute or dict-key getter trying multiple names."""
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] is not None:
                return obj[n]
            # also try nested student dict
            student = obj.get("student")
            if isinstance(student, dict) and n in student and student[n] is not None:
                return student[n]
        return None
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    student = getattr(obj, "student", None)
    if student is not None:
        for n in names:
            if hasattr(student, n):
                v = getattr(student, n)
                if v is not None:
                    return v
    return None


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
        year = row.get(ENTRY_YEAR_COL)
        if year is None:
            continue
        try:
            entry_years.add(int(year))
        except (TypeError, ValueError):
            pass
    print(f"  entry years in sheet: {sorted(entry_years)}")

    client = EnrolHQClient()
    print("Fetching existing applications...")
    index = fetch_apps_by_year(client, entry_years)
    print(f"  indexed {len(index)} applications")

    mode = "LIVE" if args.live else "DRY-RUN"
    print(f"\n=== {mode} ===\n")

    updated = skipped = missing = 0
    for i, row in enumerate(rows, start=2):  # row 1 is header
        ext_id = _norm(row.get(STUDENT_PK[0]))
        dob = _norm_dob(row.get(STUDENT_PK[1]))
        if not ext_id or not dob:
            print(f"row {i}: SKIP — missing student PK")
            skipped += 1
            continue

        app = index.get((str(ext_id), dob))
        if app is None:
            print(f"row {i}: NOT FOUND — student_external_id={ext_id} dob={dob}")
            missing += 1
            continue

        payload = build_payload(row)
        if not payload:
            print(f"row {i}: nothing to update")
            skipped += 1
            continue

        app_id = _get(app, "id", "application_id")
        print(f"row {i}: app_id={app_id} student={ext_id} sections={list(payload)}")
        for section, fields in payload.items():
            print(f"    {section}: {fields}")

        if args.live:
            try:
                client.applications.update(app_id, **payload)
                updated += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ERROR: {exc}")
        else:
            updated += 1

    print(f"\nDone. updated={updated} skipped={skipped} missing={missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
