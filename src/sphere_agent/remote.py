"""Explicit native Voyager boundary checks and original-journey reconciliation."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adapters import bound_identity, number, player_hash, positive_id, require


def authority(config):
    require(config["allow_remote_flight"] is True, "Remote flight disabled in owner configuration")
    raw = Path(config["remote_authorization_file"]).read_bytes()
    require(len(raw) <= 16384, "Owner authorization too large")
    data = json.loads(raw)
    require(isinstance(data, dict) and isinstance(data.get("id"), str) and bool(data["id"]), "Missing owner authorization ID")
    require(data.get("scope") == "one_roundtrip_manual_recovery_review" and data.get("return_authorized") is True
            and data.get("manual_recovery_review_authorized") is True and data.get("maximum_progress_loss_ticks") == 0,
            "Exact return/manual recovery authority required; no automatic reload or progress loss")
    expiry = datetime.fromisoformat(data["expires_at_utc"].replace("Z", "+00:00"))
    require(expiry > datetime.now(timezone.utc), "Owner remote authorization expired")
    for key in ("home_planet_id", "home_star_id", "destination_planet_id", "destination_star_id"):
        positive_id(data[key])
    require(data["home_planet_id"] != data["destination_planet_id"], "Remote destination must differ from home")
    return data, hashlib.sha256(raw).hexdigest()


async def route_preview(engine, identity, destination_planet_id, destination_star_id, journey_id=None):
    positive_id(destination_planet_id)
    positive_id(destination_star_id)
    catalog = await engine.read("spherewright_get_voyager_star_catalog", {**identity, "limit": 32, "stateHashVersion": 1})
    require(catalog.get("sessionId") == identity["sessionId"] and catalog.get("originPlanetId") == identity["planetId"],
            "Voyager catalog origin changed")
    require(all(catalog.get(key) is False for key in ("prepared", "executable", "commitAllowedNow")), "Catalog advertised unsafe execution")
    star = next((value for value in catalog["stars"] if value["starId"] == destination_star_id), None)
    require(star and star.get("planetsVisible") is True, "Destination star/planet not natively visible")
    planet = next((value for value in star.get("planets") or [] if value["planetId"] == destination_planet_id), None)
    require(planet and planet.get("starId") == destination_star_id and planet.get("isGasGiant") is False,
            "Exact visible solid destination required; no guessed star-only landing")
    require(number(planet["distanceMeters"]) <= engine.c["max_remote_distance"], "Remote distance exceeds configured boundary")
    player = await engine.read("spherewright_get_player_state", identity)
    bound_identity(player, identity)
    require(player["stateHashVersion"] == catalog["stateHashVersion"], "Voyager hash version mismatch")
    request = {**identity, **player_hash(player), "expectedCatalogStateHash": catalog["stateHash"],
               "destinationPlanetId": destination_planet_id, "destinationStarId": destination_star_id,
               "maximumFlightGameTicks": engine.c["max_remote_game_ticks"],
               "returnReserveGameTicks": engine.c["return_reserve_game_ticks"],
               "minimumCoreEnergyRatio": engine.c["remote_min_core_ratio"],
               "extraWarperReserve": engine.c["extra_warper_reserve"],
               "maximumWarpGameTicksPerLeg": engine.c["max_warp_game_ticks"],
               "maximumArrivalOffsetMeters": engine.c["max_arrival_offset"]}
    if journey_id:
        request["journeyId"] = journey_id
    else:
        request["maximumRoundTripWallSeconds"] = engine.c["remote_wall_seconds"]
    route = await engine.read("spherewright_preview_voyager_route", request)
    engine.ledger.event("remote_route", {"request": request, "route": route, "catalog": catalog, "player": player})
    require(route.get("sessionId") == identity["sessionId"] and route.get("originPlanetId") == identity["planetId"]
            and route.get("originStarId") == catalog.get("originStarId")
            and route.get("destinationPlanetId") == destination_planet_id and route.get("destinationStarId") == destination_star_id,
            "Route scope mismatch")
    require(all(route.get(key) is False for key in ("prepared", "executable", "commitAllowedNow")), "Preview is not execution authority")
    require(route.get("playerStateHash") == player["stateHash"] and route.get("catalogStateHash") == catalog["stateHash"]
            and route.get("routeStateHash"), "Route hash domain mismatch")
    return route, player, catalog


def sufficient(route, config):
    require(0 < number(route["distanceMeters"]) <= config["max_remote_distance"], "Native route outside remote distance boundary")
    require(route.get("budgetComputed") is True and route.get("budgetSufficient") is True and route.get("blockers") == [],
            "Remote budget not proved sufficient")
    for key in ("technologyAllowsWarp", "destinationVisible", "nativeWarpEntryEnergySufficient",
                "warperReserveSufficient", "storedEnergySufficient", "coreBufferSufficient"):
        require(route.get(key) is True, "Remote safety prerequisite missing: " + key)
    require(route.get("destinationIsStarOnly") is False, "Star-only arrival cannot meet exact planet boundary")
    require(number(route["availableEnergyJoules"]) >= number(route["requiredEnergyJoules"]) > 0,
            "Loaded energy does not cover outbound and protected return")
    require(number(route["coreCapacityJoules"]) > 0 and number(route["coreEnergyJoules"]) / number(route["coreCapacityJoules"])
            >= config["remote_min_core_ratio"], "Departure core energy ratio")
    require(type(route.get("availableWarpers")) is int and type(route.get("requiredWarpers")) is int
            and route["availableWarpers"] >= route["requiredWarpers"] >= 1 + config["extra_warper_reserve"],
            "Insufficient real warp entries and reserve")
    protected_energy = route["requiredEnergyJoules"] if route.get("isReturnLeg") is True else route["returnEnergyBudgetJoules"]
    require(number(protected_energy) >= config["min_return_energy"]
            and number(route["returnCoreEnergyMinimumJoules"]) >= config["min_core_energy"], "Return energy floor too low")
    require(type(route.get("maximumFlightGameTicks")) is int and 0 < route["maximumFlightGameTicks"] <= config["max_remote_game_ticks"]
            and type(route.get("maximumWarpGameTicksPerLeg")) is int
            and 0 < route["maximumWarpGameTicksPerLeg"] <= config["max_warp_game_ticks"], "Native flight tick budget mismatch")


class Voyager:
    kind, tool = "interplanetary-flight", "voyager_flight"

    async def prepare(self, engine, args, observation, identity):
        owner, digest = authority(engine.c)
        require(owner.get("session_id") == identity["sessionId"], "Owner authorization belongs to another session")
        journey_id = args.get("journey_id")
        if journey_id:
            journey = engine.ledger.journey(identity["sessionId"], journey_id)
            require(journey.get("state") == "remote_landed" and journey["authority_id"] == owner["id"]
                    and journey["authority_digest"] == digest, "Original journey/authority mismatch")
            origin_planet, origin_star = owner["destination_planet_id"], owner["destination_star_id"]
            target_planet, target_star = owner["home_planet_id"], owner["home_star_id"]
        else:
            journey = None
            origin_planet, origin_star = owner["home_planet_id"], owner["home_star_id"]
            target_planet, target_star = owner["destination_planet_id"], owner["destination_star_id"]
            expiry = datetime.fromisoformat(owner["expires_at_utc"].replace("Z", "+00:00"))
            require(expiry >= datetime.now(timezone.utc) + timedelta(seconds=engine.c["remote_wall_seconds"]),
                    "Owner authority does not cover the complete round-trip wall window")
        require(identity["planetId"] == origin_planet and args["destination_planet_id"] == target_planet
                and args["destination_star_id"] == target_star, "Remote leg differs from explicit owner boundary")
        require(owner.get("maximum_game_ticks") == engine.c["max_remote_game_ticks"]
                and owner.get("maximum_wall_seconds") == engine.c["remote_wall_seconds"], "Owner flight budget differs from configured budget")
        route, player, catalog = await route_preview(engine, identity, target_planet, target_star, journey_id)
        require(catalog["originStarId"] == origin_star and route.get("isReturnLeg") is bool(journey_id), "Remote star/return leg mismatch")
        sufficient(route, engine.c)
        if journey:
            require(type(route.get("protectedRemainingJourneyGameTicks")) is int
                    and route["protectedRemainingJourneyGameTicks"] > 0
                    and route.get("originalJourneyDeadlineUtc") == journey["deadline_utc"], "Original return window lost")
        else:
            require(route.get("maximumRoundTripWallSeconds") == engine.c["remote_wall_seconds"]
                    and route.get("returnReserveGameTicks") == engine.c["return_reserve_game_ticks"], "Original return reserve not echoed")
        request = {**identity, **player_hash(player), "expectedRouteStateHash": route["routeStateHash"]}
        if journey_id:
            request["journeyId"] = journey_id
        return request, {"route": route, "catalog": catalog, "before": player, "materials": route["requiredWarpers"],
                         "authority_id": owner["id"], "authority_digest": digest, "authority": owner,
                         "return_leg": bool(journey_id), "journey_id": journey_id,
                         "destinationPlanetId": target_planet, "destinationStarId": target_star}

    def plan(self, plan, context, config):
        work = plan.get("voyagerMechaWork") or {}
        require(work.get("quiescenceProved") is True and work.get("nativeResearchOrInventoryMutationPerformed") is False,
                "Native mecha work is not quiescent")
        require(type(plan.get("estimatedGameTicks")) is int and 0 < plan["estimatedGameTicks"] <= config["max_remote_game_ticks"],
                "Flight plan exceeds original tick budget")

    def before_commit(self, engine, pending):
        owner, digest = authority(engine.c)
        require(owner["id"] == pending["authority_id"] and digest == pending["authority_digest"], "Owner authority changed during confirmation")
        if not pending["return_leg"]:
            require(datetime.fromisoformat(owner["expires_at_utc"].replace("Z", "+00:00")) >= datetime.now(timezone.utc)
                    + timedelta(seconds=engine.c["remote_wall_seconds"]), "Authorization no longer covers the full outbound round-trip window")

    async def verify(self, engine, pending, action, observation, identity):
        readback = action.get("voyagerFlightReadback") or {}
        require(bool(action.get("flightCheckpointId")), "Missing original native flight checkpoint identity")
        require(readback.get("bindingVerified") is True and readback.get("nativeExitObserved") is True
                and readback.get("stopRequested") is False and not readback.get("failureReason"), "Native flight exit/settlement not proved")
        require(readback.get("actualPlanetId") == pending["destinationPlanetId"]
                and readback.get("actualStarId") == pending["destinationStarId"]
                and readback.get("destinationStarId") == pending["destinationStarId"]
                and readback.get("originStarId") == pending["starId"]
                and readback.get("isReturnLeg") is pending["return_leg"], "Flight origin/destination receipt mismatch")
        require(identity["sessionId"] == pending["sessionId"] and identity["planetId"] == pending["destinationPlanetId"],
                "Fresh arrival identity mismatch")
        journey_id = readback.get("journeyId")
        require(isinstance(journey_id, str) and bool(journey_id)
                and (not pending["return_leg"] or pending["journey_id"] == journey_id), "Original journey ID lost")
        expected_phase = "home_landed_awaiting_save" if pending["return_leg"] else "remote_landed"
        require(readback.get("phase") == expected_phase, "Native landing phase incomplete")
        require(readback.get("selectedDestinationPlanetId") == pending["destinationPlanetId"], "Selected landing target changed")
        require(readback.get("entryAttempted") is True and readback.get("nativeWarpersConsumed") == 1,
                "Native warp entry/real warper consumption not proved")
        player = observation["player"]
        require(player.get("isAlive") is True and player.get("isOnPlanet") is True and player.get("isFlying") is False
                and player.get("isSailing") is False and number(player["speed"]) <= 0.1, "Arrival not grounded/idle")
        require(number(player["coreEnergy"]) >= engine.c["min_core_energy"]
                and number(readback["coreEnergyJoules"]) >= engine.c["min_core_energy"], "Arrival energy reserve exhausted")
        require(readback.get("nativeWarpCommand") is False and number(readback["nativeWarpState"]) == 0,
                "Warp state not settled")
        require(number(readback["loadedEnergyJoules"]) >= number(readback["returnEnergyFloorJoules"]), "Protected return energy lost")
        require(type(readback.get("nativeWarperCount")) is int and type(readback.get("packageWarperCount")) is int
                and readback["nativeWarperCount"] + readback["packageWarperCount"]
                >= engine.c["extra_warper_reserve"] + (0 if pending["return_leg"] else 1), "Protected return warpers lost")
        require(type(readback.get("warpGameTicks")) is int and 0 <= readback["warpGameTicks"] <= engine.c["max_warp_game_ticks"],
                "Warp tick accounting exceeded")
        deadline = readback.get("journeyDeadlineUtc")
        require(isinstance(deadline, str), "Original journey deadline missing")
        if pending["return_leg"]:
            old = engine.ledger.journey(pending["sessionId"], journey_id)
            require(deadline == old["deadline_utc"], "Return silently renewed journey window")
        observation["journey"] = {"journey_id": journey_id, "authority_id": pending["authority_id"],
            "authority_digest": pending["authority_digest"], "session_id": identity["sessionId"],
            "home_planet_id": pending["authority"]["home_planet_id"], "home_star_id": pending["authority"]["home_star_id"],
            "remote_planet_id": pending["authority"]["destination_planet_id"], "remote_star_id": pending["authority"]["destination_star_id"],
            "deadline_utc": deadline, "state": "home_awaiting_save" if pending["return_leg"] else "remote_landed", "terminal": action}


REMOTE_ADAPTERS = {"voyager_flight": Voyager()}
