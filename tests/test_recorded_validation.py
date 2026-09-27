"""Contract tests for sealed validation records and their replay adapter."""

from __future__ import annotations

import copy

import pytest

from agent_runtime.adapters.validation import RecordedValidationProgramQuery
from agent_runtime.codec import bytes_digest, digest
from agent_runtime.domain import FixedSnapshot, QueryRequest
from agent_runtime.errors import EvidenceIntegrityError, InvalidInput, PolicyDenied, StaleSnapshot
from agent_runtime.validation import (
    ValidationArtifact,
    ValidationBuildOutcome,
    ValidationExecutionOutcome,
    ValidationRecord,
    ValidationResource,
    load_validation_record,
    validation_record_bytes,
    validation_record_to_dict,
)


def artifact(name: str) -> ValidationArtifact:
    data = name.encode("utf-8")
    return ValidationArtifact(bytes_digest(data), len(data))


def make_record(*, execution: ValidationExecutionOutcome | None = None) -> ValidationRecord:
    source = artifact("source")
    policy = artifact("policy")
    snapshot = FixedSnapshot("repo", "scope", source.sha256, digest("profile"), policy.sha256)
    return ValidationRecord(
        repository_id="repo",
        runtime_snapshot_digest=snapshot.snapshot_digest,
        scope_id="scope",
        source=source,
        tool_policy=policy,
        toolchain=artifact("toolchain"),
        dependency=artifact("dependency"),
        recipe=artifact("recipe"),
        test_input=artifact("input"),
        build=ValidationBuildOutcome("succeeded", artifact("binary"), artifact("build stdout"), artifact("build stderr")),
        execution=execution or ValidationExecutionOutcome(
            "normal_exit", "target_observed", 0, stdout=artifact("run stdout"), stderr=artifact("run stderr"), observation_stage="asan", observation_digest=artifact("observation").sha256
        ),
        resources=(ValidationResource("wall_time_ms", 1000, 20),),
        omissions=("only one input",),
        limitations=("fixture only",),
    )


def snapshot_for(record: ValidationRecord) -> FixedSnapshot:
    return FixedSnapshot(
        record.repository_id,
        record.scope_id,
        record.source.sha256,
        digest("profile"),
        record.tool_policy.sha256,
    )


def request(snapshot: FixedSnapshot, record_id: str, **changes) -> QueryRequest:
    values = dict(
        query_id="query",
        task_id="task",
        check_id="validation",
        operation="read_recorded_validation",
        snapshot_digest=snapshot.snapshot_digest,
        backend_id="recorded-validation",
        backend_version="1",
        tool_policy_digest=snapshot.tool_policy_digest,
        scope={"scope_id": snapshot.scope_id, "selectors": {"record_id": record_id}},
        args={"record_id": record_id},
        idempotency_key="key",
        timeout_ms=1000,
    )
    values.update(changes)
    return QueryRequest(**values)


def test_record_id_is_canonical_and_parser_rejects_tampering_and_execution_fields():
    record = make_record()
    encoded = validation_record_to_dict(record)
    assert load_validation_record(encoded) == record
    tampered = copy.deepcopy(encoded)
    tampered["recipe"]["sha256"] = artifact("changed").sha256
    with pytest.raises(EvidenceIntegrityError):
        load_validation_record(tampered)
    for field, value in (("command", ["cc", "x.cpp"]), ("env", {"PATH": "/tmp"}), ("unknown", True)):
        polluted = copy.deepcopy(encoded)
        polluted[field] = value
        with pytest.raises(InvalidInput):
            load_validation_record(polluted)


def test_every_output_and_resource_field_changes_record_identity():
    baseline = validation_record_to_dict(make_record())
    mutations = (
        ("toolchain", "sha256"),
        ("dependency", "sha256"),
        ("recipe", "sha256"),
        ("test_input", "sha256"),
        ("build", "stdout", "sha256"),
        ("build", "stderr", "sha256"),
        ("execution", "stdout", "sha256"),
        ("execution", "stderr", "sha256"),
        ("resources", 0, "observed"),
    )
    for path in mutations:
        changed = copy.deepcopy(baseline)
        changed.pop("record_id")
        target = changed
        for key in path[:-1]:
            target = target[key]
        key = path[-1]
        if key == "sha256":
            target[key] = artifact("mutated").sha256
        else:
            target[key] += 1
        rebuilt = ValidationRecord(
            repository_id=changed["repository_id"],
            runtime_snapshot_digest=changed["runtime_snapshot_digest"],
            scope_id=changed["scope_id"],
            source=ValidationArtifact(**changed["source"]),
            tool_policy=ValidationArtifact(**changed["tool_policy"]),
            toolchain=ValidationArtifact(**changed["toolchain"]),
            dependency=ValidationArtifact(**changed["dependency"]),
            recipe=ValidationArtifact(**changed["recipe"]),
            test_input=ValidationArtifact(**changed["test_input"]),
            build=ValidationBuildOutcome(
                changed["build"]["status"],
                ValidationArtifact(**changed["build"]["binary"]),
                ValidationArtifact(**changed["build"]["stdout"]),
                ValidationArtifact(**changed["build"]["stderr"]),
            ),
            execution=ValidationExecutionOutcome(
                status=changed["execution"]["status"],
                target_status=changed["execution"]["target_status"],
                exit_code=changed["execution"]["exit_code"],
                signal=changed["execution"]["signal"],
                stdout=ValidationArtifact(**changed["execution"]["stdout"]),
                stderr=ValidationArtifact(**changed["execution"]["stderr"]),
                observation_stage=changed["execution"]["observation_stage"],
                observation_digest=changed["execution"]["observation_digest"],
            ),
            resources=tuple(ValidationResource(**item) for item in changed["resources"]),
            omissions=tuple(changed["omissions"]),
            limitations=tuple(changed["limitations"]),
        )
        assert rebuilt.record_id != baseline["record_id"]


def test_nested_executable_fields_are_rejected():
    encoded = validation_record_to_dict(make_record())
    for container in ("source", "build", "execution"):
        polluted = copy.deepcopy(encoded)
        polluted[container]["argv"] = ["forbidden"]
        with pytest.raises(InvalidInput):
            load_validation_record(polluted)


def test_lifecycle_and_digest_validation_are_strict():
    with pytest.raises(InvalidInput):
        ValidationArtifact("A" * 64, 0)
    with pytest.raises(InvalidInput):
        ValidationResource("cpu", 0, 0)
    with pytest.raises(InvalidInput):
        ValidationBuildOutcome("failed", artifact("binary"))
    with pytest.raises(InvalidInput):
        ValidationExecutionOutcome("normal_exit", "not_run", 1)
    with pytest.raises(InvalidInput):
        ValidationExecutionOutcome("timeout", "target_observed")
    failed = make_record()
    with pytest.raises(InvalidInput):
        ValidationRecord(
            **{**failed.__dict__, "build": ValidationBuildOutcome("failed"), "execution": ValidationExecutionOutcome("normal_exit", "not_run", 0)}
        )


def test_recorded_validation_adapter_only_serves_registered_bound_records():
    record = make_record()
    snapshot = snapshot_for(record)
    backend = RecordedValidationProgramQuery(snapshot, {record.record_id: record})
    result = backend.query(request(snapshot, record.record_id))
    assert result.status == "complete"
    assert result.raw == validation_record_bytes(record)
    assert result.coverage.basis_refs == (bytes_digest(result.raw),)
    with pytest.raises(PolicyDenied):
        backend.query(request(snapshot, "0" * 64))
    with pytest.raises(InvalidInput):
        backend.query(request(snapshot, record.record_id, args={"record_id": record.record_id, "record": {}}))
    with pytest.raises(StaleSnapshot):
        backend.query(request(snapshot, record.record_id, snapshot_digest=digest("other")))


@pytest.mark.parametrize(
    ("execution", "status"),
    [
        (ValidationExecutionOutcome("executor_failed", "not_run"), "failed"),
        (ValidationExecutionOutcome("timeout", "not_run"), "timeout"),
        (ValidationExecutionOutcome("normal_exit", "no_trigger", 0), "partial"),
    ],
)
def test_incomplete_validation_lifecycles_never_claim_complete(execution, status):
    record = make_record(execution=execution)
    snapshot = snapshot_for(record)
    result = RecordedValidationProgramQuery(snapshot, {record.record_id: record}).query(request(snapshot, record.record_id))
    assert result.status == status
    assert result.status != "complete"


def test_recorded_target_fact_can_drive_scoped_exclusion_across_reopen(tmp_path):
    from agent_runtime import (
        CandidateIdentity,
        CheckItem,
        CppUnreachableEvaluator,
        DefectRuntime,
        EvidenceReviewExecutor,
        FactAssessment,
        RoleCoordinator,
        SQLiteStore,
        analysis_profile_digest,
        cpp_unreachable_profile,
        cpp_unreachable_rule,
    )
    from agent_runtime.adapters import CompositeProgramQuery
    from agent_runtime.adapters.source import FrozenSourceProgramQuery

    sources = {"a.cpp": "int f() {\n  return 0;\n  use();\n}\n"}
    policy_digest = digest({"operations": ["read_source", "read_recorded_validation"]})
    rule = cpp_unreachable_rule()
    profile = cpp_unreachable_profile(
        policy_digest,
        allowed_operations=("read_source", "read_recorded_validation"),
    )
    snapshot = FixedSnapshot(
        "repo",
        "scope",
        FrozenSourceProgramQuery.source_digest(sources),
        analysis_profile_digest(rule, profile=profile),
        policy_digest,
    )
    record = ValidationRecord(
        repository_id="repo",
        runtime_snapshot_digest=snapshot.snapshot_digest,
        scope_id="scope",
        source=ValidationArtifact(snapshot.source_digest, 1),
        tool_policy=ValidationArtifact(snapshot.tool_policy_digest, 1),
        toolchain=artifact("toolchain"),
        dependency=artifact("dependency"),
        recipe=artifact("recipe"),
        test_input=artifact("input-one"),
        build=ValidationBuildOutcome("succeeded", artifact("binary")),
        execution=ValidationExecutionOutcome(
            "normal_exit",
            "target_observed",
            0,
            observation_stage="asan",
            observation_digest=artifact("observation").sha256,
        ),
        resources=(ValidationResource("wall_time_ms", 1000, 20),),
        limitations=(),
    )
    validation = RecordedValidationProgramQuery(snapshot, {record.record_id: record})
    source = FrozenSourceProgramQuery(snapshot, sources)
    backend = CompositeProgramQuery(source, validation)

    database = tmp_path / "state.sqlite"
    artifacts = tmp_path / "artifacts"
    store = SQLiteStore(database, artifacts)
    runtime = DefectRuntime(store, evaluator=CppUnreachableEvaluator())
    analysis_id = runtime.create_analysis(snapshot, rule, profile=profile)
    identity = CandidateIdentity(
        rule.rule_id,
        rule.rule_version,
        snapshot.snapshot_digest,
        {"path": "a.cpp", "line": 2, "input_digest": record.test_input.sha256},
        f"a.cpp:2:{record.test_input.sha256}",
    )
    task = runtime.propose_candidate(analysis_id, identity, discovery_ref="fixture")

    def run_query(check_id, operation, selectors, args, key):
        query = QueryRequest(
            str(__import__("uuid").uuid4()),
            task.task_id,
            check_id,
            operation,
            snapshot.snapshot_digest,
            backend.backend_id,
            backend.backend_version,
            snapshot.tool_policy_digest,
            {"scope_id": snapshot.scope_id, "selectors": selectors, "max_results": 1},
            args,
            key,
            1000,
        )
        return runtime.query_program(query, backend)

    source_outcome, source_ref = run_query(
        "source_window",
        "read_source",
        {"path": "a.cpp"},
        {"path": "a.cpp", "start_line": 1, "end_line": 4},
        "source-window",
    )
    validation_outcome, validation_ref = run_query(
        "semantic_validation",
        "read_recorded_validation",
        {"record_id": record.record_id},
        {"record_id": record.record_id},
        "recorded-validation",
    )
    assert validation_outcome.status == "complete"
    facts = (
        FactAssessment(
            str(__import__("uuid").uuid4()), task.task_id, "source_window",
            "source_window", "positive", (source_ref,), identity.scope, "tool_observed",
        ),
        FactAssessment(
            str(__import__("uuid").uuid4()), task.task_id, "semantic_validation",
            "semantic_unreachable", "negative", (validation_ref,), identity.scope,
            "tool_observed",
        ),
    )
    for fact, outcome in zip(facts, (source_outcome, validation_outcome)):
        runtime.record_fact(fact)
        runtime.update_check(CheckItem(
            fact.check_id,
            task.task_id,
            fact.check_id,
            "complete",
            "refuted" if fact.polarity == "negative" else "supported",
            fact.evidence_refs,
            outcome.coverage,
        ))

    decision = RoleCoordinator(
        runtime,
        EvidenceReviewExecutor(),
        backend,
        owner_id="fixture",
        working_dir=str(tmp_path),
    ).run_candidate(task.task_id)
    assert decision.verdict == "refuted"
    assert runtime.find_exclusion(task.task_id) is not None
    store.close()

    class MustNotRun:
        def describe_capabilities(self):
            return EvidenceReviewExecutor().describe_capabilities()

        def run(self, request, tools):
            raise AssertionError("a valid exclusion must bypass the agent")

    reopened_store = SQLiteStore(database, artifacts)
    try:
        reopened = DefectRuntime(reopened_store, evaluator=CppUnreachableEvaluator())
        replay = RoleCoordinator(
            reopened,
            MustNotRun(),
            validation,
            owner_id="replay",
            working_dir=str(tmp_path),
        ).run_candidate(task.task_id)
        assert replay.verdict == "refuted"
        assert replay.evaluator_id == "exclusion-index"

        other_identity = CandidateIdentity(
            rule.rule_id,
            rule.rule_version,
            snapshot.snapshot_digest,
            {"path": "a.cpp", "line": 2, "input_digest": artifact("input-two").sha256},
            f"a.cpp:2:{artifact('input-two').sha256}",
        )
        other = reopened.propose_candidate(
            analysis_id, other_identity, discovery_ref="different-input"
        )
        assert reopened.find_exclusion(other.task_id) is None
        other_decision = RoleCoordinator(
            reopened,
            MustNotRun(),
            validation,
            owner_id="different-input",
            working_dir=str(tmp_path),
        ).run_candidate(other.task_id)
        assert other_decision.verdict == "inconclusive"
        assert any("AssertionError" in blocker for blocker in other_decision.blockers)

        reopened_store.artifact_path(validation_ref.artifact_digest).write_bytes(b"tampered")
        with pytest.raises(EvidenceIntegrityError):
            reopened.evidence_ref(validation_ref.evidence_id)
    finally:
        reopened_store.close()
