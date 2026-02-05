# shared/schemas/anomaly.py
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime

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
