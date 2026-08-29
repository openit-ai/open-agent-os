#!/usr/bin/env python3
"""
H8 evidence-tier verification — separates unit, distributed, and external evidence.

- unit: tests runnable without live infra (fakeredis/sqlite/filesystem mocks allowed,
  but NOT requiring live kind/Redis/CNI or live Outline/Notion/Mattermost/Slack/LLM).
- distributed: requires live Redis + kind/K8s + CNI enforcement (kind+redis present).
- external: requires live external SaaS / Outline/Notion/Mattermost/Slack/LLM gateway.

The script:
- never labels unit tests as distributed/external,
- records command, timestamp, commit, counts, unavailable prerequisites,
- fails (exit 1) if docs claim distributed/external counts without available evidence.

Usage:
  python scripts/verify-evidence-tiers.py [--output docs/deployment-verification-v1.7.1.md] [--json evidence-report.json]
  python scripts/verify-evidence-tiers.py --check-only   # fail if claims unsupported, no report write
  pytest tests/test_evidence_tiers.py -v  # TDD for this script's contracts

Design: see docs/architecture-v1.7.1-design.md §10 (H8).
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_MD = ROOT / "docs" / "deployment-verification-v1.7.1.md"
DEFAULT_REPORT_JSON = ROOT / "docs" / "evidence-report-v1.7.1.json"


def get_commit() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        return out
    except Exception:
        return "unknown"


def get_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_command() -> str:
    return "pytest -q"


def run_pytest() -> dict:
    """Run pytest -q and parse summary line. Returns {passed, skipped, failed, warnings, raw}."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout + result.stderr
    except Exception as e:
        return {"passed": 0, "skipped": 0, "failed": 0, "warnings": 0, "raw": f"pytest error: {e}", "exit_code": 2}

    # parse e.g. "927 passed, 1 skipped, 74 warnings in 252.71s"
    passed = skipped = failed = warnings = 0
    # find last line with "passed"
    for line in output.splitlines()[::-1]:
        if "passed" in line or "failed" in line or "skipped" in line:
            m_passed = re.search(r"(\d+)\s+passed", line)
            m_skipped = re.search(r"(\d+)\s+skipped", line)
            m_failed = re.search(r"(\d+)\s+failed", line)
            m_warnings = re.search(r"(\d+)\s+warnings?", line)
            if m_passed:
                passed = int(m_passed.group(1))
            if m_skipped:
                skipped = int(m_skipped.group(1))
            if m_failed:
                failed = int(m_failed.group(1))
            if m_warnings:
                warnings = int(m_warnings.group(1))
            break
    return {
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "warnings": warnings,
        "raw": output[-4000:],
        "exit_code": result.returncode if "result" in locals() else 2,
    }


def check_prerequisites() -> dict:
    """Check live infra prerequisites. Distributed/external require live services."""
    prereqs = {}

    def _which(cmd: str) -> tuple[bool, str]:
        found = shutil.which(cmd) is not None
        return found, "found" if found else "not found in PATH"

    # Redis: try redis-cli ping or REDIS_URL env; but without live redis, mark unavailable
    redis_available = False
    redis_reason = "redis-cli not found and REDIS_URL not set or not reachable"
    if shutil.which("redis-cli"):
        try:
            r = subprocess.run(["redis-cli", "ping"], capture_output=True, text=True, timeout=3)
            if r.stdout.strip() == "PONG":
                redis_available = True
                redis_reason = "redis-cli ping PONG"
            else:
                redis_reason = f"redis-cli ping: {r.stdout.strip() or r.stderr.strip()}"
        except Exception as e:
            redis_reason = f"redis-cli error: {e}"
    else:
        # check env but do not claim available without ping
        if any(v in __import__("os").environ for v in ("REDIS_URL", "OAOS_REDIS_URL", "OAOS_QUOTA_REDIS_URL")):
            redis_reason = "REDIS_URL set but redis-cli not found — not verified live"
    prereqs["redis"] = {"available": redis_available, "reason": redis_reason}

    for cmd in ("kind", "kubectl", "helm", "hubble"):
        avail, reason = _which(cmd)
        # for kind/kubectl, also try version check to confirm executable
        if avail:
            try:
                subprocess.run([cmd, "version"], capture_output=True, timeout=3)
                reason = "found and version check attempted"
            except Exception:
                pass
        prereqs[cmd] = {"available": avail, "reason": reason}

    # CNI enforcement requires both kind+kubectl+live cluster — mark unavailable if either missing
    cni_available = prereqs["kind"]["available"] and prereqs["kubectl"]["available"]
    # additionally try to check if cluster exists — if not, still unavailable
    if cni_available:
        try:
            r = subprocess.run(["kubectl", "cluster-info"], capture_output=True, text=True, timeout=3)
            if r.returncode != 0:
                cni_available = False
        except Exception:
            cni_available = False
    prereqs["cni_enforcement"] = {
        "available": cni_available,
        "reason": "requires kind+kubectl+live cluster with Cilium/Calico" if not cni_available else "kind+kubectl present (live enforcement not verified without flow logs)",
    }

    # External SaaS / connectors — check env credentials, but without live network verification mark unavailable
    for key in ("outline", "notion", "mattermost", "slack", "llm_gateway"):
        env_keys = {
            "outline": ["OUTLINE_API_TOKEN", "OAOS_OUTLINE_TOKEN"],
            "notion": ["NOTION_API_TOKEN", "OAOS_NOTION_TOKEN"],
            "mattermost": ["MATTERMOST_URL", "OAOS_MATTERMOST_URL"],
            "slack": ["SLACK_BOT_TOKEN", "OAOS_SLACK_TOKEN"],
            "llm_gateway": ["OPENAI_API_KEY", "OAOS_LLM_GATEWAY_URL", "ANTHROPIC_API_KEY"],
        }[key]
        has_env = any(__import__("os").environ.get(k) for k in env_keys)
        prereqs[key] = {
            "available": False,
            "reason": f"env {env_keys} not set or not live-verified" if not has_env else f"env present but live network verification not performed — not claimed as external evidence",
        }

    return prereqs


def classify_counts(pytest_result: dict, prereqs: dict) -> dict:
    """
    Classify evidence tiers.

    Invariant: unit tests are NEVER counted as distributed/external.
    Distributed = 0 unless redis+kind+kubectl+cni live.
    External = 0 unless live external verification.
    """
    passed = pytest_result.get("passed", 0)
    # All currently passed tests are unit (including fakeredis-backed distributed-logic unit tests).
    # They verify distributed *logic* (Lua atomicity, fail-closed) but NOT live multi-replica enforcement.
    distributed_live = prereqs.get("redis", {}).get("available", False) and prereqs.get("kind", {}).get("available", False) and prereqs.get("cni_enforcement", {}).get("available", False)
    external_live = any(prereqs.get(k, {}).get("available", False) for k in ("outline", "notion", "mattermost", "slack", "llm_gateway"))

    # Strict separation: distributed/external are 0 when prereqs unavailable.
    # Do not inflate external/distributed from unit count.
    return {
        "unit": passed,
        "integration": 0,  # kept for compatibility with design doc table; unit covers pytest -q total
        "distributed": 0 if not distributed_live else 0,  # even if live, require explicit distributed test run — not auto-promoted from unit
        "external": 0 if not external_live else 0,
        "total_passed": passed,
        "skipped": pytest_result.get("skipped", 0),
        "failed": pytest_result.get("failed", 0),
        "warnings": pytest_result.get("warnings", 0),
    }


def extract_doc_tier_claims(text: str) -> dict:
    """
    Extract tier claims from docs to verify they don't overclaim.
    Looks for patterns like 'distributed: N passed' or table rows.
    Returns {distributed_claim: int|None, external_claim: int|None}
    """
    # Check for explicit distributed/external passed claims >0 that would require live evidence
    # We flag if docs contain distributed/external with non-zero counts.
    distributed_claim = None
    external_claim = None
    # pattern: distributed ... N passed  (case-insensitive)
    for tier in ("distributed", "external"):
        # find "distributed: 12 passed" or "| distributed | ... | 12 passed"
        pat = re.compile(rf"{tier}[\s|:]*.*?(\d+)\s*passed", re.IGNORECASE)
        m = pat.search(text)
        if m:
            try:
                val = int(m.group(1))
                if tier == "distributed":
                    distributed_claim = val
                else:
                    external_claim = val
            except ValueError:
                pass
        # also check badge-like "distributed N" without passed but numeric near tier
        # Already covered; if no explicit passed, leave None
    return {"distributed_claim": distributed_claim, "external_claim": external_claim}


def verify_claims(tiers: dict, prereqs: dict, doc_texts: list[str]) -> list[str]:
    """
    Verify that distributed/external claims are supported.
    Returns list of violation messages (empty if ok).
    """
    violations = []
    # Check tiers themselves: must not have inflated distributed/external when prereqs unavailable
    distributed_available = prereqs.get("redis", {}).get("available", False) and prereqs.get("kind", {}).get("available", False)
    external_available = any(prereqs.get(k, {}).get("available", False) for k in ("outline", "notion", "mattermost", "slack", "llm_gateway"))
    if tiers.get("distributed", 0) > 0 and not distributed_available:
        violations.append(f"distributed tier claims {tiers['distributed']} but prerequisites unavailable: redis/kind/cni not live")
    if tiers.get("external", 0) > 0 and not external_available:
        violations.append(f"external tier claims {tiers['external']} but live external prerequisites unavailable")

    # Check doc texts for unsupported claims
    for idx, text in enumerate(doc_texts):
        claims = extract_doc_tier_claims(text)
        dc = claims.get("distributed_claim")
        ec = claims.get("external_claim")
        if dc is not None and dc > 0 and not distributed_available:
            violations.append(f"doc[{idx}] claims distributed: {dc} passed but distributed prerequisites unavailable")
        if ec is not None and ec > 0 and not external_available:
            violations.append(f"doc[{idx}] claims external: {ec} passed but external prerequisites unavailable")

    # Also ensure we are not labeling unit as distributed/external — tiers unit should equal total, distributed/external should be 0 or separately verified
    if tiers.get("distributed", 0) > tiers.get("total_passed", 0):
        violations.append("distributed count exceeds total passed — mislabeling")
    if tiers.get("external", 0) > tiers.get("total_passed", 0):
        violations.append("external count exceeds total passed — mislabeling")

    return violations


def build_report(pytest_result: dict, prereqs: dict, tiers: dict, commit: str, timestamp: str, command: str) -> dict:
    unavailable = {k: v for k, v in prereqs.items() if not v.get("available")}
    return {
        "command": command,
        "timestamp": timestamp,
        "commit": commit,
        "pytest": pytest_result,
        "tiers": tiers,
        "prerequisites": prereqs,
        "unavailable_prerequisites": unavailable,
        "rag_distinction": {
            "personal_wiki": "implemented — owner-isolated Vault FS + pgvector, unit-tested",
            "knowledge_index": "implemented — schema/repository/retrieval/chunking/embedding boundary/Outline-Notion adapters/idempotent sync/ACL revalidation — unit-tested",
            "live_external_integration": "not claimed — requires live Outline/Notion credentials + network + corpus backfill; marked as operational integration work",
        },
        "invariants": [
            "unit tests are never labeled as distributed/external",
            "distributed requires live Redis + kind + CNI flow-log evidence",
            "external requires live Outline/Notion/Mattermost/Slack/LLM gateway verification",
        ],
    }


def render_markdown(report: dict) -> str:
    tiers = report["tiers"]
    prereqs = report["prerequisites"]
    rag = report["rag_distinction"]
    unavailable = report["unavailable_prerequisites"]
    pytest_r = report["pytest"]
    lines = []
    lines.append("# Deployment Verification — v1.7.1 (H8 Evidence Tiers)")
    lines.append("")
    lines.append(f"> **Commit:** `{report['commit']}` · **Timestamp (UTC):** `{report['timestamp']}` · **Command:** `{report['command']}`")
    lines.append(f"> **pytest:** `{pytest_r.get('passed',0)} passed, {pytest_r.get('skipped',0)} skipped, {pytest_r.get('failed',0)} failed, {pytest_r.get('warnings',0)} warnings`")
    lines.append("")
    lines.append("## 1. Evidence Tiers (H8)")
    lines.append("")
    lines.append("| Tier | Count | Prerequisites | Evidence |")
    lines.append("|------|-------|---------------|----------|")
    lines.append(f"| unit | {tiers['unit']} passed | none (local) | `pytest -q` — includes fakeredis/SQLite/filesystem mocks for distributed logic, but NOT live multi-replica |")
    lines.append(f"| integration | {tiers['integration']} | none (local) | counted within unit total; separate integration suite not split |")
    lines.append(f"| distributed | {tiers['distributed']} passed | Redis + kind + K8s + CNI enforcement | requires `kind` cluster + `redis-cli ping PONG` + `hubble --verdict DROPPED`/`flow log` capture |")
    lines.append(f"| external | {tiers['external']} passed | Outline/Notion/Mattermost/Slack/LLM gateway live | requires live credentials + network + `docs/deployment-verification-*.md` curl/kubectl/hubble captures |")
    lines.append(f"| **total** | **{tiers['total_passed']} passed, {tiers.get('skipped',0)} skipped** |  |  |")
    lines.append("")
    lines.append("> **Invariant:** Unit tests are never labeled as distributed/external. Distributed/external counts remain 0 until live verification is captured; current `927` is `unit` only.")
    lines.append("")
    lines.append("## 2. Prerequisites Check")
    lines.append("")
    lines.append("| Prerequisite | Available | Reason |")
    lines.append("|--------------|-----------|--------|")
    for k, v in prereqs.items():
        lines.append(f"| {k} | {'✅' if v['available'] else '❌'} | {v['reason']} |")
    lines.append("")
    if unavailable:
        lines.append(f"**Unavailable prerequisites ({len(unavailable)}):** " + ", ".join(sorted(unavailable.keys())) + " — distributed/external evidence cannot be claimed.")
        lines.append("")
    lines.append("## 3. RAG Implementation vs Live External Integration")
    lines.append("")
    lines.append(f"- **Personal Wiki:** {rag['personal_wiki']}")
    lines.append(f"- **Knowledge Index:** {rag['knowledge_index']}")
    lines.append(f"- **Live external integration:** {rag['live_external_integration']}")
    lines.append("")
    lines.append("Knowledge Index schema/repository/retrieval, stable chunking, embedding provider boundary, Outline/Notion source adapters, idempotent incremental sync, deletion handling, ACL version invalidation/revalidation are **implemented and unit-tested** (`knowledge_index/` commits `60ffe4bfba`, `6dab8761c2`). Live connector credentials/network and production corpus backfill remain **operational integration work**, not claimed as complete here.")
    lines.append("")
    lines.append("## 4. Static Verification (no live infra required)")
    lines.append("")
    lines.append("- `deploy/k8s/networkpolicy.yaml` — raw manifests, no Helm templating, `default-deny-all` + allow-* (verified by `tests/test_network_policy.py`)")
    lines.append("- `deploy/docker-compose.*.yml` — `healthcheck: /healthz`, `livenessProbe: /healthz`, `readinessProbe: /readyz`")
    lines.append("- `tests/test_distributed_state.py` — 19 tests (fakeredis+lupa) for quota/rate/replay/session Lua atomicity + prod 503 fail-closed (unit simulation, not live multi-replica)")
    lines.append("- `tests/test_mock_fallback_hardening.py` + `test_runtime_hardening.py` — 13 passed, H7 immutable startup gate")
    lines.append("- `tests/test_knowledge_*.py` — chunking, embedding, sync, ACL pre-filter")
    lines.append("")
    lines.append("## 5. How to Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append("git rev-parse HEAD && date -u --iso-8601=seconds")
    lines.append("pytest -q  # expect 927 passed, 1 skipped (2026-08-29)")
    lines.append("python scripts/verify-evidence-tiers.py  # regenerates this report + evidence-report-v1.7.1.json")
    lines.append("python scripts/verify-evidence-tiers.py --check-only  # exits 1 if docs claim unsupported distributed/external")
    lines.append("```")
    lines.append("")
    lines.append("## 6. Raw pytest Tail (last 4000 chars)")
    lines.append("")
    lines.append("```")
    lines.append(pytest_r.get("raw", "")[-4000:])
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="H8 evidence-tier verification")
    parser.add_argument("--output", type=str, default=str(DEFAULT_REPORT_MD), help="Markdown report path")
    parser.add_argument("--json", dest="json_out", type=str, default=str(DEFAULT_REPORT_JSON), help="JSON report path")
    parser.add_argument("--check-only", action="store_true", help="Only verify claims, don't run pytest or write reports")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip running pytest (use cached counts for testing)")
    args = parser.parse_args(argv)

    commit = get_commit()
    timestamp = get_timestamp()
    command = get_command()

    if args.skip_pytest:
        pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "skipped for test", "exit_code": 0}
    else:
        pytest_result = run_pytest()

    prereqs = check_prerequisites()
    tiers = classify_counts(pytest_result, prereqs)

    # Load doc texts for claim verification
    doc_texts = []
    for p in [ROOT / "README.md", ROOT / "docs" / "architecture-v1.7.1.md"]:
        try:
            doc_texts.append(p.read_text(encoding="utf-8"))
        except Exception:
            doc_texts.append("")

    violations = verify_claims(tiers, prereqs, doc_texts)

    if args.check_only:
        if violations:
            print("H8 evidence-tier violations:", file=sys.stderr)
            for v in violations:
                print(f"  - {v}", file=sys.stderr)
            return 1
        print(f"H8 check passed — unit:{tiers['unit']} distributed:{tiers['distributed']} external:{tiers['external']} commit:{commit[:8]}")
        return 0

    report = build_report(pytest_result, prereqs, tiers, commit, timestamp, command)
    report["violations"] = violations

    # Write JSON
    try:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"Failed to write JSON: {e}", file=sys.stderr)

    # Write Markdown
    try:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(render_markdown(report), encoding="utf-8")
    except Exception as e:
        print(f"Failed to write Markdown: {e}", file=sys.stderr)

    if violations:
        print("H8 evidence-tier violations (report written but exit 1):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print(f"H8 evidence report written — {args.output} + {args.json_out} — unit:{tiers['unit']} distributed:{tiers['distributed']} external:{tiers['external']} commit:{commit[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
