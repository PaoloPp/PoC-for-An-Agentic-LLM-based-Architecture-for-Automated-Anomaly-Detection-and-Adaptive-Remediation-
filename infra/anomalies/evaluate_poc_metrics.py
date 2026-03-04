import os
import json
import time
import argparse
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import requests
from sqlalchemy import create_engine, text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_content_id(payload: Dict[str, Any]) -> str:
    """
    Stable identifier for 'same anomaly content' across K runs.
    Strips fields that change per run.
    """
    p = dict(payload)
    for k in ["anomaly_id", "ingested_at", "ingested_at_ms", "run_tag", "run_id", "posted_at"]:
        p.pop(k, None)
    s = json.dumps(p, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def post_anomaly(url: str, payload: Dict[str, Any], timeout: int = 30) -> None:
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()


def wait_for_cases(engine, run_tag: str, expected: int, timeout_sec: int = 180, poll_sec: float = 1.0) -> List[Dict[str, Any]]:
    """
    Poll Postgres until we see expected cases whose anomaly_id contains run_tag.
    We do NOT rely on extra fields (run_tag/run_id) being persisted, since Pydantic might drop them.
    """
    deadline = time.time() + timeout_sec
    last: List[Dict[str, Any]] = []

    pattern = f"%{run_tag}%"

    while time.time() < deadline:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, status, created_at, anomaly, strategy, validation
                    FROM cases
                    WHERE (CAST(anomaly AS jsonb)->>'anomaly_id') LIKE :pattern
                    ORDER BY created_at ASC
                """),
                {"pattern": pattern},
            ).mappings().all()
            last = [dict(r) for r in rows]

        if len(last) >= expected:
            return last

        time.sleep(poll_sec)

    return last

def wait_for_cases_settle(engine, run_tag: str, timeout_sec: int = 600, poll_sec: float = 1.0, settle_sec: int = 60):
    """
    Poll until:
    - timeout, OR
    - case count hasn't increased for `settle_sec` seconds
    Returns all matching cases.
    """
    deadline = time.time() + timeout_sec
    pattern = f"%{run_tag}%"

    last_count = -1
    last_change = time.time()
    last_rows = []

    while time.time() < deadline:
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, status, created_at, anomaly, strategy, validation
                    FROM cases
                    WHERE (CAST(anomaly AS jsonb)->>'anomaly_id') LIKE :pattern
                    ORDER BY created_at ASC
                """),
                {"pattern": pattern},
            ).mappings().all()
            last_rows = [dict(r) for r in rows]

        count = len(last_rows)
        if count != last_count:
            last_count = count
            last_change = time.time()
            print(f"[db] match count={count} (changed)")
        else:
            print(f"[db] match count={count}")

        # If nothing new arrived for settle_sec, assume we're done
        if time.time() - last_change >= settle_sec:
            print(f"[i] settled after {settle_sec}s with {count} cases")
            return last_rows

        time.sleep(poll_sec)

    print(f"[i] timeout reached with {len(last_rows)} cases")
    return last_rows

def parse_json_field(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except Exception:
                return v
        return v
    return v


def action_set_from_plan(plan: Dict[str, Any]) -> set:
    """
    Extract action category set from plan steps (tool_id suffix after dot).
    """
    out = set()
    for step in (plan or {}).get("steps", []):
        tid = (step.get("tool_id") or "")
        action = tid.split(".", 1)[-1].strip().lower() if "." in tid else tid.strip().lower()
        if action:
            out.add(action)
    return out


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def compute_group_stability(action_sets: List[set]) -> float:
    """
    Mean pairwise Jaccard similarity across K runs.
    """
    if len(action_sets) <= 1:
        return 1.0
    sims = []
    for i in range(len(action_sets)):
        for j in range(i + 1, len(action_sets)):
            sims.append(jaccard(action_sets[i], action_sets[j]))
    return sum(sims) / len(sims) if sims else 1.0


def group_key_from_anomaly_id(anomaly_id: str) -> str:
    """
    Your script generates anomaly_id like:
      <base>-<run_tag>-01
      <base>-<run_tag>-02
    Group key is anomaly_id without trailing -NN.
    """
    parts = anomaly_id.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) == 2:
        return "-".join(parts[:-1])
    return anomaly_id


def safe_get(d: Any, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anomalies", nargs="+", required=True, help="List of anomaly JSON files")
    ap.add_argument("-k", "--runs", type=int, default=5, help="Runs per anomaly")
    ap.add_argument("--ingest-url", default=os.getenv("INGEST_URL", "http://localhost:8001/confirmed-anomalies"))
    ap.add_argument("--db-url", default=os.getenv("DATABASE_URL", ""))
    ap.add_argument("--timeout", type=int, default=600, help="Seconds to wait for cases")
    ap.add_argument("--poll", type=float, default=1.0, help="Polling interval")
    ap.add_argument("--out", default="poc_eval_results.csv")
    ap.add_argument("--run-tag", default=None, help="Optional run tag. If not set, generated automatically.")
    args = ap.parse_args()

    if not args.db_url:
        raise SystemExit("DATABASE_URL env var missing (or pass --db-url).")

    engine = create_engine(args.db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        print("[db] current_database():", conn.execute(text("select current_database()")).scalar())
        print("[db] inet_server_addr():", conn.execute(text("select inet_server_addr()")).scalar())
        print("[db] inet_server_port():", conn.execute(text("select inet_server_port()")).scalar())
        print("[db] cases count:", conn.execute(text("select count(*) from cases")).scalar())

    if args.run_tag:
        run_tag = args.run_tag
    else:
        run_tag = f"eval-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    print(f"[i] run_tag={run_tag}")
    print(f"[i] ingest_url={args.ingest_url}")
    print(f"[i] runs_per_anomaly={args.runs}")

    # Load anomalies
    base_payloads: List[Tuple[str, Dict[str, Any]]] = []
    for fp in args.anomalies:
        with open(fp, "r", encoding="utf-8") as f:
            payload = json.load(f)
        base_payloads.append((fp, payload))

    expected_total = len(base_payloads) * args.runs

    # Post anomalies K times each
    print(f"[i] Posting {expected_total} anomalies...")
    sent = 0

    for fp, base in base_payloads:
        base_id = base.get("anomaly_id", "a")
        content_id = stable_content_id(base)

        for i in range(args.runs):
            payload = dict(base)

            # Make it unique but parseable for grouping and DB search
            payload["anomaly_id"] = f"{base_id}-{run_tag}-{i+1:02d}"

            # These extra fields might be dropped by ingestion schema, but harmless
            payload["run_tag"] = run_tag
            payload["run_content_id"] = content_id
            payload["posted_at"] = utc_now_iso()

            post_anomaly(args.ingest_url, payload)
            sent += 1
            if sent % 10 == 0 or sent == expected_total:
                print(f"  sent {sent}/{expected_total}")

    print("[i] Waiting for cases in Postgres...")
    # cases = wait_for_cases(engine, run_tag, expected_total, timeout_sec=args.timeout, poll_sec=args.poll)
    cases = wait_for_cases_settle(engine, run_tag, timeout_sec=args.timeout, poll_sec=args.poll, settle_sec=args.poll * 6)
    print(f"[i] Retrieved {len(cases)}/{expected_total} cases")

    if not cases:
        print("[!] No cases found. Check that the worker is running and that cases are persisted.")
        print("    Also verify your DATABASE_URL points to the same Postgres used by the PoC.")
        return

    # Per-case rows and grouping for stability
    rows_out: List[Dict[str, Any]] = []
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for c in cases:
        anomaly = parse_json_field(c.get("anomaly")) or {}
        plan = parse_json_field(c.get("strategy")) or {}
        validation = parse_json_field(c.get("validation")) or {}

        anomaly_id = str(anomaly.get("anomaly_id", "unknown"))
        gk = group_key_from_anomaly_id(anomaly_id)
        grouped.setdefault(gk, []).append({"case": c, "anomaly": anomaly, "plan": plan, "validation": validation})

        timing = (validation.get("timing") or {}) if isinstance(validation, dict) else {}
        quality = (validation.get("quality") or {}) if isinstance(validation, dict) else {}

        actions = sorted(list(action_set_from_plan(plan)))

        rows_out.append({
            "run_tag": run_tag,
            "group_key": gk,
            "case_id": c.get("id"),
            "status": c.get("status"),
            "created_at": str(c.get("created_at")),
            "anomaly_id": anomaly_id,
            "signal_type": safe_get(anomaly, "signal", "type"),
            "severity": safe_get(anomaly, "signal", "severity"),
            "confidence": safe_get(anomaly, "signal", "confidence"),
            "allowed_actions": json.dumps(safe_get(anomaly, "constraints", "allowed_actions", default=[])),
            "forbidden_actions": json.dumps(safe_get(anomaly, "constraints", "forbidden_actions", default=[])),
            "mttr_ms": timing.get("mttr_ms"),
            "llm_ms": timing.get("llm_ms"),
            "pqs": safe_get(quality, "pqs_total"),
            "coverage_fit": safe_get(quality, "scores", "coverage_fit"),
            "safety_disruption": safe_get(quality, "scores", "safety_disruption"),
            "completeness": safe_get(quality, "scores", "completeness"),
            "actionability": safe_get(quality, "scores", "actionability"),
            "cacao_validity": safe_get(quality, "scores", "cacao_validity"),
            "actions": ";".join(actions),
        })

    # Stability summary
    stability_rows: List[Dict[str, Any]] = []
    for gk, items in grouped.items():
        action_sets = [action_set_from_plan(x["plan"]) for x in items]
        stab = compute_group_stability(action_sets)

        pqs_vals = []
        for x in items:
            q = (x["validation"].get("quality") or {}) if isinstance(x["validation"], dict) else {}
            v = q.get("pqs_total")
            if isinstance(v, (int, float)):
                pqs_vals.append(float(v))

        avg_pqs = (sum(pqs_vals) / len(pqs_vals)) if pqs_vals else None

        # also show mean MTTR and mean LLM time if present
        mttr_vals = []
        llm_vals = []
        for x in items:
            t = (x["validation"].get("timing") or {}) if isinstance(x["validation"], dict) else {}
            if isinstance(t.get("mttr_ms"), (int, float)):
                mttr_vals.append(float(t["mttr_ms"]))
            if isinstance(t.get("llm_ms"), (int, float)):
                llm_vals.append(float(t["llm_ms"]))

        stability_rows.append({
            "run_tag": run_tag,
            "group_key": gk,
            "k": len(items),
            "stability_jaccard_mean": stab,
            "avg_pqs": avg_pqs,
            "avg_mttr_ms": (sum(mttr_vals) / len(mttr_vals)) if mttr_vals else None,
            "avg_llm_ms": (sum(llm_vals) / len(llm_vals)) if llm_vals else None,
        })

    # Write CSV
    import csv
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        fieldnames = list(rows_out[0].keys()) if rows_out else ["run_tag"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for rrow in rows_out:
            w.writerow(rrow)

        f.write("\n# ---- stability_summary ----\n")
        if stability_rows:
            w2 = csv.DictWriter(f, fieldnames=list(stability_rows[0].keys()))
            w2.writeheader()
            for srow in stability_rows:
                w2.writerow(srow)

    print(f"[i] Wrote results to {args.out}")
    print("[i] Stability summary:")
    for s in stability_rows:
        print(f"  group={s['group_key']} k={s['k']} stability={s['stability_jaccard_mean']:.3f} avg_pqs={s['avg_pqs']} avg_llm_ms={s['avg_llm_ms']}")


if __name__ == "__main__":
    main()