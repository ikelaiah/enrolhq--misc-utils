# enrolhq--misc-utils

Utilities for writing student/parent enrichment fields back to
[EnrolHQ](https://enrolhq.com.au/) from an Excel export, via its Python SDK.

The core is the **`enrolhq_sync`** package (`src/enrolhq_sync/`): a
config-driven, unit-tested batch updater. School-specific data (which columns
to sync, the religion allowlist, the enum/label maps) lives in
[`config/*.toml`](config/) — not in code — so the same tool serves multiple
schools by swapping config files.

## Input template

The input spreadsheet is the **2025 EnrolHQ Import Template**, whose `EXTRA_`
columns hold the enrichment values this sync writes/updates back into EnrolHQ.
Row and parent matching, however, follows the **2026 EnrolHQ Import Template**
column layout — it matches on **name and email** (plus external id and mobile)
using the 2026 template's header names. The column names referenced throughout
[`config/`](config/) reflect that mix: `EXTRA_*` write targets from the 2025
template, and matching keys from the 2026 template.

## Why a package (and not the old single script)

The previous implementation was one ~1,450-line file. This version splits the
concerns so each piece is testable and replaceable:

| Module | Responsibility |
|--------|----------------|
| [`config.py`](src/enrolhq_sync/config.py)       | Load + type the `config/*.toml` files |
| [`normalize.py`](src/enrolhq_sync/normalize.py) | Pure value/name/email/phone normalizers |
| [`excel.py`](src/enrolhq_sync/excel.py)         | Read the spreadsheet into rows |
| [`matching.py`](src/enrolhq_sync/matching.py)   | Student index + 2-strategy match; parent-slot resolution |
| [`transforms.py`](src/enrolhq_sync/transforms.py) | Registry of field transforms (enum/fk/compound) |
| [`planner.py`](src/enrolhq_sync/planner.py)     | Pure `apply_changes()` → `ChangeSet` (no I/O) |
| [`client.py`](src/enrolhq_sync/client.py)       | SDK wrapper: retry/backoff + dictionary cache |
| [`reporting.py`](src/enrolhq_sync/reporting.py) | Console/file logging + JSON run report |
| [`cli.py`](src/enrolhq_sync/cli.py)             | Argument parsing + the per-row loop (the only I/O) |

The planner is pure: it mutates a copy and returns a `ChangeSet`, so the whole
matching/translation core is tested with **no network and no mocks**
(`tests/`, run with `pytest`).

## How it works

EnrolHQ's API is **PUT (full replacement)**, not PATCH. To do a partial update
the tool GETs the full application, mutates only the mapped fields whose Excel
cells are non-empty, then PUTs the whole object back. Empty cells are skipped,
so existing values survive the round-trip.

1. Read every data row from the input spreadsheet.
2. Collect the distinct `Student Entry Year`s and `list()` applications once
   per year (`has_external_id=True`) to build a client-side index.
3. For each row: resolve it to one application, GET the full object, run the
   planner, and (in `--live`) PUT it back.

### Matching (resilient to rewritten IDs)

External IDs in EnrolHQ are **not stable** — integration jobs rewrite them — so
matching never trusts one identifier:

- **Student:** `(external_id, dob)` first, then `(last, first, middle, dob)`
  fallback. A fallback that hits more than one record is reported **AMBIGUOUS**
  and skipped (twins won't share all four parts).
- **Parent slot:** Parent One → `user_parent`, Parent Two → `non_user_parent`,
  matched by **ANY** of external_id / email / mobile / (first, last). A detected
  cross-slot swap is **refused** by default, or cross-mapped with
  `--allow-parent-swap`.

The columns each strategy reads are configured in
[`config/matching.toml`](config/matching.toml).

## Configuring what gets synced

Everything is data. Edit the TOML; no code change needed.

- **[`config/column_map.toml`](config/column_map.toml)** — one `[[column]]`
  block per synced field: `header`, `section`, `field`, optional `transform`.
  Comment out a block to stop syncing that column. Anything not listed is
  ignored. **Don't** remove the `section = "key"` blocks — they drive matching.
- **[`config/enum_maps.toml`](config/enum_maps.toml)** — label→code tables for
  the integer-enum fields (indigenous, education, degree, occupation group).
- **[`config/religion.sample.toml`](config/religion.sample.toml)** — a
  **generic example** religion allowlist + aliases (an invented list, not any
  real school's). `religion` is validated server-side; an invalid value 400s
  the whole PUT, so the tool only sends allowlisted values and skips the rest
  with a warning. There is no API for the dropdown, so to use this for real:
  open the school's EnrolHQ admin UI, transcribe its religion dropdown into
  `config/religion.<school>.toml`, and pass `--religion-config`. Those
  per-school files are **git-ignored** so real client lists are never
  committed — only the example ships.

Transforms available to `column_map.toml`:

| `transform` | Effect |
|-------------|--------|
| *(omitted)* | plain free-text / date passthrough |
| `enum:<table>` / `enum_array:<table>` | label → int / `[int]` via `enum_maps.toml` |
| `fk:<dict>` / `fk_array:<dict>` | label → `{id}` / `[{id}]` via a reference dictionary |
| `religion` | school-allowlisted choice (skips invalid values) |
| `home_language` | compound `home_language` + `is_speak_other_language` gate |
| `how_hear` | free-text "Other", deduped against the structured `how_hear` list |

### Scope: EXTRA_* only

By design the column map only writes the `EXTRA_*` enrichment fields (religion,
place of worship, education, occupation group, indigenous status, country of
birth, nationality, home language, heard-about). Names, addresses, and phones
are intentionally **not** mapped, so a re-run cannot clobber data the school
maintains elsewhere. Re-add a `[[column]]` block to sync more.

Parent DOB is deliberately omitted: de-scoped by the client and disabled
instance-wide in EnrolHQ (locked parents silently drop the write).

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

## Usage

```powershell
# Dry-run (default) — prints the per-row diff + summary, writes a JSON report,
# makes NO changes.
python -m enrolhq_sync

# Live run — actually sends PUTs.
python -m enrolhq_sync --live

# Single-record canary before a full live run.
python -m enrolhq_sync --live --limit 1

# Other flags
python -m enrolhq_sync --input path/to/file.xlsx
python -m enrolhq_sync --allow-parent-swap
python -m enrolhq_sync --religion-config religion.othrschool.toml
python -m enrolhq_sync --no-log-file --no-report
```

The original script path still works as a thin shim:
`python src/batch-update-student-parent-profiles.py --live`.

### Output

- **Console + `logs/run-<ts>.log`** — human-readable per-row diff, warnings,
  and an end-of-run summary (outcomes, match paths, parent-slot/translation
  issue counts).
- **`logs/run-<ts>.json`** — the same data, machine-readable: per-row outcome,
  diff, warnings, and match path, plus the summary. Diffable across runs;
  suppress with `--no-report`.

### Inspecting a record

```powershell
python src/inspect_application.py <application-uuid>
```

Read-only dump of the EXTRA_*-relevant fields (including `is_name_editable`, to
spot a locked parent slot).

## Tests

The pure layers (normalize, transforms, matching, planner) are covered by a
pytest suite with no network:

```powershell
pytest
```

## Caveats

Mappings are verified against the local OpenAPI spec at
[docs/EnrolHQ API (v2).yaml](docs/EnrolHQ%20API%20%28v2%29.yaml) (`ApplicationV2`
/ `AdminParentV1` / `Address`). Notable points:

- **Parent Two is `non_user_parent`**, not flat `parent2_*` fields.
- **Addresses are nested `Address` objects** (`apartment`, `street_address`,
  `city`, `suburb`, `state`, `postcode`, `country`).
- **`Parent One/Two Email` are read-only** on `AdminParentV1` — kept as `key`
  columns for matching, never written.
- **`religion` is server-validated** per school against a dropdown with no API;
  transcribe it from the EnrolHQ UI into a git-ignored
  `config/religion.<school>.toml` and refresh it if writes start getting skipped.
- **No `external_id` list filter** on `/applications-list/`, so applications are
  fetched per entry year and indexed client-side.

## Resources

- [EnrolHQ Python SDK](https://github.com/team-and-systems-hq/enrolhq-python)
- [EnrolHQ API docs (Swagger)](https://demo.enrolhq.com.au/api-docs/swagger/)
