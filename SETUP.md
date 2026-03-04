# Fitbit Swims → Strava — Setup Guide

## Step 1 — Register a Fitbit developer app

1. Go to https://dev.fitbit.com/apps/new and sign in with your Fitbit account.
2. Fill in the form:
   - **Application Name**: anything (e.g. "My Swim Sync")
   - **Description**: anything
   - **Application Website URL**: `http://localhost`
   - **Organization**: your name
   - **Organization Website URL**: `http://localhost`
   - **Terms of Service URL**: `http://localhost`
   - **Privacy Policy URL**: `http://localhost`
   - **OAuth 2.0 Application Type**: **Personal** (gives full data access for your own account)
   - **Redirect URL**: `http://localhost:5000/fitbit/callback`
   - **Default Access Type**: Read Only
3. Submit and copy your **Client ID** and **Client Secret**.

---

## Step 2 — Register a Strava developer app

1. Go to https://www.strava.com/settings/api and sign in.
2. Fill in:
   - **Application Name**: anything
   - **Category**: Data Importer (or whatever fits)
   - **Club**: leave blank
   - **Website**: `http://localhost`
   - **Authorization Callback Domain**: `localhost`
3. Click **Create** and copy your **Client ID** and **Client Secret**.

---

## Step 3 — Configure the app

```
cd C:\Users\Colin\OneDrive\Documents\PersonalWork\stravaimporter
```

Copy `.env.example` to `.env`:
```
copy .env.example .env
```

Open `.env` in a text editor and fill in your four credentials.
For the `SECRET_KEY`, generate one with:
```
python -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 4 — Install dependencies

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

---

## Step 5 — Run the app

```
python app.py
```

Open your browser to http://localhost:5000

1. Click **Connect Fitbit** and authorise the app.
2. Click **Connect Strava** and authorise the app.
3. Choose how many days to look back, then click **Sync now**.

Each swim is only uploaded once — synced IDs are stored in `synced_activities.json`,
so you can run it as often as you like without creating duplicates.

---

## Notes

- Only activities Fitbit classifies as **Swimming** (pool or open water) are synced.
- Activities are created on Strava as manual entries (no GPS track) — this is correct
  for pool swimming and matches how Strava itself records them.
- Your OAuth tokens are saved locally in `tokens.json` and refreshed automatically.
  Do not share this file.
