"""Realign every serial sequence to MAX(col) of its owning table.

Run once after any pg_dump/pg_restore migration (Neon -> Cloud SQL, etc.)
to prevent the duplicate-key-on-pkey storm that happens when sequences
lag behind restored row ids.

Idempotent + read-mostly: a sequence already at-or-ahead of max(id) is
left untouched.

    python3 scripts/realign_sequences.py [--dry-run]
"""
from __future__ import annotations
import argparse, os, sys
import psycopg2

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL not set", file=sys.stderr); return 2
    fixed = skipped = empty = 0
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute("""
          SELECT s.relname,
                 d.refobjid::regclass::text AS tbl,
                 a.attname AS col
            FROM pg_class s
            JOIN pg_namespace n ON n.oid = s.relnamespace
            JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
            JOIN pg_attribute a ON a.attrelid = d.refobjid
                               AND a.attnum = d.refobjsubid
           WHERE s.relkind = 'S' AND n.nspname = 'public'
           ORDER BY s.relname
        """)
        for seq, tbl, col in cur.fetchall():
            cur.execute(f'SELECT max("{col}") FROM {tbl}')
            mx = cur.fetchone()[0]
            cur.execute(f'SELECT last_value, is_called FROM "{seq}"')
            lv, ic = cur.fetchone()
            nxt = lv + (1 if ic else 0)
            if mx is None:
                empty += 1; continue
            if mx < nxt:
                skipped += 1; continue
            target = mx
            if args.dry_run:
                print(f"would setval({seq}, {target}, true) (max={mx} next_was={nxt})")
            else:
                cur.execute("SELECT setval(%s, %s, true)", (seq, target))
                print(f"setval({seq}, {target}) (max={mx} next_was={nxt})")
            fixed += 1
        if not args.dry_run:
            conn.commit()
    print(f"\ndone. fixed={fixed} already_ahead={skipped} empty={empty}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
