from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, text
import os, redis

app = FastAPI(title="Playbook Agent API")
db = create_engine(os.environ["DATABASE_URL"])
r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)

class Decision(BaseModel):
    reviewer: str
    notes: str = ""

@app.post("/cases/{case_id}/approve")
def approve(case_id: str, d: Decision):
    with db.begin() as conn:
        conn.execute(text("UPDATE approvals SET status='APPROVED', reviewer=:r, notes=:n, updated_at=now() WHERE case_id=:id"),
                     {"id": case_id, "r": d.reviewer, "n": d.notes})
        conn.execute(text("UPDATE cases SET status='APPROVED' WHERE id=:id"), {"id": case_id})
    r.xadd("playbook_approved", {"case_id": case_id}, maxlen=10000, approximate=True)
    return {"ok": True}

@app.post("/cases/{case_id}/reject")
def reject(case_id: str, d: Decision):
    with db.begin() as conn:
        conn.execute(text("UPDATE approvals SET status='REJECTED', reviewer=:r, notes=:n, updated_at=now() WHERE case_id=:id"),
                     {"id": case_id, "r": d.reviewer, "n": d.notes})
        conn.execute(text("UPDATE cases SET status='REJECTED' WHERE id=:id"), {"id": case_id})
    return {"ok": True}
