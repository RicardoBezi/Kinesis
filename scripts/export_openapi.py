"""Export the FastAPI OpenAPI document to docs/api/openapi.json (ADR 0008).

python scripts/export_openapi.py          # write the file
python scripts/export_openapi.py --check  # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kinesis.main import create_app

OUT = Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"


def render() -> str:
    spec = create_app().openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if the file is out of date")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != rendered:
            print(f"{OUT} is out of date: run `uv run task openapi`", file=sys.stderr)
            return 1
        print(f"{OUT} is up to date")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(rendered.encode("utf-8"))  # bytes: keep LF on Windows
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
