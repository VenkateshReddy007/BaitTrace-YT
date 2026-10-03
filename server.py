import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

app = FastAPI(title="BaitTrace Command Center")

VENV_PYTHON = Path("venv/Scripts/python.exe")
PYTHON_EXEC = str(VENV_PYTHON.resolve()) if VENV_PYTHON.exists() else sys.executable

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("FATAL: SUPABASE_URL and SUPABASE_KEY must be set in environment or .env file.")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

class ScanRequest(BaseModel):
    query_type: str
    limit: int
    max_comments: int

class SchedulerStartRequest(BaseModel):
    interval_minutes: int = 180

class ConnectionManager:
    def __init__(self): self.active_connections: list[WebSocket] = []
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections: self.active_connections.remove(websocket)
    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try: await connection.send_text(message)
            except Exception: self.disconnect(connection)

manager = ConnectionManager()

# --- Scheduler process state ---
_scheduler_process: subprocess.Popen | None = None
_scheduler_interval: int = 0
_scheduler_streaming_task: asyncio.Task | None = None

async def stream_process(cmd: list[str]):
    loop = asyncio.get_running_loop()
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    while True:
        line = await loop.run_in_executor(None, process.stdout.readline)
        if not line and process.poll() is not None: break
        if line: await manager.broadcast(line)
    await loop.run_in_executor(None, process.wait)

async def run_pipeline(scan_req: ScanRequest):
    cmd_main = [
        PYTHON_EXEC, "-u", "main.py", "--once",
        "--limit", str(scan_req.limit), "--max-comments", str(scan_req.max_comments),
        "--query-type", scan_req.query_type
    ]
    await manager.broadcast(f">>> EXECUTING: {' '.join(cmd_main)}\n")
    await stream_process(cmd_main)
    
    cmd_enrich = [PYTHON_EXEC, "-u", "enricher.py"]
    await manager.broadcast(f"\n>>> EXECUTING: {' '.join(cmd_enrich)}\n")
    await stream_process(cmd_enrich)
    await manager.broadcast("\n>>> SWEEP SEQUENCE COMPLETE\n")

async def _stream_scheduler():
    """Stream the scheduler subprocess stdout to the websocket."""
    global _scheduler_process
    if _scheduler_process is None:
        return
    loop = asyncio.get_running_loop()
    try:
        while _scheduler_process and _scheduler_process.poll() is None:
            line = await loop.run_in_executor(None, _scheduler_process.stdout.readline)
            if not line:
                if _scheduler_process.poll() is not None:
                    break
                continue
            await manager.broadcast(line)
    except Exception:
        pass
    finally:
        await manager.broadcast("\n>>> SCHEDULER PROCESS ENDED\n")

@app.websocket("/ws/terminal")
async def websocket_terminal(websocket: WebSocket):
    await manager.connect(websocket)
    await websocket.send_text("📡 Uplink established. Live stream ready.\n")
    try:
        while True: await websocket.receive_text()
    except Exception: manager.disconnect(websocket)

@app.post("/api/scan")
async def trigger_scan(scan_req: ScanRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(run_pipeline, scan_req)
    return {"status": "Sweep Initiated"}

# --- Scheduler Endpoints ---

@app.post("/api/scheduler/start")
async def scheduler_start(req: SchedulerStartRequest, background_tasks: BackgroundTasks):
    global _scheduler_process, _scheduler_interval, _scheduler_streaming_task

    # Reject if already running
    if _scheduler_process is not None and _scheduler_process.poll() is None:
        return JSONResponse(
            status_code=409,
            content={"error": "Scheduler is already running. Stop it first before starting a new one."}
        )

    cmd = [
        PYTHON_EXEC, "-u", "scheduler.py",
        "--interval-minutes", str(req.interval_minutes)
    ]
    await manager.broadcast(f"\n>>> STARTING SCHEDULER: {' '.join(cmd)}\n")

    _scheduler_process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    _scheduler_interval = req.interval_minutes

    # Stream scheduler output to the websocket in background
    _scheduler_streaming_task = asyncio.create_task(_stream_scheduler())

    return {"status": "Scheduler started", "interval_minutes": req.interval_minutes, "pid": _scheduler_process.pid}


@app.post("/api/scheduler/stop")
async def scheduler_stop():
    global _scheduler_process, _scheduler_interval, _scheduler_streaming_task

    if _scheduler_process is None or _scheduler_process.poll() is not None:
        return JSONResponse(
            status_code=404,
            content={"error": "No scheduler process is currently running."}
        )

    pid = _scheduler_process.pid
    await manager.broadcast(f"\n>>> STOPPING SCHEDULER (PID {pid}) — sending SIGINT for graceful shutdown...\n")

    # Send CTRL_C_EVENT on Windows, SIGINT on Unix
    try:
        if sys.platform == "win32":
            # On Windows, we need to terminate since CTRL_C_EVENT doesn't work reliably
            # for child processes not sharing a console group
            _scheduler_process.terminate()
        else:
            _scheduler_process.send_signal(signal.SIGINT)
    except OSError:
        pass

    # Wait briefly for graceful shutdown, then force-kill if needed
    try:
        _scheduler_process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _scheduler_process.kill()
        _scheduler_process.wait()

    _scheduler_process = None
    _scheduler_interval = 0

    if _scheduler_streaming_task and not _scheduler_streaming_task.done():
        _scheduler_streaming_task.cancel()
    _scheduler_streaming_task = None

    return {"status": "Scheduler stopped", "pid": pid}


@app.get("/api/scheduler/status")
async def scheduler_status():
    global _scheduler_process, _scheduler_interval

    if _scheduler_process is not None and _scheduler_process.poll() is not None:
        # Process has exited — clean up stale reference
        _scheduler_process = None
        _scheduler_interval = 0

    running = _scheduler_process is not None
    return {
        "running": running,
        "interval_minutes": _scheduler_interval if running else 0,
        "pid": _scheduler_process.pid if running else None,
    }


@app.get("/api/config/defaults")
def get_config_defaults():
    """Expose config defaults so the dashboard can read them."""
    from config import DEFAULT_SWEEP_INTERVAL_MINUTES
    return {"default_sweep_interval_minutes": DEFAULT_SWEEP_INTERVAL_MINUTES}


@app.get("/api/metrics")
def get_metrics():
    try:
        v = supabase.table("seen_videos").select("video_id", count="exact").execute().count or 0
        c = supabase.table("seen_comments").select("comment_id", count="exact").execute().count or 0
        s = 0
        try:
            s = supabase.table("candidate_sightings").select("id", count="exact").execute().count or 0
        except Exception:
            pass
        l = 0
        try:
            l = supabase.table("actionable_leads").select("*", count="exact").execute().count or 0
        except Exception:
            pass
        return {"videos": v, "comments": c, "sightings": s, "leads": l}
    except Exception:
        return {"videos": 0, "comments": 0, "sightings": 0, "leads": 0}

@app.get("/api/leads")
def get_leads():
    try:
        # First attempt to fetch from actionable_leads
        res = supabase.table("actionable_leads").select("*").order("campaign_score", desc=True).limit(50).execute()
        if res.data:
            return {"leads": res.data}
        # Fallback to enriched_leads
        res_old = supabase.table("enriched_leads").select("*").order("enriched_at", desc=True).limit(50).execute()
        return {"leads": res_old.data or []}
    except Exception:
        return {"leads": []}

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f: return f.read()