"""The browser's photo API snapshot must match the mounted backend routes."""

import json
from pathlib import Path

from deeptutor.api.contracts.export import render_contracts


def test_photo_inbox_browser_contract_matches_mounted_api():
    current = json.loads(render_contracts()["openapi.json"])
    snapshot = json.loads(
        (Path(__file__).parents[2] / "web/contracts/schema/openapi.json").read_text()
    )
    photo_paths = {
        path: item for path, item in current["paths"].items() if "/practice/photo-inbox" in path
    }
    assert len(photo_paths) == 7
    for path, item in photo_paths.items():
        assert snapshot["paths"][path] == item
    for name, schema in current["components"]["schemas"].items():
        if name.startswith("Photo") or name.startswith("Body_supplement_photo_"):
            assert snapshot["components"]["schemas"][name] == schema
