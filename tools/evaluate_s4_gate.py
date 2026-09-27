"""Evaluate a SPEC 008 report against a pre-registered Q-07 gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import evaluate_release_gate, load_json_object


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    decision = evaluate_release_gate(
        load_json_object(args.report), load_json_object(args.policy)
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "decision": decision["decision"],
        "policy_id": decision["policy_id"],
        "decision_digest": decision["decision_digest"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
