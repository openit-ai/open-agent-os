"""H8 evidence-tier verification TDD — ensures script separates unit/distributed/external correctly."""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_evidence_tiers as vet


def test_classify_does_not_label_unit_as_distributed_or_external():
    prereqs = vet.check_prerequisites()
    # In this env, distributed/external prereqs are unavailable — tiers must be 0
    # Even with 927 passed, distributed/external must not be inflated
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    tiers = vet.classify_counts(pytest_result, prereqs)
    assert tiers["unit"] == 927
    assert tiers["distributed"] == 0, "unit tests must not be labeled as distributed"
    assert tiers["external"] == 0, "unit tests must not be labeled as external"
    assert tiers["total_passed"] == 927


def test_report_records_required_fields():
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "927 passed", "exit_code": 0}
    prereqs = vet.check_prerequisites()
    tiers = vet.classify_counts(pytest_result, prereqs)
    report = vet.build_report(pytest_result, prereqs, tiers, commit="abc123", timestamp="2026-08-29T00:00:00+00:00", command="pytest -q")
    assert report["command"] == "pytest -q"
    assert report["timestamp"] == "2026-08-29T00:00:00+00:00"
    assert report["commit"] == "abc123"
    assert "tiers" in report
    assert "prerequisites" in report
    assert "unavailable_prerequisites" in report
    assert len(report["unavailable_prerequisites"]) > 0, "should record unavailable prerequisites"
    assert "rag_distinction" in report
    # must contain unavailable keys
    assert "redis" in report["unavailable_prerequisites"] or "kind" in report["unavailable_prerequisites"]


def test_fails_when_claims_unsupported():
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    prereqs = vet.check_prerequisites()
    tiers = vet.classify_counts(pytest_result, prereqs)
    # doc claims distributed 12 passed but prereqs unavailable -> violation
    doc_text = "| distributed | kind + redis `pytest -k distributed` | 12 passed | kind log |"
    violations = vet.verify_claims(tiers, prereqs, [doc_text])
    assert any("distributed" in v for v in violations), f"should fail when distributed claim unsupported, got {violations}"


def test_distributed_tier_zero_when_prereqs_unavailable():
    prereqs = vet.check_prerequisites()
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    tiers = vet.classify_counts(pytest_result, prereqs)
    # distributed prerequisites are unavailable in CI without kind/redis
    assert prereqs["redis"]["available"] is False or prereqs["kind"]["available"] is False
    assert tiers["distributed"] == 0


def test_external_tier_zero_when_prereqs_unavailable():
    prereqs = vet.check_prerequisites()
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    tiers = vet.classify_counts(pytest_result, prereqs)
    for k in ("outline", "notion", "mattermost", "slack", "llm_gateway"):
        assert prereqs[k]["available"] is False
    assert tiers["external"] == 0


def test_rag_distinction_preserved():
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    prereqs = vet.check_prerequisites()
    tiers = vet.classify_counts(pytest_result, prereqs)
    report = vet.build_report(pytest_result, prereqs, tiers, commit="abc", timestamp="now", command="pytest -q")
    rag = report["rag_distinction"]
    assert "implemented" in rag["knowledge_index"]
    assert "unit-tested" in rag["knowledge_index"]
    assert "not claimed" in rag["live_external_integration"] or "operational integration" in rag["live_external_integration"]
    md = vet.render_markdown(report)
    assert "Knowledge Index" in md
    assert "Live external integration" in md or "live_external" in md.lower() or "operational integration" in md


def test_no_mislabeling_even_if_doc_claims_zero():
    # Ensure script itself doesn't mislabel: distributed should never exceed total
    prereqs = vet.check_prerequisites()
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    tiers = vet.classify_counts(pytest_result, prereqs)
    assert tiers["distributed"] <= tiers["total_passed"]
    assert tiers["external"] <= tiers["total_passed"]


def test_verify_claims_passes_when_no_overclaim():
    prereqs = vet.check_prerequisites()
    pytest_result = {"passed": 927, "skipped": 1, "failed": 0, "warnings": 74, "raw": "", "exit_code": 0}
    tiers = vet.classify_counts(pytest_result, prereqs)
    doc_text = "unit: 927 passed, distributed: 0 passed, external: 0 passed"
    violations = vet.verify_claims(tiers, prereqs, [doc_text])
    assert violations == [], f"should pass when no overclaim, got {violations}"
