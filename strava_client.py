import urllib.parse
from datetime import datetime, timedelta, timezone

import requests

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"


class StravaClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret

    def get_auth_url(self, redirect_uri):
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "approval_prompt": "force",
            "scope": "activity:write,activity:read_all",
        }
        return f"{STRAVA_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code):
        resp = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # Strava returns expires_at as a Unix timestamp — store as ISO string too
        data["expires_at_iso"] = datetime.fromtimestamp(
            data["expires_at"], tz=timezone.utc
        ).isoformat()
        return data

    def refresh_if_needed(self, token_data):
        expires_at = datetime.fromisoformat(token_data["expires_at_iso"])
        if datetime.now(timezone.utc) >= expires_at - timedelta(minutes=5):
            return self._refresh_token(token_data["refresh_token"])
        return token_data

    def _refresh_token(self, refresh_token):
        resp = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        data["expires_at_iso"] = datetime.fromtimestamp(
            data["expires_at"], tz=timezone.utc
        ).isoformat()
        return data

    def create_activity(self, access_token, payload):
        resp = requests.post(
            f"{STRAVA_API_BASE}/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            data=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def get_swim_activities(self, access_token, after_date):
        """Fetch all Swim activities from Strava after the given date."""
        after = int(after_date.timestamp())
        activities = []
        page = 1
        while True:
            resp = requests.get(
                f"{STRAVA_API_BASE}/athlete/activities",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"after": after, "per_page": 100, "page": page},
            )
            resp.raise_for_status()
            page_acts = resp.json()
            if not page_acts:
                break
            activities.extend(a for a in page_acts if a.get("sport_type") == "Swim")
            if len(page_acts) < 100:
                break
            page += 1
        return activities

    def find_activity_at(self, access_token, start_date_local_str):
        """Return the Strava activity ID of a Swim starting at the given local time, or None."""
        # Parse the local time string and search a ±5 min window in UTC
        dt_local = datetime.fromisoformat(start_date_local_str).replace(tzinfo=timezone.utc)
        after  = int((dt_local - timedelta(minutes=5)).timestamp())
        before = int((dt_local + timedelta(minutes=5)).timestamp())

        resp = requests.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"after": after, "before": before, "per_page": 10},
        )
        resp.raise_for_status()
        for act in resp.json():
            if act.get("sport_type") == "Swim":
                return act["id"]
        return None

    def update_activity(self, access_token, activity_id, payload):
        """Update metadata on an existing Strava activity (name, description, sport_type)."""
        resp = requests.put(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "name": payload.get("name"),
                "description": payload.get("description"),
                "sport_type": payload.get("sport_type"),
            },
        )
        resp.raise_for_status()
        return resp.json()

    def delete_activity(self, access_token, activity_id):
        resp = requests.delete(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()

    def upload_activity(self, access_token, tcx_content, name, description):
        """Upload a TCX file to Strava, wait for processing, set sport_type to Swim."""
        import time
        resp = requests.post(
            f"{STRAVA_API_BASE}/uploads",
            headers={"Authorization": f"Bearer {access_token}"},
            files={"file": ("activity.tcx", tcx_content.encode("utf-8"), "application/xml")},
            data={"data_type": "tcx", "name": name, "description": description},
        )
        resp.raise_for_status()
        upload_id = resp.json()["id"]

        for _ in range(20):
            time.sleep(2)
            poll = requests.get(
                f"{STRAVA_API_BASE}/uploads/{upload_id}",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            poll.raise_for_status()
            status = poll.json()
            if status.get("activity_id"):
                activity_id = status["activity_id"]
                upd = requests.put(
                    f"{STRAVA_API_BASE}/activities/{activity_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"name": name, "description": description,
                          "sport_type": "Swim", "type": "Swim"},
                )
                print(f"[upload] sport_type PUT {upd.status_code}: {upd.text[:400]}")
                upd.raise_for_status()
                return {"id": activity_id}
            if status.get("error"):
                raise RuntimeError(f"Strava upload error: {status['error']}")
        raise TimeoutError("Strava upload did not complete in time")
