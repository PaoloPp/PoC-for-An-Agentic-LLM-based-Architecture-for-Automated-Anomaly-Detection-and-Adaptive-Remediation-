import os
import json
from datetime import datetime
from typing import Any, Dict, Optional

import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError


DATABASE_URL = os.environ.get("DATABASE_URL", "")

st.set_page_config(page_title="SOAR Approval UI", layout="wide")


def _engine() -> Engine:
    if not DATABASE_URL:
        st.error("DATABASE_URL env var is missing.")
        st.stop()
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _to_jsonable(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list, int, float, bool)):
        return v
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        try:
            return v.decode("utf-8")
        except Exception:
            return str(v)
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return v
        return v
    return str(v)


def _get_nested(d: Any, path: list, default=None):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _fmt_ms(ms):
    if ms is None:
        return "N/A"
    try:
        ms = int(ms)
    except Exception:
        return "N/A"
    if ms < 1000:
        return f"{ms} ms"
    return f"{ms/1000:.2f} s"


@st.cache_data(ttl=2)
def load_cases() -> list[Dict[str, Any]]:
    eng = _engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, status, anomaly, created_at
                FROM cases
                ORDER BY created_at DESC NULLS LAST
                LIMIT 200
                """
            )
        ).mappings().all()
        return [dict(r) for r in rows]


def load_case(case_id: str) -> Optional[Dict[str, Any]]:
    eng = _engine()
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM cases WHERE id=:id"),
            {"id": case_id},
        ).mappings().one_or_none()
        return dict(row) if row else None


def case_label(c: Dict[str, Any]) -> str:
    anomaly = _to_jsonable(c.get("anomaly")) or {}
    if isinstance(anomaly, str):
        try:
            anomaly = json.loads(anomaly)
        except Exception:
            anomaly = {}
    signal = (anomaly or {}).get("signal") or {}
    summary = signal.get("summary") or "no summary"
    severity = signal.get("severity") or "n/a"
    status = c.get("status") or "UNKNOWN"
    short_id = str(c.get("id"))[:8]
    return f"{status} | {severity} | {summary} | {short_id}"


st.title("Approval UI")

col_a, col_b = st.columns([1, 1])
with col_a:
    st.caption("Showing latest cases from Postgres.")
with col_b:
    if st.button("Refresh"):
        st.cache_data.clear()

try:
    cases = load_cases()
except SQLAlchemyError as e:
    st.error(f"DB error while loading cases: {e}")
    st.stop()

if not cases:
    st.info("No cases in the database yet. Post an anomaly and let the worker generate a case.")
    st.stop()

selected_case = st.selectbox(
    "Select a case",
    options=cases,
    format_func=case_label,
)

case_id = str(selected_case["id"])
case = load_case(case_id)

if case is None:
    st.warning("Case not found (it may have been deleted or not committed).")
    st.stop()

validation = _to_jsonable(case.get("validation")) or {}
if isinstance(validation, str):
    try:
        validation = json.loads(validation)
    except Exception:
        validation = {}

timing = _get_nested(validation, ["timing"], {}) or {}
quality = _get_nested(validation, ["quality"], {}) or {}

# Header metrics
m1, m2, m3, m4, m5 = st.columns([1, 1, 1, 1, 1])
with m1:
    st.metric("Status", str(case.get("status", "UNKNOWN")))
with m2:
    st.metric("Case ID", case_id[:8])
with m3:
    st.metric("MTTR", _fmt_ms(timing.get("mttr_ms")))
with m4:
    st.metric("LLM time", _fmt_ms(timing.get("llm_ms")))
with m5:
    st.metric("PQS", str(quality.get("pqs_total", "N/A")))

left, right = st.columns([1, 1])

with left:
    st.subheader("Anomaly")
    st.json(_to_jsonable(case.get("anomaly")), expanded=2)

    st.subheader("Strategy / Plan")
    st.json(_to_jsonable(case.get("strategy")), expanded=2)

with right:
    st.subheader("CACAO draft")
    st.json(_to_jsonable(case.get("cacao_draft")), expanded=1)

    st.subheader("Validation + Metrics")
    st.json(_to_jsonable(case.get("validation")), expanded=2)

    if isinstance(quality, dict) and quality:
        st.subheader("Quality Breakdown")
        st.json(quality, expanded=2)

with st.expander("Raw case record"):
    st.json({k: _to_jsonable(v) for k, v in case.items()}, expanded=1)