#!/usr/bin/env python3
"""Backfill occurred_start/occurred_end from event_date — box2 port (v3).

Adapted from the small-box claude's v2 (Aug 15) by river, Sep 5 2026.
Changes: peer-auth connect (run as postgres OS user, no password),
per-bank scope via sys.argv, backup table dated 20260905.
Original semantics preserved: backup first (own txn), fill ONLY NULLs
with COALESCE(col, event_date), 2000-row batches, 3s lock_timeout, 8 retries.
"""
import sys, time
import psycopg2
from psycopg2 import errors

BANK = sys.argv[1] if len(sys.argv) > 1 else "river"
BACKUP = f"_date_backfill_backup_20260905_{BANK}"
BATCH = 2000
LOCK_TIMEOUT_MS = 3000
MAX_RETRY = 8

# peer auth: run as postgres OS user, unix socket, no host/password
c = psycopg2.connect(dbname="hindsight", user="postgres")
c.autocommit = False
cur = c.cursor()
cur.execute(f"SET lock_timeout = '{LOCK_TIMEOUT_MS}ms'")
c.commit()

# bank_id is the text key itself on box2 (no surrogate id)
cur.execute("SELECT bank_id FROM banks WHERE bank_id = %s", (BANK,))
row = cur.fetchone()
if not row:
    print(f"bank '{BANK}' not found"); sys.exit(1)
bank_id = row[0]

# 1) backup — own committed transaction
cur.execute("DROP TABLE IF EXISTS " + BACKUP)
cur.execute(
    f"CREATE TABLE {BACKUP} AS "
    "SELECT id, occurred_start, occurred_end FROM memory_units "
    "WHERE bank_id = %s AND (occurred_start IS NULL OR occurred_end IS NULL)",
    (bank_id,),
)
c.commit()
print(f"backup committed: {cur.rowcount:,} rows -> {BACKUP}")

cur.execute(
    "SELECT count(*) FROM memory_units "
    "WHERE bank_id = %s AND (occurred_start IS NULL OR occurred_end IS NULL)",
    (bank_id,),
)
total = cur.fetchone()[0]
print(f"to backfill in {BANK}: {total:,}")
if total == 0:
    print("nothing to do."); c.close(); sys.exit(0)

# 2) batched backfill
cur.execute(
    "SELECT id FROM memory_units "
    "WHERE bank_id = %s AND (occurred_start IS NULL OR occurred_end IS NULL) ORDER BY id",
    (bank_id,),
)
ids = [r[0] for r in cur.fetchall()]
print(f"fetched {len(ids):,} ids")

done, i = 0, 0
while i < len(ids):
    chunk = ids[i:i + BATCH]
    for attempt in range(1, MAX_RETRY + 1):
        try:
            cur.execute(
                "UPDATE memory_units "
                "SET occurred_start = COALESCE(occurred_start, event_date), "
                "    occurred_end   = COALESCE(occurred_end,   event_date) "
                "WHERE id = ANY(%s::uuid[])",
                (chunk,),
            )
            c.commit()
            done += cur.rowcount
            break
        except (errors.DeadlockDetected, errors.LockNotAvailable) as e:
            c.rollback()
            if attempt == MAX_RETRY:
                print(f"FAILED batch at index {i} after {MAX_RETRY} tries: {e}")
                raise
            time.sleep(0.3 * attempt)
    i += BATCH
    if done % 50000 < BATCH:
        print(f"  progress: {done:,}/{total:,}")

# 3) verify
cur.execute(
    "SELECT count(*) FROM memory_units "
    "WHERE bank_id = %s AND (occurred_start IS NULL OR occurred_end IS NULL)",
    (bank_id,),
)
remaining = cur.fetchone()[0]
print(f"done: {done:,} rows backfilled in {BANK}")
print(f"remaining null occurred_*: {remaining:,}")
c.close()
print("COMPLETE.")
