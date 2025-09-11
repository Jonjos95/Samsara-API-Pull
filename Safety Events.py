import requests
import csv
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Iterable, Tuple
from collections import Counter
import os
import sys
import json

# =========================
# CONFIG
# =========================
HEADERS_BASE = {"Accept": "application/json"}
CSV_FILE = r"C:\Users\JonathanJoseph\OneDrive - United Utility\Samsara Safety Dashboard\safety_event_report.csv"
DISPLAY_TZ = "America/New_York"
ORG_ID: Optional[str] = "10005082"

# Put your tag→company table here (2-column CSV: Vehicle Tag(s), Company)
COMPANY_MAP_CSV = r"C:\Users\JonathanJoseph\OneDrive - United Utility\TagLookup.csv"

LOOKBACK_DAYS = 7
END_MS = int(datetime.now(timezone.utc).timestamp() * 1000)
START_MS = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

REQUEST_TIMEOUT = 60
PAGE_PAUSE_SEC = 0.12
PAGE_SIZE_CANDIDATES = [1000, 500, 200]
MAX_PAGES_SAFEGUARD = 20000

# Include ALL statuses
REVIEW_ONLY = False

# Collapse bursty detections (same veh/driver/type within window)
ENABLE_BURST_DEDUP = True
DEDUP_WINDOW_SEC = 90
DEDUP_BY_EVENT_ID = True

# Debug (set False to quiet)
DEBUG_PRINT_FIRST_HIERARCHY = False
DEBUG_PRINT_FIRST_JSON = False
DEBUG_PRINT_FIRST_ROW_SNAPSHOT = False
DEBUG_MAX_DEPTH = 10
DEBUG_TRUNCATE_LISTS_TO = 1

# =========================
# LOGGING / TIME HELPERS
# =========================
def log(msg: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)

def fmt_local(ms: int, tz_name: str, with_seconds: bool = False) -> str:
    try:
        from zoneinfo import ZoneInfo
        dt_local = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz_name))
    except Exception:
        dt_local = datetime.fromtimestamp(ms / 1000).astimezone()
    if os.name == "nt":
        return dt_local.strftime("%#m/%#d/%Y %#H:%M:%S" if with_seconds else "%#m/%#d/%Y %#H:%M")
    return dt_local.strftime("%-m/%-d/%Y %-H:%M:%S" if with_seconds else "%-m/%-d/%Y %-H:%M")

def fmt_utc(ms: int, with_seconds: bool = True) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC" if with_seconds else "%Y-%m-%d %H:%M UTC")

def parse_any_time(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val if val >= 100_000_000_000 else val * 1000)
    if isinstance(val, str):
        s = val.strip()
        if s.replace(".", "", 1).isdigit():
            f = float(s)
            return int(f if f >= 100_000_000_000 else f * 1000)
        try:
            s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    if isinstance(val, dict):
        for k in ("ms", "epochMs", "timestampMs", "timeMs", "value"):
            if k in val and val[k] is not None:
                return parse_any_time(val[k])
    return None

# =========================
# API HELPERS
# =========================
def get_api_token() -> str:
    token = os.getenv("SAMSARA_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Missing SAMSARA_API_TOKEN environment variable.")
    return token

def auth_headers() -> Dict[str, str]:
    return {**HEADERS_BASE, "Authorization": f"Bearer {get_api_token()}"}

def _collect_tag_names(val) -> List[str]:
    """
    Extract raw tag names from the API 'tags' value into a plain list of strings.
    Works with arrays of strings or arrays of {name} / {tag: {name}} objects.
    """
    if not val:
        return []
    names: List[str] = []
    if isinstance(val, list):
        for t in val:
            if isinstance(t, str):
                names.append(t)
            elif isinstance(t, dict):
                if isinstance(t.get("name"), str):
                    names.append(t["name"])
                elif isinstance(t.get("tag"), dict) and isinstance(t["tag"].get("name"), str):
                    names.append(t["tag"]["name"])
    return [n.strip() for n in names if n and str(n).strip()]

def join_tag_names(val) -> str:
    """String for CSV display."""
    names = sorted(set(_collect_tag_names(val)))
    return ", ".join(names)

def safe_get(d: Dict[str, Any], *path, default=None):
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def humanize_event_type(raw: Optional[str]) -> str:
    if not raw:
        return ""
    s = str(raw).replace("_", " ").replace("-", " ")
    out = []
    for i, ch in enumerate(s):
        if i > 0 and ch.isupper() and s[i-1].islower():
            out.append(" ")
        out.append(ch)
    s2 = "".join(out).lower()
    return s2.replace("braking", "brake").replace("turning", "turn")

def humanize_label(raw: Optional[str]) -> str:
    if raw is None:
        return ""
    s = str(raw).replace("_", " ").replace("-", " ")
    out = []
    for i, ch in enumerate(s):
        if i > 0 and ch.isupper() and s[i-1].islower():
            out.append(" ")
        out.append(ch)
    return "".join(out).strip().title()

def build_event_url(event: Dict[str, Any]) -> str:
    for key in ("eventUrl", "reviewUrl", "url"):
        val = event.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    ev_id = event.get("id") or event.get("eventId")
    org_id = (ORG_ID or event.get("orgId") or event.get("organizationId"))
    if ev_id and org_id:
        return f"https://cloud.samsara.com/o/{org_id}/fleet/safety/event_review?selectedEvent={ev_id}"
    return ""

# =========================
# KEY TREE PRINTER (optional)
# =========================
def _type_name(v: Any) -> str:
    if isinstance(v, dict): return "object"
    if isinstance(v, list): return "array"
    if isinstance(v, str): return "string"
    if isinstance(v, bool): return "bool"
    if v is None: return "null"
    if isinstance(v, (int, float)): return "number"
    return type(v).__name__

def print_key_tree(obj: Any, prefix: str = "", depth: int = 0) -> None:
    if depth > DEBUG_MAX_DEPTH:
        print(prefix + "… (max depth reached)")
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{prefix}- {k} ({_type_name(v)})")
            print_key_tree(v, prefix + "  ", depth + 1)
    elif isinstance(obj, list):
        ln = len(obj)
        print(f"{prefix}- [array] len={ln}")
        if ln:
            n = min(ln, DEBUG_TRUNCATE_LISTS_TO)
            for i in range(n):
                print(f"{prefix}  └─ [{i}] ({_type_name(obj[i])})")
                print_key_tree(obj[i], prefix + "     ", depth + 1)

# =========================
# EVENT-TYPE EXTRACTION
# =========================
def _walk_keyvals(obj: Any, prefix: Tuple[str, ...] = ()) -> Iterable[Tuple[Tuple[str, ...], Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_keyvals(v, prefix + (str(k),))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keyvals(v, prefix + (str(i),))
    else:
        yield prefix, obj

def extract_event_type(ev: Dict[str, Any]) -> str:
    candidates: List[Any] = [
        ev.get("eventType"),
        ev.get("type"),
        safe_get(ev, "category"),
        safe_get(ev, "event", "type"),
        safe_get(ev, "eventType", "type"),
        safe_get(ev, "classification"),
        safe_get(ev, "detection", "type"),
        safe_get(ev, "safety", "type"),
        safe_get(ev, "details", "type"),
        safe_get(ev, "summary", "type"),
        safe_get(ev, "metadata", "type"),
        safe_get(ev, "label"),
    ]
    for cand in candidates:
        if not cand:
            continue
        if isinstance(cand, dict):
            for k in ("type", "name", "code", "key", "category", "label"):
                v = cand.get(k)
                if v:
                    return humanize_event_type(str(v))
        if isinstance(cand, str):
            return humanize_event_type(cand)

    bl = ev.get("behaviorLabels")
    if isinstance(bl, list) and bl:
        first = bl[0]
        if isinstance(first, str):
            return humanize_event_type(first)
        if isinstance(first, dict):
            for k in ("name", "label", "type", "code"):
                if isinstance(first.get(k), str) and first.get(k):
                    return humanize_event_type(first[k])

    for path, val in _walk_keyvals(ev):
        if val and isinstance(val, str) and any("type" in p.lower() for p in path):
            return humanize_event_type(val)

    if "maxAccelerationForce" in ev:
        return "hard acceleration"
    return ""

# =========================
# PAGINATED FETCH (SAFE)
# =========================
def fetch_all(endpoint: str, params: Dict[str, Any], label: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 0
    seen_cursors = set()
    last_first_id = None

    while True:
        page += 1
        if page > MAX_PAGES_SAFEGUARD:
            raise RuntimeError(f"Pagination exceeded {MAX_PAGES_SAFEGUARD} pages; aborting to avoid infinite loop.")

        shown = {k: v for k, v in params.items()
                 if k not in ("limit", "cursor", "after", "startingAfter", "nextToken", "pageToken")}
        log(f"GET {label}: page {page} params={shown}")
        resp = requests.get(endpoint, headers=auth_headers(), params=params, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 401:
            raise PermissionError(f"401 from {endpoint}: {resp.text[:300]}")
        if resp.status_code != 200:
            raise RuntimeError(f"GET {endpoint} -> {resp.status_code}: {resp.text[:500]}")
        data = resp.json()

        data_list = None
        for key in ("data", "events", "items", "safetyEvents"):
            if key in data and isinstance(data[key], list):
                data_list = data[key]; break
        if data_list is None:
            data_list = data if isinstance(data, list) else []

        if not data_list:
            log(f"{label}: +0 (no items) — stopping.")
            break

        out.extend(data_list)
        log(f"{label}: +{len(data_list)} (total {len(out)})")

        first_id = None
        if isinstance(data_list[0], dict):
            first_id = data_list[0].get("id") or data_list[0].get("eventId")
        if first_id and last_first_id and first_id == last_first_id:
            log("Detected repeated page (same first id) — stopping pagination.")
            break
        last_first_id = first_id

        pag = data.get("pagination") or data.get("page") or {}
        next_cursor = None
        has_next = False
        token_name = None

        if isinstance(pag, dict):
            has_next = bool(pag.get("hasNextPage") or pag.get("hasMore") or pag.get("more"))
            if pag.get("nextPageToken"):
                next_cursor = pag.get("nextPageToken"); token_name = "pageToken"
            elif pag.get("endCursor") or pag.get("nextCursor") or pag.get("cursor"):
                next_cursor = pag.get("endCursor") or pag.get("nextCursor") or pag.get("cursor")
                token_name = "after"
        if not next_cursor and data.get("nextToken"):
            next_cursor = data.get("nextToken"); token_name = "nextToken"; has_next = bool(next_cursor)

        if not has_next or not next_cursor:
            break

        key = f"{token_name}:{next_cursor}"
        if key in seen_cursors:
            log("Next cursor repeats — stopping pagination.")
            break
        seen_cursors.add(key)

        for param_key in ("after", "startingAfter", "pageToken", "nextToken", "cursor"):
            if param_key in params and param_key != token_name:
                params.pop(param_key, None)
        if token_name:
            params[token_name] = next_cursor
        else:
            params["after"] = next_cursor

        time.sleep(PAGE_PAUSE_SEC)

    return out

def fetch_all_with_pagesize(url: str, base_params: Dict[str, Any], label: str) -> List[Dict[str, Any]]:
    last_err = None
    for size in PAGE_SIZE_CANDIDATES:
        params = dict(base_params)
        params["limit"] = size
        try:
            log(f"→ Using page size {size}")
            out = fetch_all(url, params, label)
            log(f"✔ page size {size} accepted")
            return out
        except RuntimeError as e:
            txt = str(e)
            last_err = e
            if "limit" in txt.lower() or "page size" in txt.lower() or "invalid parameter" in txt.lower():
                log(f"↪ fallback to smaller page size (server said: {txt[:120]} …)")
                continue
            raise
    raise last_err if last_err else RuntimeError("Failed to fetch with any page size")

def fetch_safety_events(start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    url = "https://api.samsara.com/fleet/safety-events"
    try:
        log("→ Querying safety events with startMs/endMs…")
        return fetch_all_with_pagesize(
            url,
            {"startMs": start_ms, "endMs": end_ms, "include": "driver,vehicle,location,assignedCoach"},
            "safety-events",
        )
    except RuntimeError as e:
        txt = str(e)
        if "starttime" in txt.lower() or "missing" in txt.lower() or "bad request" in txt.lower():
            iso_start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            iso_end   = datetime.fromtimestamp(end_ms   / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            log("→ Retrying with startTime/endTime (ISO)…")
            return fetch_all_with_pagesize(
                url,
                {"startTime": iso_start, "endTime": iso_end, "include": "driver,vehicle,location,assignedCoach"},
                "safety-events",
            )
        raise

# =========================
# OPTIONAL MAPS
# =========================
def soft_fetch_map(name: str, endpoint: str, id_fields: List[str], name_keys: List[str], tag_paths: List[List[str]]):
    try:
        items = fetch_all(endpoint, {"limit": 500}, label=name)
    except PermissionError as e:
        log(f"⚠️ Skipping {name} map (scope/token): {e}"); return {}
    except Exception as e:
        log(f"⚠️ Skipping {name} map: {e}"); return {}

    m = {}
    for it in items:
        _id = ""
        for k in id_fields:
            v = it.get(k) or safe_get(it, *k.split("."))
            if v: _id = str(v); break
        if not _id:
            continue

        nm = ""
        if "firstName" in it or "lastName" in it:
            nm = f"{it.get('firstName','')} {it.get('lastName','')}".strip()
        if not nm:
            for k in name_keys:
                v = it.get(k) or safe_get(it, *k.split("."))
                if v:
                    nm = str(v).strip(); break

        tval = []
        for path in tag_paths:
            v = safe_get(it, *path)
            if v:
                tval = v; break

        m[_id] = {"name": nm, "tags": tval}
    log(f"{name} map ready: {len(m)}")
    return m

def fetch_vehicles_map() -> Dict[str, Dict[str, Any]]:
    return soft_fetch_map(
        "vehicles", "https://api.samsara.com/fleet/vehicles",
        id_fields=["id","vehicleId"],
        name_keys=["name","vehicleName","externalIds.vin"],
        tag_paths=[["tags"],["device","tags"],["vehicle","tags"]],
    )

def fetch_drivers_map() -> Dict[str, Dict[str, Any]]:
    return soft_fetch_map(
        "drivers", "https://api.samsara.com/fleet/drivers",
        id_fields=["id","driverId"],
        name_keys=["name"],
        tag_paths=[["tags"]],
    )

# =========================
# COMPANY MAP (Device Tags → Company)
# =========================
def _norm_tag(s: str) -> str:
    return " ".join(str(s).split()).strip()  # collapse spaces & trim

def load_company_map(csv_path: str) -> List[Tuple[frozenset, str]]:
    """
    Returns a list of (tag_set, company) where tag_set is a frozenset of normalized tags.
    Each CSV row: first cell = one or many tags (comma-separated), second cell = Company.
    The order of tags in the CSV does NOT matter.
    """
    entries: List[Tuple[frozenset, str]] = []
    if not csv_path or not os.path.exists(csv_path):
        log(f"⚠️ Company map CSV not found: {csv_path}. Company column will default to 'Unknown'.")
        return entries

    with open(csv_path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader, None)  # optional header
        for row in reader:
            if not row or len(row) < 2:
                continue
            left, company = row[0], row[1]
            if not left or not company:
                continue
            tags = [ _norm_tag(x) for x in left.split(",") if _norm_tag(x) ]
            tagset = frozenset(tags)
            entries.append((tagset, company.strip()))
    # Sort by specificity (more tags first)
    entries.sort(key=lambda kv: len(kv[0]), reverse=True)
    log(f"Company map loaded: {len(entries)} rows")
    return entries

def infer_company_from_tags(device_tag_names: List[str], company_map: List[Tuple[frozenset, str]]) -> str:
    """
    Given the list of tag names on the device, choose the best company by subset matching.
    - Find all mapping rows whose tagset ⊆ device_tags_set.
    - Pick the one with the largest tagset (most specific).
    - If none match, try single-tag contains/equals heuristics.
    """
    if not device_tag_names:
        return "Unknown"
    s = set(_norm_tag(t) for t in device_tag_names if t)

    # 1) Subset match (most specific first; map already sorted)
    for tagset, company in company_map:
        if tagset and tagset.issubset(s):
            return company

    # 2) Heuristic fallback: if a single-tag entry appears as an exact tag, use it
    singles = { next(iter(ts)): comp for ts, comp in company_map if len(ts) == 1 }
    for t in s:
        if t in singles:
            return singles[t]

    # 3) Heuristic fallback: substring match against single-tag entries
    for t in s:
        for single_tag, comp in singles.items():
            if single_tag in t or t in single_tag:
                return comp

    return "Unknown"

# =========================
# MAIN
# =========================
def main():
    try:
        _ = get_api_token()
    except RuntimeError as e:
        log(f"❌ {e}"); sys.exit(1)

    log(f"Date window (last {LOOKBACK_DAYS} days)")
    log(f"  Local ({DISPLAY_TZ}): {fmt_local(START_MS, DISPLAY_TZ, True)}  →  {fmt_local(END_MS, DISPLAY_TZ, True)}")
    log(f"  UTC:                 {fmt_utc(START_MS)}  →  {fmt_utc(END_MS)}")

    # Load company map
    company_map = load_company_map(COMPANY_MAP_CSV)

    log("Fetching safety events…")
    try:
        events = fetch_safety_events(START_MS, END_MS)
    except PermissionError as e:
        log("❌ Token lacks Safety Events read scope."); log(str(e)); sys.exit(1)
    except Exception as e:
        log(f"❌ Error fetching safety events: {e}"); sys.exit(1)

    raw_count = len(events)
    log(f"Total raw events fetched: {raw_count}")

    if events:
        first = events[0]
        if DEBUG_PRINT_FIRST_HIERARCHY:
            log("DEBUG HIERARCHY (first raw event):")
            print_key_tree(first)
        if DEBUG_PRINT_FIRST_JSON:
            log("DEBUG FULL JSON (first raw event):")
            try:
                print(json.dumps(first, indent=2, ensure_ascii=False)[:200000])
            except Exception:
                log("⚠️ Could not pretty-print event JSON")

    log("Fetching vehicles map (optional)…")
    vehicles_map = fetch_vehicles_map()

    log("Fetching drivers map (optional)…")
    drivers_map = fetch_drivers_map()

    os.makedirs(os.path.dirname(CSV_FILE), exist_ok=True)

    header = [
        "Time",
        "Vehicle",
        "Driver",
        "Driver Tags",
        "Event Type",
        "Status",
        "Address",
        "Latitude",
        "Longitude",
        "Event URL",
        "Assigned Coach",
        "Device Tags",
        "Company",            # <— new column
    ]

    kept_after_review = 0
    kept_after_dedup = 0
    missing_vehicle = 0
    missing_driver = 0
    type_counter = Counter()
    status_counter = Counter()
    seen_buckets = set()
    seen_event_ids = set()

    log(f"Writing CSV → {CSV_FILE}")
    written = 0
    with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)

        for ev in events:
            if DEDUP_BY_EVENT_ID:
                ev_id = ev.get("id") or ev.get("eventId")
                if ev_id:
                    if ev_id in seen_event_ids:
                        continue
                    seen_event_ids.add(ev_id)

            ts_ms = (
                parse_any_time(ev.get("timestampMs"))
                or parse_any_time(ev.get("eventTimeMs"))
                or parse_any_time(ev.get("timeMs"))
                or parse_any_time(safe_get(ev, "time", "ms"))
                or parse_any_time(ev.get("time"))
            )
            time_str = fmt_local(ts_ms, DISPLAY_TZ) if ts_ms else ""

            vehicle_id = str(
                ev.get("vehicleId") or safe_get(ev, "vehicle","id")
                or safe_get(ev, "device","vehicleId") or ""
            )
            vehicle_name = (
                safe_get(ev, "vehicle","name") or ev.get("vehicleName")
                or vehicles_map.get(vehicle_id, {}).get("name") or ""
            )
            if not vehicle_name:
                missing_vehicle += 1

            # Device tags (both list for matching and string for CSV)
            device_tags_val = (
                safe_get(ev, "device","tags") or safe_get(ev, "vehicle","tags")
                or vehicles_map.get(vehicle_id, {}).get("tags") or []
            )
            device_tag_names = sorted(set(_collect_tag_names(device_tags_val)))
            device_tags_str = ", ".join(device_tag_names)

            # Driver info
            driver_id = str(ev.get("driverId") or safe_get(ev, "driver","id") or "")
            driver_name = (
                safe_get(ev, "driver","name") or drivers_map.get(driver_id, {}).get("name") or ""
            ) or "-"
            if driver_name == "-":
                missing_driver += 1

            driver_tags_val = (
                safe_get(ev, "driver","tags") or drivers_map.get(driver_id, {}).get("tags") or []
            )
            driver_tags = ", ".join(sorted(set(_collect_tag_names(driver_tags_val))))

            event_type = extract_event_type(ev)

            # Status (use top-level coachingState first)
            status_raw = (
                ev.get("coachingState") or
                ev.get("status") or
                ev.get("reviewStatus") or
                safe_get(ev, "coaching", "status") or
                "needsReview"
            )
            status = humanize_label(status_raw)
            status_counter[status] += 1

            event_url = build_event_url(ev)

            address = (
                safe_get(ev, "location","formattedAddress")
                or safe_get(ev, "address","formattedAddress")
                or safe_get(ev, "location","name") or ""
            )
            lat = safe_get(ev, "location","latitude") or ev.get("latitude")
            lon = safe_get(ev, "location","longitude") or ev.get("longitude")
            lat = lat if lat is not None else ""
            lon = lon if lon is not None else ""

            kept_after_review += 1

            if ENABLE_BURST_DEDUP and ts_ms and vehicle_id and event_type:
                bucket = int(ts_ms // (DEDUP_WINDOW_SEC * 1000))
                dedup_key = (vehicle_id, driver_id or "", event_type, bucket)
                if dedup_key in seen_buckets:
                    continue
                seen_buckets.add(dedup_key)
            kept_after_dedup += 1

            # New: infer company from device tags via mapping
            company = infer_company_from_tags(device_tag_names, company_map)

            if DEBUG_PRINT_FIRST_ROW_SNAPSHOT and written == 0:
                log("DEBUG FIRST ROW SNAPSHOT (derived fields before CSV):")
                snapshot = {
                    "time_ms": ts_ms,
                    "time_local": time_str,
                    "vehicle_id": vehicle_id,
                    "vehicle_name": vehicle_name,
                    "driver_id": driver_id,
                    "driver_name": driver_name,
                    "driver_tags": driver_tags,
                    "device_tags": device_tags_str,
                    "event_type": event_type,
                    "status": status,
                    "address": address,
                    "latitude": lat,
                    "longitude": lon,
                    "event_url": event_url,
                    "assigned_coach": (
                        safe_get(ev, "coaching","assignedCoach","name")
                        or safe_get(ev, "assignedCoach","name") or "Unassigned"
                    ),
                    "company": company,
                }
                print(json.dumps(snapshot, indent=2, ensure_ascii=False))

            assigned_coach = (
                safe_get(ev, "coaching","assignedCoach","name")
                or safe_get(ev, "assignedCoach","name") or "Unassigned"
            )

            writer.writerow([
                time_str,
                vehicle_name,
                driver_name,
                driver_tags,
                event_type,
                status,
                address,
                lat,
                lon,
                event_url,
                assigned_coach,
                device_tags_str,
                company,  # <— new column value
            ])
            written += 1
            type_counter[event_type] += 1

            if written % 200 == 0:
                log(f"…written {written} rows")

    log("----- SUMMARY -----")
    log(f"Raw events fetched:        {raw_count}")
    log(f"After review-only filter:  {kept_after_review}")
    log(f"After burst dedup:         {kept_after_dedup}")
    log(f"Rows written:              {written}")
    log(f"Missing vehicle names:     {missing_vehicle}")
    log(f"Missing driver names:      {missing_driver}")
    if type_counter:
        log("Top event types (first 10): " + ", ".join(f"{k}:{v}" for k, v in type_counter.most_common(10)))
    if status_counter:
        log("Statuses seen: " + ", ".join(f"{k}:{v}" for k, v in status_counter.most_common()))
    log(f"Saved file: {CSV_FILE}")

if __name__ == "__main__":
    main()
