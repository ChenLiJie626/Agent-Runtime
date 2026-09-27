"""Generate machine-readable and Markdown SPEC 008 quality reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent_runtime.evaluation import (
    build_quality_report,
    load_json_object,
    render_quality_markdown,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--json-output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path, required=True)
    args = parser.parse_args()

    report = build_quality_report(
        load_json_object(args.manifest), load_json_object(args.run)
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(
        render_quality_markdown(report), encoding="utf-8"
    )
    print(json.dumps({
        "status": "passed",
        "report_id": report["report_id"],
        "report_digest": report["report_digest"],
        "json_output": str(args.json_output.resolve()),
        "markdown_output": str(args.markdown_output.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
