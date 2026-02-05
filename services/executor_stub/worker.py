import os, uuid, json
import redis
from sqlalchemy import create_engine, text

r = redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
db = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)

def worker_loop():
    group = "executor"
    consumer = f"exec-{uuid.uuid4().hex[:6]}"
    stream = "playbook_approved"

    try:
        r.xgroup_create(stream, group, id="0-0", mkstream=True)
    except Exception:
        pass

    while True:
        msgs = r.xreadgroup(group, consumer, {stream: ">"}, count=1, block=5000)
        if not msgs:
            continue

        _, entries = msgs[0]
        msg_id, fields = entries[0]
        case_id = fields["case_id"]

        with db.begin() as conn:
            c = conn.execute(text("SELECT cacao_draft FROM cases WHERE id=:id"), {"id": case_id}).mappings().one()
        pb = c["cacao_draft"]

        # Walk workflow, print any 'openc2' payloads
        workflow = pb.get("workflow", {})
        for node_id, node in workflow.items():
            if isinstance(node, dict) and "openc2" in node:
                print(f"[EXEC] case={case_id} node={node_id} openc2={json.dumps(node['openc2'])}")

        with db.begin() as conn:
            conn.execute(text("UPDATE cases SET status='EXECUTED' WHERE id=:id"), {"id": case_id})

        r.xack(stream, group, msg_id)

if __name__ == "__main__":
    worker_loop()
