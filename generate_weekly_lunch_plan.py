"""Generates next week's lunch meal plan in Mealie from tagged recipe pools,
then notifies Home Assistant via webhook. Runs on a schedule (CRON_SCHEDULE)
inside its own long-running process -- see README.md for the design.
"""
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

from croniter import croniter

MEALIE_URL = os.environ["MEALIE_URL"].rstrip("/")
MEALIE_TOKEN = os.environ["MEALIE_TOKEN"]
HA_WEBHOOK_URL = os.environ["HA_WEBHOOK_URL"]
CRON_SCHEDULE = os.environ.get("CRON_SCHEDULE", "0 18 * * 0")

LUNCH_TAG = "diet4-lunch"
CATEGORY_ORDER = [
    ("ospria", "Όσπρια"),
    ("zumarika", "Ζυμαρικά"),
    ("kotopoulo", "Κοτόπουλο"),
    ("psari", "Ψάρι"),
    ("ladera", "Λαδερά"),
    ("khoirino-moskhari", "Χοιρινό/Μοσχάρι"),
    ("cheat-day", "Cheat day"),
]
GREEK_WEEKDAYS = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def mealie_request(method, path, body=None):
    url = f"{MEALIE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {MEALIE_TOKEN}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def get_pool(category_slug, exclude_ids):
    qs = urllib.parse.urlencode(
        [("tags", LUNCH_TAG), ("tags", category_slug), ("requireAllTags", "true"), ("perPage", "100")]
    )
    data = mealie_request("GET", f"/api/recipes?{qs}")
    return [r for r in data.get("items", []) if r["id"] not in exclude_ids]


def clear_existing_lunches(start, end):
    qs = urllib.parse.urlencode(
        [("start_date", start.isoformat()), ("end_date", end.isoformat()), ("perPage", "100")]
    )
    data = mealie_request("GET", f"/api/households/mealplans?{qs}")
    existing = [e for e in data.get("items", []) if e.get("entryType") == "lunch"]
    for entry in existing:
        mealie_request("DELETE", f"/api/households/mealplans/{entry['id']}")
    if existing:
        log(f"Cleared {len(existing)} existing lunch entries for {start}..{end}")


def notify_ha(summary_lines, week_start, week_end):
    text = "Το εβδομαδιαίο πρόγραμμα μεσημεριανών (" + week_start.isoformat() + " - " + week_end.isoformat() + "):\n" + "\n".join(summary_lines)
    payload = {"text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(HA_WEBHOOK_URL, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.URLError as e:
        log(f"ERROR: failed to notify HA webhook: {e}")


def generate():
    start = date.today() + timedelta(days=1)
    end = start + timedelta(days=6)
    log(f"Generating lunch plan for {start} .. {end}")

    try:
        clear_existing_lunches(start, end)
    except Exception as e:
        log(f"ERROR clearing existing entries: {e}")
        return

    used_ids = set()
    summary_lines = []
    for i, (tag_slug, tag_name) in enumerate(CATEGORY_ORDER):
        day = start + timedelta(days=i)
        day_label = GREEK_WEEKDAYS[day.weekday()]
        try:
            pool = get_pool(tag_slug, used_ids)
        except Exception as e:
            log(f"ERROR fetching pool for {tag_name}: {e}")
            continue
        if not pool:
            log(f"WARNING: no available recipes for {tag_name} ({tag_slug}) on {day} -- skipping")
            summary_lines.append(f"{day_label} ({tag_name}): -- κανένα διαθέσιμο --")
            continue
        recipe = random.choice(pool)
        used_ids.add(recipe["id"])
        try:
            mealie_request(
                "POST",
                "/api/households/mealplans",
                {"date": day.isoformat(), "entryType": "lunch", "recipeId": recipe["id"]},
            )
        except Exception as e:
            log(f"ERROR creating mealplan entry for {day}: {e}")
            summary_lines.append(f"{day_label} ({tag_name}): -- σφάλμα --")
            continue
        log(f"{day} [{tag_name}] -> {recipe['name']}")
        summary_lines.append(f"{day_label} ({tag_name}): {recipe['name']}")

    notify_ha(summary_lines, start, end)
    log("Done.")


def main():
    if "--once" in sys.argv:
        generate()
        return

    log(f"Started. CRON_SCHEDULE={CRON_SCHEDULE!r}")
    while True:
        nxt = croniter(CRON_SCHEDULE, time.time()).get_next(float)
        wait = max(0.0, nxt - time.time())
        log(f"Sleeping {wait / 3600:.1f}h until next run")
        time.sleep(wait)
        try:
            generate()
        except Exception as e:
            log(f"ERROR: generation run failed: {e}")


if __name__ == "__main__":
    main()
