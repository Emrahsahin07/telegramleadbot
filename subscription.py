import os
import json
from config import LOCATION_ALIAS


if not os.path.exists("subscriptions.json"):
    subscriptions = {}
else:
    with open("subscriptions.json", "r", encoding="utf-8") as f:
        subscriptions = json.load(f)

def save_subscriptions():
    tmp_file = "subscriptions.json.tmp"
    with open(tmp_file, "w", encoding="utf-8") as wf:
        json.dump(subscriptions, wf, ensure_ascii=False, indent=2)
    os.replace(tmp_file, "subscriptions.json")

for uid, prefs in subscriptions.items():
    normalized_locs = []
    for loc in prefs.get("locations", []):
        canonical = LOCATION_ALIAS.get(loc.lower(), loc)
        normalized_locs.append(canonical)
    prefs["locations"] = sorted(set(normalized_locs))