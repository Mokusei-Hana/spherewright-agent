"""FastAPI application for the Spherewright Agent Web Console."""

import asyncio
import json
import os
import platform
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from ..storage import Ledger, redact
from ..cli import load_config


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class TaskCreate(BaseModel):
    goal: str


class WriteToggle(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_config(app):
    """Load and return the validated config from the stored path."""
    return load_config(app.state.config_path)


def _open_ledger(config):
    """Open a Ledger instance for per-request use (no writer lock)."""
    return Ledger(config["agent"]["database"])


async def _poll_ledger_events(app):
    """Background task pushing new events and status to active WebSocket clients."""
    last_event_id = 0
    while True:
        try:
            await asyncio.sleep(1.0)
            if not app.state.ws_clients:
                continue

            config = _get_config(app)
            db_path = config["agent"]["database"]
            if not Path(db_path).exists():
                continue

            conn = sqlite3.connect(db_path)
            try:
                if last_event_id == 0:
                    row = conn.execute("SELECT MAX(id) FROM events").fetchone()
                    last_event_id = row[0] or 0
                else:
                    rows = conn.execute(
                        "SELECT id, kind, data FROM events WHERE id > ? ORDER BY id ASC LIMIT 20",
                        (last_event_id,),
                    ).fetchall()
                    for r_id, r_kind, r_data in rows:
                        last_event_id = r_id
                        try:
                            parsed = json.loads(r_data) if r_data else {}
                        except (json.JSONDecodeError, TypeError):
                            parsed = {"raw": r_data}
                        msg = {
                            "type": "event",
                            "kind": r_kind,
                            "event": {"id": r_id, "kind": r_kind, "data": redact(parsed)},
                        }
                        for ws in list(app.state.ws_clients):
                            try:
                                await ws.send_json(msg)
                            except Exception:
                                pass
            finally:
                conn.close()
        except asyncio.CancelledError:
            break
        except Exception:
            await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_path: str = "config.toml") -> FastAPI:
    app = FastAPI(title="Spherewright Agent Console", version="0.1.0")

    # -- state ---------------------------------------------------------------
    app.state.config_path = config_path
    app.state.write_enabled = False
    app.state.emergency_stop = False
    app.state.ws_clients: list[WebSocket] = []

    # -- CORS ----------------------------------------------------------------
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -----------------------------------------------------------------------
    # Lifespan background task
    # -----------------------------------------------------------------------

    @app.on_event("startup")
    async def startup_event():
        asyncio.create_task(_poll_ledger_events(app))

    # -----------------------------------------------------------------------
    # REST endpoints
    # -----------------------------------------------------------------------

    @app.get("/api/status")
    async def api_status():
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)

        ledger = _open_ledger(config)
        try:
            pending = redact(ledger.pending())
            tasks = ledger.tasks()
        finally:
            ledger.db.close()

        pause_exists = Path(config["agent"]["pause_file"]).exists()

        # Doctor diagnosis info
        doctor = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "database": config["agent"]["database"],
            "provider_key_present": bool(os.environ.get(config["provider"]["api_key_env"])),
            "mcp_command": config["mcp"]["command"],
        }

        # Extract latest observation / planet / icarus info if available in ledger
        planet_info = None
        icarus_info = None
        mcp_status = None
        try:
            conn = sqlite3.connect(config["agent"]["database"])
            try:
                row = conn.execute(
                    "SELECT data FROM events WHERE kind IN ('observation', 'entry', 'system_observation') ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    latest = json.loads(row[0])
                    if "status" in latest:
                        mcp_status = latest["status"]
                    if "player" in latest:
                        p = latest["player"]
                        pos = p.get("position", {})
                        core_e = p.get("coreEnergy")
                        cap_e = p.get("coreCapacity")
                        icarus_info = {
                            "isAlive": p.get("isAlive"),
                            "isOnPlanet": p.get("isOnPlanet"),
                            "isFlying": p.get("isFlying"),
                            "isSailing": p.get("isSailing"),
                            "position": pos,
                            "coreEnergy": core_e,
                            "coreCapacity": cap_e,
                            "coreEnergyRatio": round(core_e / cap_e, 4) if core_e and cap_e else None,
                            "inventoryCount": len(p.get("inventory", [])),
                        }
                    if "session" in latest:
                        s = latest["session"]
                        planet_info = {
                            "sessionId": s.get("sessionId"),
                            "localPlanetId": s.get("localPlanetId"),
                            "gameLoaded": s.get("gameLoaded"),
                            "ownedBySpherewright": s.get("ownedBySpherewright"),
                            "peacefulMode": s.get("peacefulMode"),
                            "writeHealth": s.get("writeHealth"),
                        }
            finally:
                conn.close()
        except Exception:
            pass

        return {
            "agent_version": "0.1.0",
            "doctor": doctor,
            "python": doctor["python"],
            "platform": doctor["platform"],
            "database": doctor["database"],
            "provider_type": config["provider"]["type"],
            "provider_model": config["provider"]["model"],
            "mcp_command": doctor["mcp_command"],
            "mcp_status": mcp_status,
            "provider_key_present": doctor["provider_key_present"],
            "allow_write": config["agent"]["allow_write"],
            "write_enabled": app.state.write_enabled,
            "emergency_stop": app.state.emergency_stop,
            "pause_file_exists": pause_exists,
            "planet": planet_info,
            "icarus": icarus_info,
            "pending": pending,
            "task_count": len(tasks),
        }

    @app.get("/api/ledger")
    async def api_ledger(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        status: str | None = Query(None),
    ):
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)

        db_path = config["agent"]["database"]
        conn = sqlite3.connect(db_path)
        try:
            filter_kinds = None
            if status:
                s = status.strip().lower()
                if s == "pending":
                    filter_kinds = ["prepare_intent", "pending"]
                elif s == "committed":
                    filter_kinds = ["prepare", "action", "commit_intent"]
                elif s == "verified":
                    filter_kinds = ["verified"]
                elif s == "failed":
                    filter_kinds = ["failed", "error"]
                else:
                    filter_kinds = [status.strip()]

            if filter_kinds:
                placeholders = ",".join("?" for _ in filter_kinds)
                total_row = conn.execute(
                    f"SELECT COUNT(*) FROM events WHERE kind IN ({placeholders})", filter_kinds
                ).fetchone()
                total = total_row[0] if total_row else 0
                rows = conn.execute(
                    f"SELECT id, kind, data FROM events WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT ? OFFSET ?",
                    (*filter_kinds, page_size, (page - 1) * page_size),
                ).fetchall()
            else:
                total_row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
                total = total_row[0] if total_row else 0
                rows = conn.execute(
                    "SELECT id, kind, data FROM events ORDER BY id DESC LIMIT ? OFFSET ?",
                    (page_size, (page - 1) * page_size),
                ).fetchall()
        except sqlite3.OperationalError:
            return {"events": [], "total": 0, "page": page, "page_size": page_size}
        finally:
            conn.close()

        events = []
        for row in rows:
            try:
                data = json.loads(row[2]) if row[2] else {}
            except (json.JSONDecodeError, TypeError):
                data = {"raw": row[2]}
            events.append({"id": row[0], "kind": row[1], "data": redact(data)})

        return {
            "events": events,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/tasks")
    async def api_tasks():
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)
        ledger = _open_ledger(config)
        try:
            tasks = [redact(t) for t in ledger.tasks()]
        finally:
            ledger.db.close()
        return {"tasks": tasks}

    @app.get("/api/tasks/{task_id}")
    async def api_get_task(task_id: str):
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)
        ledger = _open_ledger(config)
        try:
            task = ledger.task(task_id)
            return {"task": redact(task)}
        except ValueError:
            return JSONResponse({"error": "Task not found"}, 404)
        finally:
            ledger.db.close()

    @app.post("/api/tasks")
    async def api_create_task(body: TaskCreate):
        if not app.state.write_enabled:
            return JSONResponse(
                {"error": "Write mode is not enabled. Toggle write mode first."},
                status_code=403,
            )
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)
        ledger = _open_ledger(config)
        try:
            task = ledger.create_task(body.goal, config)
        finally:
            ledger.db.close()
        return {"task": redact(task)}

    @app.post("/api/tasks/{task_id}/pause")
    async def api_pause_task(task_id: str):
        if not app.state.write_enabled:
            return JSONResponse({"error": "Write mode not enabled"}, 403)
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)
        ledger = _open_ledger(config)
        try:
            task = ledger.task(task_id)
            if task["state"] in ("running", "waiting_confirmation"):
                task["state"] = "paused"
                task["detail"] = "Paused via Web Console"
                ledger.save_task(task)
            return {"task": redact(task)}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
        finally:
            ledger.db.close()

    @app.post("/api/tasks/{task_id}/resume")
    async def api_resume_task(task_id: str):
        if not app.state.write_enabled:
            return JSONResponse({"error": "Write mode not enabled"}, 403)
        try:
            config = _get_config(app)
        except Exception as exc:
            return JSONResponse({"error": f"Config load failed: {type(exc).__name__}"}, 500)
        ledger = _open_ledger(config)
        try:
            task = ledger.task(task_id)
            if task["state"] in ("paused", "blocked", "failed"):
                task["state"] = "pending"
                task["detail"] = "Resumed via Web Console"
                ledger.save_task(task)
            return {"task": redact(task)}
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 400)
        finally:
            ledger.db.close()

    @app.post("/api/write-toggle")
    async def api_write_toggle(body: WriteToggle):
        app.state.write_enabled = body.enabled
        return {"write_enabled": app.state.write_enabled}

    @app.post("/api/emergency-stop")
    async def api_emergency_stop():
        app.state.emergency_stop = True
        try:
            config = _get_config(app)
            pause_path = Path(config["agent"]["pause_file"])
            pause_path.touch()
        except Exception:
            pass  # Best effort
        for ws in list(app.state.ws_clients):
            try:
                await ws.send_json({"type": "emergency_stop", "message": "紧急停止已激活 / Emergency stop activated"})
            except Exception:
                pass
        return {"stopped": True}

    @app.post("/api/emergency-stop/reset")
    async def api_emergency_stop_reset():
        app.state.emergency_stop = False
        try:
            config = _get_config(app)
            pause_path = Path(config["agent"]["pause_file"])
            if pause_path.exists():
                pause_path.unlink()
        except Exception:
            pass
        for ws in list(app.state.ws_clients):
            try:
                await ws.send_json({"type": "emergency_reset", "message": "紧急停止已解除 / Emergency stop cleared"})
            except Exception:
                pass
        return {"stopped": False}

    # -----------------------------------------------------------------------
    # WebSocket
    # -----------------------------------------------------------------------

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        await ws.accept()
        app.state.ws_clients.append(ws)
        try:
            await ws.send_json({"type": "connected", "message": "WebSocket connected to Spherewright Agent Console"})
            while True:
                data = await ws.receive_text()
                await ws.send_json({"type": "ack", "received": data})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            if ws in app.state.ws_clients:
                app.state.ws_clients.remove(ws)

    # -----------------------------------------------------------------------
    # Static file serving (SPA catch-all, must come last)
    # -----------------------------------------------------------------------

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

    return app
