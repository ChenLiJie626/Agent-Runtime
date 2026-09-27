"""Package-owned CodeQL query and fixed live-acceptance contracts."""
from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.adapters.codeql import CodeQLReplayProgramQuery
from agent_runtime.errors import InvalidInput

ROOT = Path(__file__).parents[1]
PACK = ROOT / "src/agent_runtime/adapters/codeql_pack"
MANIFEST = ROOT / "evaluation/manifests/codeql-cpp-v2.27.1.json"


def test_pinned_codeql_manifest_and_package_query_are_consistent():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["release"] == "codeql-bundle-v2.27.1"
    assert manifest["archive"] == {
        "url": "https://github.com/github/codeql-action/releases/download/codeql-bundle-v2.27.1/codeql-bundle-osx64.tar.zst",
        "size": 876476743,
        "sha256": "b63286d8189b90f6045a18797d191603f611f0003216783cade7166ec80ada45",
    }
    assert manifest["extracted_tree"]["file_count"] == 37160
    assert manifest["extracted_tree"]["manifest_digest"] == (
        "bfb92727a9c57df5549afce544d6acce9dd82a6280706032eac2aafc41647169"
    )
    query = (PACK / "PotentialAccessAfterDelete.ql").read_text()
    assert "@id cpp/potential-access-after-delete-candidate" in query
    assert (PACK / "qlpack.yml").is_file()
    assert (PACK / "codeql-pack.lock.yml").read_text().startswith("---\nlockVersion: 1.0.0")


def test_implicit_srcroot_is_narrowly_supported():
    result = {
        "ruleId": "cpp/potential-access-after-delete-candidate",
        "message": {"text": "candidate"},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "positive.cpp", "uriBaseId": "%SRCROOT%"},
            "region": {"startLine": 6, "startColumn": 10, "endColumn": 14},
        }}],
    }
    normalized = CodeQLReplayProgramQuery._normalize_result(
        result, source_root=str(ROOT), allowed_uri_base_ids=("%SRCROOT%",),
    )
    assert normalized["uri"] == "positive.cpp"
    changed = json.loads(json.dumps(result))
    changed["locations"][0]["physicalLocation"]["artifactLocation"]["uriBaseId"] = "%OTHER%"
    try:
        CodeQLReplayProgramQuery._normalize_result(
            changed, source_root=str(ROOT), allowed_uri_base_ids=("%SRCROOT%",),
        )
    except InvalidInput:
        pass
    else:
        raise AssertionError("unregistered implicit URI base was accepted")
