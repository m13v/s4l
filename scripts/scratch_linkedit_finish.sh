#!/bin/bash
# scratch helper: release lock (tolerant), update posts DB, backfill short-link attribution
# usage: scratch_linkedit_finish.sh POST_ID LINK_SOURCE MINTED_SESSION   (LINK_TEXT via env LT)
set -uo pipefail
cd ~/social-autoposter
source .env
export DATABASE_URL
POST_ID="$1"; LINK_SOURCE="$2"; MINTED="$3"
python3 scripts/reddit_browser_lock.py release 2>&1 || echo "release_skip (held by other / already free)"
LT="$LT" PID="$POST_ID" LS="$LINK_SOURCE" python3 -c "
import os, psycopg2
conn=psycopg2.connect(os.environ['DATABASE_URL']); cur=conn.cursor()
cur.execute('UPDATE posts SET link_edited_at=NOW(), link_edit_content=%s, link_source=%s WHERE id=%s',(os.environ['LT'],os.environ['LS'],int(os.environ['PID'])))
conn.commit(); print('db_updated rows=',cur.rowcount)
"
python3 scripts/dm_short_links.py backfill-post --minted-session "$MINTED" --post-id "$POST_ID" 2>&1
