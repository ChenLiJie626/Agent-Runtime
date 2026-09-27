"""Contracts for the frozen cpp-peglib reproduction and observer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from agent_runtime.validation import ValidationTermination
from agent_runtime.validation_capture import ValidationArtifactStore
from tools.run_cpp_peglib_validation import MANIFEST, observation


def test_manifest_binds_real_revisions_and_probe():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["schema"] == "agent-runtime/public-cpp-peglib-validation/v1"
    assert [item["commit"] for item in manifest["revisions"]] == [
        "14305f9f53cde207568f21675a1b9294a3ab28b4",
        "0061f393de54cf0326621c079dc2988336d1ebb3",
    ]
    probe = Path(manifest["probe"]["path"])
    body = probe.read_bytes()
    assert len(body) == manifest["probe"]["size_bytes"]
    assert hashlib.sha256(body).hexdigest() == manifest["probe"]["sha256"]
    assert manifest["inputs"] == ["ignored", "ordinary"]


def evidence(store, stdout: bytes, stderr: bytes, termination=None):
    return SimpleNamespace(
        stdout=store.put(stdout),
        stderr=store.put(stderr),
        termination=termination or ValidationTermination("exit", exit_code=1),
    )


def test_observer_requires_phase_sanitizer_type_and_numbered_candidate_frame(tmp_path):
    store = ValidationArtifactStore(tmp_path / "cas")
    stdout = b'{"phase":"parsed","parse_ok":true,"ast_nonnull":false}\n'
    report = b'''ERROR: AddressSanitizer: SEGV on unknown address 0x0
    #0 0x1 in peg::AstOptimizer::optimize /source/peglib.h:3650:32
    #1 0x2 in main /source/probe.cpp:26:33
SUMMARY: AddressSanitizer: SEGV /source/peglib.h:3650:32
'''
    assert observation(store, evidence(store, stdout, report)) == (
        "ast_optimizer_invalid_access", True
    )
    assert observation(store, evidence(store, stdout, report.replace(b"peglib.h", b"other.h"))) == (
        "no_matching_observation", False
    )
    assert observation(store, evidence(store, b"", report)) == (
        "no_matching_observation", False
    )


def test_observer_scopes_optimized_completion_to_successful_execution(tmp_path):
    store = ValidationArtifactStore(tmp_path / "cas")
    stdout = b'{"phase":"optimized","ast_nonnull":true}\n'
    complete = evidence(
        store, stdout, b"", ValidationTermination("exit", exit_code=0)
    )
    failed = evidence(
        store, stdout, b"", ValidationTermination("exit", exit_code=1)
    )
    assert observation(store, complete) == ("optimized_completed", True)
    assert observation(store, failed) == ("no_matching_observation", False)
