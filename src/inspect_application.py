"""Dump one EnrolHQ application's EXTRA_*-relevant fields to the console.

A read-only debugging companion to the ``enrolhq_sync`` package. Given an
application UUID, it GETs the full record and prints the exact fields the batch
sync writes — so you can eyeball what EnrolHQ holds before/after a
``--live --limit 1`` canary, confirm a value landed, or check whether a parent
slot is locked (``is_name_editable``).

It makes no changes — only ``client.applications.get()``.

Usage
-----
    python src/inspect_application.py <application-uuid>

The UUID is the plain id from the EnrolHQ app URL or the batch sync's dry-run
log. Requires ``.env`` with ``ENROLHQ_API_TOKEN`` (and optionally
``ENROLHQ_BASE_URL``), same as the batch sync.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

# Student-level EXTRA_* targets (top-level fields on the application).
STUDENT_FIELDS = (
    "first_name", "last_name", "born_country", "nationalities",
    "aboriginal_statuses", "home_language", "is_speak_other_language",
)

# Parent-level EXTRA_* targets. `is_name_editable` is included because a
# locked/synced parent (False) silently drops writes.
PARENT_FIELDS = (
    "religion", "church_attended", "how_hear", "how_hear_other",
    "highest_school_year", "degree", "occupational_group",
    "home_language", "is_speak_other_language", "is_name_editable",
)


def dump(app: dict) -> None:
    print("=== STUDENT-LEVEL ===")
    for key in STUDENT_FIELDS:
        print(f"  {key:30s} {app.get(key)!r}")

    for slot in ("user_parent", "non_user_parent"):
        parent = app.get(slot) or {}
        print(f"=== {slot} ({parent.get('first_name')} {parent.get('last_name')}) ===")
        for key in PARENT_FIELDS:
            print(f"  {key:30s} {parent.get(key)!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump an EnrolHQ application's EXTRA_* fields.")
    parser.add_argument("application_id", help="Application UUID to inspect.")
    args = parser.parse_args(argv)

    load_dotenv()
    from enrolhq import EnrolHQClient

    client = EnrolHQClient()
    dump(client.applications.get(args.application_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
