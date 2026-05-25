# enrolhq--misc-utils

Miscellaneous utility scripts for working with [EnrolHQ](https://enrolhq.com.au/) via its Python SDK.

## Scripts

### `src/batch-update-student-parent-profiles.py`

Batch-update student, parent, and application fields in EnrolHQ from an Excel sheet based on the EnrolHQ Import Template 2026.

#### How it works

1. Reads every data row from `src/input_spreadsheet.xlsx`.
2. Collects the unique values of `Student Entry Year 11L` in the sheet and calls `client.applications.list(entry_year=YYYY)` once per year to build an in-memory index keyed by `(Student External Id 0A, Student Dob 5F)`.
3. For each row, looks up the matching application and builds a payload bucketed into `student`, `parent1`, `parent2`, and `application` sections. **Empty cells are skipped** so existing values are preserved (partial update).
4. Calls `client.applications.update(app_id, **payload)`.

#### Primary keys used for matching

| Entity   | Primary keys                                              |
|----------|-----------------------------------------------------------|
| Student  | `Student External Id 0A`, `Student Dob 5F`                |
| Parent 1 | `Parent One External Id 22W`, `Parent One Email 29AD`     |
| Parent 2 | `Parent Two External Id 40AO`, `Parent Two Email 47AV`    |

`Student Entry Year 11L` is used to scope the initial `applications.list()` fetches.

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
# "Student Middle Name 3D":        ("student", "middle_name"),
# "Parent One Occupation 28AC":    ("parent1", "occupation"),
```

Any column not present in `COLUMN_MAP` is silently ignored — the cell's value in the spreadsheet doesn't matter.

**Do not comment out** the six `("key", ...)` rows or `ENTRY_YEAR_COL` — they drive the lookup, not the update payload, and removing them will break student matching. They are already excluded from the update payload by `build_payload()`.

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

- The `COLUMN_MAP` API field names (e.g. `first_name`, `residential_suburb`) are best-guess mappings. The [Swagger UI](https://demo.enrolhq.com.au/api-docs/swagger/) is JS-rendered, so the exact schema could not be inspected during scaffolding. After your first dry-run, verify a couple of field names against a real `client.applications.get()` response and adjust the right-hand side of the mapping in [src/batch-update-student-parent-profiles.py](src/batch-update-student-parent-profiles.py) as needed.
- The `client.applications.update()` call shape (`update(app_id, **payload)` with section keys `student` / `parent1` / `parent2` / `application`) is also a guess. If the SDK expects a single flat dict or a different nested shape, the bucketing in `build_payload()` is the only place that needs to change.
- Parent matching by `(External Id, Email)` is captured in the column map as `key` rows, but the script currently only matches the **student**. Once an application is found, its parents are updated in place — there is no guard that the sheet's Parent One actually corresponds to the application's Parent One. Add an explicit PK check there if your data may have parent-slot mismatches.

## Resources

- [EnrolHQ Python SDK](https://github.com/team-and-systems-hq/enrolhq-python)
- [EnrolHQ API docs (Swagger)](https://demo.enrolhq.com.au/api-docs/swagger/)
