"""Read-only inspection and integrity validation for validation record files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .codec import canonical_json
from .errors import RuntimeFailure
from .validation import (
    load_validation_record,
    validation_record_to_dict,
    verify_artifact_root,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="defect-validation-record")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "inspect"):
        subparser = subcommands.add_parser(command)
        subparser.add_argument("record", type=Path, metavar="RECORD.json")
        subparser.add_argument(
            "--artifact-root", type=Path,
            help="read-only root containing <digest-prefix>/<digest> artifacts",
        )
    return parser


def _load(path: Path, artifact_root: Path | None):
    record = load_validation_record(path)
    if artifact_root is not None:
        verify_artifact_root(record, artifact_root)
    return record


def main(argv: Sequence[str] | None = None) -> int:
    """Run the deliberately small, non-executing validation-record CLI."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        record = _load(arguments.record, arguments.artifact_root)
    except RuntimeFailure as exc:
        parser.error(f"{exc.code}: {exc}")
        return 2  # pragma: no cover - argparse exits above.
    if arguments.command == "validate":
        print(record.record_id)
    else:
        print(canonical_json(validation_record_to_dict(record)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
