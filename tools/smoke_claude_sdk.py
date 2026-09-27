"""Small live Claude SDK smoke with bounded budget and optional tool probe.

The output intentionally stores no prompt transcript or authentication details.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import secrets
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = {
    "type": "object",
    "properties": {"marker": {"type": "string"}},
    "required": ["marker"],
    "additionalProperties": False,
}


async def attempt(
    cwd: str,
    prompt: str,
    resume: str | None,
    sdk_env: dict[str, str],
    model: str | None,
) -> dict[str, object]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

    options = ClaudeAgentOptions(
        cwd=cwd,
        tools=[],
        allowed_tools=[],
        disallowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Agent",
            "NotebookEdit",
        ],
        permission_mode="dontAsk",
        setting_sources=[],
        env=sdk_env,
        model=model,
        strict_mcp_config=True,
        max_turns=2,
        max_budget_usd=0.25,
        output_format={"type": "json_schema", "schema": SCHEMA},
        resume=resume,
    )
    final = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                final = message
    if final is None:
        return {"status": "no-result"}
    return {
        "status": "success"
        if not final.is_error and final.structured_output
        else "failed",
        "subtype": final.subtype,
        "session_id": final.session_id,
        "api_error_status": final.api_error_status,
        "terminal_reason": final.terminal_reason,
        "structured_marker": (
            final.structured_output.get("marker")
            if isinstance(final.structured_output, dict)
            else None
        ),
        "cost_usd": final.total_cost_usd,
    }


async def probe_tool_and_settings(
    cwd: str, sdk_env: dict[str, str], model: str | None
) -> dict[str, object]:
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        create_sdk_mcp_server,
        tool,
    )

    marker = secrets.token_hex(12)
    hook_path = Path(cwd) / "project-hook-ran"
    hook_code = (
        "from pathlib import Path; Path(" + repr(str(hook_path)) + ").write_text('ran')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(hook_code)}"
    settings_dir = Path(cwd) / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "mcp__probe__probe_marker",
                            "hooks": [{"type": "command", "command": command}],
                        }
                    ]
                }
            }
        )
    )

    async def run_one(setting_sources: list[str]) -> dict[str, object]:
        calls = 0

        @tool(
            "probe_marker",
            "Return an unpredictable marker for this SDK integration test",
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
        async def probe_marker(_arguments: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"content": [{"type": "text", "text": marker}]}

        server = create_sdk_mcp_server(name="probe", tools=[probe_marker])
        name = "mcp__probe__probe_marker"
        options = ClaudeAgentOptions(
            cwd=cwd,
            env=sdk_env,
            model=model,
            tools=[name],
            allowed_tools=[name],
            disallowed_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebFetch",
                "WebSearch",
                "Agent",
                "NotebookEdit",
            ],
            permission_mode="dontAsk",
            setting_sources=setting_sources,
            strict_mcp_config=True,
            mcp_servers={"probe": server},
            max_turns=3,
            max_budget_usd=0.25,
            output_format={"type": "json_schema", "schema": SCHEMA},
        )
        final = None
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                "Call the probe_marker tool exactly once. Return JSON with marker "
                "equal to the tool's text result. Do not guess the marker."
            )
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    final = message
        value = final.structured_output if final else None
        return {
            "tool_calls": calls,
            "structured_match": isinstance(value, dict)
            and value.get("marker") == marker,
            "hook_ran": hook_path.exists(),
            "api_error_status": final.api_error_status if final else None,
        }

    isolated = await run_one([])
    hook_path.unlink(missing_ok=True)
    control = await run_one(["project"])
    return {
        "isolated": isolated,
        "project_control": control,
        "status": "passed"
        if isolated["tool_calls"] == control["tool_calls"] == 1
        and isolated["structured_match"]
        and control["structured_match"]
        and not isolated["hook_ran"]
        and control["hook_ran"]
        else "failed",
    }


async def probe_role_isolation(
    cwd: str, sdk_env: dict[str, str], model: str | None
) -> dict[str, object]:
    """Check a new verifier session cannot read an investigator-only nonce."""
    nonce = secrets.token_hex(16)
    investigator = await attempt(
        cwd,
        f"You are the investigator. Memorize this private marker: {nonce}. "
        "Return JSON with marker equal to that private marker.",
        None,
        sdk_env,
        model,
    )
    verifier = await attempt(
        cwd,
        "You are the independent verifier in a fresh session. Return JSON "
        "with marker equal to the private marker given only to a prior "
        "investigator session. If that marker is unavailable to you, "
        "return marker exactly unknown. Do not guess.",
        None,
        sdk_env,
        model,
    )
    passed = (
        investigator["status"] == verifier["status"] == "success"
        and investigator["structured_marker"] == nonce
        and verifier["structured_marker"] == "unknown"
        and investigator["session_id"] != verifier["session_id"]
    )
    # The report must not persist the private nonce, even on failure.
    return {
        "status": "passed" if passed else "failed",
        "investigator_status": investigator["status"],
        "verifier_status": verifier["status"],
        "distinct_sessions": investigator.get("session_id")
        != verifier.get("session_id"),
        "investigator_returned_nonce": investigator.get("structured_marker") == nonce,
        "verifier_reported_unknown": verifier.get("structured_marker") == "unknown",
    }


async def probe_interrupt(
    cwd: str, sdk_env: dict[str, str], model: str | None
) -> dict[str, object]:
    """Interrupt while a controlled MCP tool is waiting, then release it."""
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        create_sdk_mcp_server,
        tool,
    )

    started = asyncio.Event()
    release = asyncio.Event()

    @tool(
        "wait_marker",
        "Wait for the integration test to release this call",
        {"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def wait_marker(_arguments: dict[str, object]) -> dict[str, object]:
        started.set()
        await release.wait()
        return {"content": [{"type": "text", "text": "released"}]}

    name = "mcp__interrupt_probe__wait_marker"
    options = ClaudeAgentOptions(
        cwd=cwd,
        env=sdk_env,
        model=model,
        tools=[name],
        allowed_tools=[name],
        disallowed_tools=[
            "Bash",
            "Read",
            "Write",
            "Edit",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "Agent",
            "NotebookEdit",
        ],
        permission_mode="dontAsk",
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers={
            "interrupt_probe": create_sdk_mcp_server(
                name="interrupt_probe", tools=[wait_marker]
            )
        },
        max_turns=3,
        max_budget_usd=0.25,
        output_format={"type": "json_schema", "schema": SCHEMA},
    )
    final = None
    interrupt_returned = False
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Call wait_marker exactly once. After the tool returns, return "
            "JSON with marker equal to its text."
        )

        async def consume() -> None:
            nonlocal final
            async for message in client.receive_response():
                if isinstance(message, ResultMessage):
                    final = message

        receiver = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), timeout=45)
            await asyncio.wait_for(client.interrupt(), timeout=10)
            interrupt_returned = True
        finally:
            release.set()
            try:
                await asyncio.wait_for(receiver, timeout=20)
            except TimeoutError:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
    return {
        "status": "passed"
        if interrupt_returned and final and final.is_error
        else "failed",
        "tool_started": started.is_set(),
        "interrupt_returned": interrupt_returned,
        "final_subtype": final.subtype if final else None,
        "final_is_error": final.is_error if final else None,
    }


async def smoke(
    cli_version: str,
    sdk_env: dict[str, str],
    model: str | None,
    probe_tools: bool,
    probe_roles: bool,
    probe_interrupt_requested: bool,
) -> dict[str, object]:
    report: dict[str, object] = {
        "sdk_version": importlib.metadata.version("claude-agent-sdk"),
        "cli_version": cli_version,
        "probes": [
            "structured-output",
            "fresh-session",
            "explicit-resume",
            *(["custom-mcp", "settings-isolation"] if probe_tools else []),
            *(["role-isolation"] if probe_roles else []),
            *(["interrupt"] if probe_interrupt_requested else []),
        ],
        "route_host": urlsplit(sdk_env.get("ANTHROPIC_BASE_URL", "")).netloc,
        "model": model,
    }
    with tempfile.TemporaryDirectory() as cwd:
        first = await attempt(
            cwd, "Return a JSON object with marker exactly first.", None, sdk_env, model
        )
        report["first"] = first
        if first["status"] != "success" or not first.get("session_id"):
            report["status"] = "blocked-before-resume"
            return report
        resumed = await attempt(
            cwd,
            "Return a JSON object with marker exactly resumed.",
            str(first["session_id"]),
            sdk_env,
            model,
        )
        report["resumed"] = resumed
        fresh = await attempt(
            cwd, "Return a JSON object with marker exactly fresh.", None, sdk_env, model
        )
        report["fresh"] = fresh
        report["status"] = (
            "passed"
            if resumed["status"] == fresh["status"] == "success"
            and first["structured_marker"] == "first"
            and resumed["structured_marker"] == "resumed"
            and fresh["structured_marker"] == "fresh"
            and resumed["session_id"] == first["session_id"]
            and fresh["session_id"] != first["session_id"]
            else "failed"
        )
        if report["status"] == "passed" and probe_tools:
            report["tool_and_settings_probe"] = await probe_tool_and_settings(
                cwd, sdk_env, model
            )
            if report["tool_and_settings_probe"]["status"] != "passed":
                report["status"] = "failed-tool-or-settings-probe"
        if report["status"] == "passed" and probe_roles:
            report["role_isolation_probe"] = await probe_role_isolation(
                cwd, sdk_env, model
            )
            if report["role_isolation_probe"]["status"] != "passed":
                report["status"] = "failed-role-isolation-probe"
        if report["status"] == "passed" and probe_interrupt_requested:
            report["interrupt_probe"] = await probe_interrupt(cwd, sdk_env, model)
            if report["interrupt_probe"]["status"] != "passed":
                report["status"] = "failed-interrupt-probe"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--model", help="SDK model override for route diagnostics")
    parser.add_argument("--probe-tools", action="store_true")
    parser.add_argument("--probe-roles", action="store_true")
    parser.add_argument("--probe-interrupt", action="store_true")
    parser.add_argument(
        "--cc-switch",
        action="store_true",
        help="Use only the active local CC Switch route from ~/.claude/settings.json",
    )
    args = parser.parse_args()
    sdk_env: dict[str, str] = {}
    model: str | None = None
    if args.cc_switch:
        settings = json.loads((Path.home() / ".claude/settings.json").read_text())
        source = settings.get("env", {})
        url = source.get("ANTHROPIC_BASE_URL", "")
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise SystemExit("CC Switch local Claude route is not active")
        sdk_env = {
            key: value
            for key, value in source.items()
            if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
            if isinstance(value, str)
        }
        # The SDK sends a Claude role alias; CC Switch maps it to gpt-6-sol.
        # Passing gpt-6-sol directly makes Claude Code treat it as unknown.
        model = source.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")
    if args.model:
        model = args.model
    cli_version = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        report = asyncio.run(
            asyncio.wait_for(
                smoke(
                    cli_version,
                    sdk_env,
                    model,
                    args.probe_tools,
                    args.probe_roles,
                    args.probe_interrupt,
                ),
                timeout=args.timeout_seconds,
            )
        )
    except TimeoutError:
        report = {
            "status": "timeout",
            "sdk_version": importlib.metadata.version("claude-agent-sdk"),
            "cli_version": cli_version,
            "route_host": urlsplit(sdk_env.get("ANTHROPIC_BASE_URL", "")).netloc,
            "model": model,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps({"status": report["status"], "output": str(args.output.resolve())})
    )


if __name__ == "__main__":
    main()
