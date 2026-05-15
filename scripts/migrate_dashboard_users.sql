-- 2026-05-14: dashboard_users
-- Single source of truth for who has access to the dashboard (Firebase claims)
-- AND who receives the per-project daily reports. Previously claims were set
-- ad-hoc with firebase-admin and recipients were hardcoded to i@m13v.com.
--
-- email          : login email + report recipient. Lowercased.
-- firebase_uid   : populated after createUser/getUserByEmail at provisioning.
--                  Nullable so a row can exist before Firebase is touched.
-- admin          : true for the operator account (i@m13v.com). Admins see all
--                  projects in the dashboard and receive the unscoped master
--                  daily report.
-- projects       : list of project names matching config.json casing
--                  (e.g. ['Runner','Agora','Podlog']). NULL or empty for admins.
-- report_enabled : if false, skip this user during daily report fan-out
--                  (kept for soft-disable without dropping the row).
-- name           : display name for logs and email greetings.
-- notes          : free-text, e.g. 'NightOwl founder via 2026-05-14 onboarding'.

CREATE TABLE IF NOT EXISTS dashboard_users (
    email           TEXT PRIMARY KEY,
    firebase_uid    TEXT UNIQUE,
    admin           BOOLEAN NOT NULL DEFAULT FALSE,
    projects        TEXT[] NOT NULL DEFAULT '{}',
    report_enabled  BOOLEAN NOT NULL DEFAULT TRUE,
    name            TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_signin_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_dashboard_users_report_enabled
    ON dashboard_users(report_enabled) WHERE report_enabled;

-- Trigger to keep updated_at fresh on any UPDATE.
CREATE OR REPLACE FUNCTION dashboard_users_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_dashboard_users_updated_at ON dashboard_users;
CREATE TRIGGER trg_dashboard_users_updated_at
    BEFORE UPDATE ON dashboard_users
    FOR EACH ROW EXECUTE FUNCTION dashboard_users_touch_updated_at();
