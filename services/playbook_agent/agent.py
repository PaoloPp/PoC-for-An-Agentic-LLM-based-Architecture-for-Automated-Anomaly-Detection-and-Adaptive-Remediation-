import os
import json
import uuid
import time
import logging
from typing import Any, Dict, List
from pathlib import Path

import redis
import requests
from jsonschema import Draft202012Validator, validate as js_validate, ValidationError
from sqlalchemy import create_engine, text, event
from pgvector.psycopg import register_vector

# ---------------- Config ----------------
REDIS_URL = os.environ["REDIS_URL"]
DB_URL = os.environ["DATABASE_URL"]

LLM_MODE = os.getenv("LLM_MODE", "stub").lower()  # "stub" or "ollama"
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama:11434")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.1:8b-instruct")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "embeddinggemma")  # used only if you embed later

STREAM_IN = os.getenv("STREAM_IN", "confirmed_anomalies")
STREAM_OUT_READY = os.getenv("STREAM_OUT_READY", "playbook_ready")
STREAM_OUT_ERRORS = os.getenv("STREAM_OUT_ERRORS", "playbook_errors")
STREAM_NOTIFICATIONS = os.getenv("STREAM_NOTIFICATIONS", "notifications")

CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "playbook_agent")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", f"agent-{uuid.uuid4().hex[:6]}")
MAX_STREAM_LEN = int(os.getenv("MAX_STREAM_LEN", "10000"))
BLOCK_MS = int(os.getenv("BLOCK_MS", "5000"))

CACAO_SCHEMA_REPO = os.getenv("CACAO_SCHEMA_REPO", "cyentific-rni/cacao-json-schemas")
CACAO_SCHEMA_BRANCH = os.getenv("CACAO_SCHEMA_BRANCH", "cacao-v2.0-cs01")
CACAO_SCHEMA_PATH = os.getenv("CACAO_SCHEMA_PATH", "schemas/playbook.json")

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("playbook_agent_worker")

# ---------------- Redis ----------------
r = redis.from_url(REDIS_URL, decode_responses=True)

# ---------------- DB + pgvector registration ----------------
db = create_engine(DB_URL, pool_pre_ping=True)

@event.listens_for(db, "connect")
def _register_pgvector(dbapi_connection, connection_record):
    register_vector(dbapi_connection)

def _ollama_api_url(path: str) -> str:
    base = OLLAMA_BASE_URL.rstrip("/")
    if base.endswith("/api"):
        return f"{base}{path}"
    return f"{base}/api{path}"

# ---------------- CACAO Schema ----------------
def load_cacao_schema() -> Dict[str, Any]:
    schema_url = f"https://raw.githubusercontent.com/{CACAO_SCHEMA_REPO}/{CACAO_SCHEMA_BRANCH}/{CACAO_SCHEMA_PATH}"
    resp = requests.get(schema_url, timeout=20)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch CACAO schema: {schema_url} -> {resp.status_code} {resp.text[:200]}")
    return resp.json()

CACAO_SCHEMA = load_cacao_schema()
CACAO_VALIDATOR = Draft202012Validator(CACAO_SCHEMA)

def validate_cacao(playbook: Dict[str, Any]) -> List[str]:
    errors = sorted(CACAO_VALIDATOR.iter_errors(playbook), key=lambda e: list(e.path))
    return [f"{list(e.path)}: {e.message}" for e in errors]

# ---------------- Tool registry ----------------
def load_tools(environment: str, assets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    asset_types = sorted({a.get("asset_type","server") for a in (assets or [])})
    with db.begin() as conn:
        rows = conn.execute(
            text("""
                SELECT id, name, description, risk, requires_approval, environments, asset_types,
                       inputs_schema, openc2_template
                FROM tools
                WHERE :env = ANY(environments)
            """),
            {"env": environment},
        ).mappings().all()

    tools = [dict(r) for r in rows]
    # Filter down to relevant asset types (keep "general" tools if you tag them with many types)
    if asset_types:
        tools = [t for t in tools if any(at in (t.get("asset_types") or []) for at in asset_types)]
    return tools

def tool_index(tools: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {t["id"]: t for t in tools}

# ---------------- LLM Plan ----------------
PLAN_SCHEMA = json.loads(Path("schemas/plan.schema.json").read_text())


def llm_plan_stub(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Dumb but deterministic fallback
    first = tools[0]["id"] if tools else "noop"
    return {
        "objective": "investigate",
        "steps": [{
            "step_id": "S1",
            "tool_id": first,
            "inputs": {},
            "why": "Stub plan",
            "verify": "Check telemetry",
            "rollback": "N/A"
        }],
        "confidence": 0.2,
        "risk_notes": ["Stub mode"]
    }

def llm_plan_ollama(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    # Provide the model the anomaly + tools (capabilities).
    tool_summaries = [{
        "id": t["id"],
        "name": t["name"],
        "description": t["description"],
        "risk": t["risk"],
        "requires_approval": t["requires_approval"],
        "inputs_schema": t["inputs_schema"],
        "asset_types": t.get("asset_types", [])
    } for t in tools]

    prompt = {
        "anomaly": anomaly,
        "capabilities": tool_summaries,
        "guidance": [
            "Pick tools appropriate for asset types and business impact.",
            "Prefer least-disruptive actions first for critical services unless confidence is very high.",
            "Every disruptive step must include verify and rollback.",
            "Every step MUST include verify and rollback. If not applicable, use \"N/A\"."
        ]
    }

    resp = requests.post(
        _ollama_api_url("/generate"),
        json={
            "model": OLLAMA_CHAT_MODEL,
            "prompt": (
                "You are a SOAR agent that plans incident response.\n"
                "You MUST choose tool_id values from the capabilities list.\n"
                "Use severity, confidence, impact, and asset types to decide.\n"
                "Return JSON matching the schema.\n\n"
                f"INPUT:\n{json.dumps(prompt)}\n"
            ),
            "format": PLAN_SCHEMA,   # structured outputs
            "stream": False
        },
        timeout=180
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()
    raw = resp.json().get("response", "")
    if isinstance(raw, str):
        raw = raw.strip()
        return json.loads(raw)
    return raw

def plan_selector(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    if LLM_MODE == "ollama":
        return llm_plan_ollama(anomaly, tools)
    return llm_plan_stub(anomaly, tools)

# ---------------- Plan validation / gating ----------------
def validate_plan(plan: Dict[str, Any], tools: List[Dict[str, Any]]) -> List[str]:
    errors = []
    try:
        js_validate(plan, PLAN_SCHEMA)
    except ValidationError as e:
        return [f"Plan schema invalid: {e.message}"]

    idx = tool_index(tools)

    for step in plan.get("steps", []):
        tid = step.get("tool_id")
        if tid not in idx:
            errors.append(f"Unknown tool_id: {tid}")
            continue

        tool = idx[tid]
        schema = tool.get("inputs_schema")
        try:
            js_validate(step.get("inputs", {}), schema)
        except ValidationError as e:
            errors.append(f"Invalid inputs for {tid}: {e.message}")

        # Require verify/rollback for medium+ risk tools (PoC policy)
        risk = (tool.get("risk") or "low").lower()
        if risk in ("medium","high","critical"):
            if not step.get("verify"):
                errors.append(f"{tid} missing verify field")
            if not step.get("rollback"):
                errors.append(f"{tid} missing rollback field")

    return errors

# ---------------- OpenC2 templating ----------------
def render_template(obj: Any, inputs: Dict[str, Any]) -> Any:
    if isinstance(obj, dict):
        return {k: render_template(v, inputs) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render_template(v, inputs) for v in obj]
    if isinstance(obj, str):
        out = obj
        for k, v in inputs.items():
            out = out.replace(f"{{{{{k}}}}}", str(v))
        return out
    return obj

# ---------------- CACAO generation from validated plan ----------------
def generate_cacao_from_plan(anomaly: Dict[str, Any], plan: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    idx = tool_index(tools)
    pb_id = str(uuid.uuid4())
    start_id = "start--1"
    end_id = "end--1"

    steps = plan["steps"]
    workflow = {
        start_id: {"id": start_id, "type": "start", "on_completion": steps[0]["step_id"] if steps else end_id},
        end_id: {"id": end_id, "type": "end"}
    }

    for i, s in enumerate(steps):
        tid = s["tool_id"]
        tool = idx[tid]
        openc2 = render_template(tool["openc2_template"], s.get("inputs", {}))

        next_step = steps[i + 1]["step_id"] if i < len(steps) - 1 else end_id
        workflow[s["step_id"]] = {
            "id": s["step_id"],
            "type": "action",
            "name": tool["name"],
            "description": f"{tool['description']} | why: {s.get('why')}",
            "openc2": openc2,
            "on_completion": next_step
        }

    return {
        "type": "playbook",
        "spec_version": "cacao-2.0",
        "id": f"playbook--{pb_id}",
        "name": f"Auto-playbook: {anomaly['signal']['type']} {anomaly['signal']['summary'][:80]}",
        "description": "Generated draft from LLM plan + tool registry (PoC).",
        "workflow_start": start_id,
        "workflow": workflow,
    }

# ---------------- Persistence ----------------
def persist_case(anomaly: Dict[str, Any], plan: Dict[str, Any], cacao: Dict[str, Any], validation: Dict[str, Any]) -> str:
    case_id = str(uuid.uuid4())
    params = {
        "id": case_id,
        "status": "DRAFT_READY",
        "anomaly": json.dumps(anomaly),
        "strategy": json.dumps(plan),
        "cacao": json.dumps(cacao),
        "validation": json.dumps(validation),
    }
    with db.begin() as conn:
        conn.execute(text("""
            INSERT INTO cases (id, status, anomaly, strategy, cacao_draft, validation)
            VALUES (:id, :status,
                    CAST(:anomaly AS jsonb),
                    CAST(:strategy AS jsonb),
                    CAST(:cacao AS jsonb),
                    CAST(:validation AS jsonb))
        """), params)
        conn.execute(text("INSERT INTO approvals (case_id, status) VALUES (:id, 'PENDING')"), {"id": case_id})
    return case_id

# ---------------- Notifications ----------------
def notify(kind: str, payload: Dict[str, Any]):
    try:
        r.xadd(STREAM_NOTIFICATIONS, {"kind": kind, "json": json.dumps(payload)},
               maxlen=MAX_STREAM_LEN, approximate=True)
    except Exception:
        log.exception("Failed to notify kind=%s", kind)

# ---------------- Streams worker ----------------
def ensure_group(stream: str, group: str):
    try:
        r.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception:
        pass

def worker_loop():
    ensure_group(STREAM_IN, CONSUMER_GROUP)
    log.info("Worker started. stream=%s group=%s consumer=%s", STREAM_IN, CONSUMER_GROUP, CONSUMER_NAME)

    while True:
        msgs = r.xreadgroup(CONSUMER_GROUP, CONSUMER_NAME, {STREAM_IN: ">"}, count=1, block=BLOCK_MS)
        if not msgs:
            continue

        _, entries = msgs[0]
        msg_id, fields = entries[0]

        try:
            anomaly = json.loads(fields["json"])
            notify("anomaly_received", {"anomaly_id": anomaly.get("anomaly_id"), "env": anomaly.get("environment")})

            env = anomaly.get("environment", "prod")
            assets = anomaly.get("assets", [])
            tools = load_tools(env, assets)

            plan = plan_selector(anomaly, tools)
            plan_errors = validate_plan(plan, tools)

            cacao = generate_cacao_from_plan(anomaly, plan, tools) if not plan_errors else {}
            schema_errors = validate_cacao(cacao) if cacao else ["CACAO not generated due to plan errors"]

            validation = {
                "plan_errors": plan_errors,
                "schema_errors": schema_errors,
                "tool_count": len(tools)
            }

            if plan_errors:
                # dead-letter + ack poison pill (PoC choice)
                r.xadd(STREAM_OUT_ERRORS, {"msg_id": msg_id, "error": json.dumps(validation), "payload": fields["json"][:8000]},
                       maxlen=MAX_STREAM_LEN, approximate=True)
                r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
                continue

            case_id = persist_case(anomaly, plan, cacao, validation)
            r.xadd(STREAM_OUT_READY, {"case_id": case_id}, maxlen=MAX_STREAM_LEN, approximate=True)
            notify("case_created", {"case_id": case_id, "anomaly_id": anomaly.get("anomaly_id")})

            r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
            log.info("OK anomaly=%s case_id=%s", anomaly.get("anomaly_id"), case_id)

        except Exception as e:
            log.exception("Failed processing msg_id=%s", msg_id)
            r.xadd(STREAM_OUT_ERRORS, {"msg_id": msg_id, "error": repr(e), "payload": fields.get("json","")[:8000]},
                   maxlen=MAX_STREAM_LEN, approximate=True)
            r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
            time.sleep(0.25)

if __name__ == "__main__":
    worker_loop()
