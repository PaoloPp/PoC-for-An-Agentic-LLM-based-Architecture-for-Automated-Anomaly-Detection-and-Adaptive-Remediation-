import os
import json
import uuid
import time
import logging
from copy import deepcopy
from typing import Any, Dict, List, Tuple
from pathlib import Path
from datetime import datetime, timezone

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
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "llama3.1:8b")

STREAM_IN = os.getenv("STREAM_IN", "confirmed_anomalies")
STREAM_OUT_READY = os.getenv("STREAM_OUT_READY", "playbook_ready")
STREAM_OUT_ERRORS = os.getenv("STREAM_OUT_ERRORS", "playbook_errors")
STREAM_NOTIFICATIONS = os.getenv("STREAM_NOTIFICATIONS", "notifications")

CONSUMER_GROUP = os.getenv("CONSUMER_GROUP", "playbook_agent")
CONSUMER_NAME = os.getenv("CONSUMER_NAME", f"agent-{uuid.uuid4().hex[:6]}")

MAX_STREAM_LEN = int(os.getenv("MAX_STREAM_LEN", "10000"))
BLOCK_MS = int(os.getenv("BLOCK_MS", "5000"))

CACAO_SCHEMA_REPO = os.getenv("CACAO_SCHEMA_REPO", "oasis-open/cacao-json-schemas")
CACAO_SCHEMA_BRANCH = os.getenv("CACAO_SCHEMA_BRANCH", "cacao-v2.0-cs01")
CACAO_SCHEMA_PATH = os.getenv("CACAO_SCHEMA_PATH", "schemas/playbook.json")

PLAN_SCHEMA_PATH = os.getenv("PLAN_SCHEMA_PATH", "schemas/plan.schema.json")

# Idempotency behavior
IDEMP_TTL_SEC = int(os.getenv("IDEMP_TTL_SEC", str(7 * 24 * 3600)))   # 7 days for processed
INPROGRESS_TTL_SEC = int(os.getenv("INPROGRESS_TTL_SEC", "900"))      # 15 minutes for in-progress


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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    asset_types = sorted({a.get("asset_type", "server") for a in (assets or [])})
    with db.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name, description, risk, requires_approval, environments, asset_types,
                       inputs_schema, openc2_template
                FROM tools
                WHERE :env = ANY(environments)
                """
            ),
            {"env": environment},
        ).mappings().all()

    tools = [dict(r) for r in rows]

    # Filter down to relevant asset types
    if asset_types:
        tools = [t for t in tools if any(at in (t.get("asset_types") or []) for at in asset_types)]
    return tools


def tool_index(tools: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {t["id"]: t for t in tools}


def _tool_to_action_name(tool_id: str) -> str:
    # "edr.isolate_host" -> "isolate_host"
    if not tool_id:
        return ""
    return tool_id.split(".", 1)[-1].strip().lower()


def filter_tools_by_constraints(tools: List[Dict[str, Any]], anomaly: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    HARD filter before the LLM sees the tool catalogue.
    - If allowed_actions is non-empty => whitelist mode
    - Always remove forbidden_actions
    """
    constraints = (anomaly or {}).get("constraints") or {}
    allowed = [a.lower() for a in (constraints.get("allowed_actions") or [])]
    forbidden = set(a.lower() for a in (constraints.get("forbidden_actions") or []))
    whitelist_mode = len(allowed) > 0

    filtered: List[Dict[str, Any]] = []
    for t in tools:
        tid = (t.get("id") or "").strip()
        action = _tool_to_action_name(tid)

        if action in forbidden:
            continue
        if whitelist_mode and action not in allowed:
            continue

        filtered.append(t)

    return filtered


def enforce_constraints(plan: Dict[str, Any], anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []

    constraints = (anomaly or {}).get("constraints") or {}
    allowed = [a.lower() for a in (constraints.get("allowed_actions") or [])]
    forbidden = [a.lower() for a in (constraints.get("forbidden_actions") or [])]
    whitelist_mode = len(allowed) > 0

    idx = tool_index(tools)

    for step in plan.get("steps", []):
        tid = (step.get("tool_id") or "").strip()
        if tid not in idx:
            continue

        action_name = _tool_to_action_name(tid)

        if action_name in forbidden:
            errors.append(f"Forbidden action '{action_name}' used by tool_id '{tid}'")

        if whitelist_mode and action_name not in allowed:
            errors.append(f"Action '{action_name}' (tool_id '{tid}') not in allowed_actions={allowed}")

    return errors


# ---------------- Plan schema ----------------
PLAN_SCHEMA: Dict[str, Any] = json.loads(Path(PLAN_SCHEMA_PATH).read_text())


def build_plan_schema_with_tool_enum(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Constrain tool_id to only valid tool IDs so the model can't invent 'unknown'.
    """
    schema = deepcopy(PLAN_SCHEMA)
    tool_ids = [t["id"] for t in tools if "id" in t]
    try:
        schema["properties"]["steps"]["items"]["properties"]["tool_id"]["enum"] = tool_ids
    except Exception:
        log.warning("Could not inject tool_id enum into PLAN_SCHEMA (schema structure unexpected)")
    return schema


# ---------------- Deterministic fallback plan ----------------
def build_deterministic_triage_plan(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Safe fallback: if there is a collect_triage tool, use it.
    Otherwise: return an investigate plan with zero steps (safe no-op).
    """
    triage_tool_id = None
    for t in tools:
        tid = t.get("id")
        if _tool_to_action_name(tid) == "collect_triage" or tid == "soar.collect_triage":
            triage_tool_id = tid
            break

    if triage_tool_id:
        return {
            "objective": "investigate",
            "steps": [
                {
                    "step_id": "s-triage-0001",
                    "tool_id": triage_tool_id,
                    "inputs": {},
                    "why": "Deterministic fallback: collect triage only (safe default).",
                    "verify": "Review collected artifacts/telemetry.",
                    "rollback": "N/A",
                }
            ],
            "confidence": 0.1,
            "risk_notes": ["Fallback mode (triage-only)."],
        }

    # If no triage tool exists, safest is to do nothing rather than hallucinate actions.
    return {
        "objective": "investigate",
        "steps": [],
        "confidence": 0.05,
        "risk_notes": ["Fallback mode: no permitted tools available under constraints."],
    }


# ---------------- LLM Plan ----------------
def llm_plan_stub(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    start = time.perf_counter()
    plan = build_deterministic_triage_plan(anomaly, tools)
    ms = int((time.perf_counter() - start) * 1000)
    return plan, ms


def llm_plan_ollama(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    tool_summaries = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t["description"],
            "risk": t["risk"],
            "requires_approval": t["requires_approval"],
            "inputs_schema": t["inputs_schema"],
            "asset_types": t.get("asset_types", []),
        }
        for t in tools
    ]

    schema_for_call = build_plan_schema_with_tool_enum(tools)

    prompt_obj = {
        "anomaly": anomaly,
        "capabilities": tool_summaries,
        "rules": [
            "Return ONLY valid JSON that matches the provided JSON schema.",
            "You MUST pick tool_id values that exist in capabilities[].id.",
            "You MUST NOT output any step whose action is in forbidden_actions.",
            "If allowed_actions is non-empty, you MUST ONLY use those actions.",
            "Respect constraints.allowed_actions and constraints.forbidden_actions.",
            "Every step MUST include verify and rollback. If not applicable, use 'N/A'.",
            "If no allowed tool exists, output objective=investigate and an empty steps list.",
            "Prefer least-disruptive actions first for critical services unless confidence is very high.",
            "If allowed_actions includes rollback_release AND evidence.details has release_version and prev_version, you MUST include a rollback_release step.",
            "When you include rollback_release, set inputs.service from entities.services[0] (or assets k8s/app name), namespace from entities.namespaces[0] (or assets namespace), and to_version from evidence.details.prev_version.",
            "Preferred sequence for release regressions: collect_triage -> rollback_release."
        ],
    }

    t0 = time.perf_counter()
    resp = requests.post(
        _ollama_api_url("/generate"),
        json={
            "model": OLLAMA_CHAT_MODEL,
            "prompt": (
                "You are an incident response planner (SOAR agent).\n"
                "Output MUST be JSON only.\n\n"
                f"INPUT:\n{json.dumps(prompt_obj, ensure_ascii=False)}\n"
            ),
            "format": schema_for_call,
            "stream": False,
        },
        timeout=240,
    )
    llm_ms = int((time.perf_counter() - t0) * 1000)

    if resp.status_code != 200:
        raise RuntimeError(f"Ollama error {resp.status_code}: {resp.text[:1000]}")
    resp.raise_for_status()

    body = resp.json()
    raw = body.get("response", "")

    if isinstance(raw, str):
        raw = raw.strip()
        plan = json.loads(raw)
        return plan, llm_ms

    if isinstance(raw, dict):
        return raw, llm_ms

    raise RuntimeError(f"Unexpected Ollama response type: {type(raw)}")


def plan_selector(anomaly: Dict[str, Any], tools: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], int]:
    if LLM_MODE == "ollama":
        return llm_plan_ollama(anomaly, tools)
    return llm_plan_stub(anomaly, tools)


# ---------------- Plan validation / gating ----------------
def validate_plan(plan: Dict[str, Any], tools: List[Dict[str, Any]]) -> List[str]:
    errors: List[str] = []
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
        schema = tool.get("inputs_schema") or {"type": "object"}

        try:
            js_validate(step.get("inputs", {}), schema)
        except ValidationError as e:
            errors.append(f"Invalid inputs for {tid}: {e.message}")

        risk = (tool.get("risk") or "low").lower()
        if risk in ("medium", "high", "critical"):
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
def _cacao_ts_z(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cacao_id(prefix: str) -> str:
    return f"{prefix}--{uuid.uuid4()}"


def generate_cacao_from_plan(anomaly: Dict[str, Any], plan: Dict[str, Any], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    idx = tool_index(tools)

    pb_id = f"playbook--{uuid.uuid4()}"
    created_by = f"identity--{uuid.uuid4()}"
    now_z = _cacao_ts_z()

    start_id = f"start--{uuid.uuid4()}"
    end_id = f"end--{uuid.uuid4()}"

    agent_id = _cacao_id("http-api")
    executor_url = os.getenv("EXECUTOR_BASE_URL", "http://executor.stub:8000")

    agent_obj = {
        "id": agent_id,
        "type": "http-api",
        "name": "poc-soar-agent",
        "description": "PoC execution agent (stub/executor over HTTP).",
        "address": {"url": [executor_url]},
    }

    entities = anomaly.get("entities") or {}
    hosts = entities.get("hosts") or []
    ips = entities.get("ips") or []

    target_defs: Dict[str, Any] = {}
    target_ids: List[str] = []

    for h in hosts:
        tid = _cacao_id("linux")
        target_defs[tid] = {
            "id": tid,
            "type": "linux",
            "name": h,
            "description": "Host extracted from anomaly entities.",
            "address": {"hostname": h},
        }
        target_ids.append(tid)

    for ip in ips:
        tid = _cacao_id("net-address")
        target_defs[tid] = {
            "id": tid,
            "type": "net-address",
            "name": ip,
            "description": "IP extracted from anomaly entities.",
            "address": {"ipv4": [ip]},
        }
        target_ids.append(tid)

    if not target_defs:
        dummy_id = _cacao_id("linux")
        target_defs[dummy_id] = {
            "id": dummy_id,
            "type": "linux",
            "name": "unknown",
            "description": "No targets provided by anomaly.",
            "address": {"hostname": "unknown"},
        }
        target_ids.append(dummy_id)

    steps = plan.get("steps") or []

    step_map: Dict[str, str] = {}
    for s in steps:
        sid = s.get("step_id") or f"s-{uuid.uuid4().hex[:6]}"
        step_map[sid] = f"action--{uuid.uuid4()}"

    workflow: Dict[str, Any] = {
        start_id: {"id": start_id, "type": "start", "on_completion": (step_map[steps[0]["step_id"]] if steps else end_id)},
        end_id: {"id": end_id, "type": "end"},
    }

    for i, s in enumerate(steps):
        sid = s["step_id"]
        action_id = step_map[sid]

        tool = idx[s["tool_id"]]
        openc2 = render_template(tool["openc2_template"], s.get("inputs", {}))

        next_id = step_map[steps[i + 1]["step_id"]] if i < len(steps) - 1 else end_id

        action_obj: Dict[str, Any] = {
            "id": action_id,
            "type": "action",
            "name": tool["name"],
            "description": f"{tool['description']} | why: {s.get('why')}",
            "agent": agent_id,
            "commands": [{"type": "openc2", "command": openc2}],
            "on_completion": next_id,
        }

        if target_ids:
            action_obj["targets"] = target_ids

        workflow[action_id] = action_obj

    playbook: Dict[str, Any] = {
        "type": "playbook",
        "spec_version": "cacao-2.0",
        "id": pb_id,
        "name": f"Auto-playbook: {anomaly['signal']['type']} {anomaly['signal']['summary'][:80]}",
        "description": (
            f"LLM-generated CACAO draft (model={OLLAMA_CHAT_MODEL}) for anomaly "
            f"{anomaly.get('anomaly_id','n/a')} [{anomaly['signal']['type']}|{anomaly['signal'].get('severity','n/a')}]. "
            f"Objective={plan.get('objective','n/a')}. Env={anomaly.get('environment','n/a')}. "
            f"Approval={((anomaly.get('constraints') or {}).get('requires_human_approval'))}."
        ),
        "created": now_z,
        "modified": now_z,
        "created_by": created_by,
        "workflow_start": start_id,
        "workflow": workflow,
        "agent_definitions": {agent_id: agent_obj},
        "target_definitions": target_defs,
    }

    return playbook


# ---------------- Persistence ----------------
def persist_case(
    anomaly: Dict[str, Any],
    plan: Dict[str, Any],
    cacao: Dict[str, Any],
    validation: Dict[str, Any],
    status: str = "DRAFT_READY",
) -> str:
    case_id = str(uuid.uuid4())
    params = {
        "id": case_id,
        "status": status,
        "anomaly": json.dumps(anomaly),
        "strategy": json.dumps(plan),
        "cacao": json.dumps(cacao),
        "validation": json.dumps(validation),
    }
    with db.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO cases (id, status, anomaly, strategy, cacao_draft, validation)
                VALUES (:id, :status,
                        CAST(:anomaly AS jsonb),
                        CAST(:strategy AS jsonb),
                        CAST(:cacao AS jsonb),
                        CAST(:validation AS jsonb))
                """
            ),
            params,
        )
        conn.execute(text("INSERT INTO approvals (case_id, status) VALUES (:id, 'PENDING')"), {"id": case_id})
    return case_id


# ---------------- Notifications ----------------
def notify(kind: str, payload: Dict[str, Any]) -> None:
    try:
        r.xadd(
            STREAM_NOTIFICATIONS,
            {"kind": kind, "json": json.dumps(payload)},
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )
    except Exception:
        log.exception("Failed to notify kind=%s", kind)


# ---------------- Streams worker ----------------
def ensure_group(stream: str, group: str) -> None:
    try:
        r.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception:
        pass


def _idemp_key(anomaly: Dict[str, Any]) -> str:
    anomaly_id = str(anomaly.get("anomaly_id") or "unknown")
    ing_ms = int(anomaly.get("ingested_at_ms") or 0)
    return f"{anomaly_id}:{ing_ms}"


def worker_loop() -> None:
    ensure_group(STREAM_IN, CONSUMER_GROUP)
    log.info("Worker started. stream=%s group=%s consumer=%s", STREAM_IN, CONSUMER_GROUP, CONSUMER_NAME)

    while True:
        msgs = r.xreadgroup(
            CONSUMER_GROUP,
            CONSUMER_NAME,
            {STREAM_IN: ">"},
            count=1,
            block=BLOCK_MS,
        )
        if not msgs:
            continue

        _, entries = msgs[0]
        msg_id, fields = entries[0]
        log.info("Processing msg_id=%s", msg_id)

        try:
            anomaly = json.loads(fields["json"])

            # -------- Idempotency guard (processed) --------
            ik = _idemp_key(anomaly)
            processed_key = f"processed:{ik}"
            inprog_key = f"inprog:{ik}"

            already = r.get(processed_key)
            if already:
                log.info("Idempotency: anomaly already processed (case_id=%s). Acking msg_id=%s", already, msg_id)
                r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
                continue

            # best-effort "in progress" lock
            locked = r.set(inprog_key, CONSUMER_NAME, nx=True, ex=INPROGRESS_TTL_SEC)
            if not locked:
                # Another worker/process likely handling it; PoC choice: ack to avoid duplicates/jams.
                log.info("Idempotency: in-progress lock exists for %s. Acking msg_id=%s", ik, msg_id)
                r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
                continue

            notify("anomaly_received", {"anomaly_id": anomaly.get("anomaly_id"), "env": anomaly.get("environment")})

            env = anomaly.get("environment", "prod")
            assets = anomaly.get("assets", [])  # ok if absent

            tools_all = load_tools(env, assets)
            tools = filter_tools_by_constraints(tools_all, anomaly)

            log.info("Loaded tools: %d (pre-filter=%d)", len(tools), len(tools_all))
            log.info("Capabilities tool ids: %s", [t["id"] for t in tools])

            # ---- Plan generation (LLM) ----
            plan, llm_ms = plan_selector(anomaly, tools)
            log.info("Generated plan")

            # ---- Plan validation ----
            plan_errors = validate_plan(plan, tools)
            plan_errors += enforce_constraints(plan, anomaly, tools)

            fallback_used = False
            if plan_errors:
                # Deterministic safe fallback
                fallback_used = True
                log.warning("Plan invalid; applying deterministic fallback. Errors=%s", plan_errors)
                plan = build_deterministic_triage_plan(anomaly, tools)

                # Re-validate fallback plan
                plan_errors = validate_plan(plan, tools)
                plan_errors += enforce_constraints(plan, anomaly, tools)

            # ---- CACAO generation + validation (best-effort) ----
            cacao: Dict[str, Any] = {}
            schema_errors: List[str] = []

            if not plan_errors:
                cacao = generate_cacao_from_plan(anomaly, plan, tools)
                schema_errors = validate_cacao(cacao)
            else:
                schema_errors = ["CACAO not generated due to plan errors"]

            # ---- Timing ----
            now_ms = int(time.time() * 1000)
            ingest_ms = int(anomaly.get("ingested_at_ms", now_ms))
            timing = {
                "ingested_at": anomaly.get("ingested_at"),
                "ingested_at_ms": ingest_ms,
                "plan_generated_at": utc_now_iso(),
                "mttr_ms": now_ms - ingest_ms,
                "llm_ms": llm_ms,
            }

            validation: Dict[str, Any] = {
                "plan_errors": plan_errors,
                "schema_errors": schema_errors,
                "tool_count": len(tools),
                "timing": timing,
                "fallback_used": fallback_used,
                "tools_pre_filter": len(tools_all),
            }

            log.info("Plan errors: %d | CACAO schema errors: %d", len(plan_errors), len(schema_errors))

            # ---- Fatal only if plan invalid ----
            if plan_errors:
                r.xadd(
                    STREAM_OUT_ERRORS,
                    {"msg_id": msg_id, "error": json.dumps(validation), "payload": fields["json"][:8000]},
                    maxlen=MAX_STREAM_LEN,
                    approximate=True,
                )
                r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
                # release lock
                r.delete(inprog_key)
                continue

            # ---- Persist always (even if CACAO invalid) ----
            status = "DRAFT_READY" if not schema_errors else "DRAFT_INVALID_CACAO"
            log.info("Persisting case (status=%s)...", status)

            case_id = persist_case(anomaly, plan, cacao, validation, status=status)
            log.info("Persisted case_id=%s", case_id)

            # mark processed for idempotency + clear lock
            r.set(processed_key, case_id, ex=IDEMP_TTL_SEC)
            r.delete(inprog_key)

            r.xadd(STREAM_OUT_READY, {"case_id": case_id}, maxlen=MAX_STREAM_LEN, approximate=True)
            notify("case_created", {"case_id": case_id, "anomaly_id": anomaly.get("anomaly_id")})

            r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
            log.info("OK anomaly=%s case_id=%s", anomaly.get("anomaly_id"), case_id)

        except Exception as e:
            log.exception("Failed processing msg_id=%s", msg_id)
            r.xadd(
                STREAM_OUT_ERRORS,
                {"msg_id": msg_id, "error": repr(e), "payload": fields.get("json", "")[:8000]},
                maxlen=MAX_STREAM_LEN,
                approximate=True,
            )
            r.xack(STREAM_IN, CONSUMER_GROUP, msg_id)
            time.sleep(0.25)


if __name__ == "__main__":
    worker_loop()
