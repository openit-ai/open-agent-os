# Deployment Verification — v0.1.3 (H8 Evidence Tiers)

> **Product:** `v0.1.3` · **Architecture:** `v1.7.2` · **Code-test commit:** `6b5e71203b03f6b96acf58528b1dd5a0964d4021`
> **Runtime-applied commit:** `6b5e71203b03f6b96acf58528b1dd5a0964d4021` · **Verification timestamp (UTC):** `2026-08-31T12:04:54Z` · **Command:** `python -m pytest -q`
> **pytest:** `1255 passed, 5 skipped, 0 failed, 85 warnings in 367.89s` · **Branch:** `release/v0.1.3-remediation`
> **Historical v1.7.1 evidence preserved:** `docs/deployment-verification-v1.7.1.md` / `docs/evidence-report-v1.7.1.json` not overwritten — this is the current v0.1.3 candidate.

## 1. Evidence Tiers (H8)

| Tier | Count | Prerequisites | Evidence |
|------|-------|---------------|----------|
| unit | 1255 passed | none (local) | `python -m pytest -q` — includes fakeredis/SQLite/filesystem mocks for distributed logic, but NOT live multi-replica |
| integration | 0 | none (local) | counted within unit total; separate integration suite not split |
| distributed | 0 passed | Redis + kind + K8s + CNI enforcement | requires `kind` cluster + `redis-cli ping PONG` + `hubble --verdict DROPPED`/`flow log` capture |
| external | 0 passed | Outline/Notion/Mattermost/Slack/LLM gateway live | requires live credentials + network + `docs/deployment-verification-*.md` curl/kubectl/hubble captures |
| **total** | **1255 passed, 5 skipped, 0 failed, 85 warnings** |  |  |

> **Invariant:** Unit tests are never labeled as distributed/external. Distributed/external counts remain 0 until live verification is captured; current `1255` is `unit` only. `integration` is 0 as a separate suite was not split — integrated coverage is within the unit total. 367.89s wall time for the authoritative single run.

## 2. Prerequisites Check

| Prerequisite | Available | Reason |
|--------------|-----------|--------|
| redis | ❌ | redis-cli not found and REDIS_URL not set or not reachable |
| kind | ✅ | found and version check attempted |
| kubectl | ✅ | found and version check attempted |
| helm | ✅ | found and version check attempted |
| hubble | ❌ | not found in PATH |
| cni_enforcement | ❌ | requires kind+kubectl+live cluster with Cilium/Calico |
| outline | ❌ | env ['OUTLINE_API_TOKEN', 'OAOS_OUTLINE_TOKEN'] not set or not live-verified |
| notion | ❌ | env ['NOTION_API_TOKEN', 'OAOS_NOTION_TOKEN'] not set or not live-verified |
| mattermost | ❌ | env ['MATTERMOST_URL', 'OAOS_MATTERMOST_URL'] not set or not live-verified |
| slack | ❌ | env ['SLACK_BOT_TOKEN', 'OAOS_SLACK_TOKEN'] not set or not live-verified |
| llm_gateway | ❌ | env ['OPENAI_API_KEY', 'OAOS_LLM_GATEWAY_URL', 'ANTHROPIC_API_KEY'] not set or not live-verified |

**Unavailable prerequisites (8):** cni_enforcement, hubble, llm_gateway, mattermost, notion, outline, redis, slack — distributed/external evidence cannot be claimed.

Note: host env has `OUTLINE_API_KEY`/`OUTLINE_API_URL` and `MATTERMOST_BOT_TOKEN` but the verifier checks `OUTLINE_API_TOKEN`/`OAOS_OUTLINE_TOKEN` and `MATTERMOST_URL`/`OAOS_MATTERMOST_URL` respectively, and in any case `available` remains `false` without live network verification — not claimed as external evidence (same invariant as v1.7.1).

## 3. RAG Implementation vs Live External Integration

- **Personal Wiki:** implemented — owner-isolated Vault FS + pgvector, unit-tested
- **Knowledge Index:** implemented — schema/repository/retrieval/chunking/embedding boundary/Outline-Notion adapters/idempotent sync/ACL revalidation — unit-tested
- **Live external integration:** not claimed — requires live Outline/Notion credentials + network + corpus backfill; marked as operational integration work

Knowledge Index schema/repository/retrieval, stable chunking, embedding provider boundary, Outline/Notion source adapters, idempotent incremental sync, deletion handling, ACL version invalidation/revalidation are **implemented and unit-tested** (`knowledge_index/` commits `60ffe4bfba`, `6dab8761c2`). Live connector credentials/network and production corpus backfill remain **operational integration work**, not claimed as complete here.

## 4. Static Verification (no live infra required)

- `deploy/k8s/networkpolicy.yaml` — raw manifests, no Helm templating, `default-deny-all` + allow-* (verified by `tests/test_network_policy.py`)
- `deploy/docker-compose.*.yml` — `healthcheck: /healthz`, `livenessProbe: /healthz`, `readinessProbe: /readyz`
- `tests/test_distributed_state.py` — 19 tests (fakeredis+lupa) for quota/rate/replay/session Lua atomicity + prod 503 fail-closed (unit simulation, not live multi-replica)
- `tests/test_mock_fallback_hardening.py` + `test_runtime_hardening.py` — 13 passed, H7 immutable startup gate
- `tests/test_knowledge_*.py` — chunking, embedding, sync, ACL pre-filter

## 5. How to Reproduce

```bash
git rev-parse HEAD && date -u --iso-8601=seconds
# Expected at this commit (authoritative single run):
# 6b5e71203b03f6b96acf58528b1dd5a0964d4021  1255 passed, 5 skipped, 0 failed, 85 warnings in 367.89s
python -m pytest -q  # full suite — do NOT treat historical 1031/927 counts as current
python scripts/verify-evidence-tiers.py --output docs/deployment-verification-v0.1.3.md --json docs/evidence-report-v0.1.3.json
python scripts/verify-evidence-tiers.py --check-only  # exits 1 if docs claim unsupported distributed/external
```

### 5.1 Safe generation method used here

The evidence-tier script accepts explicit `--output` / `--json` paths. This report records the fresh `python -m pytest -q` result at commit `6b5e71203b`; historical v1.7.1 files were preserved. Distributed and external tiers remain zero because live multi-replica, CNI/Hubble, connector, and user-path evidence were not executed.

## 6. Raw pytest Summary (authoritative single run)

```
1255 passed, 5 skipped, 85 warnings in 367.89s (0:06:07)
Commit: 6b5e71203b03f6b96acf58528b1dd5a0964d4021
Branch: release/v0.1.3-remediation
Command: python -m pytest -q
Exit code: 0

Evidence generation timestamp (UTC): 2026-08-31T12:09:29Z
Generation method: fresh full-suite execution followed by runtime read-back.
Runtime read-back: oaos-memory PID 1085944; oaos-admin-api PID 1085946; Memory /health 200; /readyz 200 with database/redis ok.
Prerequisites at generation: Outline read-only probe OK (1 document, has_more=true); Notion adapter missing (fail-closed); distributed and external user-path evidence remain unclaimed.
```

Historical `v1.7.1` raw (commit `a36d236761e68c4e3e5c4ef656268048400fc336`, 1031 passed / 1 skipped / 2 failed, 84 warnings in 307.89s) remains in `docs/deployment-verification-v1.7.1.md` and is not treated as current.

## 7. Limitations

- Distributed evidence (live Redis + 2-replica Control Plane + quota/rate/replay/session concurrency + Redis failure 503 + k6 + CNI/Hubble `DROPPED` flow) **not claimed** — `distributed: 0`.
- External evidence (live Outline/Notion/Mattermost/Slack/LLM gateway round-trip with post/thread ID, trace ID, audit event ID, source reference read-back) **not claimed** — `external: 0`.
- Knowledge Index live health/backfill/ACL/deletion, Adaptive Profile worker/cache/Skill/Hermes runtime E2E, and Mattermost→OAOS→runtime→thread real round-trip remain operational integration work per `docs/release-v0.1.3-remediation-guide.md` P1; not asserted as complete here.
- No external credentials were written; source commit and service restart were completed separately. No tag or GitHub Release was created.

## 8. Verification

```bash
# H8 invariant check (no distributed/external overclaim):
python scripts/verify-evidence-tiers.py --check-only
# H8 check: distributed/external remain 0; current commit is 6b5e7120
# Validate JSON invariants:
python -c "import json; d=json.load(open('docs/evidence-report-v0.1.3.json')); assert d['tiers']['distributed']==0 and d['tiers']['external']==0; assert d['pytest']['passed']==1255 and d['pytest']['failed']==0; assert d['commit'].startswith('6b5e7120'); print('JSON ok', d['commit'][:8], d['tiers'])"
# Historical files untouched:
ls -l docs/deployment-verification-v1.7.1.md docs/evidence-report-v1.7.1.json docs/deployment-verification-v0.1.3.md docs/evidence-report-v0.1.3.json
```
