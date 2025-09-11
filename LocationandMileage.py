import os
import re
import json
import time
import requests
import pandas as pd
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ================== CONFIG ==================
API_BASE_URL = "https://api.samsara.com"
API_TOKEN = os.getenv("SAMSARA_API_TOKEN")  # set in your env; rotate if exposed
if not API_TOKEN:
    raise RuntimeError("Missing SAMSARA_API_TOKEN environment variable.")

HEADERS = {
    "Authorization": f"Bearer {API_TOKEN}",
    "Accept": "application/json"
}

# Stats to pull
STAT_TYPES = "obdOdometerMeters,gpsOdometerMeters,gpsDistanceMeters"

# 5-day window (UTC, no microseconds)
END_DATE = datetime.now(timezone.utc).replace(microsecond=0)
START_DATE = (END_DATE - timedelta(days=5)).replace(microsecond=0)

OUTPUT_CSV = r"C:\Users\JonathanJoseph\OneDrive - United Utility\Vehicle stats\vehicle_mileage_report.csv"

# Timezone + formats
ET_TZ = ZoneInfo("America/New_York")
FMT_UTC = "%Y-%m-%d %H:%M:%S"
FMT_ET  = "%m/%d/%Y %H:%M"

# Debug toggles
DEBUG_SHOW_KEYS = True
DEBUG_SHOW_FIRST_OBJECTS = True
DEBUG_SAMPLE_COUNT = 3

# Retry / backoff
MAX_RETRIES = 6
BACKOFF_BASE = 0.75
REQUEST_TIMEOUT = 60

# ================== HELPERS ==================
def parse_company_from_name(name: str) -> Optional[str]:
    if not isinstance(name, str):
        return None
    m = re.match(r'^\s*\[([^\]]+)\]\s*(.+)?$', name)
    if m:
        return m.group(1).strip()
    for sep in (" | ", " - ", " — ", " – ", ":", "|", "-", "—", "–"):
        if sep in name:
            head = name.split(sep, 1)[0].strip()
            return head or None
    return None

def company_from_tags(tags: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if not tags:
        return None
    tags_by_id = {t.get("id"): t for t in tags if isinstance(t, dict) and t.get("id") is not None}
    tag_ids = set(tags_by_id.keys())
    roots = []
    for t in tags_by_id.values():
        cur = t
        while cur.get("parentTagId") in tag_ids:
            cur = tags_by_id[cur["parentTagId"]]
        roots.append(cur)
    seen, unique_roots = set(), []
    for r in roots:
        rid = r.get("id")
        if rid not in seen:
            unique_roots.append(r); seen.add(rid)
    if not unique_roots:
        return None
    chosen = sorted(unique_roots, key=lambda x: (str(x.get("name") or ""), x.get("id") or 0))[0]
    nm = chosen.get("name")
    return nm.strip() if isinstance(nm, str) else None

def trim_vehicle_for_debug(v: dict) -> dict:
    keys = ["id", "name", "vin", "licensePlate", "tags", "gateway", "createdAtTime", "updatedAtTime"]
    return {k: v[k] for k in keys if k in v}

def parse_time_to_dt(val) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        ts = val / 1000.0 if val > 1e10 else float(val)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(val, str):
        s = val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None

def fmt(dt: Optional[datetime], tz: timezone, fmt_str: str) -> Optional[str]:
    if dt is None:
        return None
    return dt.astimezone(tz).strftime(fmt_str)

def last_value_and_time(obj) -> Tuple[Optional[float], Optional[datetime]]:
    if isinstance(obj, dict):
        return obj.get("value"), parse_time_to_dt(obj.get("time"))
    if isinstance(obj, list) and obj:
        last = obj[-1]
        return last.get("value"), parse_time_to_dt(last.get("time"))
    return None, None

def to_float_or_none(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def http_get(url: str, *, headers: Dict[str, str], params: Optional[Dict[str, Any]] = None) -> requests.Response:
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if attempt >= MAX_RETRIES:
                raise
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt >= MAX_RETRIES:
                raise Exception(f"HTTP {resp.status_code} after {attempt} attempts: {resp.text[:500]}")
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    sleep_for = float(retry_after)
                except ValueError:
                    sleep_for = BACKOFF_BASE * (2 ** (attempt - 1))
            else:
                sleep_for = BACKOFF_BASE * (2 ** (attempt - 1))
            time.sleep(sleep_for)
            continue

        if resp.status_code != 200:
            raise Exception(f"Failed GET {url} -> {resp.status_code} - {resp.text[:500]}")
        return resp

# ================== API CALLS ==================
def fetch_active_vehicles() -> List[Dict[str, Any]]:
    url = f"{API_BASE_URL}/fleet/vehicles"
    vehicles, after, page = [], None, 0
    while True:
        page += 1
        params = {"limit": 100, "expand": "tags,gateway"}
        if after:
            params["after"] = after
        resp = http_get(url, headers=HEADERS, params=params)
        data = resp.json()
        batch = data.get("data", []) or []
        for v in batch:
            gw = v.get("gateway") or {}
            if not v.get("isDeactivated") and gw.get("serial"):
                vehicles.append(v)
        if page == 1 and batch:
            if DEBUG_SHOW_KEYS:
                print("\n[Vehicles] JSON header (keys) of a vehicle object:")
                print(sorted(list(batch[0].keys())))
            if DEBUG_SHOW_FIRST_OBJECTS:
                print("\n[Vehicles] Trimmed sample vehicle object(s):")
                for i, sv in enumerate(batch[:min(DEBUG_SAMPLE_COUNT, len(batch))], start=1):
                    print(f"\n--- Vehicle sample {i} ---")
                    print(json.dumps(trim_vehicle_for_debug(sv), indent=2, default=str))
        after = (data.get("pagination") or {}).get("endCursor")
        if not after:
            break
    print(f"\nLoaded {len(vehicles)} active, gateway-equipped vehicles.")
    return vehicles

def fetch_vehicle_stats(vehicle_ids: List[int]) -> List[Dict[str, Any]]:
    print("\nFetching vehicle stats (batched GET requests)...")
    all_stats, first_batch_seen = [], False
    for i in range(0, len(vehicle_ids), 50):
        batch_ids = vehicle_ids[i:i+50]
        ids_param = ",".join(str(vid) for vid in batch_ids)
        url = f"{API_BASE_URL}/fleet/vehicles/stats"
        params = {
            "startTime": iso_z(START_DATE),
            "endTime": iso_z(END_DATE),
            "types": STAT_TYPES,
            "vehicleIds": ids_param
        }
        resp = http_get(url, headers=HEADERS, params=params)
        batch = (resp.json().get("data") or [])
        all_stats.extend(batch)
        if not first_batch_seen and batch:
            first_batch_seen = True
            if DEBUG_SHOW_KEYS:
                print("\n[Stats] JSON header (keys) of a stats entry:")
                print(sorted(list(batch[0].keys())))
            if DEBUG_SHOW_FIRST_OBJECTS:
                print("\n[Stats] Sample stats entry(ies):")
                for j, it in enumerate(batch[:min(DEBUG_SAMPLE_COUNT, len(batch))], start=1):
                    print(f"\n--- Stats sample {j} ---")
                    print(json.dumps(it, indent=2, default=str))
        time.sleep(0.15)
    print(f"Total stats records returned: {len(all_stats)}")
    return all_stats

# ================== MAIN REPORT ==================
def compile_mileage_report() -> pd.DataFrame:
    vehicles = fetch_active_vehicles()
    if not vehicles:
        print("No vehicles returned after filtering (active + gateway).")
        return pd.DataFrame()

    vehicle_index: Dict[int, Dict[str, Any]] = {}
    for v in vehicles:
        vid = v.get("id")
        if vid is None:
            continue
        vname = v.get("name", "")
        comp = company_from_tags(v.get("tags")) or parse_company_from_name(vname)
        created_dt = parse_time_to_dt(v.get("createdAtTime"))
        updated_dt = parse_time_to_dt(v.get("updatedAtTime"))
        vehicle_index[vid] = {
            "vehicle_name": vname,
            "company": comp,
            "created_dt": created_dt,
            "updated_dt": updated_dt,
        }

    print("\n[Company/time parsing samples]")
    for vid, meta in list(vehicle_index.items())[:min(DEBUG_SAMPLE_COUNT, len(vehicle_index))]:
        print(
            f"  {vid}: Vehicle='{meta['vehicle_name']}' | Company='{meta['company']}' | "
            f"CreatedUTC={fmt(meta['created_dt'], timezone.utc, FMT_UTC)} | "
            f"UpdatedUTC={fmt(meta['updated_dt'], timezone.utc, FMT_UTC)}"
        )

    vehicle_ids = list(vehicle_index.keys())
    stats_data = fetch_vehicle_stats(vehicle_ids)

    rows = []
    for s in stats_data:
        vid = s.get("id")
        if vid not in vehicle_ids:
            continue

        meta = vehicle_index.get(vid, {})
        veh_name = meta.get("vehicle_name")
        company = meta.get("company")

        # raw latest values in window
        obd_val,  obd_dt  = last_value_and_time(s.get("obdOdometerMeters"))
        gpso_val, gpso_dt = last_value_and_time(s.get("gpsOdometerMeters"))
        gdist_val, gdist_dt = last_value_and_time(s.get("gpsDistanceMeters"))  # NOT the odometer

        obd_val   = to_float_or_none(obd_val)
        gpso_val  = to_float_or_none(gpso_val)
        gdist_val = to_float_or_none(gdist_val)

        # choose preferred odometer: OBD if available, else GPS
        if obd_val is not None and obd_val > 0:
            odo_meters = obd_val
            odo_source = "OBD"
            odo_time_utc = obd_dt
        elif gpso_val is not None and gpso_val > 0:
            odo_meters = gpso_val
            odo_source = "GPS"
            odo_time_utc = gpso_dt
        else:
            odo_meters = None
            odo_source = None
            odo_time_utc = None

        # conversions
        gps_odometer_miles = (gpso_val / 1609.34) if gpso_val is not None else None
        obd_odometer_miles = (obd_val  / 1609.34) if obd_val  is not None else None
        distance_miles     = (gdist_val / 1609.34) if gdist_val is not None else None
        odo_miles          = (odo_meters / 1609.34) if odo_meters is not None else None

        row = {
            "Vehicle ID": vid,
            "Company": company,
            "Vehicle Name": veh_name,

            "Vehicle Created (UTC)": fmt(meta.get("created_dt"), timezone.utc, FMT_UTC),
            "Vehicle Created (ET)":  fmt(meta.get("created_dt"), ET_TZ, FMT_ET),
            "Vehicle Updated (UTC)": fmt(meta.get("updated_dt"), timezone.utc, FMT_UTC),
            "Vehicle Updated (ET)":  fmt(meta.get("updated_dt"), ET_TZ, FMT_ET),

            # raw meters
            "obdOdometerMeters":  obd_val,
            "gpsOdometerMeters":  gpso_val,
            "gpsDistanceMeters":  gdist_val,

            # timestamps of last readings (for traceability)
            "obdOdometer Time (UTC)": fmt(obd_dt,        timezone.utc, FMT_UTC),
            "obdOdometer Time (ET)":  fmt(obd_dt,        ET_TZ,        FMT_ET),
            "gpsOdometer Time (UTC)": fmt(gpso_dt,       timezone.utc, FMT_UTC),
            "gpsOdometer Time (ET)":  fmt(gpso_dt,       ET_TZ,        FMT_ET),
            "gpsDistance Time (UTC)": fmt(gdist_dt,      timezone.utc, FMT_UTC),
            "gpsDistance Time (ET)":  fmt(gdist_dt,      ET_TZ,        FMT_ET),

            # conversions (clarified)
            "gpsDistanceMiles (NOT Odometer)": (round(distance_miles, 2) if distance_miles is not None else None),
            "gpsOdometerMiles_raw": (round(gps_odometer_miles, 2) if gps_odometer_miles is not None else None),
            "obdOdometerMiles_raw": (round(obd_odometer_miles, 2) if obd_odometer_miles is not None else None),

            # selection info
            "Odometer Source": odo_source,
            "OdometerMeters": odo_meters,

            # FINAL column: preferred odometer miles (OBD then GPS)
            "OdometerMiles": (round(odo_miles, 2) if odo_miles is not None else None),
        }
        rows.append(row)

    df = pd.DataFrame(rows)

    # Force Vehicle ID as text (avoid scientific notation in Excel)
    if "Vehicle ID" in df.columns:
        df["Vehicle ID"] = df["Vehicle ID"].astype(str)

    # Enforce column order: OdometerMiles LAST
    desired_cols = [
        "Vehicle ID","Company","Vehicle Name",
        "Vehicle Created (UTC)","Vehicle Created (ET)",
        "Vehicle Updated (UTC)","Vehicle Updated (ET)",
        "obdOdometerMeters","gpsOdometerMeters","gpsDistanceMeters",
        "obdOdometer Time (UTC)","obdOdometer Time (ET)",
        "gpsOdometer Time (UTC)","gpsOdometer Time (ET)",
        "gpsDistance Time (UTC)","gpsDistance Time (ET)",
        "gpsDistanceMiles (NOT Odometer)",
        "gpsOdometerMiles_raw","obdOdometerMiles_raw",
        "Odometer Source","OdometerMeters",
        "OdometerMiles"  # <-- LAST and this is your true mileage
    ]
    remaining = [c for c in df.columns if c not in desired_cols]
    df = df[[c for c in desired_cols if c in df.columns] + remaining]
    df = df.sort_values(["Company","Vehicle Name","Vehicle ID"], na_position="last").reset_index(drop=True)

    print("\n[DataFrame sample]")
    print(df.head(10))
    return df

# ================== ENTRYPOINT ==================
if __name__ == "__main__":
    df = compile_mileage_report()
    os.makedirs(os.path.dirname(OUTPUT_CSV), exist_ok=True)
    df.to_csv(OUTPUT_CSV, index=False)
    print(f"\n✅ Data saved to {OUTPUT_CSV}")
