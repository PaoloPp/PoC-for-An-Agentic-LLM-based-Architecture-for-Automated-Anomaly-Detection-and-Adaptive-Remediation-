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
    # pool_pre_ping helps survive container/network hiccups
    return create_engine(DATABASE_URL, pool_pre_ping=True)


def _to_jsonable(v: Any) -> Any:
    """Make DB-returned values safe/pretty for st.json()."""
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
        # if it's JSON-as-string, parse it
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return v
        return v
    return str(v)


@st.cache_data(ttl=2)  # DB-backed UI should use a short ttl to avoid stale results :contentReference[oaicite:3]{index=3}
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
        # Convert RowMapping -> dict for reliable Streamlit rendering
        return [dict(r) for r in rows]


def load_case(case_id: str) -> Optional[Dict[str, Any]]:
    eng = _engine()
    with eng.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM cases WHERE id=:id"),
            {"id": case_id},
        ).mappings().one_or_none()  # avoids NoResultFound crash :contentReference[oaicite:4]{index=4}
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
    format_func=case_label,  # display label without changing returned value :contentReference[oaicite:5]{index=5}
)

case_id = str(selected_case["id"])
case = load_case(case_id)

if case is None:
    st.warning("Case not found (it may have been deleted or not committed).")
    st.stop()

# Header info
meta1, meta2, meta3 = st.columns([1, 1, 2])
with meta1:
    st.metric("Status", str(case.get("status", "UNKNOWN")))
with meta2:
    st.metric("Case ID", case_id)
with meta3:
    st.write("")

# Main content
left, right = st.columns([1, 1])

with left:
    st.subheader("Anomaly")
    st.json(_to_jsonable(case.get("anomaly")), expanded=2)  # pretty JSON rendering :contentReference[oaicite:6]{index=6}

    st.subheader("Strategy / Plan")
    st.json(_to_jsonable(case.get("strategy")), expanded=2)  # pretty JSON rendering :contentReference[oaicite:7]{index=7}

with right:
    st.subheader("CACAO draft")
    st.json(_to_jsonable(case.get("cacao_draft")), expanded=1)  # pretty JSON rendering :contentReference[oaicite:8]{index=8}

    st.subheader("Validation")
    st.json(_to_jsonable(case.get("validation")), expanded=2)  # pretty JSON rendering :contentReference[oaicite:9]{index=9}

# Optional raw dump for debugging
with st.expander("Raw case record"):
    st.json({k: _to_jsonable(v) for k, v in case.items()}, expanded=1)  # :contentReference[oaicite:10]{index=10}
