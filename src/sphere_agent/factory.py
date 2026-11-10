"""Small native construction and local container/production operations."""

from .adapters import Unsupported, bound_identity, distance, number, player_hash, positive_id, require


async def inspect(engine, identity, object_id):
    positive_id(object_id)
    result = await engine.read("spherewright_inspect_factory_entity", {**identity, "objectId": object_id})
    bound_identity(result, identity)
    require(result.get("objectId") == object_id and result.get("objectKind") == "entity", "Exact built object required")
    require(bool(result.get("stateHash")), "Missing factory state hash")
    engine.ledger.event("factory_endpoint", result)
    quota = engine.ledger.container_quota(identity, object_id)
    if quota:
        result["clientQuota"] = {"max_items": quota["max_items"], "scope": quota["scope"]}
        require(quota["endpoint"]["itemId"] == result["itemId"] and distance(quota["endpoint"]["position"], result["position"])
                <= engine.c["geometry_tolerance"], "Local quota endpoint identity changed")
        if result.get("componentKind") == "tank":
            require(type(result.get("tankFluidCount")) is int and 0 <= result["tankFluidCount"] <= quota["max_items"],
                    "Tank stock unknown or above client quota; no automated write admitted")
    return result


def stock(items, role=None):
    """Count only item stocks, preserving total proliferator points separately."""
    result = {}
    for item in items:
        if role is not None and item.get("role") != role:
            continue
        require(item.get("countUnit", "items") == "items" and item.get("unitsPerItem", 1) == 1,
                "Non-item buffer units cannot be treated as inventory")
        item_id, count, inc = item["itemId"], item["count"], item["inc"]
        require(type(count) is int and count >= 0 and type(inc) is int and inc >= 0, "Invalid inventory count/inc")
        if count == 0:
            require(inc == 0, "Points without items")
            continue
        positive_id(item_id)
        previous = result.get(item_id, (0, 0))
        result[item_id] = (previous[0] + count, previous[1] + inc)
    return result


def deltas(action):
    result = {}
    for item in action["itemDeltas"]:
        require(type(item["delta"]) is int, "Invalid native item delta")
        result[item["itemId"]] = result.get(item["itemId"], 0) + item["delta"]
    return {key: value for key, value in result.items() if value}


def vector(args, prefix=""):
    return {axis: number(args[prefix + axis]) for axis in ("x", "y", "z")}


def edge(objects, source, target):
    """Verify the same edge in both native connection tables, with opposite directions."""
    matches = [c for c in objects[source]["connections"] if c.get("otherObjectId") == target and c.get("isOutput") is True]
    require(len(matches) == 1, "Missing/ambiguous directed native connection")
    outgoing = matches[0]
    require(any(c.get("otherObjectId") == source and c.get("isOutput") is False
                and c.get("slot") == outgoing["otherSlot"] and c.get("otherSlot") == outgoing["slot"]
                for c in objects[target]["connections"]), "Native connection is not reciprocal")


class Build:
    kind, tool = "build", "build"
    mode = "building"

    async def prepare(self, engine, args, observation, identity):
        item_id = args["item_id"]
        positive_id(item_id)
        allowed = engine.c["build_item_ids"] if self.mode == "building" else [2001] if self.mode == "belt" else [2011, 2012]
        require(item_id in allowed, "Building item outside explicit small-build allowlist")
        catalog = await engine.read("spherewright_get_build_catalog", {"sessionId": identity["sessionId"]})
        require(catalog.get("planetId") == identity["planetId"], "Build catalog planet changed")
        item = next((entry for entry in catalog["buildings"] if entry["itemId"] == item_id), None)
        require(item and item.get("unlocked") is True and item.get("available") is True, "Building unavailable")
        player = await engine.read("spherewright_get_player_state", identity)
        bound_identity(player, identity)
        require(player.get("isAlive") is True and player.get("isOnPlanet") is True
                and player.get("isFlying") is False and player.get("isSailing") is False, "Grounded build required")
        require(stock(player["inventory"]).get(item_id, (0, 0))[0] >= 1, "Building item missing")
        request = {**identity, **player_hash(player), "buildingItemId": item_id}
        context = {"mode": self.mode, "item_id": item_id, "before": player, "materials": 1,
                   "endpoints": {}, "catalog_item": item}
        if self.mode == "building":
            position = vector(args)
            require(distance(position, player["position"]) <= engine.c["max_build_distance"], "Build distance budget")
            yaw = number(args.get("yaw", 0))
            require(0 <= yaw < 360, "Yaw must be 0..360 degrees")
            request.update(preferredPositionX=position["x"], preferredPositionY=position["y"],
                           preferredPositionZ=position["z"], preferredYaw=yaw)
            context["requested_position"] = position
            if item_id == 2301:
                positive_id(args.get("node_id"))
                node = await engine.read("spherewright_inspect_resource_node",
                                         {**identity, "kind": "vein", "nodeId": args["node_id"]})
                bound_identity(node, identity)
                require(node.get("nodeId") == args["node_id"] and node.get("sameLocalPlanet") is True,
                        "Miner requires exact local vein")
                request.update(resourceNodeId=node["nodeId"], expectedResourceStateHash=node["stateHash"])
                context["resource"] = node
            else:
                require(not args.get("node_id"), "Resource binding applies only to miner")
        else:
            source_id, destination_id = args["source_id"], args["destination_id"]
            require(source_id != destination_id, "Distinct endpoints required")
            for field, object_id in (("source", source_id), ("destination", destination_id)):
                endpoint = await inspect(engine, identity, object_id)
                require(distance(endpoint["position"], player["position"]) <= engine.c["max_build_distance"], "Endpoint out of range")
                require(bool(endpoint.get("endpointStateHash")), "Missing endpoint hash domain")
                require(endpoint["endpointStateHashVersion"] == player["stateHashVersion"], "Endpoint hash version mismatch")
                context["endpoints"][field] = endpoint
            request.update(sourceObjectId=source_id, destinationObjectId=destination_id,
                           expectedSourceStateHash=context["endpoints"]["source"]["endpointStateHash"],
                           expectedDestinationStateHash=context["endpoints"]["destination"]["endpointStateHash"])
            if self.mode == "belt":
                start, end = vector(args), vector(args, "end_")
                require(1.5 <= distance(start, end) <= engine.c["max_belt_length"], "Belt path length budget")
                require(all(distance(point, player["position"]) <= engine.c["max_build_distance"] for point in (start, end)),
                        "Belt path outside bounded local area")
                request.update(preferredPositionX=start["x"], preferredPositionY=start["y"], preferredPositionZ=start["z"],
                               pathEndX=end["x"], pathEndY=end["y"], pathEndZ=end["z"], beltPathMode="native_grid")
                context.update(requested_position=start, requested_end=end)
            else:
                positive_id(args["filter_item_id"])
                request["initialSorterFilterItemId"] = args["filter_item_id"]
                context["filter_item_id"] = args["filter_item_id"]
        return request, context

    def plan(self, plan, context, config):
        path = plan.get("plannedPath")
        require(isinstance(path, list) and 1 <= len(path) <= config["max_build_objects"], "Native geometry/object budget missing")
        require(len(path) == 1 or self.mode == "belt", "Single-object build required")
        require(all(distance(point, context["before"]["position"]) <= config["max_build_distance"] for point in path),
                "Native snapped path outside local build range")
        if "requested_position" in context:
            require(distance(path[0], context["requested_position"]) <= config["max_grid_snap_distance"], "Native start snap exceeds allowance")
        if self.mode == "belt":
            require(distance(path[-1], context["requested_end"]) <= config["max_grid_snap_distance"], "Native end snap exceeds allowance")
            length = sum(distance(a, b) for a, b in zip(path, path[1:]))
            require(length <= config["max_belt_length"], "Native grid path length budget")
            belt = plan.get("plannedBeltPath") or {}
            require(belt.get("nativeValidationMode") == "full_path_stage1" and belt.get("routingMode") == "native_grid",
                    "Missing native full-path grid validation")
            require(belt.get("newObjectCount") == len(path), "Belt new-object count mismatch")
            # Retained covers have separate preservation semantics, not new paid objects.
            context["covers"] = [value for value in (belt.get("reusedSourceObjectId"), belt.get("reusedDestinationObjectId")) if value]
        else:
            context["covers"] = []
        if context["endpoints"]:
            require(plan.get("sourceObjectId") == context["endpoints"]["source"]["objectId"]
                    and plan.get("destinationObjectId") == context["endpoints"]["destination"]["objectId"], "Native endpoints changed")
        if self.mode == "sorter":
            attachment = plan.get("plannedInserterAttachment") or {}
            require(attachment.get("mode") in ("exact_slots", "native_single_belt_segment", "native_two_belt_segments"),
                    "Missing native sorter port binding")
            require(type(attachment.get("sourceSlot")) is int and type(attachment.get("destinationSlot")) is int,
                    "Missing sorter port indices")
            require(plan.get("plannedSorterFilterItemId") == context["filter_item_id"], "Sorter initial filter mismatch")
        if "resource" in context:
            require(context["resource"]["nodeId"] in plan.get("plannedResourceNodeIds", []), "Miner does not cover selected vein")
        budget = plan.get("itemBudget")
        require(isinstance(budget, list) and len(budget) == 1 and budget[0].get("itemId") == context["item_id"]
                and budget[0].get("count") == len(path) and budget[0].get("direction") == "construction-consumption",
                "Exact owned building item debit required")
        require(stock(context["before"]["inventory"]).get(context["item_id"], (0, 0))[0] >= len(path), "Insufficient complete path materials")

    async def verify(self, engine, pending, action, observation, identity):
        ids = action.get("targetObjectIds") or [action.get("targetObjectId")]
        require(ids and all(type(value) is int and value > 0 for value in ids) and len(ids) == len(set(ids)),
                "Actual positive built IDs required, not prebuilds")
        plan = pending["plan"]
        require(len(ids) == len(plan["plannedPath"]), "Native completed object count mismatch")
        require(not set(ids) & {entry["objectId"] for entry in pending["endpoints"].values()}, "Reused endpoint cannot count as new build")
        require(deltas(action) == {pending["item_id"]: -len(ids)}, "Native material debit mismatch")
        objects = {}
        unmatched = list(plan["plannedPath"])
        for object_id in ids:
            obj = await inspect(engine, identity, object_id)
            require(obj["itemId"] == pending["item_id"] and obj.get("requiredBuildItemCount") in (None, 0), "Built item/ready state mismatch")
            match = next((point for point in unmatched if distance(obj["position"], point) <= engine.c["geometry_tolerance"]), None)
            require(match is not None, "Actual object outside native grid plan")
            unmatched.remove(match)
            objects[object_id] = obj
        require(not unmatched, "Native geometry incomplete")
        for endpoint in pending["endpoints"].values():
            fresh = await inspect(engine, identity, endpoint["objectId"])
            require(fresh["itemId"] == endpoint["itemId"] and distance(fresh["position"], endpoint["position"]) <= engine.c["geometry_tolerance"],
                    "Endpoint replaced/moved")
            objects[fresh["objectId"]] = fresh
        if pending["mode"] == "sorter":
            sorter = objects[ids[0]]
            source = pending["endpoints"]["source"]["objectId"]
            destination = pending["endpoints"]["destination"]["objectId"]
            require(sorter.get("pickTargetObjectId") == source and sorter.get("insertTargetObjectId") == destination
                    and sorter.get("filterItemId") == pending["filter_item_id"], "Sorter direction/filter not ready")
            edge(objects, source, ids[0])
            edge(objects, ids[0], destination)
            attachment = plan["plannedInserterAttachment"]
            for endpoint_id, field in ((source, "sourceSlot"), (destination, "destinationSlot")):
                if objects[endpoint_id]["componentKind"] != "belt":
                    require(any(connection.get("otherObjectId") == endpoint_id
                                and connection.get("otherSlot") == attachment[field]
                                for connection in sorter["connections"]), "Actual sorter port differs from approved native plan")
        elif pending["mode"] == "belt":
            source = pending["endpoints"]["source"]["objectId"]
            destination = pending["endpoints"]["destination"]["objectId"]
            current, seen = source, set()
            for _ in range(len(ids) + 1):
                candidates = [c["otherObjectId"] for c in objects[current]["connections"] if c.get("isOutput") is True
                              and c.get("otherObjectId") in set(ids) | {destination}]
                require(len(candidates) == 1, "Belt branch, missing output or ambiguous direction")
                next_id = candidates[0]
                edge(objects, current, next_id)
                if next_id == destination:
                    break
                require(next_id not in seen, "Belt connection cycle")
                seen.add(next_id)
                current = next_id
            require(seen == set(ids) and next_id == destination, "Complete native directed path not established")
        elif "resource" in pending:
            require(pending["resource"]["nodeId"] in objects[ids[0]].get("resourceNodeIds", []), "Actual miner resource binding missing")
        observation["factory_objects"] = list(objects.values())


class Belt(Build):
    mode = "belt"


class Sorter(Build):
    mode = "sorter"


class Transfer:
    kind, tool = "transfer", "transfer"

    async def prepare(self, engine, args, observation, identity):
        positive_id(args["item_id"])
        count = args["count"]
        require(type(count) is int and 1 <= count <= engine.c["max_transfer_items"], "Transfer count budget")
        direction = args["direction"]
        require(direction in ("player-to-storage", "storage-to-player"), "Only local bilateral transfer is allowed")
        endpoint = await inspect(engine, identity, args["object_id"])
        if endpoint.get("componentKind") != "storage":
            raise Unsupported("Native transfer supports storage components; tank writes have no supported upstream mapping")
        require(endpoint.get("connections") == [], "Conservation requires an isolated container with no delivery ports")
        stack = endpoint.get("stack")
        if stack:
            require(stack.get("complete") is True and len(stack.get("layers", [])) == 1 and stack.get("attachments") == [],
                    "Stacked/attached container needs a separate whole-stack conservation adapter")
        player = await engine.read("spherewright_get_player_state", identity)
        bound_identity(player, identity)
        require(distance(endpoint["position"], player["position"]) <= engine.c["max_build_distance"], "Container out of range")
        require(player.get("handcraftQueue") == [] and player.get("inHandItem") is None
                and player.get("autoManageResearchItems") is False and player.get("autoReplenishFuel") is False,
                "Disable automatic inventory consumers and empty forge/hand before transfer")
        player_stock, container_stock = stock(player["inventory"]), stock(endpoint["buffers"])
        source = player_stock if direction == "player-to-storage" else container_stock
        source_count, source_inc = source.get(args["item_id"], (0, 0))
        require(source_count >= count, "Insufficient exact source stock")
        if source_inc and count != source_count:
            raise Unsupported("Partial enriched stacks lack native removedInc receipt; transfer the complete item stock")
        transferred_inc = source_inc if count == source_count else 0
        quota = engine.ledger.container_quota(identity, endpoint["objectId"])
        if direction == "player-to-storage":
            require(quota is not None, "Set an explicit local container quota before depositing")
            require(quota["endpoint"]["itemId"] == endpoint["itemId"], "Quota endpoint item identity changed")
            require(distance(quota["endpoint"]["position"], endpoint["position"]) <= engine.c["geometry_tolerance"],
                    "Quota endpoint geometry changed")
            require(sum(value[0] for value in container_stock.values()) + count <= quota["max_items"], "Container quota exceeded")
        request = {**identity, **player_hash(player), "storageEntityId": endpoint["objectId"],
                   "expectedStorageStateHash": endpoint["stateHash"], "direction": direction,
                   "itemId": args["item_id"], "count": count}
        return request, {"endpoint": endpoint, "before": player, "direction": direction, "item_id": args["item_id"],
                         "count": count, "transferred_inc": transferred_inc, "materials": count, "quota": quota}

    def plan(self, plan, context, config):
        require(bool(plan.get("expectedStateHash")), "Missing native transfer binding")

    async def verify(self, engine, pending, action, observation, identity):
        require(action.get("targetObjectId") == pending["endpoint"]["objectId"]
                and action.get("targetItemId") == pending["item_id"] and action.get("requestedCount") == pending["count"],
                "Transfer endpoint/item/count receipt mismatch")
        sign = -1 if pending["direction"] == "player-to-storage" else 1
        require(deltas(action) == {pending["item_id"]: sign * pending["count"]}, "Native player transfer delta mismatch")
        require(type(action.get("beforeTargetAmount")) is int and type(action.get("afterTargetAmount")) is int
                and action["afterTargetAmount"] - action["beforeTargetAmount"] == -sign * pending["count"],
                "Native container conservation missing")
        endpoint = await inspect(engine, identity, pending["endpoint"]["objectId"])
        require(endpoint["itemId"] == pending["endpoint"]["itemId"] and endpoint["connections"] == [], "Container changed")
        before_player = stock(pending["before"]["inventory"])
        after_player = stock(observation["player"]["inventory"])
        before_container = stock(pending["endpoint"]["buffers"])
        after_container = stock(endpoint["buffers"])
        require(action["beforeTargetAmount"] == before_container.get(pending["item_id"], (0, 0))[0]
                and action["afterTargetAmount"] == after_container.get(pending["item_id"], (0, 0))[0],
                "Fresh container stock differs from native transfer boundary; reconcile without replay")
        for item_id in before_player.keys() | after_player.keys() | before_container.keys() | after_container.keys():
            p0, p1 = before_player.get(item_id, (0, 0)), after_player.get(item_id, (0, 0))
            s0, s1 = before_container.get(item_id, (0, 0)), after_container.get(item_id, (0, 0))
            expected = (sign * pending["count"], sign * pending["transferred_inc"]) if item_id == pending["item_id"] else (0, 0)
            require(tuple(b - a for a, b in zip(p0, p1)) == expected
                    and tuple(b - a for a, b in zip(s0, s1)) == tuple(-value for value in expected),
                    "Item count or proliferator points not conserved across exact endpoints")
        observation["transfer_endpoint"] = endpoint
        observation["conservation"] = {"count": pending["count"], "inc": pending["transferred_inc"],
                                        "player_before": pending["before"], "container_before": pending["endpoint"]}


class ConfigureRecipe:
    kind, tool = "configure-building", "configure_building"

    async def prepare(self, engine, args, observation, identity):
        positive_id(args["recipe_id"])
        endpoint = await inspect(engine, identity, args["object_id"])
        require(endpoint["componentKind"] in ("assembler", "smelter"), "Only assembler/smelter recipe selection")
        require(endpoint.get("progress") == 0 and endpoint.get("isWorking") is False
                and not stock(endpoint["buffers"]), "Production device must be empty and idle")
        catalog = await engine.read("spherewright_get_build_catalog", {"sessionId": identity["sessionId"]})
        require(catalog.get("planetId") == identity["planetId"], "Recipe catalog planet changed")
        recipe = next((r for r in catalog["recipes"] if r["recipeId"] == args["recipe_id"]), None)
        require(recipe and recipe.get("unlocked") is True, "Production recipe unavailable")
        return ({**identity, "entityId": endpoint["objectId"], "recipeId": args["recipe_id"], "mode": "production",
                 "expectedFactoryStateHash": endpoint["stateHash"], "stateHashVersion": endpoint["stateHashVersion"]},
                {"endpoint": endpoint, "recipe_id": args["recipe_id"], "before": observation["player"], "materials": 0})

    def plan(self, plan, context, config):
        require(bool(plan.get("expectedStateHash")), "Missing native production binding")

    async def verify(self, engine, pending, action, observation, identity):
        require(action.get("targetObjectId") == pending["endpoint"]["objectId"]
                and action.get("targetItemId") == pending["recipe_id"], "Production receipt target mismatch")
        require(not deltas(action), "Configuration unexpectedly moved player items")
        endpoint = await inspect(engine, identity, pending["endpoint"]["objectId"])
        require(endpoint["itemId"] == pending["endpoint"]["itemId"] and endpoint.get("recipeId") == pending["recipe_id"],
                "Production recipe readback mismatch")
        observation["production_endpoint"] = endpoint


class ConfigureStorage:
    kind, tool = "configure-building", "configure_building"

    async def prepare(self, engine, args, observation, identity):
        endpoint = await inspect(engine, identity, args["object_id"])
        if endpoint["itemId"] not in (2101, 2102) or endpoint["componentKind"] != "storage":
            raise Unsupported("Native storage-capacity configuration supports ordinary 2101/2102 boxes, not tanks")
        configuration = endpoint.get("storageConfiguration") or {}
        banned = args["banned_grids"]
        require(type(banned) is int and 0 <= banned <= configuration["gridCount"], "Invalid disabled-grid quota")
        return ({**identity, "entityId": endpoint["objectId"], "recipeId": 0, "mode": "storage-capacity",
                 "storageOperation": "set-bans", "storageBannedGridCount": banned,
                 "expectedFactoryStateHash": endpoint["stateHash"], "stateHashVersion": endpoint["stateHashVersion"]},
                {"endpoint": endpoint, "banned_grids": banned, "materials": 0})

    def plan(self, plan, context, config):
        configuration = plan.get("plannedStorageConfiguration") or {}
        require(plan.get("plannedStorageOperation") == "set-bans" and configuration.get("bannedGridCount") == context["banned_grids"],
                "Native storage quota plan mismatch")

    async def verify(self, engine, pending, action, observation, identity):
        receipt = action.get("storageConfigurationReadback") or {}
        require(receipt.get("entityId") == pending["endpoint"]["objectId"] and receipt.get("operation") == "set-bans"
                and (receipt.get("configurationAfter") or {}).get("bannedGridCount") == pending["banned_grids"],
                "Native storage quota receipt mismatch")
        require(stock(receipt["buffersBefore"]) == stock(receipt["buffersAfter"]), "Storage quota changed inventory/inc")
        require(not deltas(action), "Storage quota moved player inventory")
        endpoint = await inspect(engine, identity, pending["endpoint"]["objectId"])
        require(endpoint["itemId"] == pending["endpoint"]["itemId"] and (endpoint.get("storageConfiguration") or {}).get("bannedGridCount")
                == pending["banned_grids"], "Fresh storage quota mismatch")
        observation["storage_endpoint"] = endpoint


FACTORY_ADAPTERS = {"build": Build(), "belt": Belt(), "sorter": Sorter(), "transfer": Transfer(),
                    "configure_recipe": ConfigureRecipe(), "configure_storage": ConfigureStorage()}
