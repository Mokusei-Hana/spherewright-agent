import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import validate

from .bridge import PLAYBOOK
from .storage import redact
from .adapters import adapter_for


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


TOOLS += [
    function("resources", "Read one bounded page of local resource nodes; page is not a complete census", {}, []),
    function("harvest", "Harvest one exact nearby resource node within configured yield/distance budgets",
             {"kind": {"type": "string", "enum": ["vein", "vegetation"]},
              "node_id": {"type": "integer", "minimum": 1}, "count": {"type": "integer", "minimum": 1}},
             ["kind", "node_id", "count"]),
    function("select_research", "Select one technology; completion means queue change, not technology unlocked",
             {"tech_id": {"type": "integer", "minimum": 1}}, ["tech_id"]),
    function("move", "One short grounded move; stop on water, obstacles or unknown energy cost",
             {axis: {"type": "number"} for axis in ("x", "y", "z")}, ["x", "y", "z"]),
]


class Engine:
    def __init__(self, bridge, ledger, config, allow_write=False, task=None):
        self.b, self.ledger, self.c = bridge, ledger, config
        self.allow_write = allow_write and config["allow_write"]
        self.task = task
        self.writes = task["writes"] if task else 0
        self.materials = task["materials"] if task else 0
        self.read_retries = task["read_retries"] if task else 0

    def paused(self):
        return Path(self.c["pause_file"]).exists()

    async def enter(self):
        resource = await asyncio.wait_for(self.b.session.read_resource(PLAYBOOK), self.b.timeout)
        guide = "\n".join(getattr(c, "text", "") for c in resource.contents)
        require(bool(guide.strip()), "Installed MCP playbook is missing")
        status = await self.read("spherewright_get_status", {})
        self.ledger.event("entry", {"status": status, "playbook": guide})
        return guide

    async def observe(self, writable=False):
        status = await self.read("spherewright_get_status", {})
        session = await self.read("spherewright_get_session_state", {})
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
        player = await self.read("spherewright_get_player_state", identity)
        require(all(player.get(k) == v for k, v in identity.items()), "Player identity changed")
        progression = await self.read("spherewright_get_progression_state", identity)
        journal = await self.read("spherewright_get_gameplay_journal", {"sessionId": identity["sessionId"]})
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
        return await self.read("spherewright_get_recipe_catalog", identity)

    def state(self, state, detail=None):
        if self.task:
            self.task.update(state=state, detail=detail)
            self.ledger.save_task(self.task)

    async def read(self, name, arguments):
        from .bridge import READS
        require(name in READS, "Only allowlisted reads use retry path")
        while True:
            try:
                return await self.b.call(name, arguments)
            except (TimeoutError, ConnectionError):
                require(self.read_retries < self.c["max_read_retries"], "Read retry budget exhausted")
                self.read_retries += 1
                if self.task:
                    self.task["read_retries"] = self.read_retries
                    self.ledger.save_task(self.task)
                self.ledger.event("read_retry", {"tool": name, "used": self.read_retries})

    async def prepare_call(self, kind, request):
        require(self.allow_write, "Read-only: enable allow_write and --enable-write")
        require(not self.paused() and not self.ledger.pending(), "Paused or unresolved intent")
        self.ledger.event("prepare_intent", {"tool": kind, "request": request,
                                           "task_id": self.task["id"] if self.task else None})
        # Prepare is a write-class MCP operation too; it never uses read retries.
        plan = await self.b.call("spherewright_prepare_" + kind, request)
        self.ledger.event("prepare", plan)
        return plan

    async def action(self, name, arguments):
        adapter = adapter_for(name)
        require(self.allow_write, "Read-only: enable allow_write and --enable-write")
        require(not self.paused(), "PAUSE file present")
        require(not self.ledger.pending(), "Unresolved original intent: reconcile; never replay")
        require(self.writes < self.c["max_writes"], "Finite write budget exhausted")
        observation, identity = await self.observe(writable=True)
        request, context = await adapter.prepare(self, arguments, observation, identity)
        require(self.materials + context["materials"] <= self.c["max_material_items"], "Material budget exhausted")
        plan = await self.prepare_call(adapter.tool, request)
        require(plan.get("prepared") is True and plan.get("commitAllowedNow") is True
                and plan.get("commitBlockers") == [] and bool(plan.get("planToken"))
                and plan.get("actionKind") == adapter.kind, "Prepare rejected or incomplete; no retry")
        adapter.plan(plan, context, self.c)
        budget = plan.get("itemBudget")
        require(isinstance(budget, list), "Missing native material budget")
        require(all(type(item.get("count")) is int and item["count"] >= 0 for item in budget), "Invalid native budget")
        charged = max(context["materials"], sum(item["count"] for item in budget))
        require(self.materials + charged <= self.c["max_material_items"], "Native material budget exhausted")
        print(json.dumps(redact({"action": name, "request": request, "plan": plan}), ensure_ascii=False, indent=2))
        self.state("waiting_confirmation")
        approved = await asyncio.to_thread(input, "批准这个精确计划？输入 yes，其余停止: ")
        if approved != "yes":
            self.state("paused", "User declined exact plan")
        require(approved == "yes", "User declined; no commit")
        require(not self.paused(), "PAUSE requested; no commit")
        expiry = datetime.fromisoformat(plan["expiresAtUtc"].replace("Z", "+00:00"))
        require(expiry > datetime.now(timezone.utc), "Plan expired; no automatic re-prepare")
        pending = {**identity, **context, "adapter": name, "actionKind": adapter.kind,
                   "task_id": self.task["id"] if self.task else None, "key": str(uuid.uuid4()),
                   "request": request, "stage": "commit_intent"}
        # A crash between intent and budget persistence freezes all writes. On recovery,
        # conservative reserved counters can be restored from this intent without replay.
        pending["reserved_writes"] = self.writes + 1
        pending["reserved_materials"] = self.materials + charged
        self.ledger.set_pending(pending)
        self.writes += 1
        self.materials += charged
        if self.task:
            self.task.update(writes=self.writes, materials=self.materials)
        self.state("running")
        accepted = await self.b.call("spherewright_commit_" + adapter.tool,
                                     {**identity, "planToken": plan["planToken"], "idempotencyKey": pending["key"]})
        pending.update(actionId=accepted.get("actionId"), receipt=accepted, stage="accepted_or_unknown")
        self.ledger.set_pending(pending)
        require(accepted.get("accepted") is True and bool(pending["actionId"]), "Commit outcome unresolved")
        require(accepted.get("idempotencyKey") == pending["key"] and accepted.get("actionKind") == adapter.kind,
                "Commit receipt mismatch")
        return await self.reconcile()

    async def reconcile(self):
        pending = self.ledger.pending()
        require(pending is not None, "No pending action")
        require(
            bool(pending.get("actionId")),
            "Commit response lost before original actionId: manual upstream reconciliation required; no resend",
        )
        deadline = time.monotonic() + self.c["action_timeout_seconds"]
        for _ in range(self.c["max_action_polls"]):
            require(time.monotonic() < deadline, "Action timeout; pending retained")
            action = await self.read("spherewright_get_action_result", {"actionId": pending["actionId"]})
            # Persist native evidence first, even if a local assertion subsequently fails.
            self.ledger.event("action", action)
            require(action.get("actionId") == pending["actionId"], "Wrong action receipt")
            require(action.get("state") not in ("outcome_unknown", "recovery_required")
                    and action.get("recoveryRequired") is not True, "Unknown outcome: writes frozen")
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
                    and action.get("actionKind") == pending.get("actionKind", "handcraft"),
                    "Terminal binding mismatch",
                )
                require(bool(action.get("afterStateHash")) and isinstance(action.get("itemDeltas"), list),
                        "Missing native terminal evidence")
                observation, identity = await self.observe()
                require(all(identity[k] == pending[k] for k in identity), "World changed before readback")
                journal = observation["journal"]
                require(journal.get("persistencePending") is False and not journal.get("persistenceError")
                        and type(journal.get("durableThroughSequence")) is int, "Terminal Journal not durable yet")
                await adapter_for(pending.get("adapter", "handcraft")).verify(self, pending, action, observation, identity)
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
        tools = TOOLS if self.allow_write else [t for t in TOOLS if t["function"]["name"] in ("observe", "catalog", "resources", "finish")]
        start_step = self.task["steps"] if self.task else 0
        for step in range(start_step, self.c["max_steps"]):
            if self.task:
                self.task["steps"] = step + 1
                self.ledger.save_task(self.task)
            require(not self.paused(), "PAUSE requested; agent stopped before next decision")
            message = await provider.decide(messages, tools)
            self.ledger.event("decision", message)
            calls = message.get("tool_calls") or []
            require(
                len(calls) <= 1, "Parallel model actions refused; configure a sequential tool-calling model"
            )
            if not calls:
                self.state("completed")
                print(message.get("content") or "Agent stopped")
                return
            call = calls[0]
            name = call["function"]["name"]
            schema = next((t["function"]["parameters"] for t in tools if t["function"]["name"] == name), None)
            require(schema is not None, "Unknown or disabled model tool")
            args = json.loads(call["function"]["arguments"])
            validate(args, schema)
            if name == "finish":
                self.state("completed")
                print(args["report"])
                return
            if name == "observe":
                result, _ = await self.observe()
            elif name == "catalog":
                result = await self.catalog()
            else:
                if name == "resources":
                    _, identity = await self.observe()
                    result = await self.read("spherewright_list_resource_nodes", {**identity, "limit": 50})
                else:
                    result = await self.action(name, args)
            messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(redact(result), ensure_ascii=False),
                }
            )
        self.state("paused", "Finite decision budget reached")
        print("Finite decision budget reached; stopped")
