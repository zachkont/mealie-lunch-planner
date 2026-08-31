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
# Monday..Sunday. Each day lists one or more candidate tag slugs -- when a day
# lists more than one (Friday: pork-or-beef, Saturday: fish-or-seafood), the
# pools are combined and one recipe is picked from either, not both.
CATEGORY_ORDER = [
    ["ospria"],              # Monday: legumes
    ["kotopoulo"],           # Tuesday: chicken
    ["ladera"],               # Wednesday: λαδερά
    ["zumarika"],             # Thursday: pasta
    ["khoirino", "moskhari"],  # Friday: pork OR beef
    ["psari", "thalassina"],   # Saturday: fish OR seafood
    ["cheat-day"],            # Sunday: cheat day
]
TAG_NAMES = {
    "ospria": "Όσπρια",
    "kotopoulo": "Κοτόπουλο",
    "ladera": "Λαδερά",
    "zumarika": "Ζυμαρικά",
    "khoirino": "Χοιρινό",
    "moskhari": "Μοσχάρι",
    "psari": "Ψάρι",
    "thalassina": "Θαλασσινά",
    "cheat-day": "Cheat day",
}
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


def week_bounds():
    """Always resolves to a Monday..Sunday span, never a misaligned 7-day window.

    On a Sunday (the day the real cron fires) this means NEXT week, since the
    job's purpose is to prepare the upcoming week in advance. On any other day
    (e.g. a startup dry-run, or a container restart mid-week) it means THIS
    week -- so an already-generated plan (created by last Sunday's run) is
    correctly found instead of skipped over.
    """
    today = date.today()
    if today.weekday() == 6:  # Sunday
        start = today + timedelta(days=1)
    else:
        start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


def get_pool_for_tag(category_slug):
    qs = urllib.parse.urlencode(
        [("tags", LUNCH_TAG), ("tags", category_slug), ("requireAllTags", "true"), ("perPage", "100")]
    )
    data = mealie_request("GET", f"/api/recipes?{qs}")
    return data.get("items", [])


def get_pool(category_slugs, exclude_ids):
    """Union of the pools for each candidate tag (OR across slugs, e.g.
    pork-or-beef), deduped by recipe id, minus recipes already used this run."""
    seen = {}
    for slug in category_slugs:
        for r in get_pool_for_tag(slug):
            if r["id"] not in seen:
                r["_source_tag"] = slug
                seen[r["id"]] = r
    return [r for r in seen.values() if r["id"] not in exclude_ids]


def get_existing_lunches(start, end):
    qs = urllib.parse.urlencode(
        [("start_date", start.isoformat()), ("end_date", end.isoformat()), ("perPage", "100")]
    )
    data = mealie_request("GET", f"/api/households/mealplans?{qs}")
    entries = [e for e in data.get("items", []) if e.get("entryType") == "lunch"]
    entries.sort(key=lambda e: e["date"])
    return entries


def clear_lunches(entries):
    for entry in entries:
        mealie_request("DELETE", f"/api/households/mealplans/{entry['id']}")


def pick_plan(start):
    """Pure selection -- does not write anything to Mealie or Home Assistant."""
    used_ids = set()
    plan = []
    for i, tag_slugs in enumerate(CATEGORY_ORDER):
        day = start + timedelta(days=i)
        day_label = GREEK_WEEKDAYS[day.weekday()]
        combined_label = "/".join(TAG_NAMES[s] for s in tag_slugs)
        try:
            pool = get_pool(tag_slugs, used_ids)
        except Exception as e:
            log(f"ERROR fetching pool for {combined_label}: {e}")
            pool = []
        recipe = None
        category_name = combined_label
        if pool:
            recipe = random.choice(pool)
            used_ids.add(recipe["id"])
            category_name = TAG_NAMES[recipe["_source_tag"]]
        else:
            log(f"WARNING: no available recipes for {combined_label} ({tag_slugs}) on {day}")
        plan.append(
            {"date": day, "day_label": day_label, "category_name": category_name, "recipe": recipe}
        )
    return plan


def format_summary(week_start, week_end, lines):
    header = f"Το εβδομαδιαίο πρόγραμμα μεσημεριανών ({week_start.isoformat()} - {week_end.isoformat()}):"
    return header + "\n" + "\n".join(lines)


def notify_ha(text):
    payload = {"text": text}
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        HA_WEBHOOK_URL, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except urllib.error.URLError as e:
        log(f"ERROR: failed to notify HA webhook: {e}")


def generate(dry_run=False, force=False):
    start, end = week_bounds()
    log(f"[{'dry-run' if dry_run else 'run'}] Checking lunch plan for {start} .. {end}")

    try:
        existing = get_existing_lunches(start, end)
    except Exception as e:
        log(f"ERROR checking existing plan: {e}")
        return

    if existing and not force:
        log(f"Plan for {start}..{end} already exists ({len(existing)} entries) -- not regenerating.")
        lines = [
            f"{GREEK_WEEKDAYS[date.fromisoformat(e['date']).weekday()]} ({e['date']}): "
            f"{e['recipe']['name'] if e.get('recipe') else '?'}"
            for e in existing
        ]
        text = format_summary(start, end, lines)
        log(text)
        if not dry_run:
            notify_ha(text)
        return

    if force and existing:
        log(f"--force: clearing {len(existing)} existing entries before regenerating")
        clear_lunches(existing)

    plan = pick_plan(start)
    lines = [
        f"{p['day_label']} ({p['category_name']}): "
        f"{p['recipe']['name'] if p['recipe'] else '-- κανένα διαθέσιμο --'}"
        for p in plan
    ]
    text = format_summary(start, end, lines)

    if dry_run:
        log("Would create the following plan (nothing written, no notification sent):")
        log(text)
        return

    for p in plan:
        if not p["recipe"]:
            continue
        try:
            mealie_request(
                "POST",
                "/api/households/mealplans",
                {"date": p["date"].isoformat(), "entryType": "lunch", "recipeId": p["recipe"]["id"]},
            )
            log(f"{p['date']} [{p['category_name']}] -> {p['recipe']['name']}")
        except Exception as e:
            log(f"ERROR creating mealplan entry for {p['date']}: {e}")

    notify_ha(text)
    log("Done.")


def main():
    if "--dry-run" in sys.argv:
        generate(dry_run=True)
        return
    if "--once" in sys.argv:
        generate(dry_run=False, force="--force" in sys.argv)
        return

    log(f"Started. CRON_SCHEDULE={CRON_SCHEDULE!r}")
    log("Startup dry run (sanity check only -- writes nothing, notifies nothing):")
    generate(dry_run=True)

    while True:
        nxt = croniter(CRON_SCHEDULE, time.time()).get_next(float)
        wait = max(0.0, nxt - time.time())
        log(f"Sleeping {wait / 3600:.1f}h until next run")
        time.sleep(wait)
        try:
            generate(dry_run=False)
        except Exception as e:
            log(f"ERROR: generation run failed: {e}")


if __name__ == "__main__":
    main()
