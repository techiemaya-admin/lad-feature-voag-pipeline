"""Check latest batches."""
from dotenv import load_dotenv
load_dotenv()
import psycopg2, os

conn = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)
cur = conn.cursor()
cur.execute(
    "SELECT id, status, completed_calls, failed_calls, total_calls, metadata->>'job_id' "
    "FROM lad_dev.voice_call_batches ORDER BY created_at DESC LIMIT 2"
)
for batch in cur.fetchall():
    batch_id = batch[0]
    print(f"JOB: {batch[5]}")
    print(f"  STATUS={batch[1]}")
    print(f"  TOTAL={batch[4]}")
    print(f"  COMPLETED={batch[2]}")
    print(f"  FAILED={batch[3]}")
    cur.execute(
        "SELECT status, COUNT(*) FROM lad_dev.voice_call_batch_entries "
        "WHERE batch_id = %s GROUP BY status",
        (batch_id,)
    )
    print(f"  ENTRIES: {dict(cur.fetchall())}")
    print()
cur.close()
conn.close()
