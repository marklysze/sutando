#!/usr/bin/env python3
"""Generate the Python and TypeScript config-key catalogs from the JSON Schema."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCHEMA = REPO / "docs" / "sutando-config.schema.json"
PYTHON_LOADER = REPO / "src" / "sutando_config.py"
TYPESCRIPT_LOADER = REPO / "src" / "sutando_config.ts"

PYTHON_START = "# BEGIN GENERATED CONFIG KEYS — scripts/gen-config-key-contract.py"
PYTHON_END = "# END GENERATED CONFIG KEYS"
TYPESCRIPT_START = "// BEGIN GENERATED CONFIG KEYS — scripts/gen-config-key-contract.py"
TYPESCRIPT_END = "// END GENERATED CONFIG KEYS"


def schema_keys() -> list[str]:
    schema = json.loads(SCHEMA.read_text())
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("config schema must declare non-empty top-level properties")
    return list(properties)


def python_block(keys: list[str]) -> str:
    entries = "\n".join(f'    "{key}",' for key in keys)
    return f"{PYTHON_START}\n_KNOWN_TOP_LEVEL_KEYS = {{\n{entries}\n}}\n{PYTHON_END}"


def typescript_block(keys: list[str]) -> str:
    entries = "\n".join(f"\t'{key}'," for key in keys)
    return (
        f"{TYPESCRIPT_START}\n"
        f"const KNOWN_TOP_LEVEL_KEYS = new Set([\n{entries}\n]);\n"
        f"{TYPESCRIPT_END}"
    )


def replace_block(path: Path, start: str, end: str, block: str, *, check: bool) -> bool:
    source = path.read_text()
    if source.count(start) != 1 or source.count(end) != 1:
        raise ValueError(f"{path.relative_to(REPO)} must contain exactly one generated-key block")
    before, remainder = source.split(start, 1)
    _, after = remainder.split(end, 1)
    rendered = before + block + after
    if rendered == source:
        return True
    if check:
        print(f"config-key-contract: {path.relative_to(REPO)} is stale", file=sys.stderr)
        return False
    path.write_text(rendered)
    print(f"config-key-contract: updated {path.relative_to(REPO)}")
    return True


def main() -> int:
    check = sys.argv[1:] == ["--check"]
    if sys.argv[1:] not in ([], ["--check"]):
        print("usage: gen-config-key-contract.py [--check]", file=sys.stderr)
        return 2

    keys = schema_keys()
    results = [
        replace_block(
            PYTHON_LOADER,
            PYTHON_START,
            PYTHON_END,
            python_block(keys),
            check=check,
        ),
        replace_block(
            TYPESCRIPT_LOADER,
            TYPESCRIPT_START,
            TYPESCRIPT_END,
            typescript_block(keys),
            check=check,
        ),
    ]
    if check and all(results):
        print(f"config-key-contract: generated loaders are up to date ({len(keys)} keys)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
