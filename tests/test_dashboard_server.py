import asyncio

import httpx

from src.dashboard import server
from src.dashboard.server import app

class ASGITestClient:
    """Synchronous facade that avoids the incompatible TestClient portal."""

    @staticmethod
    def request(method, url, **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as async_client:
                return await async_client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)


client = ASGITestClient()


def test_index_serves_dashboard_page():
    response = client.get("/")

    assert response.status_code == 200
    assert "Operations Dashboard" in response.text
    assert '<body class="dashboard-loading" aria-busy="true">' in response.text
    assert 'id="dashboard-loading"' in response.text
    assert "Loading dashboard data" in response.text


def test_run_detail_page_serves_dashboard_page():
    response = client.get("/runs/run-1")

    assert response.status_code == 200
    assert "Operations Dashboard" in response.text


def test_summary_endpoint_returns_summary(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_summary",
        lambda: {"run": {"status": "idle"}, "totals": {"applied": 3}, "last_run": {}},
    )

    response = client.get("/api/summary")

    assert response.status_code == 200
    assert response.json()["totals"]["applied"] == 3


def test_runs_endpoint_returns_history(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_history",
        lambda: [{"run_id": "run-1", "status": "completed", "jobs": {"applied": 2}}],
    )

    response = client.get("/api/runs")

    assert response.status_code == 200
    assert response.json()["runs"][0]["run_id"] == "run-1"


def test_run_events_endpoint_returns_filtered_history(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_events",
        lambda run_id, limit=200: [{"run_id": run_id, "type": "run_started"}],
    )

    response = client.get("/api/runs/run-9/events?limit=10")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-9"
    assert response.json()["events"][0]["run_id"] == "run-9"


def test_run_detail_endpoint_returns_combined_payload(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_detail",
        lambda run_id: {
            "run": {"run_id": run_id},
            "jobs": [{"status": "applied"}],
            "events": [{"type": "run_started"}],
            "screenshots": [{"path": "data/output/dashboard/screenshots/run-9/example.png"}],
        },
    )

    response = client.get("/api/runs/run-9")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-9"
    assert response.json()["run"]["run_id"] == "run-9"
    assert response.json()["jobs"][0]["status"] == "applied"
    assert response.json()["screenshots"][0]["path"].endswith("example.png")


def test_run_export_returns_downloadable_json(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_detail",
        lambda run_id: {"run": {"run_id": run_id}, "jobs": [], "events": [], "screenshots": []},
    )

    response = client.get("/api/runs/run-9/export")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="run-9.json"'
    assert response.json()["run_id"] == "run-9"
    assert response.json()["run"]["run_id"] == "run-9"


def test_run_screenshots_endpoint_returns_history(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_screenshots",
        lambda run_id: [
            {"run_id": run_id, "path": "data/output/dashboard/screenshots/run-9/example.png"}
        ],
    )

    response = client.get("/api/runs/run-9/screenshots")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-9"
    assert response.json()["screenshots"][0]["path"].endswith("example.png")


def test_run_jobs_endpoint_returns_filtered_jobs(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_run_jobs_payload",
        lambda run_id, status=None, search=None: {
            "run_id": run_id,
            "jobs": [{"run_id": run_id, "status": status, "job_title": search or "CTO"}],
            "filtered_count": 1,
            "total_count": 3,
        },
    )

    response = client.get("/api/runs/run-9/jobs?status=applied&search=cto")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-9"
    assert response.json()["jobs"][0]["status"] == "applied"
    assert response.json()["jobs"][0]["job_title"] == "cto"
    assert response.json()["filtered_count"] == 1
    assert response.json()["total_count"] == 3


def test_jobs_endpoint_passes_filters(monkeypatch):
    captured = {}

    def fake_get_jobs_payload(status=None, search=None):
        captured["status"] = status
        captured["search"] = search
        return {
            "jobs": [{"status": status, "job_title": search}],
            "filtered_count": 1,
            "total_count": 4,
        }

    monkeypatch.setattr("src.dashboard.server.get_jobs_payload", fake_get_jobs_payload)

    response = client.get("/api/jobs?status=applied&search=cto")

    assert response.status_code == 200
    assert captured == {"status": "applied", "search": "cto"}
    assert response.json()["jobs"][0]["job_title"] == "cto"
    assert response.json()["filtered_count"] == 1
    assert response.json()["total_count"] == 4


def test_search_config_update_returns_400_on_error(monkeypatch):
    def fake_update(_config):
        raise ValueError("bad config")

    monkeypatch.setattr("src.dashboard.server.update_search_config", fake_update)

    response = client.put("/api/config/search", json={"config": {}})

    assert response.status_code == 400
    assert response.json()["detail"] == "bad config"


def test_app_config_update_returns_config(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.update_app_config",
        lambda config: {**config, "HEADLESS_MODE": False},
    )

    response = client.put("/api/config/app", json={"config": {"HEADLESS_MODE": False}})

    assert response.status_code == 200
    assert response.json()["app"]["HEADLESS_MODE"] is False


def test_start_control_returns_process(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.start_bot_process",
        lambda: {"pid": 4321, "run_id": "run-1"},
    )
    monkeypatch.setattr(
        "src.dashboard.server.get_summary",
        lambda: {"run": {"status": "starting"}, "totals": {}, "last_run": {}},
    )

    response = client.post("/api/control/start")

    assert response.status_code == 200
    assert response.json()["process"]["pid"] == 4321


def test_force_stop_returns_terminated_state(monkeypatch):
    monkeypatch.setattr("src.dashboard.server.terminate_running_process", lambda: True)
    monkeypatch.setattr(
        "src.dashboard.server.get_summary",
        lambda: {"run": {"status": "stopped"}, "totals": {}, "last_run": {}},
    )

    response = client.post("/api/control/stop?force=true")

    assert response.status_code == 200
    assert response.json()["terminated"] is True


def test_screenshot_returns_404_when_missing(monkeypatch):
    class MissingPath:
        def exists(self):
            return False

    monkeypatch.setattr("src.dashboard.server.LATEST_SCREENSHOT_FILE", MissingPath())

    response = client.get("/api/screenshot")

    assert response.status_code == 404
    assert response.json()["available"] is False


def test_screenshot_file_returns_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("src.dashboard.server.ROOT_DIR", tmp_path)
    monkeypatch.setattr(
        "src.dashboard.server.SCREENSHOT_DIR",
        tmp_path / "data" / "output" / "dashboard" / "screenshots",
    )

    response = client.get(
        "/api/screenshot-file?path=data/output/dashboard/screenshots/run-1/missing.png"
    )

    assert response.status_code == 404


def test_screenshot_file_returns_file(tmp_path, monkeypatch):
    screenshot = (
        tmp_path / "data" / "output" / "dashboard" / "screenshots" / "run-1" / "example.png"
    )
    screenshot.parent.mkdir(parents=True, exist_ok=True)
    screenshot.write_bytes(b"fake-image")
    monkeypatch.setattr("src.dashboard.server.ROOT_DIR", tmp_path)
    monkeypatch.setattr("src.dashboard.server.SCREENSHOT_DIR", screenshot.parents[1])

    response = client.get(
        "/api/screenshot-file?path=data/output/dashboard/screenshots/run-1/example.png"
    )

    assert response.status_code == 200
    assert response.content == b"fake-image"


def test_screenshot_file_rejects_repository_files(tmp_path, monkeypatch):
    screenshot_dir = tmp_path / "data" / "output" / "dashboard" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".env").write_text("secret=not-for-serving", encoding="utf-8")
    monkeypatch.setattr("src.dashboard.server.ROOT_DIR", tmp_path)
    monkeypatch.setattr("src.dashboard.server.SCREENSHOT_DIR", screenshot_dir)

    response = client.get("/api/screenshot-file?path=.env")

    assert response.status_code == 400


def test_screenshot_file_rejects_symlink_escape(tmp_path, monkeypatch):
    screenshot_dir = tmp_path / "data" / "output" / "dashboard" / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    private_file = tmp_path / "private.txt"
    private_file.write_text("private", encoding="utf-8")
    link = screenshot_dir / "escape.png"
    link.symlink_to(private_file)
    monkeypatch.setattr("src.dashboard.server.ROOT_DIR", tmp_path)
    monkeypatch.setattr("src.dashboard.server.SCREENSHOT_DIR", screenshot_dir)

    response = client.get(
        "/api/screenshot-file?path=data/output/dashboard/screenshots/escape.png"
    )

    assert response.status_code == 400


def test_screenshot_returns_stable_byte_snapshot(tmp_path, monkeypatch):
    screenshot = tmp_path / "latest.png"
    screenshot.write_bytes(b"first-image")
    monkeypatch.setattr("src.dashboard.server.LATEST_SCREENSHOT_FILE", screenshot)

    response = client.get("/api/screenshot")

    screenshot.write_bytes(b"updated-image")
    assert response.status_code == 200
    assert response.content == b"first-image"


def test_event_stream_sends_initial_snapshot(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_live_state",
        lambda: {"snapshot": {"run_status": "running"}, "events": []},
    )
    monkeypatch.setattr(
        "src.dashboard.server.get_run_events",
        lambda run_id, limit=120: [{"run_id": run_id, "type": "run_started"}],
    )
    monkeypatch.setattr("src.dashboard.server.latest_event_position", lambda: 0)
    monkeypatch.setattr(
        "src.dashboard.server.read_events_since_for_run", lambda position, run_id: ([], position)
    )

    class MockRequest:
        async def is_disconnected(self):
            return False

    async def read_first_chunk():
        response = await server.stream_events(MockRequest(), run_id="run-1")
        first_chunk = await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        return first_chunk

    first_chunk = asyncio.run(read_first_chunk())

    assert "event: snapshot" in first_chunk
    assert '"selected_run_id":"run-1"' in first_chunk


def test_event_stream_stops_when_client_disconnects(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.server.get_live_state",
        lambda: {"snapshot": {"run_status": "running"}, "events": []},
    )
    monkeypatch.setattr("src.dashboard.server.latest_event_position", lambda: 0)
    monkeypatch.setattr("src.dashboard.server.read_events_since", lambda position: ([], position))

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr("src.dashboard.server.asyncio.sleep", fake_sleep)

    class DisconnectingRequest:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    async def consume_stream():
        response = await server.stream_events(DisconnectingRequest(), run_id=None)
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(consume_stream())

    assert len(chunks) == 1
    assert "event: snapshot" in chunks[0]
