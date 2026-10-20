#!/usr/bin/env python3
"""
Sync Canvas assignments to a Trello board.

- One Trello list per Canvas course (created if missing)
- One Trello label per course, named after the course code (created if missing)
- One card per assignment, with the due date and a link back to Canvas
- Never creates duplicates (each card's description holds a hidden Canvas ID marker)
- Marks a card's due date complete once the assignment is submitted in Canvas

Setup:
    pip install requests python-dotenv

    Create a .env file next to this script:
        CANVAS_BASE_URL=https://yourschool.instructure.com
        CANVAS_TOKEN=your_canvas_access_token
        TRELLO_KEY=your_trello_api_key
        TRELLO_TOKEN=your_trello_token
        TRELLO_BOARD_ID=your_board_id

    Board ID: open your board, add ".json" to the URL, and copy the top-level "id".
    (Or call GET https://api.trello.com/1/members/me/boards?key=...&token=...)

Usage:
    python canvas_to_trello.py --dry-run   # show what would happen
    python canvas_to_trello.py             # do it
"""

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

try:
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / "tokens.env")
except ImportError:
    pass  # fine if you set the environment variables yourself

# ----------------------------- config ---------------------------------------

CANVAS_BASE_URL = os.environ.get("CANVAS_BASE_URL", "").rstrip("/")
CANVAS_TOKEN = os.environ.get("CANVAS_TOKEN", "")
TRELLO_KEY = os.environ.get("TRELLO_KEY", "")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN", "")
TRELLO_BOARD_ID = os.environ.get("TRELLO_BOARD_ID", "")

TRELLO_API = "https://api.trello.com/1"

# List names use the course name without the "-Fall 2026-CODE" tail,
# e.g. "STATICS-Fall 2026-CEEN241A-K" -> "STATICS".
# Label names use just the course number, e.g. "Fall 2026.MATH112A.80091" -> "MATH112A".

# Only sync assignments due within this many days from now
DAYS_AHEAD = 7
# Also include assignments that were due up to this many days ago (0 = only future)
OVERDUE_DAYS = 0

# Label colors, cycled through as new courses are added
LABEL_COLORS = ["blue", "green", "orange", "purple", "red", "yellow", "sky", "lime", "pink", "black"]

MARKER_RE = re.compile(r"canvas-id:(\d+)-(\d+)")

canvas = requests.Session()
canvas.headers.update({"Authorization": f"Bearer {CANVAS_TOKEN}"})

trello = requests.Session()
trello.params = {"key": TRELLO_KEY, "token": TRELLO_TOKEN}


# ----------------------------- helpers --------------------------------------

def check_config():
    missing = [
        name for name, val in {
            "CANVAS_BASE_URL": CANVAS_BASE_URL,
            "CANVAS_TOKEN": CANVAS_TOKEN,
            "TRELLO_KEY": TRELLO_KEY,
            "TRELLO_TOKEN": TRELLO_TOKEN,
            "TRELLO_BOARD_ID": TRELLO_BOARD_ID,
        }.items() if not val
    ]
    if missing:
        sys.exit(f"Missing settings: {', '.join(missing)}")


def canvas_paged(path, params=None):
    """Yield every item from a paginated Canvas endpoint."""
    url = f"{CANVAS_BASE_URL}/api/v1{path}"
    params = dict(params or {}, per_page=100)
    while url:
        r = canvas.get(url, params=params)
        r.raise_for_status()
        yield from r.json()
        url = r.links.get("next", {}).get("url")
        params = None  # the "next" URL already includes the query string


def trello_get(path, **params):
    r = trello.get(f"{TRELLO_API}{path}", params=params)
    r.raise_for_status()
    return r.json()


def trello_post(path, **data):
    r = trello.post(f"{TRELLO_API}{path}", data=data)
    r.raise_for_status()
    time.sleep(0.15)  # stay well under Trello's rate limits
    return r.json()


def trello_put(path, **data):
    r = trello.put(f"{TRELLO_API}{path}", data=data)
    r.raise_for_status()
    time.sleep(0.15)
    return r.json()


def in_window(assignment):
    """True if the assignment is due within the configured window."""
    due = datetime.fromisoformat(assignment["due_at"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return now - timedelta(days=OVERDUE_DAYS) <= due <= now + timedelta(days=DAYS_AHEAD)


def short_list_name(course):
    name = course["name"]
    return re.sub(r"-(Fall|Spring|Summer|Winter)\s+\d{4}.*$", "", name, flags=re.I).strip() or name


def short_label_name(course):
    code = course.get("course_code") or course["name"]
    parts = code.split(".")
    return parts[1] if len(parts) >= 3 else code


def is_submitted(assignment):
    sub = assignment.get("submission") or {}
    return sub.get("workflow_state") in ("submitted", "graded", "pending_review") \
        or bool(sub.get("submitted_at"))


# ----------------------------- main sync ------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print actions without changing Trello")
    args = parser.parse_args()
    dry = args.dry_run

    check_config()

    # Existing Trello state
    lists_by_name = {l["name"]: l for l in trello_get(f"/boards/{TRELLO_BOARD_ID}/lists", filter="open")}
    labels_by_name = {l["name"]: l for l in trello_get(f"/boards/{TRELLO_BOARD_ID}/labels", limit=1000) if l["name"]}
    # "all" includes archived cards so archived ones don't get recreated
    cards = trello_get(f"/boards/{TRELLO_BOARD_ID}/cards/all", fields="name,desc,idList,dueComplete,closed")

    existing = {}  # (course_id, assignment_id) -> card
    for c in cards:
        m = MARKER_RE.search(c.get("desc") or "")
        if m:
            existing[(m.group(1), m.group(2))] = c

    color_i = len(labels_by_name)
    created = completed = 0

    courses = canvas_paged("/courses", {"enrollment_state": "active", "enrollment_type": "student"})
    for course in courses:
        if "name" not in course:  # course with restricted access
            continue

        course_id = str(course["id"])
        list_name = short_list_name(course)
        label_name = short_label_name(course)

        assignments = [
            a for a in canvas_paged(
                f"/courses/{course_id}/assignments",
                {"include[]": "submission", "order_by": "due_at"},
            )
            if a.get("due_at") and a.get("published", True)
        ]
        assignments = [a for a in assignments if in_window(a)]
        if not assignments:
            continue

        # Work out what needs doing before creating any list or label
        to_create = []
        for a in assignments:
            key = (course_id, str(a["id"]))
            submitted = is_submitted(a)

            if key in existing:
                card = existing[key]
                # Mark due date complete once submitted
                if submitted and not card.get("dueComplete") and not card.get("closed"):
                    print(f"[done]  {label_name}: {a['name']}")
                    if not dry:
                        trello_put(f"/cards/{card['id']}", dueComplete="true")
                    completed += 1
            elif not submitted:  # don't create cards for work that's already turned in
                to_create.append(a)

        if not to_create:
            continue

        # Ensure list exists
        if list_name not in lists_by_name:
            print(f"[list]  creating '{list_name}'")
            lists_by_name[list_name] = {"id": "DRY-RUN"} if dry else trello_post(
                "/lists", name=list_name, idBoard=TRELLO_BOARD_ID, pos="bottom"
            )

        # Ensure label exists
        if label_name not in labels_by_name:
            color = LABEL_COLORS[color_i % len(LABEL_COLORS)]
            color_i += 1
            print(f"[label] creating '{label_name}' ({color})")
            labels_by_name[label_name] = {"id": "DRY-RUN"} if dry else trello_post(
                "/labels", name=label_name, color=color, idBoard=TRELLO_BOARD_ID
            )

        list_id = lists_by_name[list_name]["id"]
        label_id = labels_by_name[label_name]["id"]

        for a in to_create:
            desc = (
                f"{a.get('html_url', '')}\n\n"
                f"Course: {course['name']}\n"
                f"Points: {a.get('points_possible', 'n/a')}\n\n"
                f"canvas-id:{course_id}-{a['id']}"
            )
            print(f"[card]  {label_name}: {a['name']} (due {a['due_at']})")
            if not dry:
                trello_post(
                    "/cards",
                    idList=list_id,
                    name=a["name"],
                    desc=desc,
                    due=a["due_at"],
                    idLabels=label_id,
                    pos="bottom",
                )
            created += 1

    print(f"\nDone. {created} card(s) created, {completed} marked complete."
          + (" (dry run, nothing changed)" if dry else ""))


if __name__ == "__main__":
    main()