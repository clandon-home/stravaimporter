import json
import os
import re
from datetime import datetime, timedelta, timezone

SYNCED_FILE = "synced_activities.json"


def load_synced_ids():
    if os.path.exists(SYNCED_FILE):
        with open(SYNCED_FILE) as f:
            return set(str(x) for x in json.load(f))
    return set()


def save_synced_ids(ids):
    with open(SYNCED_FILE, "w") as f:
        json.dump(sorted(ids), f, indent=2)


def _fitbit_time_to_utc(fitbit_time_str):
    """Return a UTC-aware datetime from a Fitbit timestamp string."""
    cleaned = re.sub(r"\.\d+", "", fitbit_time_str)
    return datetime.fromisoformat(cleaned).astimezone(timezone.utc)


def _parse_local_time(fitbit_time_str):
    """
    Fitbit timestamps look like "2024-01-15T10:00:00.000-05:00".
    Strava's start_date_local wants the local clock time, e.g. "2024-01-15T10:00:00".
    """
    cleaned = re.sub(r"\.\d+", "", fitbit_time_str)  # drop milliseconds
    dt = datetime.fromisoformat(cleaned)             # parses offset-aware
    return dt.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")


def _local_hms_to_utc(fitbit_start_time_str, date_str, hms_str):
    """Convert an intraday local HH:MM:SS to a UTC ISO-8601 string.

    Uses the UTC offset embedded in the Fitbit startTime (e.g. "2024-01-15T10:00:00.000-05:00").
    """
    cleaned = re.sub(r"\.\d+", "", fitbit_start_time_str)
    offset = datetime.fromisoformat(cleaned).utcoffset()
    local_dt = datetime.strptime(f"{date_str}T{hms_str}", "%Y-%m-%dT%H:%M:%S")
    utc_dt = (local_dt - offset).replace(tzinfo=timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_meters(distance, unit):
    if unit == "miles":
        return round(distance * 1609.344)
    return round(distance * 1000)  # km -> m


def fitbit_activity_to_strava(activity, distance_unit="km"):
    """Build a Strava activity creation payload from a Fitbit activity dict."""
    start_local = _parse_local_time(activity.get("startTime", ""))
    duration_s = activity.get("duration", 0) // 1000
    distance_m = _to_meters(activity.get("distance", 0), distance_unit)
    calories = activity.get("calories", 0)
    name = activity.get("activityName", "Swim")
    avg_hr = activity.get("averageHeartRate")
    swim_lengths = activity.get("swimLengths")

    desc_parts = ["Imported from Fitbit"]
    if swim_lengths:
        desc_parts.append(f"Laps: {swim_lengths}")
    if avg_hr:
        desc_parts.append(f"Avg HR: {avg_hr} bpm")
    desc_parts.append(f"Calories: {calories}")

    return {
        "name": f"{name} (Fitbit)",
        "sport_type": "Swim",
        "start_date_local": start_local,
        "elapsed_time": duration_s,
        "distance": distance_m,
        "description": " | ".join(desc_parts),
    }


def preview_swims(fitbit_client, fitbit_tokens, strava_client, strava_tokens, days_back=30):
    """Scan Fitbit swims and classify each as 'new' or 'conflict' against Strava."""
    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Fitbit API returns km by default (no Accept-Language header), regardless of profile locale
    distance_unit = "km"

    fitbit_swims = fitbit_client.get_swim_activities(fitbit_tokens["access_token"], after_date)
    strava_swims = strava_client.get_swim_activities(strava_tokens["access_token"], after_date)

    # Build a lookup: Strava UTC datetime → activity
    strava_by_time = {}
    for sa in strava_swims:
        dt = datetime.fromisoformat(sa["start_date"].replace("Z", "+00:00"))
        strava_by_time[dt] = sa

    activities = []
    for fa in fitbit_swims:
        log_id = str(fa.get("logId", ""))
        detail = {}
        hr_data = None

        try:
            detail = fitbit_client.get_activity_detail(fitbit_tokens["access_token"], log_id)
            for key in ("swimLengths", "averageHeartRate"):
                if detail.get(key) is not None:
                    fa = {**fa, key: detail[key]}
            if detail.get("startDate") and detail.get("startTime"):
                hr_data = fitbit_client.get_heartrate_intraday(
                    fitbit_tokens["access_token"],
                    detail["startDate"],
                    detail["startTime"],
                    detail.get("duration", fa.get("duration", 0)),
                )
                if hr_data:
                    fa = {**fa, "averageHeartRate": hr_data["avg"]}
        except Exception as _hr_exc:
            print(f"[HR fetch {log_id}]: {_hr_exc}")

        payload = fitbit_activity_to_strava(fa, distance_unit)
        fa_utc = _fitbit_time_to_utc(fa.get("startTime", ""))
        start_utc = fa_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Build UTC-stamped HR dataset for TCX generation
        max_hr = None
        hr_dataset_utc = []
        if hr_data:
            max_hr = hr_data.get("max")
            start_time_str = fa.get("startTime", "")
            date_str = detail.get("startDate") or start_time_str[:10]
            activity_end_utc = fa_utc + timedelta(milliseconds=fa.get("duration", detail.get("duration", 0)))
            for entry in hr_data.get("dataset", []):
                if entry.get("value", 0) > 0:
                    try:
                        entry_utc_str = _local_hms_to_utc(start_time_str, date_str, entry["time"])
                        entry_utc = datetime.strptime(entry_utc_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        if entry_utc <= activity_end_utc:
                            hr_dataset_utc.append({"time": entry_utc_str, "value": entry["value"]})
                    except Exception:
                        pass

        # Find a Strava swim within ±5 minutes
        existing = next(
            (sa for dt, sa in strava_by_time.items() if abs((dt - fa_utc).total_seconds()) <= 300),
            None,
        )

        activities.append({
            "log_id": log_id,
            "name": fa.get("activityName", "Swim"),
            "date": fa.get("startTime", "")[:10],
            "duration_s": payload["elapsed_time"],
            "distance_m": payload["distance"],
            "calories": fa.get("calories", 0),
            "avg_hr": fa.get("averageHeartRate"),
            "max_hr": max_hr,
            "start_utc": start_utc,
            "hr_dataset_utc": hr_dataset_utc,
            "payload": payload,
            "status": "conflict" if existing else "new",
            "existing_strava_id": existing["id"] if existing else None,
        })

    return activities


def build_tcx(activity):
    """Generate a TCX XML string for a swim activity with HR trackpoints."""
    start_utc = activity["start_utc"]
    duration_s = activity["duration_s"]
    distance_m = activity["distance_m"]
    calories = activity.get("calories", 0)
    avg_hr = activity.get("avg_hr")
    max_hr = activity.get("max_hr")
    dataset = activity.get("hr_dataset_utc", [])

    # Ensure the dataset spans the exact activity duration so Strava computes
    # elapsed_time = last_trackpoint - first_trackpoint = duration_s.
    end_utc = (
        datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        + timedelta(seconds=duration_s)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    anchored = list(dataset)
    if anchored and anchored[0]["time"] != start_utc:
        anchored.insert(0, {"time": start_utc, "value": anchored[0]["value"]})
    if anchored and anchored[-1]["time"] != end_utc:
        anchored.append({"time": end_utc, "value": anchored[-1]["value"]})

    trackpoints = "".join(
        f"\n          <Trackpoint>"
        f"<Time>{e['time']}</Time>"
        f"<HeartRateBpm><Value>{e['value']}</Value></HeartRateBpm>"
        f"</Trackpoint>"
        for e in anchored
    )
    avg_xml = f"\n        <AverageHeartRateBpm><Value>{avg_hr}</Value></AverageHeartRateBpm>" if avg_hr else ""
    max_xml = f"\n        <MaximumHeartRateBpm><Value>{max_hr}</Value></MaximumHeartRateBpm>" if max_hr else ""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<TrainingCenterDatabase'
        ' xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <Activities>\n"
        '    <Activity Sport="Swimming">\n'
        f"      <Id>{start_utc}</Id>\n"
        f'      <Lap StartTime="{start_utc}">\n'
        f"        <TotalTimeSeconds>{duration_s}</TotalTimeSeconds>\n"
        f"        <DistanceMeters>{distance_m}</DistanceMeters>\n"
        f"        <Calories>{calories}</Calories>"
        f"{avg_xml}{max_xml}\n"
        "        <Intensity>Active</Intensity>\n"
        "        <TriggerMethod>Manual</TriggerMethod>\n"
        f"        <Track>{trackpoints}\n"
        "        </Track>\n"
        "      </Lap>\n"
        "    </Activity>\n"
        "  </Activities>\n"
        "</TrainingCenterDatabase>"
    )


def sync_swims(fitbit_client, fitbit_tokens, strava_client, strava_tokens, days_back=30, replace=False):
    after_date = datetime.now(timezone.utc) - timedelta(days=days_back)

    # Fitbit API returns km by default (no Accept-Language header), regardless of profile locale
    distance_unit = "km"

    swims = fitbit_client.get_swim_activities(fitbit_tokens["access_token"], after_date)
    synced_ids = load_synced_ids()

    results = {"synced": [], "skipped": [], "errors": []}

    for activity in swims:
        log_id = str(activity.get("logId", ""))
        try:
            detail = fitbit_client.get_activity_detail(fitbit_tokens["access_token"], log_id)
            activity = {**activity, **{k: v for k, v in detail.items() if v is not None}}
        except Exception:
            pass
        display = {
            "id": log_id,
            "name": activity.get("activityName", "Swim"),
            "date": activity.get("startTime", "")[:10],
        }

        if log_id in synced_ids:
            results["skipped"].append({**display, "reason": "Already synced"})
            continue

        try:
            payload = fitbit_activity_to_strava(activity, distance_unit)
            strava_activity = strava_client.create_activity(
                strava_tokens["access_token"], payload
            )
            synced_ids.add(log_id)
            save_synced_ids(synced_ids)
            results["synced"].append(
                {
                    **display,
                    "strava_id": strava_activity.get("id"),
                    "distance_m": payload["distance"],
                    "duration_s": payload["elapsed_time"],
                }
            )
        except Exception as exc:
            import requests
            if isinstance(exc, requests.HTTPError) and exc.response.status_code == 409:
                if replace:
                    # Find and delete the existing Strava activity, then re-upload
                    try:
                        existing_id = strava_client.find_activity_at(
                            strava_tokens["access_token"], payload["start_date_local"]
                        )
                        if existing_id:
                            strava_client.delete_activity(strava_tokens["access_token"], existing_id)
                            strava_activity = strava_client.create_activity(
                                strava_tokens["access_token"], payload
                            )
                            synced_ids.add(log_id)
                            save_synced_ids(synced_ids)
                            results["synced"].append(
                                {
                                    **display,
                                    "strava_id": strava_activity.get("id"),
                                    "distance_m": payload["distance"],
                                    "duration_s": payload["elapsed_time"],
                                    "replaced": True,
                                }
                            )
                        else:
                            results["errors"].append({**display, "error": "Conflict on Strava but could not find existing activity to replace"})
                    except Exception as replace_exc:
                        results["errors"].append({**display, "error": f"Replace failed: {replace_exc}"})
                else:
                    synced_ids.add(log_id)
                    save_synced_ids(synced_ids)
                    results["skipped"].append({**display, "reason": "Already exists on Strava"})
            else:
                results["errors"].append({**display, "error": str(exc)})

    return results
