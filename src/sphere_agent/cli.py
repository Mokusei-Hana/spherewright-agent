import argparse
import asyncio
import json
import math
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
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{section}.timeout_seconds must be finite and positive")
    provider = config["provider"]
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
    for key in ("database", "pause_file"):
        agent[key] = str((base / agent[key]).resolve())
    return config


async def execute(args, config):
    if args.command == "check":
        print("Configuration valid. No model or game connection attempted.")
        return
    with writer_lock(config["agent"]["database"]):
        ledger = Ledger(config["agent"]["database"])
        try:
            if args.command == "ledger":
                print(json.dumps(ledger.pending(), ensure_ascii=False, indent=2))
                return
            async with connect(config["mcp"]) as bridge:
                engine = Engine(bridge, ledger, config["agent"], args.allow_write)
                if args.command == "run":
                    await engine.run(CompatibleProvider(config["provider"]), args.goal)
                else:
                    await engine.enter()
                    if args.command == "reconcile":
                        result = await engine.reconcile()
                    else:
                        result, _ = await engine.observe()
                    print(json.dumps(redact(result), ensure_ascii=False, indent=2))
        finally:
            ledger.db.close()


def main():
    parser = argparse.ArgumentParser(description="Bounded Spherewright Python agent")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--allow-write", action="store_true")
    parser.add_argument("command", choices=["check", "observe", "run", "reconcile", "ledger"])
    parser.add_argument("--goal", default="观察当前状态并汇报可以手工制造的下一步，不执行写入。")
    args = parser.parse_args()
    try:
        asyncio.run(execute(args, load_config(args.config)))
    except (Exception, KeyboardInterrupt) as exc:
        # Do not expose provider credentials/HTTP request headers in tracebacks.
        print(f"Stopped: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
