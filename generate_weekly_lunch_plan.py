"""Generates next week's lunch meal plan in Mealie from tagged recipe pools,
then notifies Home Assistant via webhook. Runs on a schedule (CRON_SCHEDULE)
inside its own long-running process -- see README.md for the design.
"""
import json
import math
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

LUNCH_CATEGORY = "mesemeriano"  # Mealie Category "Μεσημεριανό" -- the lunch-pool marker
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

SHOPPING_LIST_NAME = "Λίστα εβδομαδιαίων μεσημεριανών"
TARGET_SERVINGS = 6
# Mealie's own aisle-label taxonomy, bucketed into the user's 3 shopping stops.
# Anything not in PRODUCE_LABELS or MEAT_LABELS falls through to groceries.
PRODUCE_LABELS = {"Vegetables & Greens", "Fruits", "Herbs & Spices", "Mushrooms", "Berries"}
MEAT_LABELS = {"Meats", "Poultry", "Fish", "Seafood & Seaweed"}
BUCKET_ORDER = ["ΛΑΪΚΗ", "ΚΡΕΟΠΩΛΕΙΟ / ΙΧΘΥΟΠΩΛΕΙΟ", "ΣΟΥΠΕΡ ΜΑΡΚΕΤ"]


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
        [("categories", LUNCH_CATEGORY), ("tags", category_slug), ("perPage", "100")]
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


# Things that show up as recipe "ingredients" but aren't actually purchased.
EXCLUDED_FOODS = {"νερό"}


def bucket_for_label(label_name):
    if label_name in PRODUCE_LABELS:
        return "ΛΑΪΚΗ"
    if label_name in MEAT_LABELS:
        return "ΚΡΕΟΠΩΛΕΙΟ / ΙΧΘΥΟΠΩΛΕΙΟ"
    return "ΣΟΥΠΕΡ ΜΑΡΚΕΤ"


# Unit-name -> base-unit factor, so quantities in genuinely equivalent units
# (e.g. olive oil measured in ml on one recipe, in litres on another) merge
# into one clean, human-rounded line instead of separate odd-fraction ones.
# Deliberately NOT included: κ.σ./κ.γ./φλ. (tablespoon/teaspoon/cup) -- those
# are volume units too, but converting a spice's "1 κ.γ." into "5 ml" reads
# as nonsense on a shopping list (you don't buy oregano by the millilitre),
# and there's no reliable way here to tell a spoon of spice from a spoon of
# oil. They're kept as their own literal unit in the "counts" group instead.
WEIGHT_UNITS_G = {"γραμμάριο": 1.0, "κιλό": 1000.0, "χιλιοστόγραμμο": 0.001}
VOLUME_UNITS_ML = {"ml": 1.0, "χιλιοστόλιτρο": 1.0, "λίτρο": 1000.0}


def _fmt_num(n):
    return str(int(n)) if n == int(n) else f"{n:g}"


def round_weight(total_g):
    if total_g >= 1000:
        kg = round(total_g / 1000 * 4) / 4  # nearest 0.25 kg
        return _fmt_num(kg), "κιλό" if kg == 1 else "κιλά"
    g = round(total_g / 25) * 25  # nearest 25g
    return _fmt_num(g), "γρ."


def round_volume(total_ml):
    if total_ml >= 1000:
        l = round(total_ml / 1000 * 4) / 4  # nearest 0.25 L
        return _fmt_num(l), "λίτρο" if l == 1 else "λίτρα"
    ml = round(total_ml / 25) * 25  # nearest 25ml
    return _fmt_num(ml), "ml"


def get_or_create_shopping_list():
    data = mealie_request("GET", "/api/households/shopping/lists?perPage=50")
    for lst in data.get("items", []):
        if lst["name"] == SHOPPING_LIST_NAME:
            return lst["id"]
    created = mealie_request("POST", "/api/households/shopping/lists", {"name": SHOPPING_LIST_NAME})
    return created["id"]


def clear_shopping_list(list_id):
    data = mealie_request("GET", f"/api/households/shopping/lists/{list_id}")
    ids = [i["id"] for i in data.get("listItems", [])]
    if ids:
        # `ids` is a repeated query param on this endpoint, NOT a JSON body
        # field -- a JSON body here 200s but silently deletes nothing.
        qs = urllib.parse.urlencode([("ids", i) for i in ids])
        mealie_request("DELETE", f"/api/households/shopping/items?{qs}")


def build_shopping_list(plan):
    """Adds each picked recipe to a dedicated Mealie shopping list, scaled to
    TARGET_SERVINGS, then reads back Mealie's own aggregated/labeled items and
    formats them into the three shopping-stop buckets. Returns the message
    text, or None if nothing could be built."""
    recipes = [p["recipe"] for p in plan if p["recipe"]]
    if not recipes:
        return None

    list_id = get_or_create_shopping_list()
    clear_shopping_list(list_id)
    for recipe in recipes:
        servings = recipe.get("recipeServings") or TARGET_SERVINGS
        scale = TARGET_SERVINGS / servings
        try:
            mealie_request(
                "POST",
                f"/api/households/shopping/lists/{list_id}/recipe/{recipe['id']}",
                {"recipeIncrementQuantity": scale},
            )
        except Exception as e:
            log(f"ERROR adding {recipe['name']} to shopping list: {e}")

    data = mealie_request("GET", f"/api/households/shopping/lists/{list_id}")

    # Group by (bucket, food name) across ALL units first, splitting each
    # group's quantities into weight/volume/plain-count buckets so e.g. olive
    # oil in ml on one recipe and tablespoons on another merge into one line.
    grouped = {}
    for item in data.get("listItems", []):
        food = item.get("food")
        name = food["name"] if food else (item.get("note") or None)
        if not name or name in EXCLUDED_FOODS:
            continue
        label_name = (food.get("label") or {}).get("name") if food else None
        unit = item.get("unit")
        unit_name = unit["name"] if unit else None
        qty = item.get("quantity") or 0
        g = grouped.setdefault((bucket_for_label(label_name), name), {"weight": 0.0, "volume": 0.0, "counts": {}})
        if unit_name in WEIGHT_UNITS_G:
            g["weight"] += qty * WEIGHT_UNITS_G[unit_name]
        elif unit_name in VOLUME_UNITS_ML:
            g["volume"] += qty * VOLUME_UNITS_ML[unit_name]
        else:
            g["counts"][unit_name] = g["counts"].get(unit_name, 0.0) + qty

    buckets = {b: [] for b in BUCKET_ORDER}
    for (bucket, name), g in grouped.items():
        parts = []
        if g["weight"] > 0:
            parts.append(round_weight(g["weight"]))
        if g["volume"] > 0:
            parts.append(round_volume(g["volume"]))
        for unit_name, qty in g["counts"].items():
            if qty <= 0:
                continue
            # Garlic by the clove reads better converted to heads (~10/head)
            # -- a common enough case in this recipe pool to special-case.
            if name == "σκόρδο" and unit_name == "σκελίδα" and qty >= 6:
                heads = math.ceil(qty / 10)
                parts.append((str(heads), "κεφάλι" if heads == 1 else "κεφάλια"))
            else:
                parts.append((str(math.ceil(qty)), unit_name or ""))
        if not parts:
            continue
        if len(parts) == 1:
            num, unit = parts[0]
            line = f"{num} {unit} {name}".strip() if unit else f"{num} {name}"
        else:
            line = f"{name}: " + " + ".join(f"{num} {unit}".strip() for num, unit in parts)
        buckets[bucket].append(line)

    lines = [f"Λίστα ψωνιών για την εβδομάδα ({TARGET_SERVINGS} άτομα):"]
    for bucket_name in BUCKET_ORDER:
        items = sorted(buckets[bucket_name])
        if not items:
            continue
        lines.append(f"\n{bucket_name}:")
        lines.extend(f"- {it}" for it in items)
    return "\n".join(lines)


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
            shopping_plan = [{"recipe": e.get("recipe")} for e in existing]
            shopping_text = build_shopping_list(shopping_plan)
            if shopping_text:
                notify_ha(shopping_text)
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

    shopping_text = build_shopping_list(plan)
    if shopping_text:
        notify_ha(shopping_text)

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
