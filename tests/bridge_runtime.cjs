// Execute the shipped producer, not a second implementation of its wire schema.
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(__dirname + '/../endstone_bot/behavior_pack/scripts/main.js', 'utf8').replace(/^import .*;\n/gm, '');
function harness(players = []) {
    let receive;
    const commands = [], warnings = [], timers = [], intervals = [];
    class Sim {
        constructor(name) { this.name = name; this.tags = new Set(); this.isValid = true; this.location = {x:0,y:64,z:0}; this.dimension = {id:'minecraft:overworld'}; }
        hasTag(t) { return this.tags.has(t); }
        addTag(t) { assert(t.length < 256); this.tags.add(t); return true; }
        getTags() { return [...this.tags]; }
        removeTag(t) { return this.tags.delete(t); }
        disconnect() { this.isValid = false; }
        respawn() { return true; }
        useItemInSlot() { return true; }
        teleport() {}
        setRotation() {}
        getComponent() { return null; }
    }
    const context = { GameTest: {SimulatedPlayer: Sim, spawnSimulatedPlayer: (pos, name) => { const s = new Sim(name); players.push(s); return s; }},
        GameMode: {Survival:'Survival'}, system: {afterEvents:{scriptEventReceive:{subscribe:f=>receive=f}},
        runInterval:f=>intervals.push(f), runTimeout:(f,t)=>timers.push(f)},
        world: {getAllPlayers:()=>players.filter(p=>p.isValid !== false), getDimension:()=>({runCommand:c=>commands.push(c)}),
            afterEvents:{entityDie:{subscribe(){}},playerLeave:{subscribe(){}}}}, console: {warn:s=>warnings.push(s)} };
    vm.createContext(context); vm.runInContext(source, context);
    const send = (id, data, sourceType='Server') => receive({id:'bot:'+id, sourceType, message:JSON.stringify(data)});
    const decode = () => commands.map(c=>({event:c.split(' ')[1], data:JSON.parse(Buffer.from(c.split(' ')[2], 'hex').toString('utf8'))}));
    const runTimers = (limit=500) => {let n=0; while(timers.length && n++ < limit) timers.shift()(); assert(n <= limit, 'unbounded timer retry');};
    return {context, send, decode, timers, intervals, runTimers, players, Sim, warnings};
}
const token = '1'.repeat(32), nextToken='2'.repeat(32), def='a'.repeat(32), gen='b'.repeat(32);
const h = harness();
h.send('hello', {p:4,t:token}, 'Entity'); assert.equal(h.decode().length,0);
h.send('hello', {p:4,t:token}); assert.equal(h.decode()[0].event,'hello_ack');
h.send('hello', {p:4,t:nextToken}); assert.equal(h.decode().length,1);
h.send('spawn', {maxOnline:10,p:4,t:token,n:'Bot',definition:def,generation:gen,x:0,y:64,z:0,d:'overworld'});
const ack=h.decode().find(x=>x.event==='spawned').data;
assert.equal(ack.definition,def); assert.equal(ack.generation,gen); assert.equal(ack.runtime,gen);
assert(h.players[0].hasTag('endstone_bot_request:'+gen));
// Actual positions batches survive envelope encoding, including identity fields.
vm.runInContext(`flushPositions(Array.from({length:40}, (_,i)=>({n:'Bot'+i, x:i,y:64,z:0,d:'nether',definition:'${def}',generation:'${gen}',runtime:'${gen}'})))`,h.context);
const batches=h.decode().filter(x=>x.event==='positions');
assert(batches.length>1); assert.equal(batches.flatMap(x=>x.data.positions).length,40);
assert(batches.every(x=>x.data.p===4 && x.data.t===token && Array.isArray(x.data.positions)));
const before=h.decode().length; vm.runInContext('flushPositions([])',h.context);assert.equal(h.decode().length,before);
// Pending callbacks cannot run in the replacement authenticated session.
vm.runInContext('globalThis.late = false; sessionTimeout(()=>{globalThis.late=true}, 2)',h.context);
h.send('suspend',{p:4,t:'f'.repeat(32)});h.send('hello',{p:4,t:nextToken});assert(!h.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
h.send('suspend',{p:4,t:token}); assert(h.players[0].isValid);
h.send('hello',{p:4,t:nextToken});h.runTimers();assert.equal(h.context.late,false);
h.send('spawn',{maxOnline:10,p:4,t:nextToken,n:'Bot',definition:def,generation:'c'.repeat(32),x:0,y:64,z:0});
const adopted=h.decode().filter(x=>x.event==='spawned').at(-1).data;
assert.equal(adopted.runtime,gen);assert.equal(adopted.generation,'c'.repeat(32));
h.send('remove',{p:4,t:nextToken,n:'Bot',definition:'d'.repeat(32)});assert(h.players[0].isValid);
// A human occupying the same name never gains authentication tags.
const human={name:'Human',tags:new Set()};const humanHost=harness([human]);humanHost.send('hello',{p:4,t:token});
humanHost.send('spawn',{maxOnline:10,p:4,t:token,n:'Human',definition:def,generation:gen});
assert(!humanHost.decode().some(x=>x.event==='spawned'));assert.equal(human.tags.size,0);
// Runtime reset adoption preserves the provenance tag from the live simulated player.
const reset=harness(h.players);reset.send('hello',{p:4,t:token});reset.send('spawn',{maxOnline:10,p:4,t:token,n:'Bot',definition:def,generation:'e'.repeat(32)});
assert.equal(reset.decode().find(x=>x.event==='spawned').data.runtime,gen);
// Failed disconnect retains ownership and stops retrying at the bounded budget.
const blocked=harness();blocked.send('hello',{p:4,t:token});blocked.send('spawn',{maxOnline:10,p:4,t:token,n:'Bot',definition:def,generation:gen});
blocked.players[0].disconnect=()=>{};blocked.send('shutdown',{p:4,t:token});blocked.runTimers();
assert(blocked.warnings.some(x=>x.includes('drain blocked')));assert.equal(blocked.timers.length,0);
blocked.send('hello',{p:4,t:nextToken});assert(!blocked.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
// Suspend waits for existing item cleanup, but never spins indefinitely.
const busy=harness();busy.send('hello',{p:4,t:token});
vm.runInContext("tridentBusy.add('Bot')",busy.context);
busy.send('suspend',{p:4,t:token});busy.send('hello',{p:4,t:nextToken});
assert(!busy.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
vm.runInContext("tridentBusy.delete('Bot')",busy.context);busy.runTimers();
busy.send('hello',{p:4,t:nextToken});assert(busy.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
const stuck=harness();stuck.send('hello',{p:4,t:token});
vm.runInContext("tridentBusy.add('Bot')",stuck.context);stuck.send('suspend',{p:4,t:token});stuck.runTimers();
assert(stuck.warnings.some(x=>x.includes('suspend blocked')));assert.equal(stuck.timers.length,0);

// Native admission is based on attached players, independently of stored definitions.
const spawnRequest = (i, maxOnline=10) => ({p:4,t:token,n:'Quota'+i,
    definition:i.toString(16).padStart(32,'0'), generation:(i+100).toString(16).padStart(32,'0'),
    maxOnline,x:0,y:64,z:0,d:'overworld'});
const activeCount = host => host.players.filter(p=>p.isValid !== false).length;
const lastSpawnError = host => host.decode().filter(x=>x.event==='error'&&x.data.op==='spawn').at(-1)?.data;
const quota=harness();quota.send('hello',{p:4,t:token});
for(let i=0;i<9;i++) quota.send('spawn',spawnRequest(i));
assert.equal(activeCount(quota),9);
// Two consecutive requests before any callback timer can run must not claim slot 10 twice.
quota.send('spawn',spawnRequest(9));quota.send('spawn',spawnRequest(10));
assert.equal(activeCount(quota),10);
assert.deepEqual(Object.fromEntries(['n','definition','generation','op','reason','maxOnline','onlineCount']
    .map(k=>[k,lastSpawnError(quota)[k]])),
    {n:'Quota10',definition:spawnRequest(10).definition,generation:spawnRequest(10).generation,
        op:'spawn',reason:'online_limit',maxOnline:10,onlineCount:10});
// An acknowledged remove is not assumed: a still-attached object retains its slot.
const retiring=quota.players[0];retiring.disconnect=()=>{};
quota.send('remove',{p:4,t:token,n:'Quota0',definition:spawnRequest(0).definition});
quota.send('spawn',spawnRequest(10));assert.equal(activeCount(quota),10);
assert.equal(lastSpawnError(quota).reason,'online_limit');
retiring.isValid=false;quota.runTimers();
quota.send('spawn',spawnRequest(10));assert.equal(activeCount(quota),10);
assert(quota.decode().some(x=>x.event==='spawned'&&x.data.n==='Quota10'));

// A script reset must count surviving managed players before list/reconcile runs.
const atCapacity=harness(quota.players);atCapacity.send('hello',{p:4,t:token});
atCapacity.send('spawn',spawnRequest(11));assert.equal(lastSpawnError(atCapacity).onlineCount,10);
const adoptedRequest={...spawnRequest(1,0),generation:'c'.repeat(32)};
atCapacity.send('spawn',adoptedRequest);
assert(atCapacity.decode().some(x=>x.event==='spawned'&&x.data.n==='Quota1'&&x.data.generation===adoptedRequest.generation));
assert.equal(activeCount(atCapacity),10);
// Reauthenticating and respawning the same object do not create a new player at the cap.
const survivor=atCapacity.players.find(p=>p.name==='Quota1');let health=0,respawns=0;
survivor.getComponent=id=>id==='minecraft:health'?{currentValue:health}:null;
survivor.respawn=()=>{health=20;respawns++;return true;};
atCapacity.send('spawn',{...adoptedRequest,generation:'d'.repeat(32)});atCapacity.runTimers();
assert.equal(respawns,1);assert.equal(activeCount(atCapacity),10);
assert(atCapacity.decode().some(x=>x.event==='spawned'&&x.data.respawned));

// A zero cap, missing cap and invalid caps can never create a native player.
const noSlots=harness();noSlots.send('hello',{p:4,t:token});
noSlots.send('spawn',spawnRequest(12,0));assert.equal(lastSpawnError(noSlots).reason,'online_limit');
assert.equal(lastSpawnError(noSlots).onlineCount,0);
for(const maxOnline of [undefined,-1,0.5,'10']) {
    noSlots.send('spawn',{...spawnRequest(12),maxOnline});assert.equal(activeCount(noSlots),0);
    assert.equal(lastSpawnError(noSlots).generation,spawnRequest(12).generation);
}
const legacy=harness();legacy.send('hello',{p:3,t:token});
assert(!legacy.decode().some(x=>x.event==='hello_ack'));

// Failed native creations return the exact request identity and reserve no phantom slot.
const failure=harness();failure.send('hello',{p:4,t:token});
const nativeSpawn=failure.context.GameTest.spawnSimulatedPlayer;
for(const create of [()=>{throw new Error('native failure');},()=>null,
    (pos,name)=>{const sim=nativeSpawn(pos,name);sim.getHeadLocation=()=>{throw new Error('orientation failure');};return sim;}]) {
    failure.context.GameTest.spawnSimulatedPlayer=create;
    const req={...spawnRequest(13,1),dx:1};failure.send('spawn',req);
    const error=lastSpawnError(failure);
    assert.equal(error.definition,req.definition);assert.equal(error.generation,req.generation);
    assert.equal(activeCount(failure),0);
}
failure.context.GameTest.spawnSimulatedPlayer=nativeSpawn;
failure.send('spawn',spawnRequest(14,1));assert.equal(activeCount(failure),1);
// A bind failure must report the new request identity instead of the old runtime binding.
const conflict={...spawnRequest(14),definition:'f'.repeat(32),generation:'e'.repeat(32)};
failure.send('spawn',conflict);
assert.equal(lastSpawnError(failure).definition,conflict.definition);
assert.equal(lastSpawnError(failure).generation,conflict.generation);
assert.equal(activeCount(failure),1);
// Requested respawn failures also terminate their pending request, including timer failures.
const broken=failure.players.find(p=>p.isValid);
broken.getComponent=id=>id==='minecraft:health'?{currentValue:0}:null;
broken.respawn=()=>false;
const failedRespawn={...spawnRequest(14),generation:'3'.repeat(32)};
failure.send('spawn',failedRespawn);
assert.equal(lastSpawnError(failure).generation,failedRespawn.generation);
assert.equal(activeCount(failure),0);
const delayed=harness();delayed.send('hello',{p:4,t:token});delayed.send('spawn',spawnRequest(15));
delayed.players[0].getComponent=id=>id==='minecraft:health'?{currentValue:0}:null;
delayed.players[0].teleport=()=>{throw new Error('respawn positioning failure');};
const failedTimer={...spawnRequest(15),generation:'4'.repeat(32)};
delayed.send('spawn',failedTimer);delayed.runTimers();
assert.equal(lastSpawnError(delayed).generation,failedTimer.generation);
assert.equal(activeCount(delayed),0);

const respawnDisconnect=harness();respawnDisconnect.send('hello',{p:4,t:token});
respawnDisconnect.send('spawn',spawnRequest(18,1));
const failedLive=respawnDisconnect.players[0];
failedLive.getComponent=id=>id==='minecraft:health'?{currentValue:0}:null;
failedLive.respawn=()=>false;failedLive.disconnect=()=>{};
respawnDisconnect.send('spawn',{...spawnRequest(18,1),generation:'5'.repeat(32)});
respawnDisconnect.send('spawn',spawnRequest(19,1));
assert.equal(lastSpawnError(respawnDisconnect).reason,'online_limit');
assert.equal(activeCount(respawnDisconnect),1);
failedLive.isValid=false;
respawnDisconnect.send('spawn',spawnRequest(19,1));
assert(respawnDisconnect.decode().some(x=>x.event==='spawned'&&x.data.n==='Quota19'));

// Setup can fail before the managed tag exists while disconnect is still pending.
// The native object must remain counted even though discovery cannot recognize it.
for(const setupFailure of ['bind','mark','renamed']) {
    const setup=harness();setup.send('hello',{p:4,t:token});
    const create=setup.context.GameTest.spawnSimulatedPlayer;
    setup.context.GameTest.spawnSimulatedPlayer=(pos,name)=>{
        const sim=create(pos,setupFailure==='renamed'?name+'(1)':name);
        sim.disconnect=()=>{};
        const addTag=sim.addTag.bind(sim);
        sim.addTag=tag=>{
            if(setupFailure==='bind'||(setupFailure==='mark'&&tag==='endstone_bot_managed')) {
                throw new Error('setup tag failure');
            }
            return addTag(tag);
        };
        return sim;
    };
    const req=spawnRequest(16,1);setup.send('spawn',req);
    assert.equal(lastSpawnError(setup).generation,req.generation);
    assert.equal(activeCount(setup),1);
    assert(!setup.players[0].hasTag('endstone_bot_managed'));
    setup.context.GameTest.spawnSimulatedPlayer=create;
    setup.send('spawn',spawnRequest(17,1));
    assert.equal(lastSpawnError(setup).reason,'online_limit');assert.equal(activeCount(setup),1);
    setup.players[0].isValid=false;
    setup.send('spawn',spawnRequest(17,1));
    assert(setup.decode().some(x=>x.event==='spawned'&&x.data.n==='Quota17'));
    assert.equal(activeCount(setup),1);
}
console.log('PASS: runtime identity, collisions, position batches, reload adoption, authenticated suspend, bounded drain, online admission, pending disconnect, zero cap, and spawn failure cleanup');
// Optional producer fixture consumed by the Python bridge tests.
if (process.argv.includes('--json')) console.log(JSON.stringify(batches.map(x=>x.data)));
