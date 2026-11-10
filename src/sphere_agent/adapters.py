"""Explicit bounded actions, never an upstream tool passthrough."""

import math


class Unsupported(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def positive_id(value):
    require(type(value) is int and value > 0, "Expected positive runtime ID")


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), "Expected finite number")
    return value


def distance(a, b):
    return math.sqrt(sum((number(a[k]) - number(b[k])) ** 2 for k in ("x", "y", "z")))


def bound_identity(value, identity):
    require(all(value.get(k) == v for k, v in identity.items()), "Action snapshot identity changed")


def player_hash(player):
    require(bool(player.get("stateHash")), "Missing full player hash")
    return {"expectedPlayerStateHash": player["stateHash"], "stateHashVersion": player["stateHashVersion"]}


class Handcraft:
    kind, tool = "handcraft", "handcraft"

    async def prepare(self, engine, args, observation, identity):
        recipe_id, count = args["recipe_id"], args["count"]
        positive_id(recipe_id)
        require(type(count) is int and 1 <= count <= engine.c["max_recipe_executions"], "Craft count budget")
        catalog = await engine.read("spherewright_get_recipe_catalog", identity)
        bound_identity(catalog, identity)
        recipe = next((r for r in catalog["recipes"] if r["recipeId"] == recipe_id), None)
        require(recipe and recipe.get("handcraft") is True and recipe.get("unlocked") is True,
                "Recipe unavailable for handcraft")
        player = await engine.read("spherewright_get_player_state", identity)
        bound_identity(player, identity)
        require(player.get("handcraftQueue") == [], "Forge must be idle")
        required = {}
        for item in recipe["inputs"]:
            positive_id(item["itemId"])
            positive_id(item["count"])
            required[item["itemId"]] = required.get(item["itemId"], 0) + item["count"] * count
        inventory = {}
        for item in player["inventory"]:
            inventory[item["itemId"]] = inventory.get(item["itemId"], 0) + item["count"]
        require(all(inventory.get(k, 0) >= v for k, v in required.items()),
                "Explicit ingredients required; recursive automatic crafting unsupported")
        return ({**identity, **player_hash(player), "recipeId": recipe_id, "count": count},
                {"recipe": recipe, "count": count, "before": player, "materials": sum(required.values())})

    def plan(self, plan, context, config):
        pass

    async def verify(self, engine, pending, action, observation, identity):
        deltas = {}
        for item in action["itemDeltas"]:
            deltas[item["itemId"]] = deltas.get(item["itemId"], 0) + item["delta"]
        net = {}
        for direction, sign in (("inputs", -1), ("outputs", 1)):
            for item in pending["recipe"][direction]:
                net[item["itemId"]] = net.get(item["itemId"], 0) + sign * item["count"] * pending["count"]
        require(all(deltas.get(k, 0) == v for k, v in net.items()), "Native craft material/product mismatch")
        require(observation["player"].get("handcraftQueue") == [], "Forge completion not observed")


class Harvest:
    kind, tool = "harvest", "harvest"

    async def prepare(self, engine, args, observation, identity):
        positive_id(args["node_id"])
        require(args["kind"] in ("vein", "vegetation"), "Unsupported resource kind")
        count = args["count"]
        require(type(count) is int and 1 <= count <= engine.c["max_harvest_yield"], "Harvest yield budget")
        node = await engine.read("spherewright_inspect_resource_node",
                                 {**identity, "kind": args["kind"], "nodeId": args["node_id"]})
        bound_identity(node, identity)
        require(node.get("nodeId") == args["node_id"] and node.get("kind") == args["kind"], "Wrong resource")
        require(node.get("sameLocalPlanet") is True and node.get("withinPlayerBuildArea") is True,
                "Node outside local reach")
        require(0 <= number(node["distanceFromPlayer"]) <= engine.c["max_harvest_distance"], "Harvest distance budget")
        player = await engine.read("spherewright_get_player_state", identity)
        bound_identity(player, identity)
        require(distance(player["position"], node["position"]) <= engine.c["max_harvest_distance"], "Node moved out of reach")
        require(node.get("stateHash") and node["stateHashVersion"] == player["stateHashVersion"], "Resource hash unavailable")
        return ({**identity, **player_hash(player), "resourceKind": args["kind"], "nodeId": args["node_id"],
                 "requestedYieldCount": count, "expectedResourceStateHash": node["stateHash"]},
                {"node": node, "count": count, "before": player, "materials": count})

    def plan(self, plan, context, config):
        # Ordinary harvest binds its node inside the opaque plan token. This list is
        # primarily a building-plan field and can be empty on ordinary harvest.
        if plan.get("plannedResourceNodeIds"):
            require(plan["plannedResourceNodeIds"] == [context["node"]["nodeId"]], "Plan changed resource node")
        require(0 <= number(plan["estimatedDistance"]) <= config["max_harvest_distance"], "Planned harvest range")
        require(sum(item["count"] for item in plan["itemBudget"]) <= config["max_harvest_yield"],
                "Whole-node yield exceeds harvest budget")

    async def verify(self, engine, pending, action, observation, identity):
        node = pending["node"]
        require(action.get("targetObjectId") == node["nodeId"], "Harvest target mismatch")
        require(action.get("requestedCount") == pending["count"], "Harvest count mismatch")
        require(type(action.get("beforeTargetAmount")) is int and type(action.get("afterTargetAmount")) is int
                and action["afterTargetAmount"] < action["beforeTargetAmount"], "No native resource depletion proof")
        yields = {item["itemId"] for item in node["yields"]}
        require(any(d["itemId"] in yields and d["delta"] > 0 for d in action["itemDeltas"]), "No resource product readback")
        if action["afterTargetAmount"] > 0:
            fresh = await engine.read("spherewright_inspect_resource_node",
                                      {**identity, "kind": node["kind"], "nodeId": node["nodeId"]})
            bound_identity(fresh, identity)
            require(fresh.get("nodeId") == node["nodeId"] and fresh.get("remainingAmount", math.inf)
                    <= action["afterTargetAmount"], "Resource readback incomplete")
            observation["resource"] = fresh


class Research:
    kind, tool = "select-research", "select_research"

    async def prepare(self, engine, args, observation, identity):
        positive_id(args["tech_id"])
        progression = await engine.read("spherewright_get_progression_state", identity)
        bound_identity(progression, identity)
        require(bool(progression.get("selectionStateHash")), "Missing research selection hash domain")
        tech = next((t for t in progression["technologies"] if t["techId"] == args["tech_id"]), None)
        require(tech and tech.get("unlocked") is False and args["tech_id"] not in progression["techQueue"],
                "Technology already unlocked/queued or unavailable")
        return ({**identity, "techId": args["tech_id"], "expectedSelectionStateHash": progression["selectionStateHash"],
                 "stateHashVersion": progression["selectionStateHashVersion"], "prioritizeQueued": False},
                {"tech_id": args["tech_id"], "progression": progression, "materials": 0})

    def plan(self, plan, context, config):
        # expectedStateHash is an upstream composite of selection hash, tech and
        # priority, rather than the raw progression or player hash.
        require(bool(plan.get("expectedStateHash")), "Missing composite research plan hash")
        require(plan.get("stateHashVersion") == context["progression"]["selectionStateHashVersion"], "Research hash version")
        require(plan.get("researchQueueReadbackVersion") == 1, "Research queue readback capability unavailable")

    async def verify(self, engine, pending, action, observation, identity):
        readback = action.get("researchQueueReadback") or {}
        before, after = readback.get("beforeQueue"), readback.get("afterQueue")
        require(before == pending["progression"]["techQueue"] and isinstance(after, list) and before != after,
                "No exact native research queue change")
        require(pending["tech_id"] in after and readback.get("researchProgressPreserved") is True
                and readback.get("inventoryPreserved") is True, "Research selection preservation unproved")
        # Fresh progression may already have completed the selected technology; native queue readback is the boundary.
        require(bool(observation["progression"].get("selectionStateHash")), "Fresh research readback unavailable")


class Move:
    kind, tool = "move", "move"

    async def prepare(self, engine, args, observation, identity):
        target = {k: number(args[k]) for k in ("x", "y", "z")}
        player = await engine.read("spherewright_get_player_state", identity)
        bound_identity(player, identity)
        require(player.get("isAlive") is True and player.get("isOnPlanet") is True
                and player.get("isFlying") is False and player.get("isSailing") is False
                and number(player["speed"]) <= 0.1, "Move requires idle grounded player")
        require(1.5 < distance(player["position"], target) <= engine.c["max_move_distance"], "Short move distance budget")
        request = {**identity, **player_hash(player), "targetX": target["x"], "targetY": target["y"],
                   "targetZ": target["z"], "arrivalTolerance": 1.5, "allowPassiveDrift": False}
        preview = await engine.prepare_call("move", {**request, "previewWalkingRoute": True})
        route = preview.get("walkingRoutePreview") or {}
        require(route.get("terrainCoverageComplete") is True and route.get("blockers") == []
                and (route.get("geometry") or {}).get("complete") is True, "Walking obstacle geometry unknown")
        require(route.get("terrainPoints") and all(p.get("observed") is True and p.get("dry") is True
                for p in route["terrainPoints"]), "Water/unknown terrain: stopped")
        # Only one direct clear segment. Detours and automatic obstacle probes are out of scope.
        require(len(route.get("segments", [])) == 1 and len(route.get("waypoints", [])) == 2,
                "Obstacle or route detour: stopped")
        require(number(route["segments"][0]["minimumClearanceMetres"]) >= number(route["safetyMarginMetres"]),
                "Insufficient obstacle clearance")
        return request, {"target": target, "before": player, "materials": 0, "preview": route}

    def plan(self, plan, context, config):
        require(distance(plan["plannedPosition"], context["target"]) <= 1.5, "Move target changed")
        require(0 < number(plan["estimatedDistance"]) <= config["max_move_distance"], "Planned move distance budget")
        surface = plan.get("surfacePreview") or {}
        require(surface.get("unknownSampleCount") == 0 and surface.get("shoreRisk") == "not_detected"
                and surface.get("samples") and all(s.get("groundHit") is True and s.get("waterHit") is False
                for s in surface["samples"]), "Water/unknown surface: stopped")
        energy = context["before"].get("nativeLocalControlEnergy") or {}
        require(energy.get("costInputsProved") is True and (energy.get("independentDebitsExcluded") is True
                or energy.get("independentDebitsBudgeted") is True), "Movement energy cost unknown")
        ticks = plan.get("estimatedGameTicks")
        require(type(ticks) is int and ticks > 0, "Movement duration unknown")
        cost = number(energy["perNativeTickEnergyUpperJoules"]) * ticks
        require(0 <= cost <= config["max_move_energy"] and number(context["before"]["coreEnergy"]) - cost
                >= config["min_core_energy"], "Movement energy budget exhausted")

    async def verify(self, engine, pending, action, observation, identity):
        require(not action.get("stalled") and not action.get("failureKind") and not action.get("doNotRetrySameTarget"),
                "Movement blocked: stop without probe or retry")
        player = observation["player"]
        require(distance(player["position"], pending["target"]) <= 1.5 and player.get("isFlying") is False
                and player.get("isSailing") is False, "Move arrival not proved")
        require(player.get("isAlive") is True and number(player["coreEnergy"]) >= engine.c["min_core_energy"]
                and number(pending["before"]["coreEnergy"]) - number(player["coreEnergy"])
                <= engine.c["max_move_energy"], "Movement energy readback exceeded budget")


ADAPTERS = {adapter.tool: adapter for adapter in (Handcraft(), Harvest(), Research(), Move())}


def adapter_for(name):
    from .factory import FACTORY_ADAPTERS
    from .remote import REMOTE_ADAPTERS
    if name in FACTORY_ADAPTERS:
        return FACTORY_ADAPTERS[name]
    if name in REMOTE_ADAPTERS:
        return REMOTE_ADAPTERS[name]
    if name not in ADAPTERS:
        raise Unsupported(f"Unsupported action: {name}; no raw upstream passthrough")
    return ADAPTERS[name]
