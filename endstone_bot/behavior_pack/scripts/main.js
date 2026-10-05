/**
 * Endstone Bot bridge v4.
 *
 * Security model:
 * - handshake accepts native Server sources and headless Endstone console origins
 * - the first valid bot:hello establishes the per-process token
 * - every later command must carry the exact same token and protocol
 *
 * Protocol 3 adds transactional 36-slot inventory custody. Inventory editing uses
 * physical Container.swapItems operations; snapshots are maintained by the plugin
 * only for crash recovery and verification.
 */

import * as GameTest from "@minecraft/server-gametest";
import { GameMode, StructureSaveMode, system, world } from "@minecraft/server";

const PROTOCOL = 3;
const MAX_MESSAGE_CHARS = 1400;
let bridgeToken = "";
let activeTest = null;
let gameTestStartRequested = false;

const simulatedPlayers = new Map();
const pendingSpawns = [];
const tridentBusy = new Set();
const deadPlayers = new Set();
const desiredPoses = new Map();
const inventoryLeases = new Map();
const MAIN_INVENTORY_SLOTS = 36;

function sourceKind(event) {
    try {
        return String(event.sourceType ?? "");
    } catch (_) {
        return "";
    }
}

function isTrustedServerSource(event) {
    const kind = sourceKind(event);
    const normalized = kind.toLowerCase();

    // Reject sources that are explicitly known to be controllable from gameplay.
    // Accept Server and unknown/headless command origins. The latter is needed for
    // Endstone's ConsoleCommandSender on BDS builds that do not surface it as the
    // ScriptEventSource.Server enum value.
    if (
        normalized === "block" ||
        normalized === "entity" ||
        normalized === "npcdialogue"
    ) {
        return false;
    }
    return true;
}

function parseMessage(event) {
    try {
        const data = event.message ? JSON.parse(event.message) : {};
        return data && typeof data === "object" ? data : null;
    } catch (_) {
        return null;
    }
}

function dimensionId(value) {
    const raw = String(value || "overworld").replace("minecraft:", "");
    if (raw === "nether" || raw === "the_nether") return "nether";
    if (raw === "end" || raw === "the_end") return "the_end";
    return "overworld";
}

function getDimension(value) {
    return world.getDimension(dimensionId(value));
}

function rememberPose(req) {
    const name = String(req.n || "");
    const pose = {
        n: name,
        x: Number(req.x),
        y: Number(req.y),
        z: Number(req.z),
        d: dimensionId(req.d),
        pitch: Number(req.pitch ?? 0),
        yaw: Number(req.yaw ?? 0),
        dx: Number(req.dx ?? 0),
        dy: Number(req.dy ?? 0),
        dz: Number(req.dz ?? 0),
    };
    if (name) desiredPoses.set(name, pose);
    return pose;
}

function utf8Hex(text) {
    const escaped = encodeURIComponent(String(text));
    let hex = "";
    for (let i = 0; i < escaped.length; i++) {
        if (escaped[i] === "%") {
            hex += escaped.slice(i + 1, i + 3).toLowerCase();
            i += 2;
        } else {
            hex += escaped.charCodeAt(i).toString(16).padStart(2, "0");
        }
    }
    return hex;
}

function reply(eventId, data) {
    if (!bridgeToken) return false;
    const payload = { ...data, t: bridgeToken, p: PROTOCOL };
    const msg = JSON.stringify(payload);
    const encoded = utf8Hex(msg);
    const eventName = String(eventId || "").replace(/^bot:/, "");
    if (encoded.length > 6000) {
        console.warn(`[EndstoneBot] refusing oversized callback ${eventId}: ${encoded.length} chars`);
        return false;
    }
    try {
        world.getDimension("overworld").runCommand(
            `botbridge ${eventName} ${encoded}`
        );
        return true;
    } catch (e) {
        console.warn(`[EndstoneBot] callback failed (${eventId}): ${e}`);
        return false;
    }
}

function validCommand(event, data) {
    if (!isTrustedServerSource(event)) return false;
    if (!bridgeToken) return false;
    if (data.t !== bridgeToken) return false;
    return Number(data.p) === PROTOCOL;
}

function poseRotation(req) {
    const pitchRaw = Number(req.pitch ?? 0);
    const yawRaw = Number(req.yaw ?? 0);
    const pitch = Number.isFinite(pitchRaw) ? Math.max(-90, Math.min(90, pitchRaw)) : 0;
    const yaw = Number.isFinite(yawRaw) ? yawRaw : 0;
    return { x: pitch, y: yaw };
}

function orientSim(sim, req) {
    const dx = Number(req.dx ?? 0);
    const dy = Number(req.dy ?? 0);
    const dz = Number(req.dz ?? 0);
    const lengthSq = dx * dx + dy * dy + dz * dz;

    if (Number.isFinite(lengthSq) && lengthSq > 1e-8) {
        const invLength = 1 / Math.sqrt(lengthSq);
        const head = sim.getHeadLocation();
        const target = {
            x: head.x + dx * invLength * 32,
            y: head.y + dy * invLength * 32,
            z: head.z + dz * invLength * 32,
        };

        try {
            if (!activeTest || typeof activeTest.relativeLocation !== "function") {
                throw new Error("GameTest relativeLocation is unavailable");
            }

            // Test-bound SimulatedPlayer controller methods consume GameTest
            // relative coordinates. The bot itself may be teleported anywhere
            // in the world, so convert the desired absolute world target back
            // into the owning Test coordinate system before lookAtLocation().
            const relativeTarget = activeTest.relativeLocation(target);
            sim.lookAtLocation(
                relativeTarget,
                GameTest.LookDuration?.UntilMove ?? "UntilMove",
            );
            return;
        } catch (_) {}

        // Compatibility fallback only for older APIs without lookAtLocation.
        try {
            sim.lookAt(target);
            return;
        } catch (_) {}
    }

    try {
        sim.setRotation(poseRotation(req));
    } catch (_) {}
}

function teleportSim(sim, req) {
    const dimension = getDimension(req.d);
    sim.teleport(
        { x: Number(req.x), y: Number(req.y), z: Number(req.z) },
        { dimension },
    );
    orientSim(sim, req);
}


function spawnWithTestApi(req) {
    if (!activeTest) return null;
    return activeTest.spawnSimulatedPlayer({ x: 0, y: 2, z: 0 }, String(req.n));
}

function isDeadSim(name, sim) {
    if (deadPlayers.has(name)) return true;
    try {
        const health = sim.getComponent("minecraft:health");
        if (health && Number(health.currentValue) <= 0) {
            deadPlayers.add(name);
            return true;
        }
    } catch (_) {}
    return false;
}

function finishRespawn(name, sim, pose) {
    system.runTimeout(() => {
        try {
            teleportSim(sim, pose);
            deadPlayers.delete(name);
            tridentBusy.delete(name);
            reply("bot:spawned", { n: name, ok: true, respawned: true });
        } catch (e) {
            try { sim.disconnect(); } catch (_) {}
            if (simulatedPlayers.get(name) === sim) simulatedPlayers.delete(name);
            deadPlayers.delete(name);
            tridentBusy.delete(name);
            reply("bot:lost", { n: name, reason: "respawn_failed", e: String(e) });
        }
    }, 1);
}

function respawnTracked(name, sim, pose) {
    if (!sim || !deadPlayers.has(name)) return false;
    try {
        const ok = sim.respawn();
        if (!ok) {
            throw new Error("SimulatedPlayer.respawn returned false");
        }
        finishRespawn(name, sim, pose);
        return true;
    } catch (e) {
        try { sim.disconnect(); } catch (_) {}
        if (simulatedPlayers.get(name) === sim) simulatedPlayers.delete(name);
        deadPlayers.delete(name);
        tridentBusy.delete(name);
        reply("bot:lost", { n: name, reason: "respawn_failed", e: String(e) });
        return false;
    }
}

function doSpawn(req) {
    const name = String(req.n || "");
    if (!name) return;
    const pose = rememberPose(req);
    const existing = simulatedPlayers.get(name);
    if (existing) {
        let existingValid = false;
        try {
            existingValid = Boolean(existing.isValid);
        } catch (_) {}

        if (existingValid) {
            if (isDeadSim(name, existing)) {
                respawnTracked(name, existing, pose);
                return;
            }
            try {
                teleportSim(existing, pose);
                reply("bot:spawned", { n: name, ok: true, existed: true });
            } catch (e) {
                // Keep a still-valid existing player tracked. Untracking here would
                // create an orphan that later list/reconcile cannot safely own.
                reply("bot:error", { n: name, e: `existing SimulatedPlayer teleport failed: ${String(e)}` });
            }
            return;
        }

        try { existing.disconnect(); } catch (_) {}
        simulatedPlayers.delete(name);
        deadPlayers.delete(name);
        tridentBusy.delete(name);
    }

    if (!activeTest) {
        if (!pendingSpawns.some((x) => String(x.n) === name)) pendingSpawns.push(req);
        startSimulatedPlayerGameTest();
        return;
    }

    let sim = null;
    try {
        sim = spawnWithTestApi(req);
    } catch (e) {
        reply("bot:error", { n: name, e: `GameTest spawn failed: ${String(e)}` });
        return;
    }

    if (!sim) {
        reply("bot:error", { n: name, e: "Test.spawnSimulatedPlayer returned null" });
        return;
    }

    try {
        // Initial placement is deliberately performed exactly once here. If it
        // fails after spawn, disconnect the untracked object before reporting the
        // error so a failed request cannot leak an orphan SimulatedPlayer.
        teleportSim(sim, pose);
    } catch (e) {
        try { sim.disconnect(); } catch (_) {}
        deadPlayers.delete(name);
        tridentBusy.delete(name);
        desiredPoses.delete(name);
        reply("bot:error", { n: name, e: `initial SimulatedPlayer teleport failed: ${String(e)}` });
        return;
    }

    simulatedPlayers.set(name, sim);
    deadPlayers.delete(name);
    reply("bot:spawned", { n: name, ok: true });
}

function finishRemove(name, sim) {
    system.runTimeout(() => {
        try {
            if (sim.isValid) {
                reply("bot:error", { n: name, e: "disconnect did not invalidate SimulatedPlayer" });
                return;
            }
        } catch (_) {}
        if (simulatedPlayers.get(name) === sim) simulatedPlayers.delete(name);
        deadPlayers.delete(name);
        tridentBusy.delete(name);
        desiredPoses.delete(name);
        reply("bot:removed", { n: name });
    }, 2);
}

function doRemove(nameValue) {
    const name = String(nameValue || "");
    if (inventoryLeases.has(name)) {
        reply("bot:error", { n: name, e: "inventory custody is active; refusing remove" });
        return;
    }
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:removed", { n: name, existed: false });
        return;
    }
    deadPlayers.delete(name);
    tridentBusy.delete(name);
    desiredPoses.delete(name);
    try {
        sim.disconnect();
    } catch (e) {
        reply("bot:error", { n: name, e: `disconnect failed: ${String(e)}` });
        return;
    }
    finishRemove(name, sim);
}

function verifyViewSync(name, sim, pose) {
    system.runTimeout(() => {
        try {
            const expected = {
                x: Number(pose.dx ?? 0),
                y: Number(pose.dy ?? 0),
                z: Number(pose.dz ?? 0),
            };
            const expectedLen = Math.sqrt(
                expected.x * expected.x +
                expected.y * expected.y +
                expected.z * expected.z
            );
            if (!Number.isFinite(expectedLen) || expectedLen <= 1e-8) return;

            const actual = sim.getViewDirection();
            const actualLen = Math.sqrt(
                actual.x * actual.x +
                actual.y * actual.y +
                actual.z * actual.z
            );
            if (!Number.isFinite(actualLen) || actualLen <= 1e-8) return;

            const dot = (
                expected.x * actual.x +
                expected.y * actual.y +
                expected.z * actual.z
            ) / (expectedLen * actualLen);

            const expectedPitch = -Math.asin(
                Math.max(-1, Math.min(1, expected.y / expectedLen))
            ) * 180 / Math.PI;

            let headPitch = NaN;
            let headYaw = NaN;
            try {
                headPitch = Number(sim.headRotation.x);
                headYaw = Number(sim.headRotation.y);
            } catch (_) {}

            let entityPitch = NaN;
            let entityYaw = NaN;
            try {
                const rotation = sim.getRotation();
                entityPitch = Number(rotation.x);
                entityYaw = Number(rotation.y);
            } catch (_) {}

            if (dot < 0.98) {
                console.warn(
                    `[EndstoneBot] view sync mismatch [${name}]: dot=${dot.toFixed(4)} ` +
                    `expectedPitch=${expectedPitch.toFixed(2)} ` +
                    `head=(${headPitch.toFixed(2)},${headYaw.toFixed(2)}) ` +
                    `entity=(${entityPitch.toFixed(2)},${entityYaw.toFixed(2)}) ` +
                    `expected=(${expected.x.toFixed(4)},${expected.y.toFixed(4)},${expected.z.toFixed(4)}) ` +
                    `actual=(${actual.x.toFixed(4)},${actual.y.toFixed(4)},${actual.z.toFixed(4)})`
                );
            }
        } catch (e) {
            console.warn(`[EndstoneBot] view sync verification failed [${name}]: ${e}`);
        }
    }, 2);
}

function doTeleport(req) {
    const name = String(req.n || "");
    if (inventoryLeases.has(name)) {
        reply("bot:error", { n: name, e: "inventory custody is active; refusing teleport" });
        return;
    }
    const pose = rememberPose(req);
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:error", { n: name, e: "SimulatedPlayer not found" });
        return;
    }
    if (isDeadSim(name, sim)) {
        if (!respawnTracked(name, sim, pose)) {
            reply("bot:error", { n: name, e: "failed to respawn SimulatedPlayer" });
        }
        return;
    }
    try {
        teleportSim(sim, pose);
        verifyViewSync(name, sim, pose);
        reply("bot:teleported", { n: name });
    } catch (e) {
        reply("bot:error", { n: name, e: `teleport failed: ${String(e)}` });
    }
}

function findTrident(sim) {
    try {
        const inventory = sim.getComponent("minecraft:inventory");
        const container = inventory?.container;
        if (!container || !container.isValid) return null;
        for (let slot = 0; slot < container.size; slot++) {
            const item = container.getItem(slot);
            if (item && item.typeId === "minecraft:trident") {
                return { container, slot, item };
            }
        }
    } catch (_) {}
    return null;
}

function tridentResult(name, requester, ok, reason = "") {
    reply("bot:trident_result", {
        n: name,
        r: String(requester || ""),
        ok: Boolean(ok),
        reason: String(reason || ""),
    });
}

function swapTridentIntoHotbarZero(sim, found) {
    const previousSelected = Number(sim.selectedSlotIndex ?? 0);
    const oldZero = found.container.getItem(0);
    if (found.slot !== 0) {
        found.container.setItem(0, found.item);
        if (oldZero) {
            found.container.setItem(found.slot, oldZero);
        } else {
            found.container.setItem(found.slot);
        }
    }
    sim.selectedSlotIndex = 0;
    return {
        previousSelected,
        sourceSlot: found.slot,
        swapped: found.slot !== 0,
    };
}

function restoreHotbarAfterTrident(sim, found, state, thrown) {
    try {
        if (state.swapped) {
            const displaced = found.container.getItem(state.sourceSlot);
            if (!thrown) {
                const currentZero = found.container.getItem(0);
                if (currentZero) found.container.setItem(state.sourceSlot, currentZero);
                else found.container.setItem(state.sourceSlot);
            } else {
                found.container.setItem(state.sourceSlot);
            }

            if (displaced) found.container.setItem(0, displaced);
            else found.container.setItem(0);
        }
    } catch (_) {}

    try {
        sim.selectedSlotIndex = state.previousSelected;
    } catch (_) {}
}

function playerWithinOneBlock(sim) {
    try {
        const location = sim.location;
        const dimension = sim.dimension;
        const simId = String(sim.id || "");
        const players = dimension.getPlayers({
            location,
            maxDistance: 1.01,
        });

        for (const player of players) {
            try {
                if (player === sim) continue;
                if (simId && String(player.id || "") === simId) continue;

                const p = player.location;
                const dx = Number(p.x) - Number(location.x);
                const dy = Number(p.y) - Number(location.y);
                const dz = Number(p.z) - Number(location.z);
                if (dx * dx + dy * dy + dz * dz <= 1.0) {
                    return player;
                }
            } catch (_) {}
        }
    } catch (_) {}
    return null;
}

function doThrowTrident(req) {
    const name = String(req.n || "");
    const requester = String(req.r || "");
    if (inventoryLeases.has(name)) {
        tridentResult(name, requester, false, "inventory_custody");
        return;
    }
    const pose = rememberPose(req);
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        tridentResult(name, requester, false, "not_found");
        return;
    }
    if (isDeadSim(name, sim)) {
        if (!respawnTracked(name, sim, pose)) {
            tridentResult(name, requester, false, "respawn_failed");
        } else {
            tridentResult(name, requester, false, "respawning");
        }
        return;
    }
    if (tridentBusy.has(name)) {
        tridentResult(name, requester, false, "busy");
        return;
    }

    if (playerWithinOneBlock(sim)) {
        tridentResult(name, requester, false, "player_too_close");
        return;
    }

    const found = findTrident(sim);
    if (!found) {
        tridentResult(name, requester, false, "no_trident");
        return;
    }

    tridentBusy.add(name);
    let slotState = null;

    try {
        // Position and rotation were synchronized earlier by "move here".
        // Throwing must not teleport, rotate, or otherwise rewrite that state.
        slotState = swapTridentIntoHotbarZero(sim, found);
    } catch (e) {
        tridentBusy.delete(name);
        if (slotState) restoreHotbarAfterTrident(sim, found, slotState, false);
        tridentResult(name, requester, false, `prepare_failed:${String(e)}`);
        return;
    }

    system.runTimeout(() => {
        try {
            if (playerWithinOneBlock(sim)) {
                restoreHotbarAfterTrident(sim, found, slotState, false);
                tridentBusy.delete(name);
                tridentResult(name, requester, false, "player_too_close");
                return;
            }
            if (!sim.useItemInSlot(0)) {
                restoreHotbarAfterTrident(sim, found, slotState, false);
                tridentBusy.delete(name);
                tridentResult(name, requester, false, "use_failed");
                return;
            }
        } catch (e) {
            restoreHotbarAfterTrident(sim, found, slotState, false);
            tridentBusy.delete(name);
            tridentResult(name, requester, false, `use_failed:${String(e)}`);
            return;
        }

        system.runTimeout(() => {
            let thrown = false;
            try {
                sim.stopUsingItem();
                const remaining = found.container.getItem(0);
                thrown = !remaining || remaining.typeId !== "minecraft:trident";
            } catch (e) {
                restoreHotbarAfterTrident(sim, found, slotState, false);
                tridentBusy.delete(name);
                tridentResult(name, requester, false, `release_failed:${String(e)}`);
                return;
            }

            system.runTimeout(() => {
                try {
                    const remaining = found.container.getItem(0);
                    thrown = thrown || !remaining || remaining.typeId !== "minecraft:trident";
                    restoreHotbarAfterTrident(sim, found, slotState, thrown);
                    tridentResult(name, requester, thrown, thrown ? "" : "release_failed");
                } finally {
                    tridentBusy.delete(name);
                }
            }, 1);
        }, 10);
    }, 1);
}

function inventoryContainer(entity) {
    try {
        const component = entity?.getComponent("minecraft:inventory");
        const container = component?.container;
        if (!container || !container.isValid || Number(container.size) < MAIN_INVENTORY_SLOTS) {
            return null;
        }
        return container;
    } catch (_) {
        return null;
    }
}

function findRealPlayer(nameValue) {
    const wanted = String(nameValue || "");
    for (const player of world.getAllPlayers()) {
        try {
            if (String(player.name || "") !== wanted) continue;
            if (simulatedPlayers.has(String(player.name || ""))) continue;
            return player;
        } catch (_) {}
    }
    return null;
}

function gameModeValue(raw) {
    const mode = String(raw || "survival").toLowerCase();
    if (mode.includes("spectator")) return GameMode.Spectator ?? GameMode.spectator ?? "spectator";
    if (mode.includes("creative")) return GameMode.Creative ?? GameMode.creative ?? "creative";
    if (mode.includes("adventure")) return GameMode.Adventure ?? GameMode.adventure ?? "adventure";
    return GameMode.Survival ?? GameMode.survival ?? "survival";
}

function lockInventoryBot(sim) {
    let previous = "survival";
    try { previous = String(sim.getGameMode()); } catch (_) {}
    try { sim.stopMoving(); } catch (_) {}
    try { sim.stopUsingItem(); } catch (_) {}
    sim.setGameMode(gameModeValue("spectator"));
    return previous;
}

function unlockInventoryBot(sim, previousGameMode) {
    try { sim.stopMoving(); } catch (_) {}
    try { sim.stopUsingItem(); } catch (_) {}
    try {
        sim.setGameMode(gameModeValue(previousGameMode));
        return true;
    } catch (_) {
        return false;
    }
}

function swapMainInventories(first, second) {
    if (!first || !second) throw new Error("inventory container unavailable");
    if (Number(first.size) < MAIN_INVENTORY_SLOTS || Number(second.size) < MAIN_INVENTORY_SLOTS) {
        throw new Error("inventory container has fewer than 36 slots");
    }

    const completed = [];
    try {
        for (let slot = 0; slot < MAIN_INVENTORY_SLOTS; slot++) {
            first.swapItems(slot, slot, second);
            completed.push(slot);
        }
    } catch (e) {
        let rollbackError = null;
        for (let index = completed.length - 1; index >= 0; index--) {
            try {
                first.swapItems(completed[index], completed[index], second);
            } catch (rollback) {
                rollbackError = rollback;
                break;
            }
        }
        if (rollbackError) {
            throw new Error(`inventory swap failed and rollback failed: ${String(e)} / ${String(rollbackError)}`);
        }
        throw new Error(`inventory swap failed; rollback completed: ${String(e)}`);
    }
}

function mainInventoryEmpty(container) {
    if (!container || Number(container.size) < MAIN_INVENTORY_SLOTS) return false;
    for (let slot = 0; slot < MAIN_INVENTORY_SLOTS; slot++) {
        if (container.getItem(slot)) return false;
    }
    return true;
}

function inventorySessionValid(value) {
    return /^[0-9a-f]{32}$/i.test(String(value || ""));
}

function playerAlreadyLeased(playerName) {
    const wanted = String(playerName || "");
    for (const lease of inventoryLeases.values()) {
        if (String(lease.playerName || "") === wanted) return true;
    }
    return false;
}

function doInventoryBegin(req) {
    const name = String(req.n || "");
    const playerName = String(req.r || "");
    const session = String(req.s || "");
    if (!name || !playerName || !inventorySessionValid(session)) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "invalid_request" });
        return;
    }
    if (inventoryLeases.has(name) || playerAlreadyLeased(playerName)) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "already_locked" });
        return;
    }

    const sim = simulatedPlayers.get(name);
    const player = findRealPlayer(playerName);
    if (!sim || !player) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: !sim ? "bot_not_found" : "player_not_found" });
        return;
    }
    if (isDeadSim(name, sim) || tridentBusy.has(name)) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "bot_busy" });
        return;
    }

    const botInventory = inventoryContainer(sim);
    const playerInventory = inventoryContainer(player);
    if (!botInventory || !playerInventory) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "inventory_unavailable" });
        return;
    }

    const botSelected = Math.max(0, Math.min(8, Number(sim.selectedSlotIndex ?? 0)));
    const playerSelected = Math.max(0, Math.min(8, Number(player.selectedSlotIndex ?? 0)));
    let gameMode = "survival";
    let playerGameMode = "survival";
    try {
        gameMode = lockInventoryBot(sim);
        playerGameMode = lockInventoryBot(player);
        swapMainInventories(playerInventory, botInventory);
    } catch (e) {
        const poisoned = String(e).includes("rollback failed");
        if (poisoned) {
            inventoryLeases.set(name, {
                session,
                playerName,
                playerSelected,
                botSelected,
                gameMode,
                playerGameMode,
                ready: false,
                recovery: false,
                poisoned: true,
            });
        } else {
            unlockInventoryBot(sim, gameMode);
            unlockInventoryBot(player, playerGameMode);
        }
        reply("bot:inventory_error", {
            n: name, r: playerName, s: session,
            reason: poisoned ? "begin_rollback_failed" : "begin_failed",
            e: String(e),
        });
        return;
    }

    inventoryLeases.set(name, {
        session,
        playerName,
        playerSelected,
        botSelected,
        gameMode,
        playerGameMode,
        ready: false,
        recovery: false,
        poisoned: false,
    });
    reply("bot:inventory_swapped", {
        n: name,
        r: playerName,
        s: session,
        bot_slot: botSelected,
        player_slot: playerSelected,
        game_mode: gameMode,
        player_game_mode: playerGameMode,
    });
}

function doInventoryReady(req) {
    const name = String(req.n || "");
    const playerName = String(req.r || "");
    const session = String(req.s || "");
    const lease = inventoryLeases.get(name);
    if (!lease || lease.session !== session || lease.playerName !== playerName || lease.recovery) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "ready_session_mismatch" });
        return;
    }
    if (lease.poisoned) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "poisoned" });
        return;
    }
    const player = findRealPlayer(playerName);
    if (!player) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "ready_player_not_found" });
        return;
    }
    if (!lease.ready) {
        if (!unlockInventoryBot(player, lease.playerGameMode)) {
            reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "ready_restore_gamemode_failed" });
            return;
        }
        lease.ready = true;
    }
    reply("bot:inventory_ready_ack", { n: name, r: playerName, s: session });
}

function doInventoryFinish(req) {
    const name = String(req.n || "");
    const playerName = String(req.r || "");
    const session = String(req.s || "");
    const lease = inventoryLeases.get(name);
    const rollbackRequest = Boolean(req.rb);
    if (!lease || lease.session !== session || lease.playerName !== playerName || lease.recovery) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "session_mismatch" });
        return;
    }
    if (lease.poisoned) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "poisoned" });
        return;
    }
    if (!lease.ready && !rollbackRequest) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "not_ready" });
        return;
    }

    const sim = simulatedPlayers.get(name);
    const player = findRealPlayer(playerName);
    const botInventory = inventoryContainer(sim);
    const playerInventory = inventoryContainer(player);
    if (!sim || !player || !botInventory || !playerInventory) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "finish_unavailable" });
        return;
    }

    const editedSelected = rollbackRequest
        ? lease.botSelected
        : Math.max(0, Math.min(8, Number(player.selectedSlotIndex ?? 0)));
    try {
        swapMainInventories(playerInventory, botInventory);
    } catch (e) {
        if (String(e).includes("rollback failed")) lease.poisoned = true;
        reply("bot:inventory_error", {
            n: name, r: playerName, s: session,
            reason: lease.poisoned ? "finish_rollback_failed" : "finish_failed",
            e: String(e),
        });
        return;
    }

    try { sim.selectedSlotIndex = editedSelected; } catch (_) {}
    try { player.selectedSlotIndex = lease.playerSelected; } catch (_) {}
    const botModeRestored = unlockInventoryBot(sim, lease.gameMode);
    const playerModeRestored = unlockInventoryBot(player, lease.playerGameMode);
    if (!botModeRestored || !playerModeRestored) {
        lease.poisoned = true;
        reply("bot:inventory_error", {
            n: name, r: playerName, s: session,
            reason: "finish_restore_gamemode_failed",
        });
        return;
    }
    inventoryLeases.delete(name);
    reply("bot:inventory_committed", {
        n: name, r: playerName, s: session,
        bot_slot: editedSelected,
        rolled_back: rollbackRequest,
    });
}

function doInventoryRecover(req) {
    const name = String(req.n || "");
    const playerName = String(req.r || "");
    const session = String(req.s || "");
    if (!name || !playerName || !inventorySessionValid(session)) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "invalid_recovery" });
        return;
    }
    if (inventoryLeases.has(name) || playerAlreadyLeased(playerName)) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "already_locked" });
        return;
    }

    const sim = simulatedPlayers.get(name);
    const player = findRealPlayer(playerName);
    const botInventory = inventoryContainer(sim);
    const playerInventory = inventoryContainer(player);
    if (!sim || !player || !botInventory || !playerInventory) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "recovery_unavailable" });
        return;
    }
    if (!mainInventoryEmpty(botInventory)) {
        // Never overwrite a non-empty replacement bot. It may still own the
        // player's old inventory, so automatic reconstruction could duplicate.
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "recovery_bot_not_empty" });
        return;
    }

    const editedSelected = Math.max(0, Math.min(8, Number(player.selectedSlotIndex ?? 0)));
    let gameMode = "survival";
    const intendedPlayerGameMode = String(req.pg || "survival");
    let currentPlayerGameMode = intendedPlayerGameMode;
    try {
        gameMode = lockInventoryBot(sim);
        currentPlayerGameMode = lockInventoryBot(player);
        swapMainInventories(playerInventory, botInventory);
    } catch (e) {
        const poisoned = String(e).includes("rollback failed");
        if (poisoned) {
            inventoryLeases.set(name, {
                session,
                playerName,
                playerSelected: 0,
                botSelected: editedSelected,
                gameMode,
                playerGameMode: intendedPlayerGameMode,
                ready: false,
                recovery: true,
                poisoned: true,
            });
        } else {
            unlockInventoryBot(sim, gameMode);
            unlockInventoryBot(player, currentPlayerGameMode);
        }
        reply("bot:inventory_error", {
            n: name, r: playerName, s: session,
            reason: poisoned ? "recovery_rollback_failed" : "recovery_store_failed",
            e: String(e),
        });
        return;
    }

    inventoryLeases.set(name, {
        session,
        playerName,
        playerSelected: 0,
        botSelected: editedSelected,
        gameMode,
        playerGameMode: intendedPlayerGameMode,
        ready: false,
        recovery: true,
        poisoned: false,
    });
    try { sim.selectedSlotIndex = editedSelected; } catch (_) {}
    reply("bot:inventory_recovery_stored", {
        n: name, r: playerName, s: session,
        bot_slot: editedSelected,
        game_mode: gameMode,
    });
}

function doInventoryRecoveryFinalize(req) {
    const name = String(req.n || "");
    const playerName = String(req.r || "");
    const session = String(req.s || "");
    const lease = inventoryLeases.get(name);
    if (!lease || !lease.recovery || lease.session !== session || lease.playerName !== playerName) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "recovery_session_mismatch" });
        return;
    }
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "recovery_bot_lost" });
        return;
    }
    const player = findRealPlayer(playerName);
    if (!player) {
        reply("bot:inventory_error", { n: name, r: playerName, s: session, reason: "recovery_player_lost" });
        return;
    }
    const botModeRestored = unlockInventoryBot(sim, lease.gameMode);
    const playerModeRestored = unlockInventoryBot(player, lease.playerGameMode);
    if (!botModeRestored || !playerModeRestored) {
        lease.poisoned = true;
        reply("bot:inventory_error", {
            n: name, r: playerName, s: session,
            reason: "recovery_restore_gamemode_failed",
        });
        return;
    }
    inventoryLeases.delete(name);
    reply("bot:inventory_recovery_finalized", { n: name, r: playerName, s: session });
}

function clearAll() {
    const entries = Array.from(simulatedPlayers.entries());
    for (const [name, sim] of entries) {
        try {
            sim.disconnect();
        } catch (_) {}
        simulatedPlayers.delete(name);
        tridentBusy.delete(name);
        deadPlayers.delete(name);
        desiredPoses.delete(name);
    }
    pendingSpawns.length = 0;
    inventoryLeases.clear();
    reply("bot:cleared", { count: entries.length });
}

function sendList() {
    const names = Array.from(simulatedPlayers.keys()).filter((name) => !deadPlayers.has(name));
    if (names.length === 0) {
        reply("bot:list_result", { reset: true, done: true, names: [] });
        return;
    }
    let batch = [];
    let first = true;
    for (const name of names) {
        const candidate = [...batch, name];
        const probe = JSON.stringify({ reset: first, done: false, names: candidate, t: bridgeToken, p: PROTOCOL });
        if (batch.length > 0 && probe.length > MAX_MESSAGE_CHARS) {
            reply("bot:list_result", { reset: first, done: false, names: batch });
            first = false;
            batch = [name];
        } else {
            batch = candidate;
        }
    }
    reply("bot:list_result", { reset: first, done: true, names: batch });
}

function flushPositions(report) {
    if (report.length === 0) return;
    let batch = [];
    for (const item of report) {
        const candidate = [...batch, item];
        const probe = JSON.stringify({ p: candidate, t: bridgeToken, protocol: PROTOCOL });
        if (batch.length > 0 && probe.length > MAX_MESSAGE_CHARS) {
            reply("bot:positions", { p: batch });
            batch = [item];
        } else {
            batch = candidate;
        }
    }
    if (batch.length > 0) reply("bot:positions", { p: batch });
}

function ensureGameTestStructure() {
    const structureId = "endstone_bot:empty";

    try {
        const existing = world.structureManager.get(structureId);
        if (existing) return true;
    } catch (_) {}

    try {
        // Current Script API can persist at creation time.
        try {
            world.structureManager.createEmpty(
                structureId,
                { x: 1, y: 1, z: 1 },
                StructureSaveMode.World,
            );
        } catch (currentApiError) {
            // Keep the same compatibility path used by established fake-player
            // packs on older beta APIs.
            const structure = world.structureManager.createEmpty(
                structureId,
                { x: 1, y: 1, z: 1 },
            );
            if (typeof structure.saveToWorld !== "function") {
                throw currentApiError;
            }
            structure.saveToWorld();
        }

        return Boolean(world.structureManager.get(structureId));
    } catch (e) {
        console.warn(`[EndstoneBot] failed to create GameTest structure: ${e}`);
        return false;
    }
}

function startSimulatedPlayerGameTest() {
    if (gameTestStartRequested || activeTest) return;
    gameTestStartRequested = true;

    system.run(() => {
        if (!ensureGameTestStructure()) {
            gameTestStartRequested = false;
            return;
        }

        try {
            world.getDimension("overworld").runCommand(
                "execute positioned 15000000 256 15000000 run gametest run endstone_bot:sim_spawner"
            );
        } catch (e) {
            gameTestStartRequested = false;
            console.warn(`[EndstoneBot] failed to start SimulatedPlayer GameTest: ${e}`);
        }
    });
}

try {
    if (typeof GameTest.register === "function") {
        let registration = GameTest.register("endstone_bot", "sim_spawner", (test) => {
            activeTest = test;
            gameTestStartRequested = false;
            while (pendingSpawns.length > 0) doSpawn(pendingSpawns.shift());
        }).structureName("endstone_bot:empty").maxTicks(0x7fffffff);
        if (GameTest.Tags && GameTest.Tags.suiteDefault) {
            registration = registration.tag(GameTest.Tags.suiteDefault);
        }
        startSimulatedPlayerGameTest();
    }
} catch (e) {
    console.warn(`[EndstoneBot] GameTest registration failed: ${e}`);
}

system.afterEvents.scriptEventReceive.subscribe((event) => {
    if (!String(event.id || "").startsWith("bot:")) return;
    const data = parseMessage(event);
    if (!data) return;

    if (event.id === "bot:hello") {
        const kind = sourceKind(event) || "unknown";
        if (!isTrustedServerSource(event)) {
            console.warn(`[EndstoneBot] rejected hello from sourceType=${kind}`);
            return;
        }
        if (Number(data.p) !== PROTOCOL || typeof data.t !== "string" || !/^[0-9a-f]{32}$/i.test(data.t)) {
            console.warn("[EndstoneBot] rejected malformed hello payload");
            return;
        }
        if (bridgeToken && data.t !== bridgeToken) {
            console.warn("[EndstoneBot] rejected hello with a different active token");
            return;
        }
        bridgeToken = data.t;
        reply("bot:hello_ack", { ok: true });
        return;
    }

    if (!validCommand(event, data)) return;

    switch (event.id) {
        case "bot:ping":
            reply("bot:pong", {});
            break;
        case "bot:spawn":
            doSpawn(data);
            break;
        case "bot:remove":
            doRemove(data.n);
            break;
        case "bot:teleport":
            doTeleport(data);
            break;
        case "bot:trident":
            doThrowTrident(data);
            break;
        case "bot:inventory_begin":
            doInventoryBegin(data);
            break;
        case "bot:inventory_ready":
            doInventoryReady(data);
            break;
        case "bot:inventory_finish":
            doInventoryFinish(data);
            break;
        case "bot:inventory_recover":
            doInventoryRecover(data);
            break;
        case "bot:inventory_recovery_finalize":
            doInventoryRecoveryFinalize(data);
            break;
        case "bot:list":
            sendList();
            break;
        case "bot:clear":
            clearAll();
            break;
        case "bot:shutdown":
            clearAll();
            system.runTimeout(() => { bridgeToken = ""; }, 1);
            break;
        default:
            break;
    }
});

function trackedNameForEntity(entity) {
    let entityId = "";
    let entityName = "";
    try { entityId = String(entity?.id || ""); } catch (_) {}
    try { entityName = String(entity?.name || ""); } catch (_) {}

    for (const [name, sim] of simulatedPlayers.entries()) {
        try {
            if (entityId && String(sim.id || "") === entityId) return name;
        } catch (_) {}
        try {
            if (entityName && String(sim.name || "") === entityName) return name;
        } catch (_) {}
    }
    return "";
}

try {
    world.afterEvents.entityDie.subscribe((event) => {
        const name = trackedNameForEntity(event.deadEntity);
        if (!name) return;
        const sim = simulatedPlayers.get(name);
        if (!sim) return;

        deadPlayers.add(name);
        tridentBusy.delete(name);
        const lease = inventoryLeases.get(name);
        reply("bot:lost", {
            n: name,
            reason: "dead",
            inventory_session: lease ? String(lease.session || "") : "",
        });

        const pose = desiredPoses.get(name);
        if (!pose) return;
        system.runTimeout(() => {
            if (simulatedPlayers.get(name) !== sim || !deadPlayers.has(name)) return;
            respawnTracked(name, sim, pose);
        }, 1);
    });
} catch (e) {
    console.warn(`[EndstoneBot] entityDie subscription failed: ${e}`);
}

try {
    world.afterEvents.playerLeave.subscribe((event) => {
        const name = String(event.playerName || "");
        if (simulatedPlayers.has(name)) {
            // A death may also surface as a leave on some beta builds. The
            // entityDie handler owns that lifecycle and is about to respawn it.
            if (deadPlayers.has(name)) return;
            simulatedPlayers.delete(name);
            tridentBusy.delete(name);
            reply("bot:lost", { n: name, reason: "left" });
        }
    });
} catch (_) {}

system.runInterval(() => {
    if (!bridgeToken) return;
    reply("bot:heartbeat", {});
}, 100);

system.runInterval(() => {
    if (!bridgeToken || simulatedPlayers.size === 0) return;
    const report = [];
    for (const [name, sim] of Array.from(simulatedPlayers.entries())) {
        if (isDeadSim(name, sim)) {
            const pose = desiredPoses.get(name);
            if (pose) respawnTracked(name, sim, pose);
            continue;
        }
        try {
            if (!sim.isValid) {
                simulatedPlayers.delete(name);
                deadPlayers.delete(name);
                tridentBusy.delete(name);
                reply("bot:lost", { n: name, reason: "invalid" });
                continue;
            }
            const loc = sim.location;
            report.push({
                n: name,
                x: Math.round(loc.x * 100) / 100,
                y: Math.round(loc.y * 100) / 100,
                z: Math.round(loc.z * 100) / 100,
                d: sim.dimension ? sim.dimension.id : "minecraft:overworld",
            });
        } catch (_) {
            simulatedPlayers.delete(name);
            deadPlayers.delete(name);
            tridentBusy.delete(name);
            reply("bot:lost", { n: name, reason: "position_error" });
        }
    }
    flushPositions(report);
}, 100);

