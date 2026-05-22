// Debug script: replicate enrichPostCommentsTwitterRuns logic for one run.
// Goal: figure out why the 19:00 PDT batch (twcycle-20260521-190003) is not
// being matched to its run (started_at = 2026-05-22T02:00:03Z).
const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

// Load .env the same way server.js does (manual parse).
function loadEnv() {
  const raw = fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8');
  const vars = {};
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const key = trimmed.slice(0, eq);
    let val = trimmed.slice(eq + 1).replace(/^["']|["']$/g, '');
    vars[key] = val;
  }
  return vars;
}

const env = loadEnv();
const pool = new Pool({
  connectionString: env.DATABASE_URL,
  ssl: { rejectUnauthorized: false },
  max: 5,
});

function parseTwitterBatchIdMs(batchId) {
  if (!batchId) return NaN;
  const m = batchId.match(/^twcycle-(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/);
  if (!m) return NaN;
  const [, y, mo, d, hh, mm, ss] = m;
  return new Date(`${y}-${mo}-${d}T${hh}:${mm}:${ss}`).getTime();
}

(async () => {
  console.log('Node TZ:', Intl.DateTimeFormat().resolvedOptions().timeZone);
  console.log('process.env.TZ:', process.env.TZ);

  const runStartedAt = '2026-05-22T02:00:03.000Z'; // 19:00:03 PDT
  const runFinishedAt = '2026-05-22T03:33:28.000Z';
  const startMs = new Date(runStartedAt).getTime();
  const endMs = new Date(runFinishedAt).getTime() + 60 * 1000;
  const since = new Date(startMs - 2 * 60 * 1000).toISOString();

  console.log(`runStartedAt: ${runStartedAt} (ms=${startMs})`);
  console.log(`since:        ${since}`);

  const searchRows = (await pool.query(
    "SELECT ran_at, tweets_found, batch_id FROM twitter_search_attempts WHERE ran_at >= $1::timestamp",
    [since]
  )).rows;
  console.log(`\nsearchRows: ${searchRows.length}`);
  console.log('first row sample:', JSON.stringify({
    ran_at: searchRows[0]?.ran_at,
    ran_at_type: typeof searchRows[0]?.ran_at,
    ran_at_is_date: searchRows[0]?.ran_at instanceof Date,
    batch_id: searchRows[0]?.batch_id,
  }));

  // Replicate the matching loop
  let ownBatchId = null;
  let ownBatchDelta = Infinity;
  for (const s of searchRows) {
    if (!s.batch_id) continue;
    const bms = parseTwitterBatchIdMs(s.batch_id);
    if (!Number.isFinite(bms)) continue;
    const delta = Math.abs(bms - startMs);
    if (delta > 10 * 1000) continue;
    console.log(`  MATCH: batch=${s.batch_id} bms=${bms} delta=${delta}`);
    if (delta < ownBatchDelta) { ownBatchDelta = delta; ownBatchId = s.batch_id; }
  }
  console.log(`\nownBatchId from searchRows: ${ownBatchId}`);

  // Print all unique batch_ids and their parsed ms
  const uniq = new Map();
  for (const s of searchRows) {
    if (!s.batch_id || uniq.has(s.batch_id)) continue;
    const bms = parseTwitterBatchIdMs(s.batch_id);
    uniq.set(s.batch_id, { ms: bms, delta: Math.abs(bms - startMs) });
  }
  console.log(`\nUnique batch_ids and deltas:`);
  for (const [k, v] of [...uniq].sort((a,b) => a[1].delta - b[1].delta).slice(0, 10)) {
    console.log(`  ${k} -> ms=${v.ms} delta=${v.delta}`);
  }

  await pool.end();
})().catch(e => { console.error(e); process.exit(1); });
