"""Compare SPEC 008 baseline, Agent, release and ablation reports at one budget."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import compare_quality_reports, load_json_object


def _ablation(value: str) -> tuple[str, Path]:
    component, separator, path = value.partition("=")
    if not separator or not component.strip() or not path.strip():
        raise argparse.ArgumentTypeError("expected COMPONENT=REPORT.json")
    return component.strip(), Path(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--agent", type=Path, required=True)
    parser.add_argument("--previous-release", type=Path, required=True)
    parser.add_argument(
        "--partition", choices=("all", "tuning", "holdout", "shadow"),
        default="holdout",
    )
    parser.add_argument(
        "--ablation", action="append", type=_ablation, default=[],
        metavar="COMPONENT=REPORT.json",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    ablation_paths = dict(args.ablation)
    if len(ablation_paths) != len(args.ablation):
        parser.error("each ablation component may be specified only once")
    comparison = compare_quality_reports(
        {
            "baseline": load_json_object(args.baseline),
            "agent": load_json_object(args.agent),
            "previous_release": load_json_object(args.previous_release),
        },
        partition=args.partition,
        ablations={
            component: load_json_object(path)
            for component, path in ablation_paths.items()
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "comparable",
        "comparison_id": comparison["comparison_id"],
        "comparison_digest": comparison["comparison_digest"],
        "partition": comparison["partition"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
