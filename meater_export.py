#!/usr/bin/env python3
"""
Meater Cook History Exporter
Exports full-resolution temperature data from completed cooks to CSV.

Usage:
    python meater_export.py                         # interactive
    python meater_export.py --list                  # list cooks only
    python meater_export.py --all                   # export all cooks
    python meater_export.py --cook <id_prefix>      # export one cook
    python meater_export.py --dump                  # also save raw JSON

Credentials via args or environment variables MEATER_EMAIL / MEATER_PASSWORD.
"""

import argparse
import csv
import getpass
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("requests not found. Run: pip install requests")

PRIVATE_BASE = "https://api.cloud.meater.com"
PUBLIC_BASE  = "https://public-api.cloud.meater.com"
CONFIG_FILE  = Path(__file__).parent / "meater_config.json"
CUTS_FILE    = Path(__file__).parent / "meater_cuts.json"  # optional: the cuts database JSON

APP_VERSION  = "4.4.2"
APP_BUILD    = "12305"
USER_AGENT   = f"MEATER/{APP_BUILD} CFNetwork/1568.300.101 Darwin/24.2.0"


# ── Auth ──────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def login(email: str, password: str) -> tuple[str, str]:
    """
    Returns (private_token, device_id).
    Caches tokens in meater_config.json to avoid repeated logins.
    """
    cfg = load_config()
    if cfg.get("token") and cfg.get("device_id"):
        print("  Using cached token.")
        return cfg["token"], cfg["device_id"]

    device_id = cfg.get("device_id") or str(uuid.uuid4()).upper()

    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT})

    print(f"  Logging in as {email}...")
    r = sess.post(
        f"{PRIVATE_BASE}/login",
        json={
            "check_terms": 1,
            "email": email,
            "password": password,
            "clientVersion": f"MEATER-iOS-v{APP_VERSION}",
            "device": {
                "id":          device_id,
                "model":       "iPhone",
                "os_name":     "iOS",
                "os_version":  "18.2",
                "locale":      "en_NZ",
                "app_version": APP_VERSION,
                "app_build":   APP_BUILD,
            },
        },
        timeout=15,
    )

    if r.status_code not in (200, 201):
        sys.exit(f"Login failed: HTTP {r.status_code}\n{r.text}")

    body = r.json()
    token = body.get("accessToken")
    if not token:
        sys.exit(f"No accessToken in response:\n{json.dumps(body, indent=2)}")

    cfg = {"token": token, "device_id": device_id}
    save_config(cfg)
    print("  Login successful. Token cached in meater_config.json.")
    return token, device_id


def build_cuts_lookup(cuts_json_path: Path) -> dict:
    """
    Parse the Meater cuts database JSON into flat lookup dicts:
      cuts_by_id[cut_id]   -> {"cut": "sirloin", "animal": "beef", "category": "beef", "cut_type": "steak"}
      presets_by_id[id]    -> "meater_recommends" | "medium_rare" | etc.
    """
    cuts_by_id   = {}
    presets_by_id = {}

    if not cuts_json_path.exists():
        return cuts_by_id, presets_by_id

    try:
        data = json.loads(cuts_json_path.read_text())
    except Exception:
        return cuts_by_id, presets_by_id

    for category in data.get("categories", []):
        cat_name = category.get("name", "")
        for animal in category.get("animals", []):
            animal_name = animal.get("name", "")
            for cut_type in animal.get("cut_types", []):
                cut_type_name = cut_type.get("name", "")
                for cut in cut_type.get("cuts", []):
                    cut_id = cut.get("id")
                    cut_name = cut.get("name", "")
                    cuts_by_id[cut_id] = {
                        "category": cat_name,
                        "animal":   animal_name,
                        "cut_type": cut_type_name,
                        "cut":      cut_name,
                    }
                    for tr in cut.get("temperature_ranges", []):
                        presets_by_id[tr.get("id")] = tr.get("name", "")

    return cuts_by_id, presets_by_id


def make_session(token: str) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({
        "User-Agent":    USER_AGENT,
        "Authorization": f"Bearer {token}",
    })
    return sess


# ── Cook list ─────────────────────────────────────────────────────────────────

def get_cooks(sess: requests.Session) -> list[dict]:
    cooks = []
    page = 1
    while True:
        r = sess.get(f"{PRIVATE_BASE}/v2/cooks", params={"page": page}, timeout=15)
        if r.status_code != 200:
            sys.exit(f"Failed to fetch cooks page {page}: HTTP {r.status_code}\n{r.text}")
        body = r.json()
        page_cooks = body.get("data") or []
        cooks.extend(page_cooks)
        has_more = body.get("meta", {}).get("pagination", {}).get("hasMore", False)
        print(f"  Page {page}: {len(page_cooks)} cooks (total so far: {len(cooks)})")
        if not has_more:
            break
        page += 1
    return cooks


def get_cook_detail(sess: requests.Session, cook_id: str) -> dict:
    r = sess.get(f"{PRIVATE_BASE}/v2/cooks/{cook_id}", timeout=15)
    if r.status_code != 200:
        sys.exit(f"Failed to fetch cook {cook_id}: HTTP {r.status_code}\n{r.text}")
    body = r.json()
    return body.get("data") or body


# ── Data extraction ───────────────────────────────────────────────────────────

def ts_to_str(ts) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts)


def extract_readings(detail: dict) -> list[dict]:
    """
    Extract temperature time series from a cook detail response.

    Values are stored as integers in units of 1/16 °C, so divide by 16.
    Timestamps are derived from startTime + (index * interval seconds).
    """
    history = detail.get("raw", {}).get("history", {})
    values     = history.get("values", [])
    start_time = history.get("startTime")
    interval   = history.get("interval", 0)

    if not values or start_time is None:
        return []

    readings = []
    for i, v in enumerate(values):
        ts       = start_time + i * interval
        internal = v.get("internal")
        ambient  = v.get("ambient")
        readings.append({
            "timestamp":       ts,
            "time_utc":        ts_to_str(ts),
            "internal_temp_c": round(internal / 16, 3) if internal is not None else None,
            "ambient_temp_c":  round(ambient  / 16, 3) if ambient  is not None else None,
        })
    return readings


# ── Display ───────────────────────────────────────────────────────────────────

def cook_summary(i: int, cook: dict) -> str:
    cid      = cook.get("id", "?")
    name     = cook.get("name") or cook.get("raw", {}).get("setup", {}).get("name") or "Unnamed"
    raw      = cook.get("raw", {})
    started  = ts_to_str(raw.get("history", {}).get("startTime"))
    peak_raw = raw.get("peak")
    peak_s   = f"  peak={peak_raw/16:.1f}°C" if peak_raw else ""
    duration = cook.get("totalTime", 0)
    dur_s    = f"  {duration//3600}h{(duration%3600)//60}m" if duration else ""
    return f"  {i:>2}. [{cid[:8]}]  {name:<22} {started}{dur_s}{peak_s}"


# ── Export ────────────────────────────────────────────────────────────────────

def export_to_csv(cook: dict, detail: dict, outdir: Path,
                  cuts_by_id: dict = None, presets_by_id: dict = None) -> bool:
    readings = extract_readings(detail)
    if not readings:
        print(f"     WARNING: No readings found. Keys in response: {list(detail.keys())}")
        print(f"     Try --dump to inspect the raw JSON.")
        return False

    # Resolve meat type from cuts lookup
    setup    = detail.get("raw", {}).get("setup", {})
    cut_id   = setup.get("cutID")
    preset_id = setup.get("presetID")  # which temperature range was targeted
    alarms   = setup.get("alarms", [])

    cut_info = (cuts_by_id or {}).get(cut_id, {})
    preset_name = (presets_by_id or {}).get(preset_id, "")

    name  = (cook.get("name") or "cook").replace(" ", "_").replace("/", "-")
    cid   = cook.get("id", "unknown")
    fpath = outdir / f"{name}_{cid[:8]}.csv"

    fieldnames = [
        "timestamp", "time_utc",
        "internal_temp_c", "ambient_temp_c",
        "category", "animal", "cut_type", "cut", "preset",
    ]

    with open(fpath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in readings:
            r.update({
                "category":  cut_info.get("category", ""),
                "animal":    cut_info.get("animal", ""),
                "cut_type":  cut_info.get("cut_type", ""),
                "cut":       cut_info.get("cut", ""),
                "preset":    preset_name,
            })
            writer.writerow(r)

    meat_desc = " / ".join(filter(None, [
        cut_info.get("animal"), cut_info.get("cut"), preset_name
    ])) or "unknown cut"
    print(f"     Saved {len(readings)} readings → {fpath}  [{meat_desc}]")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Export Meater cook data to CSV")
    parser.add_argument("--email",    help="Meater account email")
    parser.add_argument("--password", help="Meater account password")
    parser.add_argument("--list",     action="store_true", help="List cooks and exit")
    parser.add_argument("--all",      action="store_true", help="Export all cooks")
    parser.add_argument("--cook",     help="Export cook matching this ID prefix")
    parser.add_argument("--dump",     action="store_true", help="Save raw JSON alongside CSV")
    parser.add_argument("--outdir",   default="meater_exports", help="Output directory")
    parser.add_argument("--reauth",   action="store_true", help="Force fresh login (clear cached token)")
    args = parser.parse_args()

    if args.reauth and CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        print("Cleared cached token.")

    email    = args.email    or os.environ.get("MEATER_EMAIL")    or input("Meater email: ")
    password = args.password or os.environ.get("MEATER_PASSWORD") or getpass.getpass("Meater password: ")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Load cuts lookup if the JSON file is present
    cuts_by_id, presets_by_id = build_cuts_lookup(CUTS_FILE)
    if cuts_by_id:
        print(f"  Loaded cuts database: {len(cuts_by_id)} cuts, {len(presets_by_id)} presets")

    print("\n── Login ──────────────────────────────────────")
    token, _ = login(email, password)
    sess = make_session(token)

    print("\n── Cooks ──────────────────────────────────────")
    cooks = get_cooks(sess)
    if not cooks:
        sys.exit("No cooks returned. Have you completed a cook while connected to Meater Cloud?")

    print(f"Found {len(cooks)} cook(s):\n")
    for i, c in enumerate(cooks, 1):
        print(cook_summary(i, c))

    if args.list:
        return

    # Determine which cooks to export
    if args.cook:
        to_export = [c for c in cooks if c.get("id", "").startswith(args.cook)]
        if not to_export:
            sys.exit(f"No cook found matching ID prefix '{args.cook}'")
    elif args.all:
        to_export = cooks
    else:
        print()
        choice = input("Enter cook number to export (or 'all'): ").strip().lower()
        if choice == "all":
            to_export = cooks
        else:
            try:
                to_export = [cooks[int(choice) - 1]]
            except (ValueError, IndexError):
                sys.exit("Invalid selection.")

    print(f"\n── Exporting {len(to_export)} cook(s) ─────────────────")
    for cook in to_export:
        cid  = cook.get("id", "?")
        name = cook.get("name") or "Unnamed"
        print(f"\n  {name} [{cid[:8]}...]")

        detail = get_cook_detail(sess, cid)

        if args.dump:
            dump_path = outdir / f"{(name or 'cook').replace(' ','_')}_{cid[:8]}.json"
            dump_path.write_text(json.dumps(detail, indent=2))
            print(f"     JSON → {dump_path}")

        export_to_csv(cook, detail, outdir, cuts_by_id, presets_by_id)

    print("\nDone.")


if __name__ == "__main__":
    main()
