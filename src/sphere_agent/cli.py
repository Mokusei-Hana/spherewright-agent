import argparse
import asyncio
import json
import math
import os
import platform
import tomllib
from pathlib import Path

from .bridge import connect
from .engine import Engine
from .provider import CompatibleProvider
from .storage import Ledger, redact, writer_lock


def load_config(path):
    with open(path, "rb") as stream:
        config = tomllib.load(stream)
    agent = config["agent"]
    defaults = {"max_material_items": 100, "max_read_retries": 2, "max_action_polls": 120,
                "max_harvest_yield": 10, "max_harvest_distance": 12, "max_move_distance": 12,
                "max_move_energy": 1000000, "min_core_energy": 100000,
                "max_build_objects": 12, "max_build_distance": 16, "max_belt_length": 20,
                "max_grid_snap_distance": 3, "geometry_tolerance": 0.25, "max_transfer_items": 100,
                "max_container_items": 10000, "build_item_ids": [2201, 2203, 2301, 2101, 2102, 2106, 2302, 2303],
                "allow_remote_flight": False, "remote_authorization_file": "remote-authorization.json",
                "max_remote_distance": 100000000, "max_remote_game_ticks": 36000,
                "return_reserve_game_ticks": 18000, "remote_min_core_ratio": 0.95,
                "extra_warper_reserve": 1, "max_warp_game_ticks": 1800, "max_arrival_offset": 3000000,
                "min_return_energy": 1000000, "remote_wall_seconds": 600}
    caps = {"max_steps": 100, "max_writes": 20, "max_recipe_executions": 1000,
            "max_material_items": 10000, "max_read_retries": 5, "max_action_polls": 600,
            "max_harvest_yield": 100, "max_harvest_distance": 16, "max_move_distance": 16,
            "max_move_energy": 100000000, "min_core_energy": 1000000000,
            "poll_seconds": 60, "action_timeout_seconds": 600}
    caps.update(max_build_objects=32, max_build_distance=30, max_belt_length=30, max_grid_snap_distance=3,
                geometry_tolerance=0.5, max_transfer_items=10000, max_container_items=100000,
                max_remote_distance=1000000000000, max_remote_game_ticks=108000, return_reserve_game_ticks=104400,
                remote_min_core_ratio=1, extra_warper_reserve=18, max_warp_game_ticks=108000,
                max_arrival_offset=10000000, min_return_energy=1000000000000, remote_wall_seconds=86400)
    for key, value in defaults.items():
        agent.setdefault(key, value)
    for key, cap in caps.items():
        value = agent[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= cap:
            raise ValueError(f"agent.{key} must be finite, positive and at most {cap}")
    for key in ("max_material_items", "max_read_retries", "max_action_polls", "max_harvest_yield"):
        if type(agent[key]) is not int:
            raise ValueError(f"agent.{key} must be an integer")
    for key in ("max_build_objects", "max_transfer_items", "max_container_items", "max_remote_game_ticks",
                "return_reserve_game_ticks", "extra_warper_reserve", "max_warp_game_ticks", "remote_wall_seconds"):
        if type(agent[key]) is not int:
            raise ValueError(f"agent.{key} must be an integer")
    if not (7200 <= agent["max_remote_game_ticks"] and 3600 <= agent["return_reserve_game_ticks"]
            <= agent["max_remote_game_ticks"] - 3600 and agent["remote_min_core_ratio"] >= 0.8
            and agent["remote_wall_seconds"] >= 600):
        raise ValueError("Remote round-trip/return/energy/wall budgets violate native limits")
    if type(agent["allow_remote_flight"]) is not bool:
        raise ValueError("agent.allow_remote_flight must be boolean")
    if not isinstance(agent["build_item_ids"], list) or not agent["build_item_ids"] or any(
        type(value) is not int or value not in (2201, 2203, 2301, 2101, 2102, 2106, 2302, 2303)
        for value in agent["build_item_ids"]
    ):
        raise ValueError("build_item_ids must be a nonempty subset of the supported simple buildings")
    for key in ("max_steps", "max_writes", "max_recipe_executions", "poll_seconds", "action_timeout_seconds"):
        if (
            not isinstance(agent[key], (int, float))
            or isinstance(agent[key], bool)
            or not math.isfinite(agent[key])
            or agent[key] <= 0
        ):
            raise ValueError(f"agent.{key} must be positive")
    for key in ("max_steps", "max_writes", "max_recipe_executions"):
        if type(agent[key]) is not int:
            raise ValueError(f"agent.{key} must be an integer")
    if type(agent["allow_write"]) is not bool:
        raise ValueError("agent.allow_write must be boolean")
    if agent["max_recipe_executions"] > 1000:
        raise ValueError("Native handcraft limit is 1000 executions")
    for section in ("provider", "mcp"):
        timeout = config[section]["timeout_seconds"]
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError(f"{section}.timeout_seconds must be finite and within 0..600")
    provider = config["provider"]
    provider.setdefault("context_bytes", 64000)
    if type(provider["context_bytes"]) is not int or not 4096 <= provider["context_bytes"] <= 1000000:
        raise ValueError("provider.context_bytes must be 4096..1000000 UTF-8 bytes")
    if provider["type"] != "openai-compatible":
        raise ValueError("Only openai-compatible Provider is implemented")
    for key in ("base_url", "model", "api_key_env"):
        if not isinstance(provider[key], str) or not provider[key].strip():
            raise ValueError(f"provider.{key} must be nonempty")
    if type(provider["max_tokens"]) is not int or provider["max_tokens"] <= 0:
        raise ValueError("provider.max_tokens must be a positive integer")
    if not isinstance(config["mcp"]["command"], str) or not config["mcp"]["command"].strip():
        raise ValueError("mcp.command must be nonempty")
    if not isinstance(config["mcp"]["args"], list) or any(type(a) is not str for a in config["mcp"]["args"]):
        raise ValueError("mcp.args must be an array of strings")
    base = Path(path).resolve().parent
    for key in ("database", "pause_file", "remote_authorization_file"):
        agent[key] = str((base / agent[key]).resolve())
    return config


async def execute(args, config):
    if args.command == "check":
        print("Configuration valid. No model or game connection attempted.")
        return
    with writer_lock(config["agent"]["database"]):
        ledger = Ledger(config["agent"]["database"])
        task = None
        try:
            if args.command in ("new", "create"):
                print(json.dumps(ledger.create_task(args.goal, config), ensure_ascii=False, indent=2))
                return
            if args.command == "status":
                print(json.dumps({"tasks": [ledger.task(args.task_id)] if args.task_id else ledger.tasks(),
                                  "pending": ledger.pending()}, ensure_ascii=False, indent=2))
                return
            if args.command in ("run", "resume", "action"):
                if args.command == "resume":
                    if not args.task_id:
                        raise ValueError("resume requires --task-id")
                    task = ledger.task(args.task_id)
                    # Config switching is persisted even with an unresolved action. Retain
                    # original transport identity and finite budgets for reconciliation.
                    task["config"]["provider"] = redact(config["provider"])
                    ledger.save_task(task)
                    config = task["config"]
                    if task["state"] == "completed":
                        print(json.dumps(task, ensure_ascii=False, indent=2))
                        return
                else:
                    if ledger.pending():
                        raise RuntimeError("Unresolved intent: resume/reconcile original action first")
                    if args.command == "action" and args.task_id:
                        raise ValueError("Direct action creates its own bounded task; use resume for existing actions")
                    task = ledger.task(args.task_id) if args.task_id else ledger.create_task(args.goal, config)
                    if task["state"] == "completed" or task["state"] == "failed":
                        raise RuntimeError("Terminal tasks cannot run again; create a new task")
                    # All original task budgets survive both process restart and config edits.
                    task["config"]["provider"] = redact(config["provider"])
                    config = task["config"]
                    if task["state"] in ("running", "waiting_confirmation", "blocked"):
                        raise RuntimeError("Interrupted running task: use resume first")
                # Old A-D tasks gain safe new adapter defaults without relaxing original budgets.
                for key, value in load_config(args.config)["agent"].items():
                    task["config"]["agent"].setdefault(key, value)
                ledger.save_task(task)
            if args.command == "ledger":
                print(json.dumps(ledger.pending(), ensure_ascii=False, indent=2))
                return
            if args.command in ("doctor", "diagnose"):
                print(json.dumps({"python": platform.python_version(), "platform": platform.system(),
                    "database": config["agent"]["database"], "pending": redact(ledger.pending()),
                    "provider_key_present": bool(os.environ.get(config["provider"]["api_key_env"])),
                    "mcp_command": config["mcp"]["command"]}, ensure_ascii=False, indent=2))
            async with connect(config["mcp"]) as bridge:
                engine = Engine(bridge, ledger, config["agent"], args.enable_write, task)
                if args.command == "run":
                    engine.state("running")
                    await engine.run(CompatibleProvider(config["provider"]), task["goal"])
                elif args.command == "action":
                    if not args.action or args.arguments is None:
                        raise ValueError("action requires --action NAME and --arguments JSON object")
                    arguments = json.loads(args.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("--arguments must be a JSON object")
                    engine.state("running")
                    task["steps"] += 1
                    ledger.save_task(task)
                    await engine.enter()
                    result = await engine.action(args.action, arguments)
                    engine.state("completed")
                    print(json.dumps(redact({"task_id": task["id"], "result": result}), ensure_ascii=False, indent=2))
                elif args.command in ("factory", "inspect", "quota", "route", "system"):
                    await engine.enter()
                    observation, identity = await engine.observe()
                    if args.command == "system":
                        result = await engine.system(identity)
                    elif args.command == "factory":
                        result = await engine.read("spherewright_list_factory_entities", {**identity, "objectKind": "entity", "limit": 50})
                    elif args.command == "route":
                        from .remote import route_preview
                        result, _, _ = await route_preview(engine, identity, args.destination_planet_id, args.destination_star_id, args.journey_id)
                    else:
                        from .factory import inspect
                        endpoint = await inspect(engine, identity, args.object_id)
                        result = endpoint
                        if args.command == "quota":
                            if not engine.allow_write or engine.paused():
                                raise RuntimeError("Quota changes require --enable-write, allow_write=true and no PAUSE")
                            if args.max_items is None or not 0 <= args.max_items <= config["agent"]["max_container_items"]:
                                raise ValueError("--max-items must fit local container quota budget")
                            if endpoint["componentKind"] not in ("storage", "tank"):
                                raise ValueError("Quota requires exact local box or tank")
                            if endpoint["componentKind"] == "tank" and type(endpoint.get("tankFluidCount")) is not int:
                                raise RuntimeError("Tank stock unknown; cannot admit a quota")
                            if endpoint["componentKind"] == "tank" and endpoint["tankFluidCount"] > args.max_items:
                                raise RuntimeError("Current tank stock already exceeds proposed quota")
                            scope = await engine.world_scope(identity)
                            result = ledger.set_container_quota(scope, endpoint["objectId"], args.max_items, endpoint)
                    print(json.dumps(redact(result), ensure_ascii=False, indent=2))
                elif args.command == "resume":
                    pending = ledger.pending()
                    if pending:
                        if pending.get("task_id") != task["id"]:
                            raise RuntimeError("Pending intent belongs to another task")
                        task["writes"] = max(task["writes"], pending.get("reserved_writes", 0))
                        task["materials"] = max(task["materials"], pending.get("reserved_materials", 0))
                        ledger.save_task(task)
                        result = await engine.reconcile()
                    else:
                        result = {"task": task["id"], "pending": None}
                    engine.state("paused", "Recovery inspected original state only; explicit run starts new decisions")
                    print(json.dumps(redact(result), ensure_ascii=False, indent=2))
                elif args.command in ("doctor", "diagnose"):
                    status = await engine.read("spherewright_get_status", {})
                    session = await engine.read("spherewright_get_session_state", {})
                    journal = await engine.read("spherewright_get_gameplay_journal", {"sessionId": session["sessionId"]}) if session.get("ownedBySpherewright") else None
                    print(json.dumps(redact({"python": platform.python_version(), "platform": platform.system(),
                        "provider_key_present": bool(os.environ.get(config["provider"]["api_key_env"])),
                        "mcp_command": config["mcp"]["command"], "advertised_tools": sorted(bridge.schemas),
                        "status": status, "journal": journal, "pending": ledger.pending()}), ensure_ascii=False, indent=2))
                else:
                    await engine.enter()
                    if args.command == "reconcile":
                        result = await engine.reconcile()
                    else:
                        result, _ = await engine.observe()
                    print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        except BaseException as exc:
            if task and task["state"] != "completed":
                task["state"] = "blocked" if ledger.pending() else "paused" if task["state"] == "paused" or isinstance(exc, KeyboardInterrupt) or Path(config["agent"]["pause_file"]).exists() else "failed"
                task["detail"] = type(exc).__name__
                ledger.save_task(task)
            raise
        finally:
            ledger.db.close()


def main():
    parser = argparse.ArgumentParser(description="Bounded Spherewright Python agent")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--enable-write", "--allow-write", dest="enable_write", action="store_true")
    parser.add_argument("command", choices=["check", "observe", "run", "reconcile", "ledger", "new", "create", "status", "resume", "doctor", "diagnose", "action", "factory", "inspect", "quota", "route", "system", "serve"])
    parser.add_argument("--action")
    parser.add_argument("--arguments")
    parser.add_argument("--object-id", type=int)
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--destination-planet-id", type=int)
    parser.add_argument("--destination-star-id", type=int)
    parser.add_argument("--journey-id")
    parser.add_argument("--task-id")
    parser.add_argument("--goal", default="观察当前状态并汇报可以手工制造的下一步，不执行写入。")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.command == "serve":
        from .server.run import serve
        serve(config_path=args.config, host=args.host, port=args.port)
        return
    try:
        asyncio.run(execute(args, load_config(args.config)))
    except (Exception, KeyboardInterrupt) as exc:
        # Do not expose provider credentials/HTTP request headers in tracebacks.
        # Third-party HTTP/MCP exceptions can include credentials or response bodies.
        detail = str(exc) if type(exc) in (ValueError, RuntimeError) else "See local configuration, MCP and Journal state with doctor"
        print(f"Stopped: {type(exc).__name__}: {detail}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
