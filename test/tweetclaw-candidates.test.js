'use strict';

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

function test(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (e) {
    console.error(`FAIL ${name}: ${e.message}`);
    process.exitCode = 1;
  }
}

test('tweetclaw_candidates normalizes reviewed TweetClaw records', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'tweetclaw-candidates-'));
  const input = path.join(dir, 'tweetclaw.json');
  fs.writeFileSync(
    input,
    JSON.stringify({
      data: [
        {
          id: '1876543210987654321',
          text: 'agents shipping real workflows',
          author: { username: '@builder', followers_count: '12.5K' },
          created_at: '2026-06-06T08:00:00.000Z',
          public_metrics: {
            reply_count: 3,
            retweet_count: 4,
            like_count: '1.2K',
            view_count: '44,100',
            bookmark_count: 5,
          },
        },
        {
          url: 'https://x.com/builder/status/1876543210987654321',
          text: 'duplicate by status id',
        },
        {
          text: 'missing url and id',
        },
      ],
    })
  );

  const stdout = execFileSync(
    'python3',
    [
      'scripts/tweetclaw_candidates.py',
      '--file',
      input,
      '--project',
      'Demo',
      '--search-topic',
      'agent workflows',
      '--query',
      'agent workflows min_faves:10',
    ],
    { cwd: path.join(__dirname, '..'), encoding: 'utf8' }
  );
  const rows = JSON.parse(stdout);
  assert.strictEqual(rows.length, 1);
  assert.strictEqual(rows[0].tweetUrl, 'https://x.com/builder/status/1876543210987654321');
  assert.strictEqual(rows[0].handle, 'builder');
  assert.strictEqual(rows[0].likes, 1200);
  assert.strictEqual(rows[0].views, 44100);
  assert.strictEqual(rows[0].author_followers, 12500);
  assert.strictEqual(rows[0].matched_project, 'Demo');
  assert.strictEqual(rows[0].source, 'tweetclaw');
});
