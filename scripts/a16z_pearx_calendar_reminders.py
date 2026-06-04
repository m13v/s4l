#!/usr/bin/env python3
"""One-shot: create recurring accelerator-application reminders in
matt@mediar.ai's Google Calendar via DWD (same SA as the gmail keepalive),
requesting a calendar scope. If the scope isn't authorized in the mediar.ai
Workspace DWD config, this fails with unauthorized_client and we fall back.
"""
import json, time, urllib.parse, urllib.request, urllib.error
import google.auth
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SA_EMAIL = "gmail-dwd-impersonator@gmail-api-integration-486018.iam.gserviceaccount.com"
TARGET_USER = "matt@mediar.ai"
SCOPE = "https://www.googleapis.com/auth/calendar"


def mint_access_token():
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    iat = int(time.time()); exp = iat + 3600
    claim = {"iss": SA_EMAIL, "sub": TARGET_USER, "scope": SCOPE,
             "aud": "https://oauth2.googleapis.com/token", "iat": iat, "exp": exp}
    iam = build("iamcredentials", "v1", credentials=creds, cache_discovery=False)
    signed = iam.projects().serviceAccounts().signJwt(
        name=f"projects/-/serviceAccounts/{SA_EMAIL}",
        body={"payload": json.dumps(claim)}).execute()
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": signed["signedJwt"]}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"token exchange {e.code}: {e.read().decode()}") from None


def main():
    from google.oauth2.credentials import Credentials
    token = mint_access_token()
    cal = build("calendar", "v3", credentials=Credentials(token=token),
                cache_discovery=False)

    events = [
        {
            "summary": "Apply to a16z Speedrun (next cohort) - S4L",
            "start": "2026-09-15",
            "rrule": "RRULE:FREQ=MONTHLY;INTERVAL=4",
            "description": (
                "Reapply S4L to a16z Speedrun. Speedrun runs ~3 cohorts/year; "
                "this fires every 4 months so you catch the next deadline.\n\n"
                "Apply: https://speedrun.a16z.com/apply (start with email i@m13v.com)\n"
                "Status / update existing app: https://speedrun.a16z.com/application-login\n\n"
                "Reuse the saved answer set (pitch, traction, funding, founder bio). "
                "Last filled 2026-06-03; still need citizenship, university, years of "
                "experience, last-round date, and the 3 investor emails."
            ),
        },
        {
            "summary": "Apply to PearX (next batch) - S4L",
            "start": "2026-10-01",
            "rrule": "RRULE:FREQ=MONTHLY;INTERVAL=6",
            "description": (
                "Reapply S4L to PearX. Pear runs 2 batches/year (summer + winter); "
                "this fires every 6 months for the next window.\n\n"
                "Apply: https://pear.vc/pearx-application/ (Airtable form; long-text "
                "fields are contenteditable divs)\n\n"
                "Reuse the saved answer set. PearX S26 app was filled 2026-06-03 "
                "(left for review, not submitted)."
            ),
        },
    ]

    created = []
    for ev in events:
        body = {
            "summary": ev["summary"],
            "description": ev["description"],
            "start": {"date": ev["start"]},
            "end": {"date": ev["start"]},
            "recurrence": [ev["rrule"]],
            "reminders": {"useDefault": False, "overrides": [
                {"method": "popup", "minutes": 24 * 60},
                {"method": "email", "minutes": 24 * 60},
            ]},
            "transparency": "transparent",
        }
        out = cal.events().insert(calendarId="primary", body=body).execute()
        created.append((ev["summary"], out.get("htmlLink")))

    for s, link in created:
        print(f"CREATED: {s}\n  {link}")


if __name__ == "__main__":
    main()
