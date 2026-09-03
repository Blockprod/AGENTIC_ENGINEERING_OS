"""Write a deterministic inventory of resources from the installed package."""

from __future__ import annotations

import argparse
import hashlib
import json
from importlib.resources import files
from pathlib import Path


def build_inventory() -> dict[str, object]:
    root = files("agentic_engineering_os.resources")
    records: list[dict[str, object]] = []

    def visit(node, relative: str = "") -> None:
        for child in sorted(node.iterdir(), key=lambda item: item.name.casefold()):
            child_relative = f"{relative}/{child.name}".lstrip("/")
            if child.is_dir():
                visit(child, child_relative)
            elif child.is_file() and "__pycache__" not in child_relative.split("/"):
                content = child.read_bytes()
                records.append(
                    {
                        "path": child_relative,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                )

    visit(root)
    return {"schema_version": "1.0", "resources": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    payload = json.dumps(build_inventory(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    arguments.output.write_text(payload, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
