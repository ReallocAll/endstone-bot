/**
 * Endstone Bot bridge v4.
 *
 * Security model:
 * - handshake accepts native Server sources and headless Endstone console origins
 * - the first valid bot:hello establishes the per-process token
 * - every later command must carry the exact same token and protocol
 *
 * Protocol 2 intentionally keeps the bridge small: spawn/remove/teleport/list/clear,
 * heartbeat, spawn acknowledgements, loss notifications, and batched positions.
 */

import * as GameTest from "@minecraft/server-gametest";
import { GameMode, ItemStack, system, world } from "@minecraft/server";

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

function teleportSim(sim, req) {
    const dimension = getDimension(req.d);
    const rotation = poseRotation(req);
    sim.teleport(
        { x: Number(req.x), y: Number(req.y), z: Number(req.z) },
        { dimension, rotation },
    );
    try {
        sim.setRotation(rotation);
    } catch (_) {}
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
                if (deadPlayers.has(name)) {
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
    if (deadPlayers.has(name)) {
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

function finishTrident(name, sim) {
    system.runTimeout(() => {
        try {
            sim.stopUsingItem();
            reply("bot:trident_thrown", { n: name, ok: true });
        } catch (e) {
            reply("bot:error", { n: name, e: `trident release failed: ${String(e)}` });
        } finally {
            tridentBusy.delete(name);
        }
    }, 12);
}

function doThrowTrident(req) {
    const name = String(req.n || "");
    const pose = rememberPose(req);
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:error", { n: name, e: "SimulatedPlayer not found" });
        return;
    }
    if (deadPlayers.has(name)) {
        if (!respawnTracked(name, sim, pose)) {
            reply("bot:error", { n: name, e: "failed to respawn before trident action" });
        }
        return;
    }
    if (tridentBusy.has(name)) {
        reply("bot:error", { n: name, e: "trident action already in progress" });
        return;
    }

    tridentBusy.add(name);
    try {
        teleportSim(sim, pose);
        const trident = new ItemStack("minecraft:trident", 1);
        if (!sim.setItem(trident, 0, true)) {
            tridentBusy.delete(name);
            reply("bot:error", { n: name, e: "failed to equip trident" });
            return;
        }
    } catch (e) {
        tridentBusy.delete(name);
        reply("bot:error", { n: name, e: `trident setup failed: ${String(e)}` });
        return;
    }

    system.runTimeout(() => {
        try {
            if (!sim.useItemInSlot(0)) {
                tridentBusy.delete(name);
                reply("bot:error", { n: name, e: "failed to start using trident" });
                return;
            }
            finishTrident(name, sim);
        } catch (e) {
            tridentBusy.delete(name);
            reply("bot:error", { n: name, e: `trident use failed: ${String(e)}` });
        }
    }, 1);
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
            simulatedPlayers.delete(name);
            tridentBusy.delete(name);
            deadPlayers.delete(name);
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
        if (deadPlayers.has(name)) continue;
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
