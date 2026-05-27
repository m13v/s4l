#!/usr/bin/env node
// dashboard_provision.js
// Reconcile Firebase Authentication users with the dashboard_users table and
// generate magic-link sign-in URLs for newly-provisioned clients.
//
// Subcommands:
//   list                            Show current state (DB ↔ Firebase diff).
//   sync                            Pull Firebase UIDs into DB; push DB claims
//                                   into Firebase for existing users. No new
//                                   user creation, no external email.
//   create <email>                  Create a Firebase user for <email>, copy
//                                   the DB row's claims, write the resulting
//                                   UID back to the DB. Idempotent — if the
//                                   user already exists, just sync claims/UID.
//   magic <email>                   Generate (but do not send) a magic-link
//                                   sign-in URL for <email>. Prints to stdout
//                                   for the operator to paste into an invite.
//
// All subcommands target Firebase project s4l-app-prod (the same project
// bin/auth.js verifies tokens against) and read DATABASE_URL from .env.

const admin = require('firebase-admin');
const fs = require('fs');
const path = require('path');
const { Client } = require('pg');

const REPO_ROOT = path.resolve(__dirname, '..');
const ENV_FILE = path.join(REPO_ROOT, '.env');
if (fs.existsSync(ENV_FILE)) {
  fs.readFileSync(ENV_FILE, 'utf8').split('\n').forEach(line => {
    const m = line.match(/^([A-Z_]+)=(.*)$/);
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].replace(/^"|"$/g, '');
  });
}
if (!process.env.DATABASE_URL) {
  console.error('ERROR: DATABASE_URL not set');
  process.exit(1);
}

// The dashboard always lives in s4l-app-prod. Hardcode rather than reading
// FIREBASE_PROJECT_ID from the environment: that var leaks in from shell rc
// files for other repos (e.g. fazm-prod) and silently misroutes provisioning
// to the wrong project, where the Firebase user has no relationship to
// app.s4l.ai. The polluted-env failure mode: create completes "successfully"
// in fazm-prod, the client signs into s4l-app-prod via magic link, lands as
// an unclaimed user, and gets 403s on every scoped endpoint until someone
// notices. To intentionally target a different project, pass --project=NAME.
const projectFlag = process.argv.find(a => a.startsWith('--project='));
const FIREBASE_PROJECT_ID = projectFlag ? projectFlag.split('=')[1] : 's4l-app-prod';
admin.initializeApp({ projectId: FIREBASE_PROJECT_ID });
console.log(`[dashboard_provision] firebase project=${FIREBASE_PROJECT_ID}`);

// Magic-link sign-in continues here. The hash includes the email so the
// dashboard's onIdTokenChanged handler can match the link against the
// requested account.
const DASHBOARD_URL = process.env.DASHBOARD_URL || 'https://app.s4l.ai';

async function db() {
  const c = new Client({ connectionString: process.env.DATABASE_URL });
  await c.connect();
  return c;
}

async function loadDbUsers(client) {
  const r = await client.query(
    'SELECT email, firebase_uid, admin, projects, name FROM dashboard_users ORDER BY admin DESC, email'
  );
  return r.rows;
}

async function listCmd() {
  const client = await db();
  try {
    const rows = await loadDbUsers(client);
    console.log('Email                                | DB UID                          | FB exists | FB claims match | FB last signin');
    console.log(''.padEnd(140, '-'));
    for (const row of rows) {
      let fb = null;
      try {
        fb = await admin.auth().getUserByEmail(row.email);
      } catch (e) {
        // user-not-found
      }
      const exists = !!fb;
      const fbClaims = (fb && fb.customClaims) || {};
      const dbProjects = (row.projects || []).slice().sort().join(',');
      const fbProjects = (Array.isArray(fbClaims.projects) ? fbClaims.projects : []).slice().sort().join(',');
      const claimsMatch = !!fb && fbClaims.admin === row.admin && dbProjects === fbProjects;
      const lastSignIn = (fb && fb.metadata && fb.metadata.lastSignInTime) || '-';
      console.log(
        row.email.padEnd(37) + ' | ' +
        (row.firebase_uid || '-').padEnd(31) + ' | ' +
        (exists ? 'yes' : 'no ').padEnd(9) + ' | ' +
        (exists ? (claimsMatch ? 'yes' : 'NO ') : '-  ').padEnd(15) + ' | ' +
        lastSignIn
      );
    }
  } finally {
    await client.end();
  }
}

async function syncOne(client, dbRow) {
  let fb;
  try {
    fb = await admin.auth().getUserByEmail(dbRow.email);
  } catch (e) {
    if (e.code === 'auth/user-not-found') return { email: dbRow.email, action: 'absent_skipped' };
    throw e;
  }
  // 1. Write Firebase UID back to DB if missing/changed.
  if (dbRow.firebase_uid !== fb.uid) {
    await client.query(
      'UPDATE dashboard_users SET firebase_uid=$1 WHERE email=$2',
      [fb.uid, dbRow.email]
    );
  }
  // 2. Update Firebase custom claims so they match the DB row exactly.
  await admin.auth().setCustomUserClaims(fb.uid, {
    admin: !!dbRow.admin,
    projects: dbRow.projects || [],
  });
  return { email: dbRow.email, action: 'synced', uid: fb.uid };
}

async function syncCmd() {
  const client = await db();
  try {
    const rows = await loadDbUsers(client);
    for (const row of rows) {
      const result = await syncOne(client, row);
      console.log(`  ${result.action.padEnd(18)} ${result.email}` + (result.uid ? `  uid=${result.uid}` : ''));
    }
  } finally {
    await client.end();
  }
}

async function createCmd(email) {
  if (!email) throw new Error('create requires an <email> argument');
  const client = await db();
  try {
    const r = await client.query(
      'SELECT email, firebase_uid, admin, projects, name FROM dashboard_users WHERE email=$1',
      [email.toLowerCase()]
    );
    if (!r.rows.length) {
      throw new Error(`No dashboard_users row for ${email}. Add to DB first.`);
    }
    const dbRow = r.rows[0];

    let fb;
    try {
      fb = await admin.auth().getUserByEmail(email);
      console.log(`  exists      ${email}  uid=${fb.uid} (will resync claims)`);
    } catch (e) {
      if (e.code !== 'auth/user-not-found') throw e;
      // emailVerified false: the user proves ownership when they click the
      // magic link, at which point Firebase flips this true automatically.
      fb = await admin.auth().createUser({ email, emailVerified: false });
      console.log(`  created     ${email}  uid=${fb.uid}`);
    }

    await admin.auth().setCustomUserClaims(fb.uid, {
      admin: !!dbRow.admin,
      projects: dbRow.projects || [],
    });
    await client.query(
      'UPDATE dashboard_users SET firebase_uid=$1 WHERE email=$2',
      [fb.uid, email.toLowerCase()]
    );
    console.log(`  claims set  admin=${!!dbRow.admin} projects=${JSON.stringify(dbRow.projects || [])}`);
  } finally {
    await client.end();
  }
}

async function magicCmd(email) {
  if (!email) throw new Error('magic requires an <email> argument');
  // Force the recipient to be signed out first (handleCodeInApp:true) so the
  // dashboard reads the link's payload and signs them in as <email>. The URL
  // we generate is a one-shot sign-in URL Firebase verifies server-side.
  const link = await admin.auth().generateSignInWithEmailLink(email, {
    url: DASHBOARD_URL,
    handleCodeInApp: true,
  });
  console.log(link);
}

const positionalArgs = process.argv.slice(2).filter(a => !a.startsWith('--'));
const [subcmd, ...rest] = positionalArgs;
(async () => {
  switch (subcmd) {
    case 'list':   await listCmd(); break;
    case 'sync':   await syncCmd(); break;
    case 'create': await createCmd(rest[0]); break;
    case 'magic':  await magicCmd(rest[0]); break;
    default:
      console.error('Usage: dashboard_provision.js {list|sync|create <email>|magic <email>}');
      process.exit(2);
  }
})().catch(e => {
  console.error('ERROR:', e.message);
  process.exit(1);
});
