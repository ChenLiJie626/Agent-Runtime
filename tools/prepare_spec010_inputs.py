#!/usr/bin/env python3
"""Fetch only the frozen cpp-peglib files through the required local proxy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import urllib.parse
import urllib.request
from pathlib import Path

from agent_runtime.errors import EvidenceIntegrityError, InvalidInput

PROXY = "http://127.0.0.1:17891"
MANIFEST = Path(__file__).parents[1] / "evaluation/manifests/public-cpp-peglib-validation-v1.json"


def checked_file(path: Path, size: int, digest: str) -> bytes:
    body = path.read_bytes()
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise EvidenceIntegrityError(f"frozen input differs: {path.name}")
    return body


def fetch(url: str, size: int, digest: str) -> bytes:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
        raise InvalidInput("frozen input URL is outside the approved host")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    request = urllib.request.Request(url, headers={"User-Agent": "agent-runtime-spec010/1"})
    with opener.open(request, timeout=60) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.hostname != "raw.githubusercontent.com":
            raise InvalidInput("frozen input redirected outside the approved host")
        body = response.read(size + 1)
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise EvidenceIntegrityError("downloaded frozen input differs")
    return body


def run(cache_root: Path) -> dict:
    if os.environ.get("HTTP_PROXY") != PROXY or os.environ.get("HTTPS_PROXY") != PROXY:
        raise InvalidInput("SPEC 010 input preparation requires the 0cloud loopback proxy")
    manifest = json.loads(MANIFEST.read_text())
    if manifest.get("schema") != "agent-runtime/public-cpp-peglib-validation/v1":
        raise InvalidInput("cpp-peglib validation manifest schema differs")
    cache_root.mkdir(parents=True, exist_ok=True)
    objects = cache_root / "objects"
    inputs = cache_root / "inputs"
    objects.mkdir(exist_ok=True)
    inputs.mkdir(exist_ok=True)
    probe = manifest["probe"]
    probe_body = checked_file(
        Path(__file__).parents[1] / probe["path"], probe["size_bytes"], probe["sha256"]
    )
    rows = []
    for revision in manifest["revisions"]:
        target = inputs / revision["revision_id"]
        if target.exists():
            raise InvalidInput(f"prepared revision already exists: {revision['revision_id']}")
        target.mkdir()
        for key, name in (("header", "peglib.h"), ("license_file", "LICENSE")):
            item = revision[key]
            object_path = objects / item["sha256"]
            if object_path.exists():
                body = checked_file(object_path, item["size_bytes"], item["sha256"])
            else:
                body = fetch(item["url"], item["size_bytes"], item["sha256"])
                temporary = objects / (item["sha256"] + ".pending")
                temporary.write_bytes(body)
                temporary.replace(object_path)
            (target / name).write_bytes(body)
        (target / "probe.cpp").write_bytes(probe_body)
        rows.append({
            "revision_id": revision["revision_id"],
            "commit": revision["commit"],
            "directory": str(target.resolve()),
            "header_sha256": revision["header"]["sha256"],
            "license_sha256": revision["license_file"]["sha256"],
            "probe_sha256": probe["sha256"],
        })
    report = {"schema": "agent-runtime/spec010-prepared-inputs/v1", "revisions": rows}
    (cache_root / "prepared.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", required=True, choices=["public-cpp-peglib-validation-v1"])
    parser.add_argument("--cache-root", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.cache_root)
    print(json.dumps({"status": "complete", "revisions": len(report["revisions"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
