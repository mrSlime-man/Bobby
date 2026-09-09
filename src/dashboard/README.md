# Dashboard Guide

## Overview

The dashboard is a local FastAPI web application that wraps the existing bot runtime with:

- live telemetry
- run history
- screenshot history
- config editing
- bot lifecycle controls
- per-run JSON export

Start it with:

```bash
uv run python dashboard.py
```

Default address:

```text
http://127.0.0.1:8000
```

## What The Dashboard Shows

### Live Monitoring

- run status
- active job and current stage
- counters for discovered, evaluated, interesting, applied, skipped, and failed jobs
- recent structured event timeline
- latest browser screenshot

### Historical Inspection

- run history cards
- deep-linkable run pages at `/runs/<run_id>`
- per-run jobs reconstructed from structured events
- per-run screenshot history
- per-run event stream
- per-run JSON export

### Controls

- Start Run
- Pause
- Resume
- Stop

Pause and stop are file-backed control signals. The bot checks them during execution and stops gracefully when possible.

## Architecture

### Entry Point

- `dashboard.py` starts the FastAPI app from `src/dashboard/server.py`

### Backend Modules

- `src/dashboard/server.py`
  HTTP routes, SSE stream, screenshot file serving
- `src/dashboard/data_service.py`
  Aggregates persisted YAML data, dashboard runtime files, run history, and run details
- `src/dashboard/runtime.py`
  File-backed snapshot, events, control state, process state, and screenshot indexing

### Frontend Assets

- `src/dashboard/static/index.html`
- `src/dashboard/static/app.js`
- `src/dashboard/static/styles.css`

## Runtime Data Files

The dashboard writes its own state into `data/output/dashboard/`.

### Core Files

- `events.jsonl`
  Structured append-only event log for all runs
- `snapshot.json`
  Latest known runtime snapshot
- `control.json`
  Pause and stop requests from the UI
- `process.json`
  Metadata about the background bot process started by the dashboard
- `screenshots.json`
  Screenshot metadata index used for historical browsing

### Screenshot Files

- `screenshots/latest.png`
  Latest screenshot shown in the live panel
- `screenshots/<run_id>/...`
  Archived screenshots grouped by run

## How Live Tracking Works

The bot emits structured events from the existing automation flow, including:

- run started/completed/failed/stopped
- search started/configured
- page changed
- jobs discovered
- job loaded
- evaluation started/completed
- application started
- Easy Apply progress steps
- job result
- screenshot updated

The dashboard consumes those events in two ways:

- a snapshot file for the latest live state
- an SSE stream for browser updates without polling-heavy refreshes

## Supported UI Workflows

### Live Run Workflow

1. Open `/`
2. Click `Start Run`
3. Watch counters, timeline, and latest screenshot update live
4. Use `Pause`, `Resume`, or `Stop` as needed

### Historical Run Workflow

1. Open `/`
2. Select a run card from `Run History`
3. Inspect run summary, jobs, events, and screenshot history
4. Export the run with `Export JSON`

### Direct Run Link Workflow

1. Open `/runs/<run_id>` directly
2. The dashboard loads that run context automatically

## Routes

### Pages

- `/`
- `/runs/<run_id>`

### APIs

- `/api/summary`
- `/api/live`
- `/api/jobs`
- `/api/runs`
- `/api/runs/<run_id>`
- `/api/runs/<run_id>/jobs`
- `/api/runs/<run_id>/events`
- `/api/runs/<run_id>/screenshots`
- `/api/runs/<run_id>/export`
- `/api/events/stream`
- `/api/control/start`
- `/api/control/pause`
- `/api/control/resume`
- `/api/control/stop`
- `/api/config`
- `/api/config/search`
- `/api/config/app`
- `/api/screenshot`
- `/api/screenshot-file?path=...`

## Config Editing

The dashboard supports editing:

- `config/search_config.yaml`
- a selected safe subset of `config/app_config.py`

It does not edit `.env` secrets.

The search config editor normalizes the date-posted key so `24_hours` works correctly in the UI even though the internal Pydantic model uses `day_24_hours`.

## Notes And Limitations

- The dashboard is local-first and unauthenticated by default.
- Screenshot history is only available for runs started after the dashboard screenshot archive feature was added.
- The latest screenshot panel shows the newest captured image, while historical screenshots are grouped by run.
- The separate non-Easy Apply agent emits events to the dashboard timeline, but screenshot capture is strongest in the Playwright-driven Easy Apply path.

## Troubleshooting

### Dashboard Starts But No Data Appears

- verify you opened `http://127.0.0.1:8000`
- check that `data/output/dashboard/` is being created
- if you started the bot outside the dashboard, historical YAML data will still show, but live process metadata may be absent

### Run Control Buttons Do Not Affect The Bot

- the dashboard controls only affect runs launched through `/api/control/start` or the `Start Run` button
- if another process started `main.py` manually, the dashboard can still inspect persisted output but is not managing that process

### Screenshot History Is Empty

- screenshot history is created only when the bot reaches instrumented screenshot capture points
- runs from before this feature was added will not have archived screenshots

### Need Raw Data For Debugging

- export the run from the UI
- inspect `data/output/dashboard/events.jsonl`
- inspect `data/output/dashboard/screenshots.json`

## Related Files

- `dashboard.py`
- `src/dashboard/server.py`
- `src/dashboard/data_service.py`
- `src/dashboard/runtime.py`
- `src/dashboard/static/index.html`
- `src/dashboard/static/app.js`
- `src/dashboard/static/styles.css`
