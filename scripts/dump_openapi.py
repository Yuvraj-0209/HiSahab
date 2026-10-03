"""Write the API's OpenAPI document to frontend/src/api/openapi.json (Phase 23).

The frontend's TypeScript types are generated from that file (`npm run api:types`), so every
money field reaches a screen typed as a string -- and nullable exactly where the server says
null means something. Run this after changing any request or response model:

    python scripts/dump_openapi.py        # or, from frontend/: npm run gen:api

tests/test_openapi_snapshot.py fails while the committed file is stale, so a changed API cannot
reach a deploy with the frontend still compiled against the old shapes.

Output is deterministic (sorted keys, fixed indent) so the diff of a regenerated file shows only
what actually changed.
"""

from __future__ import annotations

import json
import logging
import pathlib

SNAPSHOT = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "src" / "api" / "openapi.json"


def render() -> str:
    """The document exactly as the snapshot stores it."""
    from app.main import create_app

    spec = create_app(serve_ui=False).openapi()
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"


def main() -> None:
    logging.disable(logging.CRITICAL)
    SNAPSHOT.write_text(render())
    print(f"wrote {SNAPSHOT}")


if __name__ == "__main__":
    main()
