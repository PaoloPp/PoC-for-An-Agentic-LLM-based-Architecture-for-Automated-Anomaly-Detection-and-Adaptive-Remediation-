CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE playbooks (
  id UUID PRIMARY KEY,
  name TEXT NOT NULL,
  tags JSONB NOT NULL DEFAULT '{}',
  cacao JSONB NOT NULL,
  embedding vector(1536) NOT NULL
);

-- Fast similarity search: pick one index type.
-- HNSW is great for recall/speed tradeoffs; IVFFlat is simpler but needs tuning.
-- (Both are supported by pgvector.)
CREATE INDEX playbooks_embedding_hnsw
ON playbooks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE cases (
  id UUID PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  status TEXT NOT NULL, -- NEW, DRAFT_READY, APPROVED, REJECTED, EXECUTED
  anomaly JSONB NOT NULL,
  strategy JSONB,
  cacao_draft JSONB,
  validation JSONB
);

CREATE TABLE approvals (
  case_id UUID PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
  status TEXT NOT NULL, -- PENDING, APPROVED, REJECTED
  reviewer TEXT,
  notes TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tools (
  id TEXT PRIMARY KEY,                           -- e.g. "edr.isolate_host"
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  risk TEXT NOT NULL DEFAULT 'low',              -- low|medium|high|critical
  requires_approval BOOLEAN NOT NULL DEFAULT true,
  environments TEXT[] NOT NULL DEFAULT ARRAY['prod','staging','dev'],
  asset_types TEXT[] NOT NULL DEFAULT ARRAY['server','endpoint','k8s','db','iam'],
  inputs_schema JSONB NOT NULL,                  -- JSON Schema for inputs
  openc2_template JSONB NOT NULL                 -- OpenC2-ish template
);

INSERT INTO tools (id,name,description,risk,requires_approval,environments,asset_types,inputs_schema,openc2_template)
VALUES
('edr.isolate_host','Isolate host','Network isolate a host via EDR','high',true,ARRAY['prod','staging'],ARRAY['server','endpoint'],
 '{"type":"object","properties":{"host_id":{"type":"string"},"ttl_minutes":{"type":"integer","minimum":5,"maximum":1440}},"required":["host_id"],"additionalProperties":false}',
 '{"action":"contain","target":{"device":"{{host_id}}"},"args":{"duration_minutes":"{{ttl_minutes}}"}}'
),
('iam.disable_user','Disable user','Disable an IAM user account','high',true,ARRAY['prod','staging','dev'],ARRAY['iam'],
 '{"type":"object","properties":{"username":{"type":"string"},"reason":{"type":"string"}},"required":["username"],"additionalProperties":false}',
 '{"action":"disable","target":{"user":"{{username}}"},"args":{"reason":"{{reason}}"}}'
),
('fw.block_ip','Block IP','Block an IP at perimeter firewall','medium',true,ARRAY['prod','staging'],ARRAY['server','k8s'],
 '{"type":"object","properties":{"ip":{"type":"string"},"ttl_minutes":{"type":"integer","minimum":5,"maximum":1440}},"required":["ip"],"additionalProperties":false}',
 '{"action":"deny","target":{"ipv4_addr":"{{ip}}"},"args":{"duration_minutes":"{{ttl_minutes}}"}}'
);
