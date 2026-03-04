# PoC: Agentic LLM-based Architecture for Automated Anomaly Detection and Adaptive Remediation (CoDIT26)

This repository contains a local Proof-of-Concept for an **agentic SOAR-style pipeline** that:

1. receives **confirmed anomalies** (API),
2. generates an **action plan** (LLM or deterministic fallback),
3. converts the plan into a **CACAO 2.0 playbook draft**,
4. stores everything as a **case** in Postgres (+ pgvector),
5. waits for **human approval** (UI),
6. then “executes” actions via an **executor stub** that prints OpenC2-like commands.

Everything runs locally via Docker Compose. Because humans love complexity, this includes Redis streams, Postgres, and optional LLM inference via Ollama.

---

## Architecture (what talks to what)

**Flow:**

1. You POST a confirmed anomaly to `anomaly_api`
2. `playbook_agent_worker` consumes it from a Redis Stream (`confirmed_anomalies`)
3. Worker:
   - loads allowed tools from DB (`tools` table)
   - optionally filters tools by constraints (allowlist/denylist)
   - generates a plan (LLM or safe fallback)
   - generates CACAO playbook draft from the plan
   - validates CACAO draft against CACAO schema
   - persists a `case` in Postgres
4. You open `approval_ui` and approve/reject the case
5. Approval emits an event to Redis Stream `playbook_approved`
6. `executor_stub` consumes approvals and prints the OpenC2 commands embedded in the CACAO playbook

---

## Services

| Service | What it does | Port (host → container) |
|---|---|---|
| `redis` | Event bus (Redis Streams) | `6379:6379` |
| `db` | Postgres + pgvector (cases, tools, playbooks) | `5432:5432` |
| `anomaly_api` | Receives confirmed anomalies, pushes to Redis stream | `8001:8000` |
| `playbook_agent` | API to approve/reject cases | `8002:8000` |
| `playbook_agent_worker` | Stream worker generating plans + CACAO drafts | (no port) |
| `approval_ui` | Streamlit UI for review/approval | `8501:8501` |
| `executor_stub` | Consumes approvals, prints OpenC2 commands | `8003:8000` (not really used) |

---

## Data model (DB)

The database is initialized via `infra/init.sql`.

It creates:

- `cases`: anomaly payload + plan (“strategy”) + CACAO draft + validation metadata, with status (`NEW`, `DRAFT_READY`, `APPROVED`, `EXECUTED`, etc.)
- `approvals`: per-case approval state (`PENDING`, `APPROVED`, `REJECTED`)
- `tools`: tool registry used by the agent (risk level, JSON schema for inputs, OpenC2 template)
- `playbooks`: CACAO playbooks + embedding (pgvector)

---

## Prerequisites

Minimum:

- Docker + Docker Compose
- Ports available on your machine: `6379`, `5432`, `8001`, `8002`, `8501`

Optional (if you want real LLM planning):

- Ollama installed and running locally (default port `11434`)
- Models pulled:
  - `llama3.1:8b` (chat/planning)
  - `embeddinggemma` (declared but not used heavily in this PoC)

---

## Quickstart (local, deterministic “stub” mode)

This is the “it works even if your LLM is asleep” mode.

### 1) Start the stack

```bash
git clone https://github.com/PaoloPp/PoC-for-An-Agentic-LLM-based-Architecture-for-Automated-Anomaly-Detection-and-Adaptive-Remediation-.git
cd PoC-for-An-Agentic-LLM-based-Architecture-for-Automated-Anomaly-Detection-and-Adaptive-Remediation-/infra

docker compose up --build
