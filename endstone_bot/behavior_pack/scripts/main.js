/**
 * Endstone Bot bridge v4.
 *
 * Security model:
 * - handshake accepts native Server sources and headless Endstone console origins
 * - the first valid bot:hello establishes the per-process token
 * - every later command must carry the exact same token and protocol
 *
 * Protocol 2 intentionally keeps the bridge small: spawn/remove/teleport/trident/list/clear,
 * heartbeat, lifecycle acknowledgements, loss notifications, and batched positions.
 */

import * as GameTest from "@minecraft/server-gametest";
import { GameMode, system, world } from "@minecraft/server";

const PROTOCOL = 2;
const MAX_MESSAGE_CHARS = 1400;
let bridgeToken = "";
let activeTest = null;

const simulatedPlayers = new Map();
const pendingSpawns = [];
const tridentBusy = new Set();
const deadPlayers = new Set();
const desiredPoses = new Map();

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
        const result = world.getDimension("overworld").runCommand(
            `botbridge ${eventName} ${encoded}`
        );
        if (eventId === "bot:hello_ack") {
            console.log(
                `[EndstoneBot] botbridge hello_ack dispatched, successCount=${String(result?.successCount ?? "unknown")}`
            );
        }
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
        const nx = dx * invLength;
        const ny = dy * invLength;
        const nz = dz * invLength;

        // Minecraft rotation convention:
        //   yaw 0 = +Z, +90 = -X
        //   positive pitch looks downward.
        const rotation = {
            x: -Math.asin(Math.max(-1, Math.min(1, ny))) * 180 / Math.PI,
            y: Math.atan2(-nx, nz) * 180 / Math.PI,
        };

        let head = null;
        let target = null;
        try {
            head = sim.getHeadLocation();
            target = {
                x: head.x + nx * 32,
                y: head.y + ny * 32,
                z: head.z + nz * 32,
            };
        } catch (_) {}

        // Synchronize all three rotations used by different parts of the
        // SimulatedPlayer implementation.  In BDS 26.51 the visible entity
        // rotation can be correct while item-use still reads controller/body yaw.
        try {
            sim.setRotation(rotation);
        } catch (_) {}
        try {
            if (typeof sim.setBodyRotation === "function") {
                sim.setBodyRotation(rotation.y);
            }
        } catch (_) {}
        try {
            if (target && typeof sim.lookAtLocation === "function") {
                sim.lookAtLocation(target, GameTest.LookDuration?.Instant ?? "Instant");
            } else if (target) {
                sim.lookAt(target);
            }
        } catch (_) {}
        return;
    }

    const rotation = poseRotation(req);
    try {
        sim.setRotation(rotation);
    } catch (_) {}
    try {
        if (typeof sim.setBodyRotation === "function") {
            sim.setBodyRotation(rotation.y);
        }
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

function spawnWithGlobalApi(req) {
    const fn = GameTest.spawnSimulatedPlayer;
    if (typeof fn !== "function") return null;
    const dimension = getDimension(req.d);
    const location = {
        dimension,
        x: Number(req.x),
        y: Number(req.y),
        z: Number(req.z),
    };
    const name = String(req.n);

    // Current 2.x API requires GameMode. Keep string fallbacks for older beta builds.
    try {
        return fn(location, name, GameMode.Survival);
    } catch (currentError) {
        try {
            return fn(location, name, "Survival");
        } catch (_) {
            try {
                return fn(location, name, "survival");
            } catch (_) {
                throw currentError;
            }
        }
    }
}

function spawnWithTestApi(req) {
    if (!activeTest) return null;
    const sim = activeTest.spawnSimulatedPlayer({ x: 0, y: 2, z: 0 }, String(req.n));
    if (sim) teleportSim(sim, req);
    return sim;
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
        try {
            if (existing.isValid) {
                if (isDeadSim(name, existing)) {
                    respawnTracked(name, existing, pose);
                    return;
                }
                teleportSim(existing, pose);
                reply("bot:spawned", { n: name, ok: true, existed: true });
                return;
            }
        } catch (_) {}
        simulatedPlayers.delete(name);
        deadPlayers.delete(name);
        tridentBusy.delete(name);
    }

    let sim = null;
    let globalError = null;
    try {
        sim = spawnWithGlobalApi(req);
    } catch (e) {
        globalError = e;
    }

    if (!sim) {
        try {
            sim = spawnWithTestApi(req);
        } catch (e) {
            reply("bot:error", { n: name, e: `spawn fallback failed: ${String(e)}` });
            return;
        }
    }

    if (!sim) {
        if (!activeTest && typeof GameTest.spawnSimulatedPlayer !== "function") {
            if (!pendingSpawns.some((x) => String(x.n) === name)) pendingSpawns.push(req);
            return;
        }
        reply("bot:error", {
            n: name,
            e: globalError ? `spawn failed: ${String(globalError)}` : "spawnSimulatedPlayer returned null",
        });
        return;
    }

    simulatedPlayers.set(name, sim);
    deadPlayers.delete(name);
    teleportSim(sim, pose);
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

function doTeleport(req) {
    const name = String(req.n || "");
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

function hasUnsupportedTridentEnchantments(item) {
    try {
        const enchantable = item.getComponent("minecraft:enchantable");
        return Boolean(enchantable && enchantable.getEnchantments().length > 0);
    } catch (_) {
        return false;
    }
}

function normalizedPoseDirection(pose) {
    const dx = Number(pose.dx ?? 0);
    const dy = Number(pose.dy ?? 0);
    const dz = Number(pose.dz ?? 0);
    const lengthSq = dx * dx + dy * dy + dz * dz;
    if (!Number.isFinite(lengthSq) || lengthSq <= 1e-8) return null;
    const invLength = 1 / Math.sqrt(lengthSq);
    return {
        x: dx * invLength,
        y: dy * invLength,
        z: dz * invLength,
    };
}

function restoreInventoryItem(container, slot, originalItem) {
    try {
        container.setItem(slot, originalItem);
    } catch (_) {}
}

function removeOneInventoryItem(container, slot, originalItem) {
    const amount = Number(originalItem.amount ?? 1);
    if (amount <= 1) {
        container.setItem(slot);
        return;
    }
    const remaining = originalItem.clone();
    remaining.amount = amount - 1;
    container.setItem(slot, remaining);
}

function cleanupProjectile(entity) {
    try {
        if (entity?.isValid) entity.remove();
    } catch (_) {}
}

function tridentResult(name, requester, ok, reason = "") {
    reply("bot:trident_result", {
        n: name,
        r: String(requester || ""),
        ok: Boolean(ok),
        reason: String(reason || ""),
    });
}

function doThrowTrident(req) {
    const name = String(req.n || "");
    const requester = String(req.r || "");
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

    const found = findTrident(sim);
    if (!found) {
        tridentResult(name, requester, false, "no_trident");
        return;
    }
    if (hasUnsupportedTridentEnchantments(found.item)) {
        tridentResult(name, requester, false, "enchanted_trident_unsupported");
        return;
    }

    const direction = normalizedPoseDirection(pose);
    if (!direction) {
        tridentResult(name, requester, false, "invalid_direction");
        return;
    }

    tridentBusy.add(name);
    let projectileEntity = null;
    let inventoryChanged = false;
    const originalItem = found.item.clone();

    try {
        teleportSim(sim, pose);
        orientSim(sim, pose);

        const head = sim.getHeadLocation();
        const launch = {
            x: head.x + direction.x * 0.6,
            y: head.y + direction.y * 0.6,
            z: head.z + direction.z * 0.6,
        };

        projectileEntity = sim.dimension.spawnEntity("endstone_bot:thrown_trident", launch);
        const projectile = projectileEntity.getComponent("minecraft:projectile");
        if (!projectile) {
            throw new Error("minecraft:projectile component missing");
        }

        // Set owner before launch so collision, damage and attacker attribution
        // are evaluated as a player-thrown projectile.
        projectile.owner = sim;

        // Consume exactly the physical trident that was found in the bot's
        // inventory. Any later failure restores the exact ItemStack clone.
        removeOneInventoryItem(found.container, found.slot, originalItem);
        inventoryChanged = true;

        const speed = 2.5;
        projectile.shoot({
            x: direction.x * speed,
            y: direction.y * speed,
            z: direction.z * speed,
        });

        try {
            sim.dimension.playSound("item.trident.throw", head, {
                volume: 1.0,
                pitch: 1.0,
            });
        } catch (_) {}

        tridentResult(name, requester, true);
    } catch (e) {
        cleanupProjectile(projectileEntity);
        if (inventoryChanged) {
            restoreInventoryItem(found.container, found.slot, originalItem);
        }
        tridentResult(name, requester, false, `projectile_failed:${String(e)}`);
    } finally {
        tridentBusy.delete(name);
    }
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

try {
    if (typeof GameTest.register === "function") {
        let registration = GameTest.register("endstone_bot", "sim_spawner", (test) => {
            activeTest = test;
            while (pendingSpawns.length > 0) doSpawn(pendingSpawns.shift());
        }).structureName("endstone_bot:empty").maxTicks(0x7fffffff);
        if (GameTest.Tags && GameTest.Tags.suiteDefault) {
            registration = registration.tag(GameTest.Tags.suiteDefault);
        }
    }
} catch (e) {
    console.warn(`[EndstoneBot] fallback GameTest registration failed: ${e}`);
}

system.afterEvents.scriptEventReceive.subscribe((event) => {
    if (!String(event.id || "").startsWith("bot:")) return;
    const data = parseMessage(event);
    if (!data) return;

    if (event.id === "bot:hello") {
        const kind = sourceKind(event) || "unknown";
        console.log(
            `[EndstoneBot] hello received: sourceType=${kind}, messageLength=${String(event.message ?? "").length}`
        );
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
        reply("bot:lost", { n: name, reason: "dead" });

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

console.log(`[EndstoneBot] bridge loaded, protocol=${PROTOCOL}`);
