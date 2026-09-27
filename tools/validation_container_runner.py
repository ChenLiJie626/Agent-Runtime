#!/usr/bin/env python3
"""Fixed in-container recipes for SPEC 010 validation attempts."""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value, sort_keys=True), flush=True)


def readable(path):
    try:
        Path(path).read_bytes()
        return True
    except OSError:
        return False


def writable(path, body=b"x"):
    try:
        Path(path).write_bytes(body)
        Path(path).unlink()
        return True
    except OSError:
        return False


def network_unavailable():
    try:
        with socket.create_connection(("1.1.1.1", 53), timeout=0.2):
            return False
    except OSError:
        return True


def route_unavailable():
    try:
        rows = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return False
    return not any(row.split()[0] != "lo" for row in rows if len(row.split()) > 1)


def status_fields():
    values = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


def write_resources():
    fields = {}
    for name, path in (
        ("memory_peak_bytes", "/sys/fs/cgroup/memory.peak"),
        ("pids_peak", "/sys/fs/cgroup/pids.peak"),
    ):
        try:
            fields[name] = int(Path(path).read_text().strip())
        except (OSError, ValueError):
            fields[name] = None
    try:
        cpu = dict(
            line.split(maxsplit=1)
            for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines()
        )
        fields["cpu_time_usec"] = int(cpu["usage_usec"])
    except (OSError, ValueError, KeyError):
        fields["cpu_time_usec"] = None
    try:
        fields["writable_bytes"] = sum(
            path.stat().st_size for path in Path("/work").rglob("*") if path.is_file()
        )
    except OSError:
        fields["writable_bytes"] = None
    status = "observed" if all(type(value) is int for value in fields.values()) else "unavailable"
    report = {
        "schema": "agent-runtime/docker-resource/v1", "status": status, **fields
    }
    Path("/work/resource.json").write_text(json.dumps(report, sort_keys=True))
    emit(report)


def probe(sentinel):
    status = status_fields()
    forbidden = ("proxy", "token", "secret", "password", "key", "aws_", "gcp_", "azure_")
    environment_clean = not any(
        any(marker in key.lower() for marker in forbidden) for key in os.environ
    )
    stat = os.statvfs("/work")
    capacity = stat.f_frsize * stat.f_blocks
    checks = {
        "non_root": os.geteuid() != 0 and os.getegid() != 0,
        "no_new_privileges": status.get("NoNewPrivs") == "1",
        "capabilities_empty": int(status.get("CapEff", "1"), 16) == 0,
        "environment_clean": environment_clean,
        "sentinel_unreadable": not readable(sentinel),
        "network_unavailable": network_unavailable(),
        "route_unavailable": route_unavailable(),
        "root_read_only": not writable("/validation-root-probe"),
        "source_read_only": not writable("/source/.validation-probe"),
        "work_writable": writable("/work/.validation-probe"),
        "work_bounded": 0 < capacity <= 256 * 1024 * 1024,
    }
    emit({"schema": "agent-runtime/isolation-probe/v1", "checks": checks})
    return 0 if all(checks.values()) else 70


def main():
    if len(sys.argv) != 3:
        return 64
    recipe, sentinel = sys.argv[1:]
    try:
        if recipe == "isolation-probe":
            return probe(sentinel)
        if recipe == "isolation-normal":
            emit({"status": "normal"})
            return 0
        if recipe == "isolation-nonzero":
            emit({"status": "nonzero"})
            return 23
        if recipe == "isolation-signal":
            while True:
                pass
        if recipe == "isolation-timeout":
            while True:
                pass
        if recipe == "isolation-output":
            while True:
                os.write(1, b"x" * 65536)
        if recipe == "isolation-space":
            with open("/work/fill", "wb", buffering=0) as output:
                while True:
                    output.write(b"x" * 65536)
        if recipe == "cpp-peglib-build":
            Path("/work/artifacts").mkdir()
            version = subprocess.run(
                ["clang++", "--version"], check=False, text=True,
            )
            if version.returncode != 0:
                return version.returncode
            build = subprocess.run(
                [
                    "clang++", "-std=c++17", "-O1", "-g",
                    "-fno-omit-frame-pointer", "-fsanitize=address,undefined",
                    "-MMD", "-MF", "/work/artifacts/probe.d",
                    "-I/source", "/source/probe.cpp",
                    "-o", "/work/artifacts/probe",
                ],
                check=False,
            )
            if build.returncode == 0:
                for name, path in (
                    ("binary", "/work/artifacts/probe"),
                    ("depfile", "/work/artifacts/probe.d"),
                ):
                    emit({
                        "schema": "agent-runtime/docker-output/v1",
                        "name": name,
                        "content_base64": base64.b64encode(Path(path).read_bytes()).decode(),
                    })
            return build.returncode
        if recipe in {"cpp-peglib-run-ignored", "cpp-peglib-run-ordinary"}:
            mode = recipe.removeprefix("cpp-peglib-run-")
            return subprocess.run(["/source/probe", mode], check=False).returncode
        emit({"status": "unknown_recipe"})
        return 64
    except OSError as exc:
        if recipe == "isolation-space" and exc.errno == 28:
            Path("/work/fill").unlink(missing_ok=True)
            emit({"status": "space_exhausted"})
            return 0
        raise
    finally:
        if recipe not in {"isolation-signal", "isolation-timeout", "isolation-output"}:
            write_resources()


if __name__ == "__main__":
    raise SystemExit(main())
