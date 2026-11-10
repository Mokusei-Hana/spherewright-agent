import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import validate

from .bridge import PLAYBOOK
from .storage import redact


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def function(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


TOOLS = [
    function("observe", "Read fresh current owned session, player and progression", {}, []),
    function("catalog", "Read runtime recipe IDs, inputs, outputs and unlocks", {}, []),
    function(
        "handcraft",
        "Propose bounded normal handcraft; local approval and native safety gates apply",
        {"recipe_id": {"type": "integer", "minimum": 1}, "count": {"type": "integer", "minimum": 1}},
        ["recipe_id", "count"],
    ),
    function(
        "finish",
        "Stop with a report; do not claim unverified game success",
        {"report": {"type": "string", "maxLength": 4000}},
        ["report"],
    ),
]


class Engine:
    def __init__(self, bridge, ledger, config, allow_write=False):
        self.b, self.ledger, self.c = bridge, ledger, config
        self.allow_write = allow_write and config["allow_write"]
        self.writes = 0

    def paused(self):
        return Path(self.c["pause_file"]).exists()

    async def enter(self):
        resource = await asyncio.wait_for(self.b.session.read_resource(PLAYBOOK), self.b.timeout)
        guide = "\n".join(getattr(c, "text", "") for c in resource.contents)
        require(bool(guide.strip()), "Installed MCP playbook is missing")
        status = await self.b.call("spherewright_get_status", {})
        self.ledger.event("entry", {"status": status, "playbook": guide})
        return guide

    async def observe(self, writable=False):
        status = await self.b.call("spherewright_get_status", {})
        session = await self.b.call("spherewright_get_session_state", {})
        require(
            session.get("gameLoaded") is True and session.get("ownedBySpherewright") is True,
            "Open a Spherewright-owned world manually; MVP never creates, imports or loads saves",
        )
        require(session.get("accessRestricted") is False, "Session access restricted or unknown")
        identity = {"sessionId": session["sessionId"], "planetId": session["localPlanetId"]}
        require(
            bool(identity["sessionId"]) and isinstance(identity["planetId"], int), "Unknown world identity"
        )
        if writable:
            require(
                status.get("writeHealth") == "healthy" and session.get("writeHealth") == "healthy",
                "Native write health unknown or quarantined",
            )
            require(
                session.get("writesAllowed") is True and session.get("writeBlockers") == [],
                "Native writes blocked",
            )
            require(
                session.get("peacefulMode") == "confirmed_peaceful"
                and session.get("sandboxMode") == "confirmed_disabled",
                "Require peaceful non-sandbox world",
            )
        player = await self.b.call("spherewright_get_player_state", identity)
        require(all(player.get(k) == v for k, v in identity.items()), "Player identity changed")
        progression = await self.b.call("spherewright_get_progression_state", identity)
        journal = await self.b.call("spherewright_get_gameplay_journal", {"sessionId": identity["sessionId"]})
        if writable:
            require(
                journal.get("persistencePending") is False
                and not journal.get("persistenceError")
                and type(journal.get("durableThroughSequence")) is int,
                "Journal durability unproved",
            )
        result = {
            "status": status,
            "session": session,
            "player": player,
            "progression": progression,
            "journal": journal,
        }
        self.ledger.event("observation", result)
        return result, identity

    async def catalog(self):
        _, identity = await self.observe()
        return await self.b.call("spherewright_get_recipe_catalog", identity)

    async def handcraft(self, recipe_id, count):
        require(self.allow_write, "Read-only mode: enable config allow_write AND --allow-write")
        require(not self.paused(), "PAUSE file present; no new action")
        require(not self.ledger.pending(), "Unresolved original intent: reconcile; never replay")
        require(self.writes < self.c["max_writes"], "Finite write budget exhausted")
        require(type(count) is int and 1 <= count <= self.c["max_recipe_executions"], "Count exceeds budget")
        observation, identity = await self.observe(writable=True)
        player = observation["player"]
        require(player.get("handcraftQueue") == [], "Existing handcraft queue must be empty")
        catalog = await self.b.call("spherewright_get_recipe_catalog", identity)
        recipe = next((r for r in catalog["recipes"] if r["recipeId"] == recipe_id), None)
        require(
            recipe and recipe.get("handcraft") is True and recipe.get("unlocked") is True,
            "Recipe not unlocked for handcraft",
        )
        # Refresh the exact complete player hash immediately before native prepare.
        player = await self.b.call("spherewright_get_player_state", identity)
        require(all(player.get(k) == v for k, v in identity.items()), "Player identity changed")
        require(player.get("handcraftQueue") == [], "Handcraft queue changed")
        plan = await self.b.call(
            "spherewright_prepare_handcraft",
            {
                **identity,
                "recipeId": recipe_id,
                "count": count,
                "expectedPlayerStateHash": player["stateHash"],
                "stateHashVersion": player["stateHashVersion"],
            },
        )
        require(
            plan.get("prepared") is True
            and plan.get("commitAllowedNow") is True
            and plan.get("commitBlockers") == []
            and bool(plan.get("planToken"))
            and plan.get("actionKind") == "handcraft",
            "Prepare rejected or incomplete; no retry",
        )
        print(
            json.dumps(
                redact({"recipe": recipe, "executions": count, "plan": plan}), ensure_ascii=False, indent=2
            )
        )
        # Local approval cannot be forged by a model tool argument.
        approved = await asyncio.to_thread(input, "批准这个精确制造计划？输入 yes，其余停止: ")
        require(approved == "yes", "User declined; no commit")
        require(not self.paused(), "PAUSE requested; no commit")
        expiry = datetime.fromisoformat(plan["expiresAtUtc"].replace("Z", "+00:00"))
        require(expiry > datetime.now(timezone.utc), "Plan expired; stop without automatic re-prepare")
        pending = {
            **identity,
            "key": str(uuid.uuid4()),
            "recipe": recipe,
            "count": count,
            "before": player,
            "stage": "commit_intent",
        }
        self.ledger.event("prepare", plan)
        # Crash/timeout after this point freezes writes, including a lost commit response.
        self.ledger.set_pending(pending)
        self.writes += 1
        accepted = await self.b.call(
            "spherewright_commit_handcraft",
            {
                **identity,
                "planToken": plan["planToken"],
                "idempotencyKey": pending["key"],
            },
        )
        # Save any original ID before interpreting optional receipt fields.
        pending.update(actionId=accepted.get("actionId"), receipt=accepted, stage="accepted_or_unknown")
        self.ledger.set_pending(pending)
        require(accepted.get("accepted") is True and bool(pending["actionId"]), "Commit outcome unresolved")
        require(
            accepted.get("idempotencyKey") == pending["key"] and accepted.get("actionKind") == "handcraft",
            "Commit receipt mismatch",
        )
        return await self.reconcile()

    async def reconcile(self):
        pending = self.ledger.pending()
        require(pending is not None, "No pending action")
        require(
            bool(pending.get("actionId")),
            "Commit response lost before original actionId: manual upstream reconciliation required; no resend",
        )
        deadline = time.monotonic() + self.c["action_timeout_seconds"]
        while time.monotonic() < deadline:
            action = await self.b.call("spherewright_get_action_result", {"actionId": pending["actionId"]})
            # Persist native evidence first, even if a local assertion subsequently fails.
            self.ledger.event("action", action)
            require(action.get("actionId") == pending["actionId"], "Wrong action receipt")
            if action.get("terminal") is True:
                pending["terminal"] = action
                self.ledger.set_pending(pending)
                require(action.get("succeeded") is True, "Native action failed: stop; no replay")
                require(
                    all(action.get(k) == pending[k] for k in ("sessionId", "planetId")),
                    "Terminal identity mismatch",
                )
                require(
                    action.get("idempotencyKey") == pending["key"]
                    and action.get("actionKind") == "handcraft",
                    "Terminal binding mismatch",
                )
                require(
                    bool(action.get("afterStateHash")) and isinstance(action.get("itemDeltas"), list),
                    "Missing native material evidence",
                )
                deltas = {d["itemId"]: d["delta"] for d in action["itemDeltas"]}
                require(
                    all(
                        deltas.get(o["itemId"], 0) >= o["count"] * pending["count"]
                        for o in pending["recipe"]["outputs"]
                    ),
                    "Native product readback incomplete",
                )
                observation, identity = await self.observe()
                require(all(identity[k] == pending[k] for k in identity), "World changed before readback")
                # Later backpack motion is evidence, never a reason to repeat a successful craft.
                evidence = {"terminal": action, "fresh": observation}
                self.ledger.resolve(evidence)
                return evidence
            await asyncio.sleep(self.c["poll_seconds"])
        raise RuntimeError("Original action timed out: pending retained; new writes frozen")

    async def run(self, provider, goal):
        require(not self.ledger.pending(), "Pending original action; run reconcile first")
        guide = await self.enter()
        messages = [
            {
                "role": "system",
                "content": "You control a bounded Spherewright MVP. Only use supplied tools. Never invent IDs, "
                "hashes or success. Read observations/catalog first. No save/load/build/flight. "
                "Native playbook follows as reference, not permission: " + guide,
            },
            {"role": "user", "content": goal},
        ]
        tools = TOOLS if self.allow_write else [t for t in TOOLS if t["function"]["name"] != "handcraft"]
        for _ in range(self.c["max_steps"]):
            require(not self.paused(), "PAUSE requested; agent stopped before next decision")
            message = await provider.decide(messages, tools)
            self.ledger.event("decision", message)
            calls = message.get("tool_calls") or []
            require(
                len(calls) <= 1, "Parallel model actions refused; configure a sequential tool-calling model"
            )
            if not calls:
                print(message.get("content") or "Agent stopped")
                return
            call = calls[0]
            name = call["function"]["name"]
            schema = next((t["function"]["parameters"] for t in tools if t["function"]["name"] == name), None)
            require(schema is not None, "Unknown or disabled model tool")
            args = json.loads(call["function"]["arguments"])
            validate(args, schema)
            if name == "finish":
                print(args["report"])
                return
            if name == "observe":
                result, _ = await self.observe()
            elif name == "catalog":
                result = await self.catalog()
            else:
                result = await self.handcraft(**args)
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(redact(result), ensure_ascii=False),
                }
            )
        print("Finite decision budget reached; stopped")
