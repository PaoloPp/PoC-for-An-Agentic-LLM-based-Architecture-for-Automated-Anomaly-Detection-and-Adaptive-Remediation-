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

-- Existing + expanded tool registry
-- Safe to re-run (UPSERT)

INSERT INTO tools (
  id, name, description, risk, requires_approval, environments, asset_types, inputs_schema, openc2_template
)
VALUES
-- 1) EDR: isolate host
(
  'edr.isolate_host',
  'Isolate host',
  'Network isolate a host via EDR',
  'high',
  true,
  ARRAY['prod','staging'],
  ARRAY['server','endpoint'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "host_id":{"type":"string","minLength":1},
      "ttl_minutes":{"type":"integer","minimum":5,"maximum":1440}
    },
    "required":["host_id"]
  }'::jsonb,
  '{
    "action":"contain",
    "target":{"device":"{{host_id}}"},
    "args":{"duration_minutes":"{{ttl_minutes}}"}
  }'::jsonb
),

-- 2) IAM: disable user
(
  'iam.disable_user',
  'Disable user',
  'Disable an IAM user account',
  'high',
  true,
  ARRAY['prod','staging','dev'],
  ARRAY['iam','identity'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "username":{"type":"string","minLength":1},
      "reason":{"type":"string"}
    },
    "required":["username"]
  }'::jsonb,
  '{
    "action":"disable",
    "target":{"user":"{{username}}"},
    "args":{"reason":"{{reason}}"}
  }'::jsonb
),

-- 3) Firewall: block IP
(
  'fw.block_ip',
  'Block IP',
  'Block an IP at perimeter firewall',
  'medium',
  true,
  ARRAY['prod','staging'],
  ARRAY['server','k8s','network'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "ip":{"type":"string","minLength":7},
      "ttl_minutes":{"type":"integer","minimum":5,"maximum":1440}
    },
    "required":["ip"]
  }'::jsonb,
  '{
    "action":"deny",
    "target":{"ipv4_addr":"{{ip}}"},
    "args":{"duration_minutes":"{{ttl_minutes}}"}
  }'::jsonb
),

-- 4) IAM: reset MFA (good for MFA fatigue)
(
  'iam.reset_mfa',
  'Reset user MFA',
  'Force reset of a user MFA methods to break MFA fatigue loops and invalidate compromised factors',
  'medium',
  true,
  ARRAY['prod','staging','dev'],
  ARRAY['iam','identity'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "username":{"type":"string","minLength":1},
      "reason":{"type":"string","minLength":1}
    },
    "required":["username","reason"]
  }'::jsonb,
  '{
    "action":"update",
    "target":{"user":"{{username}}"},
    "args":{"operation":"reset_mfa","reason":"{{reason}}"}
  }'::jsonb
),

-- 5) IAM: revoke sessions (kill tokens, logouts everywhere)
(
  'iam.revoke_sessions',
  'Revoke sessions',
  'Revoke active user sessions / tokens to force re-authentication',
  'medium',
  true,
  ARRAY['prod','staging','dev'],
  ARRAY['iam','identity'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "username":{"type":"string","minLength":1},
      "scope":{"type":"string","enum":["all","web","vpn","api"],"default":"all"},
      "reason":{"type":"string"}
    },
    "required":["username"]
  }'::jsonb,
  '{
    "action":"deny",
    "target":{"user":"{{username}}"},
    "args":{"operation":"revoke_sessions","scope":"{{scope}}","reason":"{{reason}}"}
  }'::jsonb
),

-- 6) IAM/Cloud: rotate API key (service account / app key)
(
  'iam.rotate_api_key',
  'Rotate API key',
  'Rotate an application/API key for a service account or integration',
  'high',
  true,
  ARRAY['prod','staging'],
  ARRAY['iam','app','cloud'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "principal":{"type":"string","minLength":1},
      "key_id":{"type":"string"},
      "reason":{"type":"string"}
    },
    "required":["principal"]
  }'::jsonb,
  '{
    "action":"update",
    "target":{"user":"{{principal}}"},
    "args":{"operation":"rotate_api_key","key_id":"{{key_id}}","reason":"{{reason}}"}
  }'::jsonb
),

-- 7) DNS/Proxy: block domain (beaconing, malware C2, etc.)
(
  'net.block_domain',
  'Block domain',
  'Block a domain via DNS sinkhole / proxy policy',
  'medium',
  true,
  ARRAY['prod','staging'],
  ARRAY['network','server','endpoint'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "domain":{"type":"string","minLength":3},
      "ttl_minutes":{"type":"integer","minimum":5,"maximum":1440}
    },
    "required":["domain"]
  }'::jsonb,
  '{
    "action":"deny",
    "target":{"domain_name":"{{domain}}"},
    "args":{"duration_minutes":"{{ttl_minutes}}"}
  }'::jsonb
),

-- 8) Email/EDR: quarantine by hash (attachment / binary)
(
  'edr.quarantine_hash',
  'Quarantine file hash',
  'Quarantine a file across endpoints by hash (EDR action)',
  'high',
  true,
  ARRAY['prod','staging'],
  ARRAY['endpoint','server'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "sha256":{"type":"string","minLength":32},
      "reason":{"type":"string"},
      "scope":{"type":"string","enum":["all_endpoints","targeted"],"default":"targeted"}
    },
    "required":["sha256"]
  }'::jsonb,
  '{
    "action":"delete",
    "target":{"file":{"hash":"{{sha256}}"}},
    "args":{"operation":"quarantine","scope":"{{scope}}","reason":"{{reason}}"}
  }'::jsonb
),

-- 9) Endpoint: disable scheduled task (persistence)
(
  'edr.disable_scheduled_task',
  'Disable scheduled task',
  'Disable a suspicious scheduled task / cron job used for persistence',
  'medium',
  true,
  ARRAY['prod','staging'],
  ARRAY['endpoint','server'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "host_id":{"type":"string","minLength":1},
      "task_name":{"type":"string","minLength":1},
      "reason":{"type":"string"}
    },
    "required":["host_id","task_name"]
  }'::jsonb,
  '{
    "action":"update",
    "target":{"device":"{{host_id}}"},
    "args":{"operation":"disable_scheduled_task","task_name":"{{task_name}}","reason":"{{reason}}"}
  }'::jsonb
),

-- 10) Collect triage
(
  'soar.collect_triage',
  'Collect triage',
  'Collect diagnostics (logs/metrics/process/network) for investigation and evidence preservation',
  'low',
  false,
  ARRAY['prod','staging','dev'],
  ARRAY['server','endpoint','k8s','iam','identity','network','app','cloud'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "host_id":{"type":"string","minLength":1},
      "window_minutes":{"type":"integer","minimum":5,"maximum":1440,"default":30},
      "include":{
        "type":"array",
        "items":{"type":"string","enum":["logs","metrics","processes","network","auth","dns"]},
        "default":["logs","metrics","processes","network"]
      }
    },
    "required":["host_id"]
  }'::jsonb,
  '{
    "action":"query",
    "target":{"device":"{{host_id}}"},
    "args":{"window_minutes":"{{window_minutes}}","include":"{{include}}"}
  }'::jsonb
),

-- 11) Deployment: rollback release (availability regressions)
(
  'deploy.rollback_release',
  'Rollback release',
  'Rollback a deployment/release to a previously known-good version',
  'high',
  true,
  ARRAY['prod','staging'],
  ARRAY['k8s','app'],
  '{
    "type":"object",
    "additionalProperties":false,
    "properties":{
      "service":{"type":"string","minLength":1},
      "namespace":{"type":"string","default":"default"},
      "to_version":{"type":"string"},
      "reason":{"type":"string"}
    },
    "required":["service"]
  }'::jsonb,
  '{
    "action":"restore",
    "target":{"app":{"name":"{{service}}","namespace":"{{namespace}}"}},
    "args":{"operation":"rollback","to_version":"{{to_version}}","reason":"{{reason}}"}
  }'::jsonb
)
ON CONFLICT (id) DO UPDATE SET
  name = EXCLUDED.name,
  description = EXCLUDED.description,
  risk = EXCLUDED.risk,
  requires_approval = EXCLUDED.requires_approval,
  environments = EXCLUDED.environments,
  asset_types = EXCLUDED.asset_types,
  inputs_schema = EXCLUDED.inputs_schema,
  openc2_template = EXCLUDED.openc2_template;
