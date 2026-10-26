# AI disclosure: initial structure generated with help from Claude, then reviewed and tested by the team.

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Render's filesystem is ephemeral; /tmp is writable. Files vanish on redeploy/restart.
UPLOAD_DIR = Path("/tmp/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}

app = FastAPI(title="Chat about a Photo")

# 1. CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. StaticFiles (must come after UPLOAD_DIR exists)
app.mount("/images", StaticFiles(directory=UPLOAD_DIR), name="images")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        print(f"[ws] connected    (total={len(self.active)})")

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)
        print(f"[ws] disconnected (total={len(self.active)})")

    async def broadcast(self, data: dict) -> None:
        text = json.dumps(data)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
current_photo_url: str | None = None  # lets late joiners see the current photo


@app.get("/")
def health():
    return {"status": "ok", "connections": len(manager.active)}


# 3. POST /upload-photo
@app.post("/upload-photo")
async def upload_photo(file: UploadFile = File(...)):
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        ext = ".jpg"

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    filename = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / filename).write_bytes(data)
    print(f"[upload] saved {filename} ({len(data)} bytes)")

    return {"filename": filename, "url": f"/images/{filename}", "uploaded_at": now_iso()}


# 4. GET /photos (newest first)
@app.get("/photos")
def list_photos():
    files = [p for p in UPLOAD_DIR.iterdir() if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "filename": p.name,
            "url": f"/images/{p.name}",
            "uploaded_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
        }
        for p in files
    ]


# 5. WebSocket /ws
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    global current_photo_url
    await manager.connect(ws)

    if current_photo_url:
        await ws.send_text(json.dumps({"type": "photo_selected", "url": current_photo_url}))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue

            if data.get("type") == "photo_selected" and data.get("url"):
                current_photo_url = data["url"]
            elif data.get("type") == "chat":
                data.setdefault("timestamp", now_iso())
            else:
                continue

            await manager.broadcast(data)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as exc:
        print(f"[ws] error: {exc}")
        manager.disconnect(ws)
