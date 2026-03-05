import json
import os

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for

from fitbit_client import FitbitClient
from strava_client import StravaClient
from sync import build_tcx, preview_swims, sync_swims

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

TOKENS_FILE = "tokens.json"
PREVIEW_FILE = "preview_data.json"
FITBIT_REDIRECT = "http://localhost:5000/fitbit/callback"
STRAVA_REDIRECT = "http://localhost:5000/strava/callback"


def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def fitbit_client():
    return FitbitClient(os.environ["FITBIT_CLIENT_ID"], os.environ["FITBIT_CLIENT_SECRET"])


def strava_client():
    return StravaClient(os.environ["STRAVA_CLIENT_ID"], os.environ["STRAVA_CLIENT_SECRET"])


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    tokens = load_tokens()
    return render_template(
        "index.html",
        fitbit_connected="fitbit" in tokens,
        strava_connected="strava" in tokens,
    )


# ---------------------------------------------------------------------------
# Fitbit OAuth
# ---------------------------------------------------------------------------

@app.route("/fitbit/login")
def fitbit_login():
    return redirect(fitbit_client().get_auth_url(FITBIT_REDIRECT))


@app.route("/fitbit/callback")
def fitbit_callback():
    error = request.args.get("error")
    if error:
        flash(f"Fitbit authorization failed: {error}", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")
    tokens = load_tokens()
    try:
        tokens["fitbit"] = fitbit_client().exchange_code(code, FITBIT_REDIRECT)
        save_tokens(tokens)
        flash("Fitbit connected successfully!", "success")
    except Exception as exc:
        flash(f"Error connecting Fitbit: {exc}", "error")
    return redirect(url_for("index"))


@app.route("/fitbit/disconnect")
def fitbit_disconnect():
    tokens = load_tokens()
    tokens.pop("fitbit", None)
    save_tokens(tokens)
    flash("Fitbit disconnected.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Strava OAuth
# ---------------------------------------------------------------------------

@app.route("/strava/login")
def strava_login():
    return redirect(strava_client().get_auth_url(STRAVA_REDIRECT))


@app.route("/strava/callback")
def strava_callback():
    error = request.args.get("error")
    if error:
        flash(f"Strava authorization failed: {error}", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")
    tokens = load_tokens()
    try:
        tokens["strava"] = strava_client().exchange_code(code)
        save_tokens(tokens)
        flash("Strava connected successfully!", "success")
    except Exception as exc:
        flash(f"Error connecting Strava: {exc}", "error")
    return redirect(url_for("index"))


@app.route("/strava/disconnect")
def strava_disconnect():
    tokens = load_tokens()
    tokens.pop("strava", None)
    save_tokens(tokens)
    flash("Strava disconnected.", "info")
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Preview → confirm flow
# ---------------------------------------------------------------------------

@app.route("/preview", methods=["POST"])
def preview():
    tokens = load_tokens()
    if "fitbit" not in tokens or "strava" not in tokens:
        flash("Please connect both Fitbit and Strava before syncing.", "error")
        return redirect(url_for("index"))

    days_back = int(request.form.get("days_back", 30))
    fb = fitbit_client()
    st = strava_client()

    try:
        tokens["fitbit"] = fb.refresh_if_needed(tokens["fitbit"])
        tokens["strava"] = st.refresh_if_needed(tokens["strava"])
        save_tokens(tokens)

        activities = preview_swims(fb, tokens["fitbit"], st, tokens["strava"], days_back)

        with open(PREVIEW_FILE, "w") as f:
            json.dump({"activities": activities, "days_back": days_back}, f)

        return render_template("preview.html", activities=activities, days_back=days_back)

    except Exception as exc:
        flash(f"Preview error: {exc}", "error")
        return redirect(url_for("index"))


@app.route("/sync/confirmed", methods=["POST"])
def sync_confirmed():
    if not os.path.exists(PREVIEW_FILE):
        flash("Preview data expired. Please start over.", "error")
        return redirect(url_for("index"))

    with open(PREVIEW_FILE) as f:
        preview_data = json.load(f)

    selected_ids = set(request.form.getlist("selected"))
    days_back = preview_data["days_back"]

    tokens = load_tokens()
    st = strava_client()
    tokens["strava"] = st.refresh_if_needed(tokens["strava"])
    save_tokens(tokens)

    from sync import load_synced_ids, save_synced_ids
    synced_ids = load_synced_ids()
    results = {"synced": [], "skipped": [], "errors": []}

    for activity in preview_data["activities"]:
        log_id = activity["log_id"]
        display = {"id": log_id, "name": activity["name"], "date": activity["date"]}

        if log_id not in selected_ids:
            results["skipped"].append({**display, "reason": "Not selected"})
            continue

        try:
            payload = activity["payload"]
            existing_id = activity.get("existing_strava_id")
            has_hr = bool(activity.get("hr_dataset_utc"))
            print(f"[sync {log_id}] existing_id={existing_id}, has_hr={has_hr}, hr_points={len(activity.get('hr_dataset_utc') or [])}")

            def _create_or_upload():
                if has_hr:
                    tcx = build_tcx(activity)
                    print(f"[sync {log_id}] Uploading TCX ({len(tcx)} bytes)")
                    return st.upload_activity(
                        tokens["strava"]["access_token"],
                        tcx,
                        payload.get("name"),
                        payload.get("description"),
                    )
                print(f"[sync {log_id}] Using create_activity (no HR data)")
                return st.create_activity(tokens["strava"]["access_token"], payload)

            delete_error = None
            if existing_id:
                try:
                    print(f"[sync {log_id}] Trying to delete existing Strava activity {existing_id}")
                    st.delete_activity(tokens["strava"]["access_token"], existing_id)
                    print(f"[sync {log_id}] Delete succeeded")
                    strava_activity = _create_or_upload()
                    action = "replaced"
                except Exception as del_exc:
                    import requests as req_lib
                    print(f"[sync {log_id}] Delete failed: {del_exc}")
                    if isinstance(del_exc, req_lib.HTTPError) and del_exc.response.status_code in (401, 403):
                        # Strava won't permit deletion (401/403 can both mean this for file-uploaded activities)
                        strava_activity = st.update_activity(
                            tokens["strava"]["access_token"], existing_id, payload
                        )
                        action = "updated"
                        delete_error = f"HTTP {del_exc.response.status_code}"
                    else:
                        raise
            else:
                strava_activity = _create_or_upload()
                action = "created"

            synced_ids.add(log_id)
            save_synced_ids(synced_ids)
            results["synced"].append({
                **display,
                "strava_id": strava_activity.get("id"),
                "distance_m": payload["distance"],
                "duration_s": payload["elapsed_time"],
                "action": action,
                "delete_error": delete_error,
            })
        except Exception as exc:
            results["errors"].append({**display, "error": str(exc)})

    os.remove(PREVIEW_FILE)
    return render_template("results.html", results=results, days_back=days_back)


# ---------------------------------------------------------------------------
# Sync (direct, no preview)
# ---------------------------------------------------------------------------

@app.route("/sync", methods=["POST"])
def sync():
    tokens = load_tokens()
    if "fitbit" not in tokens or "strava" not in tokens:
        flash("Please connect both Fitbit and Strava before syncing.", "error")
        return redirect(url_for("index"))

    days_back = int(request.form.get("days_back", 30))
    replace = request.form.get("replace") == "on"

    fb = fitbit_client()
    st = strava_client()

    try:
        # Refresh tokens if needed and persist updated tokens
        tokens["fitbit"] = fb.refresh_if_needed(tokens["fitbit"])
        tokens["strava"] = st.refresh_if_needed(tokens["strava"])
        save_tokens(tokens)

        results = sync_swims(fb, tokens["fitbit"], st, tokens["strava"], days_back, replace=replace)
        return render_template("results.html", results=results, days_back=days_back)

    except Exception as exc:
        flash(f"Sync error: {exc}", "error")
        return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
