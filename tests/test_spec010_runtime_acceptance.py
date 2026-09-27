from __future__ import annotations

from agent_runtime import CandidateIdentity, FixedSnapshot, analysis_profile_digest
import hashlib
from agent_runtime.validation import (
    ValidationArtifact,
    ValidationBuildOutcome,
    ValidationExecutionOutcome,
    ValidationRecord,
    ValidationResource,
)
from tools.spec010_cpp_peglib_rule import cpp_peglib_profile, cpp_peglib_rule
from tools.verify_spec010_runtime import identity_for, polarity, snapshot_for


def artifact(value: str) -> ValidationArtifact:
    body = value.encode()
    return ValidationArtifact(hashlib.sha256(body).hexdigest(), len(body))


def record_for(*, target_status="target_observed"):
    policy = artifact("policy")
    rule = cpp_peglib_rule()
    profile = cpp_peglib_profile(policy.sha256)
    snapshot = FixedSnapshot(
        "yhirose/cpp-peglib", "base-ordinary", artifact("source").sha256,
        analysis_profile_digest(rule, profile=profile), policy.sha256,
    )
    record = ValidationRecord(
        repository_id=snapshot.repository_id,
        runtime_snapshot_digest=snapshot.snapshot_digest,
        scope_id=snapshot.scope_id,
        source=ValidationArtifact(snapshot.source_digest, 6),
        tool_policy=policy,
        toolchain=artifact("toolchain"),
        dependency=artifact("dependency"),
        recipe=artifact("recipe"),
        test_input=artifact("ordinary"),
        build=ValidationBuildOutcome("succeeded", artifact("binary")),
        execution=ValidationExecutionOutcome(
            "normal_exit", target_status, 0,
            observation_stage=("optimized_completed" if target_status == "target_observed" else None),
            observation_digest=(artifact("observation").sha256 if target_status == "target_observed" else None),
        ),
        resources=(ValidationResource("wall_time_ms", 1000, 20),),
        limitations=("one input",),
    )
    return record, snapshot


def row(classification="optimized_completed"):
    return {
        "revision_id": "base",
        "commit": "14305f9f53cde207568f21675a1b9294a3ab28b4",
        "input": "ordinary",
        "classification": classification,
    }


def test_application_snapshot_matches_record_profile_binding():
    record, expected = record_for()
    _, _, actual = snapshot_for(record)
    assert actual.snapshot_digest == expected.snapshot_digest


def test_candidate_identity_binds_every_exclusion_dimension():
    record, snapshot = record_for()
    candidate = identity_for(record, snapshot, row())
    required = {
        "revision_id", "commit", "source_digest", "input_digest", "record_id",
        "toolchain_digest", "recipe_digest", "observer_contract",
    }
    assert set(candidate.identity) == required
    for field in required:
        changed = dict(candidate.identity)
        changed[field] = str(changed[field]) + "-different"
        other = CandidateIdentity(
            candidate.rule_id, candidate.rule_version, candidate.snapshot_digest,
            changed, candidate.scope + ":" + field,
        )
        assert other.candidate_digest != candidate.candidate_digest


def test_observation_polarity_is_conservative():
    assert polarity(row("ast_optimizer_invalid_access")) == "positive"
    assert polarity(row("optimized_completed")) == "negative"
    assert polarity(row("no_matching_observation")) == "unknown"
    assert polarity(row("unexpected_crash")) == "unknown"
