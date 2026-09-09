import asyncio
import json
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config.app_config import JOB_SITE
from src.dashboard.data_service import (
    get_app_config,
    get_jobs_payload,
    get_live_state,
    get_messages,
    get_run_detail,
    get_run_events,
    get_run_history,
    get_run_jobs_payload,
    get_run_screenshots,
    get_search_config,
    get_summary,
    update_app_config,
    update_search_config,
)
from src.dashboard.runtime import (
    LATEST_SCREENSHOT_FILE,
    SCREENSHOT_DIR,
    ROOT_DIR,
    _signal_process_tree,
    get_process_info,
    is_process_running,
    latest_event_position,
    read_events_since,
    read_events_since_for_run,
    request_pause,
    request_resume,
    request_stop,
    start_bot_process,
    sync_process_state,
    terminate_running_process,
)

STATIC_DIR = ROOT_DIR / "src" / "dashboard" / "static"
SITE_NAME = "LinkedIn" if JOB_SITE == "linkedin" else "Indeed"


class SearchConfigPayload(BaseModel):
    config: Dict[str, Any]


class AppConfigPayload(BaseModel):
    config: Dict[str, Any]


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    pid = get_process_info().get("pid")
    terminate_running_process()
    if pid:
        for _ in range(10):
            await asyncio.sleep(0.5)
            if not is_process_running(pid):
                break
        else:
            _signal_process_tree(pid, signal.SIGKILL)


app = FastAPI(title=f"{SITE_NAME} AI Job Applier Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(run_id: str) -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/messages", response_class=HTMLResponse)
async def messages_page() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "messages.html").read_text(encoding="utf-8"))


@app.get("/api/meta")
async def meta() -> JSONResponse:
    return JSONResponse({"site_name": SITE_NAME})


@app.get("/api/summary")
async def summary() -> JSONResponse:
    return JSONResponse(get_summary())


@app.get("/api/jobs")
async def jobs(
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> JSONResponse:
    return JSONResponse(get_jobs_payload(status=status, search=search))


@app.get("/api/messages")
async def messages(
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> JSONResponse:
    return JSONResponse({"messages": get_messages(category=category, status=status)})


@app.get("/api/live")
async def live() -> JSONResponse:
    return JSONResponse(get_live_state())


@app.get("/api/runs")
async def runs() -> JSONResponse:
    return JSONResponse({"runs": get_run_history()})


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str, limit: int = Query(default=200, ge=1, le=5000)) -> JSONResponse:
    return JSONResponse({"run_id": run_id, "events": get_run_events(run_id=run_id, limit=limit)})


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str) -> JSONResponse:
    return JSONResponse({"run_id": run_id, **get_run_detail(run_id)})


@app.get("/api/runs/{run_id}/export")
async def run_export(run_id: str) -> Response:
    payload = {"run_id": run_id, **get_run_detail(run_id)}
    return Response(
        content=json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
    )


@app.get("/api/runs/{run_id}/screenshots")
async def run_screenshots(run_id: str) -> JSONResponse:
    return JSONResponse({"run_id": run_id, "screenshots": get_run_screenshots(run_id)})


@app.get("/api/runs/{run_id}/jobs")
async def run_jobs(
    run_id: str,
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> JSONResponse:
    return JSONResponse(get_run_jobs_payload(run_id=run_id, status=status, search=search))


@app.get("/api/config")
async def config() -> JSONResponse:
    return JSONResponse({"search": get_search_config(), "app": get_app_config()})


@app.put("/api/config/search")
async def save_search_config(payload: SearchConfigPayload) -> JSONResponse:
    try:
        config = update_search_config(payload.config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"search": config})


@app.put("/api/config/app")
async def save_app_config(payload: AppConfigPayload) -> JSONResponse:
    try:
        config = update_app_config(payload.config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"app": config})


@app.post("/api/control/start")
async def start() -> JSONResponse:
    process_info = start_bot_process()
    return JSONResponse({"process": process_info, "summary": get_summary()})


@app.post("/api/control/pause")
async def pause() -> JSONResponse:
    return JSONResponse({"control": request_pause()})


@app.post("/api/control/resume")
async def resume() -> JSONResponse:
    return JSONResponse({"control": request_resume()})


@app.post("/api/control/stop")
async def stop(force: bool = Query(default=False)) -> JSONResponse:
    if force:
        terminated = terminate_running_process()
        return JSONResponse({"terminated": terminated, "summary": get_summary()})
    return JSONResponse({"control": request_stop()})


@app.get("/api/process")
async def process() -> JSONResponse:
    return JSONResponse({"process": sync_process_state(), "summary": get_summary()})


@app.get("/api/screenshot")
async def screenshot():
    if not LATEST_SCREENSHOT_FILE.exists():
        return JSONResponse({"available": False}, status_code=404)
    return Response(content=LATEST_SCREENSHOT_FILE.read_bytes(), media_type="image/png")


@app.get("/api/screenshot-file")
async def screenshot_file(path: str = Query(...)):
    requested_path = Path(path)
    if requested_path.is_absolute():
        raise HTTPException(status_code=400, detail="Invalid screenshot path")

    file_path = (ROOT_DIR / requested_path).resolve()
    try:
        file_path.relative_to(SCREENSHOT_DIR.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid screenshot path") from exc

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return Response(content=file_path.read_bytes(), media_type="image/png")


@app.get("/api/events/stream")
async def stream_events(
    request: Request, run_id: str | None = Query(default=None)
) -> StreamingResponse:
    async def event_generator():
        initial = get_live_state()
        if run_id:
            initial = {
                **initial,
                "events": get_run_events(run_id=run_id, limit=120),
                "selected_run_id": run_id,
            }
        yield f"event: snapshot\ndata: {JSONResponse(content=initial).body.decode('utf-8')}\n\n"

        position = latest_event_position()
        while True:
            if await request.is_disconnected():
                break
            if run_id:
                events, position = read_events_since_for_run(position, run_id)
            else:
                events, position = read_events_since(position)
            for event in events:
                yield f"event: message\ndata: {JSONResponse(content=event).body.decode('utf-8')}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
