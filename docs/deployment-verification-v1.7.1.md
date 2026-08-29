# Deployment Verification — v1.7.1 (H8 Evidence Tiers)

> **Commit:** `e0a744df5314cdc829869274f231d0e970b27d64` · **Timestamp (UTC):** `2026-08-29T10:32:49.903851+00:00` · **Command:** `pytest -q`
> **pytest:** `927 passed, 1 skipped, 0 failed, 74 warnings`

## 1. Evidence Tiers (H8)

| Tier | Count | Prerequisites | Evidence |
|------|-------|---------------|----------|
| unit | 927 passed | none (local) | `pytest -q` — includes fakeredis/SQLite/filesystem mocks for distributed logic, but NOT live multi-replica |
| integration | 0 | none (local) | counted within unit total; separate integration suite not split |
| distributed | 0 passed | Redis + kind + K8s + CNI enforcement | requires `kind` cluster + `redis-cli ping PONG` + `hubble --verdict DROPPED`/`flow log` capture |
| external | 0 passed | Outline/Notion/Mattermost/Slack/LLM gateway live | requires live credentials + network + `docs/deployment-verification-*.md` curl/kubectl/hubble captures |
| **total** | **927 passed, 1 skipped** |  |  |

> **Invariant:** Unit tests are never labeled as distributed/external. Distributed/external counts remain 0 until live verification is captured; current `927` is `unit` only.

## 2. Prerequisites Check

| Prerequisite | Available | Reason |
|--------------|-----------|--------|
| redis | ❌ | redis-cli not found and REDIS_URL not set or not reachable |
| kind | ❌ | not found in PATH |
| kubectl | ❌ | not found in PATH |
| helm | ❌ | not found in PATH |
| hubble | ❌ | not found in PATH |
| cni_enforcement | ❌ | requires kind+kubectl+live cluster with Cilium/Calico |
| outline | ❌ | env ['OUTLINE_API_TOKEN', 'OAOS_OUTLINE_TOKEN'] not set or not live-verified |
| notion | ❌ | env ['NOTION_API_TOKEN', 'OAOS_NOTION_TOKEN'] not set or not live-verified |
| mattermost | ❌ | env ['MATTERMOST_URL', 'OAOS_MATTERMOST_URL'] not set or not live-verified |
| slack | ❌ | env ['SLACK_BOT_TOKEN', 'OAOS_SLACK_TOKEN'] not set or not live-verified |
| llm_gateway | ❌ | env ['OPENAI_API_KEY', 'OAOS_LLM_GATEWAY_URL', 'ANTHROPIC_API_KEY'] not set or not live-verified |

**Unavailable prerequisites (11):** cni_enforcement, helm, hubble, kind, kubectl, llm_gateway, mattermost, notion, outline, redis, slack — distributed/external evidence cannot be claimed.

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
pytest -q  # expect 927 passed, 1 skipped (2026-08-29)
python scripts/verify-evidence-tiers.py  # regenerates this report + evidence-report-v1.7.1.json
python scripts/verify-evidence-tiers.py --check-only  # exits 1 if docs claim unsupported distributed/external
```

## 6. Raw pytest Tail (last 4000 chars)

```
ch.ao.quantization.quantize_fx.prepare_fx,torch.ao.quantization.quantize_fx.convert_fx, please migrate to use torchao pt2e quantization API instead (prepare_pt2e, convert_pt2e) 
  3. pt2e quantization has been migrated to torchao (https://github.com/pytorch/ao/tree/main/torchao/quantization/pt2e) 
  see https://github.com/pytorch/ao/issues/2259 for more details
    torch.quantization.quantize_dynamic(net, dtype=torch.qint8, inplace=True)

tests/test_personal_wiki_e2e.py::test_e2e_extractor_dispatch_txt_md_and_importer_bulk_copy
tests/test_personal_wiki_e2e.py::test_e2e_extractor_dispatch_txt_md_and_importer_bulk_copy
  /home/openitsvc/.hermes/hermes-agent/venv/lib/python3.11/site-packages/torch/ao/quantization/quantize.py:570: DeprecationWarning: torch.ao.quantization is deprecated and will be removed in 2.10. 
  For migrations of users: 
  1. Eager mode quantization (torch.ao.quantization.quantize, torch.ao.quantization.quantize_dynamic), please migrate to use torchao eager mode quantize_ API instead 
  2. FX graph mode quantization (torch.ao.quantization.quantize_fx.prepare_fx,torch.ao.quantization.quantize_fx.convert_fx, please migrate to use torchao pt2e quantization API instead (prepare_pt2e, convert_pt2e) 
  3. pt2e quantization has been migrated to torchao (https://github.com/pytorch/ao/tree/main/torchao/quantization/pt2e) 
  see https://github.com/pytorch/ao/issues/2259 for more details
    convert(model, mapping, inplace=True)

tests/test_personal_wiki_e2e.py::test_e2e_extractor_dispatch_txt_md_and_importer_bulk_copy
  /home/openitsvc/.hermes/hermes-agent/venv/lib/python3.11/site-packages/easyocr/recognition.py:177: DeprecationWarning: torch.ao.quantization is deprecated and will be removed in 2.10. 
  For migrations of users: 
  1. Eager mode quantization (torch.ao.quantization.quantize, torch.ao.quantization.quantize_dynamic), please migrate to use torchao eager mode quantize_ API instead 
  2. FX graph mode quantization (torch.ao.quantization.quantize_fx.prepare_fx,torch.ao.quantization.quantize_fx.convert_fx, please migrate to use torchao pt2e quantization API instead (prepare_pt2e, convert_pt2e) 
  3. pt2e quantization has been migrated to torchao (https://github.com/pytorch/ao/tree/main/torchao/quantization/pt2e) 
  see https://github.com/pytorch/ao/issues/2259 for more details
    torch.quantization.quantize_dynamic(model, dtype=torch.qint8, inplace=True)

tests/test_personal_wiki_e2e.py::test_e2e_extractor_dispatch_txt_md_and_importer_bulk_copy
  /home/openitsvc/.hermes/hermes-agent/venv/lib/python3.11/site-packages/torch/ao/nn/quantized/dynamic/modules/rnn.py:162: UserWarning: torch.quantize_per_tensor, torch.quantize_per_channel and other quantized tensor creation functions that produce tensors with dtype torch.quint8, torch.qint8, and torch.qint32 are deprecated and will be removed in a future PyTorch release. Please see https://github.com/pytorch/pytorch/issues/184982 for more information. (Triggered internally at /__w/pytorch/pytorch/aten/src/ATen/quantized/Quantizer.cpp:111.)
    w_ih = torch.quantize_per_tensor(

tests/test_workstream_c.py::test_credential_vault_isolation
  /home/openitsvc/open-agent-os/tests/test_workstream_c.py:116: DeprecationWarning: EncryptedPostgresVault using legacy encrypted_postgres backend (encrypted_token in DB). Set VAULT_BACKEND=hashicorp_vault or aws_secrets for externalized secrets.
    vault = EncryptedPostgresVault(encryption_key=b"test-key-32bytes-long-enough!!")

tests/test_workstream_c.py::test_vault_encryption_roundtrip
  /home/openitsvc/open-agent-os/tests/test_workstream_c.py:140: DeprecationWarning: EncryptedPostgresVault using legacy encrypted_postgres backend (encrypted_token in DB). Set VAULT_BACKEND=hashicorp_vault or aws_secrets for externalized secrets.
    vault = EncryptedPostgresVault(encryption_key=b"another-key-32bytes!!!!")

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
927 passed, 1 skipped, 74 warnings in 255.01s (0:04:15)

```
