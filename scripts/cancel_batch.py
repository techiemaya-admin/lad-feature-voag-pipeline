"""Get exact job_id for latest batch and cancel it."""
from dotenv import load_dotenv
load_dotenv()
import psycopg2, os, httpx

conn = psycopg2.connect(
    host=os.getenv("DB_HOST"),
    port=os.getenv("DB_PORT"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)
cur = conn.cursor()
cur.execute(
    "SELECT metadata->>'job_id' FROM lad_dev.voice_call_batches "
    "WHERE status = 'running' ORDER BY created_at DESC LIMIT 1"
)
row = cur.fetchone()
cur.close()
conn.close()

if not row:
    print("No running batch found")
    exit(1)

job_id = row[0]
print(f"Found running batch job_id: {job_id}")
print(f"Length: {len(job_id)}")

# Cancel it
r = httpx.post(
    "http://localhost:8000/calls/cancel",
    json={"resource_id": job_id},
    headers={
        "X-Frontend-ID": "dev",
        "X-API-Key": "kMQgGRDAa8t5CvmkfqFYuGiXIXgNYC1EEGjYs5v8_NU",
    },
    timeout=30,
)
print(f"Cancel status: {r.status_code}")
print(f"Response: {r.text}")
