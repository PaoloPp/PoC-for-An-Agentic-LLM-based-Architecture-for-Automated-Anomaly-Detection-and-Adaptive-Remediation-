# shared/schemas/strategy.py
from pydantic import BaseModel
from typing import List, Dict, Any

class StrategyPlan(BaseModel):
    goal: str                 # "investigate" | "contain" | "eradicate"
    rationale: str
    selected_actions: List[str]
    required_approvals: List[str]
    rollback_steps: List[str]
    verification_steps: List[str]
    assumptions: List[str] = []
    risk_notes: List[str] = []
