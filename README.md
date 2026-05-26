# enrolhq--misc-utils

Miscellaneous utility scripts for working with [EnrolHQ](https://enrolhq.com.au/) via its Python SDK.

## Scripts

### `src/batch-update-student-parent-profiles.py`

Batch-update student, parent, and application fields in EnrolHQ from an Excel sheet based on the EnrolHQ Import Template 2026.

#### How it works

The EnrolHQ API uses **PUT (full replacement)**, not PATCH — see the SDK's [update example](https://github.com/team-and-systems-hq/enrolhq-python/blob/main/examples/04_update_application.py). To do a "partial" update you must GET the full object, mutate the fields you care about, then PUT the whole thing back.

1. Reads every data row from `src/input_spreadsheet.xlsx`.
2. Collects the unique values of `Student Entry Year 11L` and calls `client.applications.list(entry_year=YYYY, has_external_id=True)` once per year to build a summary index keyed by `(external_id, dob)`.
3. For each row: finds the summary, calls `client.applications.get(app_id)` for the full object, mutates only the fields named in `COLUMN_MAP` whose Excel cells are non-empty, then calls `client.applications.update(app_id, app)` with the full mutated object.
4. **Empty cells are skipped** — the existing value is preserved across the PUT because we only mutate what the sheet provides.

#### Primary keys used for matching

| Entity   | Primary keys                                              |
|----------|-----------------------------------------------------------|
| Student  | `Student External Id 0A`, `Student Dob 5F`                |
| Parent 1 | `Parent One External Id 22W`, `Parent One Email 29AD`     |
| Parent 2 | `Parent Two External Id 40AO`, `Parent Two Email 47AV`    |

`Student Entry Year 11L` is used to scope the initial `applications.list()` fetches.

#### Parent semantics — sheet-driven, with a slot-mismatch guard

- A parent (matched by Parent One/Two External Id) can sit on many sibling applications. The script does **not** fan parent edits out across siblings — it only touches the application matched by the row's `(Student External Id, Student Dob)`.
- Before writing to `user_parent` (Parent One) or `non_user_parent` (Parent Two), `_check_parent_pk()` verifies the sheet's Parent External Id matches `app["user_parent"]["external_id"]` / `app["non_user_parent"]["external_id"]`. If they disagree, that parent slot (and its address sub-sections) is skipped for the row and a `WARNING:` line is printed. Student + application-level fields still update.
- Empty Parent External Id in the sheet means "no check" — the parent edits go through (treated as the sheet just doesn't supply that key).
- If you want stale parent data on a sibling's application kept in sync, re-export and re-run with that sibling's row in the sheet.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and set your token:

```dotenv
ENROLHQ_API_TOKEN=your_token_here
ENROLHQ_BASE_URL=https://yourschool.enrolhq.com.au/api/v2/
```

Place your filled-in spreadsheet at `src/input_spreadsheet.xlsx`.

## Choosing which columns to sync

The `COLUMN_MAP` dict near the top of [src/batch-update-student-parent-profiles.py](src/batch-update-student-parent-profiles.py) controls which columns get pushed to the API. To stop syncing a column, simply **comment it out** (or delete the line):

```python
# "Student Middle Name 3D":        ("app", "middle_name"),
# "Parent One Occupation 28AC":    ("user_parent", "occupation"),
```

Any column not present in `COLUMN_MAP` is silently ignored — the cell's value in the spreadsheet doesn't matter.

**Do not comment out** the six `("key", ...)` rows or `ENTRY_YEAR_COL` — they drive the lookup, not the update payload, and removing them will break student matching. They are already excluded from the update payload by `apply_row_to_app()`.

## Usage

```powershell
# Dry-run (default) — prints payloads without calling the API
python src/batch-update-student-parent-profiles.py

# Live run — actually sends updates
python src/batch-update-student-parent-profiles.py --live

# Custom input file
python src/batch-update-student-parent-profiles.py --input path/to/file.xlsx
```

## Caveats

The `COLUMN_MAP` has been verified against the local OpenAPI spec at [docs/EnrolHQ API (v2).yaml](docs/EnrolHQ%20API%20%28v2%29.yaml) (schema `ApplicationV2` / `AdminParentV1` / `Address`). Things to be aware of:

- **Parent Two is `non_user_parent`**, not flat `parent2_*` fields. The script writes parent updates into `app["user_parent"]` (Parent One) and `app["non_user_parent"]` (Parent Two).
- **Addresses are nested `Address` objects** with keys `apartment`, `street_address`, `city`, `suburb`, `state`, `postcode`, `country`. The sheet's residential/mailing/parent address columns are written into `residential_address` / `mailing_address` / `user_parent.residential_address` / `non_user_parent.residential_address` etc.
- **Read-only fields:** `Parent One Email` and `Parent Two Email` are read-only on `AdminParentV1`. They are kept in `COLUMN_MAP` as `"key"` entries (matching only) and are NEVER written. Email changes go through a different EnrolHQ flow.
- **Fields left commented out in `COLUMN_MAP`** require translation we have not implemented yet, namely:
  - **Integer enums:** `Status 8I` (application_status), `Student Gender 6G`, `Entry Grade 10K`, `Student Residency Status 70BS`, `Student Lives With 77BZ`, `Parent One/Two Relationship`. Need a label→code map to be safely written.
  - **Foreign-key objects:** `Student Campus 75BX`, `Student Residential/Parent Country`, `Student Current School Name`, `EXTRA_*_LANG_AT_HOME`. The API accepts `{id: <uuid>}` for these, so a reference-data lookup is required.
  - **No direct top-level home:** `EOI Submission Date`, `Application Date`, `Manual Enquiry Date`, the various `Enrolment Offer / Event Booking / Interview ...Manual Made/Accepted Date` columns. These live under read-only sub-objects (`manual_submission_dates`, `progress`) and are normally set via dedicated POST actions, not via PUT on the application.
  - **EXTRA\_PARENT*\_** columns are school-required values pulled from Edumate (the `EXTRA_` prefix is a source-side tag, not an EnrolHQ custom field). The plain-string ones — `RELIGION`, `PLACE_OF_WORSHIP`, `BIRTHDATE` — are **enabled**, mapped to `AdminParentV1.religion`, `church_attended`, and `dob` respectively. The rest (`HEARD_ABOUT_SCHOOL`, `EDUCATION`, `OCCUPATION_GROUP`, `LANG_AT_HOME`, `NON_EDUCATION`) are integer enums / foreign-key objects in EnrolHQ whose codes do not match Edumate's, so they are left commented out until a per-field Edumate→EnrolHQ translation is added. Suggested target field is noted next to each commented line.
- **Sub-status:** `Sub Status 9J` has no direct equivalent on `ApplicationV2`; the closest is `custom_status` (free-text). Left commented out so you can decide.
- **No `external_id` list filter:** the `/applications-list/` endpoint accepts `has_external_id=True` but not `external_id=<value>`, so the script fetches all applications for each entry year and indexes them client-side.

## Resources

- [EnrolHQ Python SDK](https://github.com/team-and-systems-hq/enrolhq-python)
- [EnrolHQ API docs (Swagger)](https://demo.enrolhq.com.au/api-docs/swagger/)
