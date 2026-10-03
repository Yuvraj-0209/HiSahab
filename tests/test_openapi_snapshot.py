"""The frontend is compiled against the API as it is today (Phase 23).

`frontend/src/api/openapi.json` is the snapshot the frontend's TypeScript types are generated
from, and the types are what make every money field a `string` in the client -- `string | null`
exactly where the server says null means "nobody declared" or "no limit". A stale snapshot is
therefore not a cosmetic problem: a field renamed or made nullable on the server would compile
fine in the client and fail only in a browser, which is the failure §13.18 says nothing used to
catch.

If this fails, run `python scripts/dump_openapi.py` and commit the result.
"""

from __future__ import annotations

import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "dump_openapi.py"


def _dump_module():
    spec = importlib.util.spec_from_file_location("dump_openapi", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_openapi_snapshot_is_current() -> None:
    dump = _dump_module()
    assert dump.SNAPSHOT.exists(), "frontend/src/api/openapi.json is missing"
    assert dump.SNAPSHOT.read_text() == dump.render(), (
        "The API changed but frontend/src/api/openapi.json did not. "
        "Run `python scripts/dump_openapi.py` and commit the result."
    )
