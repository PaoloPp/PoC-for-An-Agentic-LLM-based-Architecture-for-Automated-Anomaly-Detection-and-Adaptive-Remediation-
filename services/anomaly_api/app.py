from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Dict, Any, List
import redis
import os, json, uuid

r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
app = FastAPI(title="Anomaly API")

class Signal(BaseModel):
    type: str
    summary: str
    severity: str
    confidence: float = Field(ge=0.0, le=1.0)

class Constraints(BaseModel):
    requires_human_approval: bool = True
    allowed_actions: List[str]
    forbidden_actions: List[str] = []

class ConfirmedAnomaly(BaseModel):
    anomaly_id: str
    timestamp: datetime
    resource_group: str
    environment: str
    signal: Signal
    entities: Dict[str, List[str]] = {}
    evidence: List[Dict[str, Any]] = []
    constraints: Constraints

@app.post("/confirmed-anomalies")
def post_confirmed_anomaly(a: ConfirmedAnomaly):
    # Convert datetimes (and other non-JSON types) to JSON-compatible types
    payload = jsonable_encoder(a)  # <- key fix
    r.xadd(
        "confirmed_anomalies",
        {"json": json.dumps(payload)},
        maxlen=10000,
        approximate=True
    )
    return {"status": "queued", "anomaly_id": a.anomaly_id}
