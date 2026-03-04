import base64
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

FITBIT_AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE = "https://api.fitbit.com"

# Fitbit activity type IDs for swimming
SWIM_ACTIVITY_IDS = {90019, 90024}  # Pool Swim, Open Water Swim


class FitbitClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret

    def get_auth_url(self, redirect_uri):
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "activity profile heartrate",
        }
        return f"{FITBIT_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def _auth_header(self):
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}

    def exchange_code(self, code, redirect_uri):
        resp = requests.post(
            FITBIT_TOKEN_URL,
            headers={**self._auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "client_id": self.client_id,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        data["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
        ).isoformat()
        return data

    def refresh_if_needed(self, token_data):
        expires_at = datetime.fromisoformat(token_data["expires_at"])
        if datetime.now(timezone.utc) >= expires_at - timedelta(minutes=5):
            return self._refresh_token(token_data["refresh_token"])
        return token_data

    def _refresh_token(self, refresh_token):
        resp = requests.post(
            FITBIT_TOKEN_URL,
            headers={**self._auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        resp.raise_for_status()
        data = resp.json()
        data["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
        ).isoformat()
        return data

    def get_user_profile(self, access_token):
        resp = requests.get(
            f"{FITBIT_API_BASE}/1/user/-/profile.json",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("user", {})

    def get_swim_activities(self, access_token, after_date):
        """Return all swimming activities logged after after_date."""
        headers = {"Authorization": f"Bearer {access_token}"}
        activities = []
        offset = 0

        while True:
            resp = requests.get(
                f"{FITBIT_API_BASE}/1/user/-/activities/list.json",
                headers=headers,
                params={
                    "afterDate": after_date.strftime("%Y-%m-%d"),
                    "sort": "asc",
                    "limit": 100,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            page = data.get("activities", [])

            for act in page:
                type_id = act.get("activityTypeId")
                name = act.get("activityName", "").lower()
                if type_id in SWIM_ACTIVITY_IDS or "swim" in name:
                    activities.append(act)

            if not data.get("pagination", {}).get("next"):
                break
            offset += 100

        return activities

    def get_activity_detail(self, access_token, log_id):
        """Fetch detailed activity data including swimLengths and averageHeartRate."""
        resp = requests.get(
            f"{FITBIT_API_BASE}/1/user/-/activities/{log_id}.json",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json().get("activityLog", {})

    def get_heartrate_intraday(self, access_token, start_date, start_time_hhmm, duration_ms):
        """Fetch intraday HR during an activity. Returns {avg, max, dataset} or None."""
        start = datetime.strptime(f"{start_date} {start_time_hhmm}", "%Y-%m-%d %H:%M")
        end = start + timedelta(milliseconds=duration_ms)
        end_str = end.strftime("%H:%M")

        resp = requests.get(
            f"{FITBIT_API_BASE}/1/user/-/activities/heart/date/{start_date}/1d/1sec"
            f"/time/{start_time_hhmm}/{end_str}.json",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        dataset = resp.json().get("activities-heart-intraday", {}).get("dataset", [])
        values = [e["value"] for e in dataset if e.get("value", 0) > 0]
        if not values:
            return None
        return {
            "avg": round(sum(values) / len(values)),
            "max": max(values),
            "dataset": dataset,
        }
