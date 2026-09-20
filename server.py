import asyncio
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
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

@app.get("/api/metrics")
def get_metrics():
    try:
        v = supabase.table("seen_videos").select("video_id", count="exact").execute().count
        c = supabase.table("seen_comments").select("comment_id", count="exact").execute().count
        l = supabase.table("actionable_leads").select("id", count="exact").execute().count
        return {"videos": v, "comments": c, "leads": l}
    except Exception: return {"videos": 0, "comments": 0, "leads": 0}

@app.get("/api/leads")
def get_leads():
    try:
        res = supabase.table("enriched_leads").select("*").order("enriched_at", desc=True).limit(50).execute()
        return {"leads": res.data}
    except Exception:
        return {"leads": []}

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    with open("dashboard.html", "r", encoding="utf-8") as f: return f.read()