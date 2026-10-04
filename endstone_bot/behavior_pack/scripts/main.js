/**
 * Endstone Bot bridge v4.
 *
 * Security model:
 * - handshake is accepted only from ScriptEvent sourceType=Server
 * - the first valid bot:hello establishes the per-process token
 * - every later command must carry the exact same token and protocol
 *
 * Protocol 2 intentionally keeps the bridge small: spawn/remove/teleport/list/clear,
 * heartbeat, spawn acknowledgements, loss notifications, and batched positions.
 */

import * as GameTest from "@minecraft/server-gametest";
import { system, world } from "@minecraft/server";

const PROTOCOL = 2;
const MAX_MESSAGE_CHARS = 1400;
let bridgeToken = "";
let activeTest = null;

const simulatedPlayers = new Map();
const pendingSpawns = [];

function isServerSource(event) {
    try {
        return String(event.sourceType ?? "").toLowerCase().includes("server");
    } catch (_) {
        return false;
    }
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

function reply(eventId, data) {
    if (!bridgeToken) return false;
    const payload = { ...data, t: bridgeToken, p: PROTOCOL };
    const msg = JSON.stringify(payload);
    if (msg.length > 2048) {
        console.warn(`[EndstoneBot] refusing oversized ${eventId}: ${msg.length} chars`);
        return false;
    }
    try {
        world.getDimension("overworld").runCommand(`scriptevent ${eventId} ${msg}`);
        return true;
    } catch (e) {
        console.warn(`[EndstoneBot] reply failed (${eventId}): ${e}`);
        return false;
    }
}

function validCommand(event, data) {
    if (!isServerSource(event)) return false;
    if (!bridgeToken) return false;
    if (data.t !== bridgeToken) return false;
    return Number(data.p) === PROTOCOL;
}

function teleportSim(sim, req) {
    const dimension = getDimension(req.d);
    sim.teleport(
        { x: Number(req.x), y: Number(req.y), z: Number(req.z) },
        { dimension },
    );
}

function spawnWithGlobalApi(req) {
    const fn = GameTest.spawnSimulatedPlayer;
    if (typeof fn !== "function") return null;
    const dimension = getDimension(req.d);
    return fn(
        {
            dimension,
            x: Number(req.x),
            y: Number(req.y),
            z: Number(req.z),
        },
        String(req.n),
    );
}

function spawnWithTestApi(req) {
    if (!activeTest) return null;
    const sim = activeTest.spawnSimulatedPlayer({ x: 0, y: 2, z: 0 }, String(req.n));
    if (sim) teleportSim(sim, req);
    return sim;
}

function doSpawn(req) {
    const name = String(req.n || "");
    if (!name) return;
    const existing = simulatedPlayers.get(name);
    if (existing) {
        try {
            if (existing.isValid) {
                reply("bot:spawned", { n: name, ok: true, existed: true });
                return;
            }
        } catch (_) {}
        simulatedPlayers.delete(name);
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
    const sim = simulatedPlayers.get(name);
    if (!sim) {
        reply("bot:error", { n: name, e: "SimulatedPlayer not found" });
        return;
    }
    try {
        teleportSim(sim, req);
        reply("bot:teleported", { n: name });
    } catch (e) {
        reply("bot:error", { n: name, e: `teleport failed: ${String(e)}` });
    }
}

function clearAll() {
    const entries = Array.from(simulatedPlayers.entries());
    for (const [name, sim] of entries) {
        try {
            sim.disconnect();
        } catch (_) {}
        simulatedPlayers.delete(name);
    }
    pendingSpawns.length = 0;
    reply("bot:cleared", { count: entries.length });
}

function sendList() {
    const names = Array.from(simulatedPlayers.keys());
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
        if (!isServerSource(event)) return;
        if (Number(data.p) !== PROTOCOL || typeof data.t !== "string" || data.t.length < 16) return;
        if (bridgeToken && data.t !== bridgeToken) {
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

try {
    world.afterEvents.playerLeave.subscribe((event) => {
        const name = String(event.playerName || "");
        if (simulatedPlayers.has(name)) {
            simulatedPlayers.delete(name);
            reply("bot:lost", { n: name });
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
        try {
            if (!sim.isValid) {
                simulatedPlayers.delete(name);
                reply("bot:lost", { n: name });
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
            reply("bot:lost", { n: name });
        }
    }
    flushPositions(report);
}, 100);

console.log(`[EndstoneBot] bridge loaded, protocol=${PROTOCOL}`);
