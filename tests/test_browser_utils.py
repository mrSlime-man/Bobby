"""Unit tests for src.utils.browser_utils helpers (no browser launch)."""

import json

from src.utils.browser_utils import _sanitize_storage_state


def test_sanitize_strips_dict_partition_key(tmp_path):
    """CHIPS-style partitioned cookies (partitionKey as dict) get the field removed."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "YSC",
                        "domain": ".youtube.com",
                        "partitionKey": {"topLevelSite": "https://icims.com"},
                    },
                    {"name": "OK", "domain": ".example.com"},
                ],
                "origins": [],
            }
        )
    )
    state = _sanitize_storage_state(str(path))
    cookies = state["cookies"]
    assert "partitionKey" not in cookies[0]
    assert cookies[1].get("partitionKey") is None
    assert len(cookies) == 2


def test_sanitize_preserves_string_partition_key(tmp_path):
    """A URL-string partitionKey is left alone (it's the expected shape)."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "X",
                        "domain": ".example.com",
                        "partitionKey": "https://example.com",
                    }
                ],
                "origins": [],
            }
        )
    )
    state = _sanitize_storage_state(str(path))
    assert state["cookies"][0]["partitionKey"] == "https://example.com"


def test_sanitize_handles_missing_file(tmp_path):
    """Missing file → empty state (defensive)."""
    state = _sanitize_storage_state(str(tmp_path / "nope.json"))
    assert state == {}


def test_sanitize_handles_corrupt_file(tmp_path):
    """Corrupt JSON → empty state (defensive)."""
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    state = _sanitize_storage_state(str(path))
    assert state == {}


def test_sanitize_leaves_origins_alone(tmp_path):
    """origins block is preserved as-is."""
    path = tmp_path / "state.json"
    origins = [{"origin": "https://example.com", "localStorage": [{"name": "k", "value": "v"}]}]
    path.write_text(json.dumps({"cookies": [], "origins": origins}))
    state = _sanitize_storage_state(str(path))
    assert state["origins"] == origins
