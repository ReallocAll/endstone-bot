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
h.send('hello', {p:3,t:token}, 'Entity'); assert.equal(h.decode().length,0);
h.send('hello', {p:3,t:token}); assert.equal(h.decode()[0].event,'hello_ack');
h.send('hello', {p:3,t:nextToken}); assert.equal(h.decode().length,1);
h.send('spawn', {p:3,t:token,n:'Bot',definition:def,generation:gen,x:0,y:64,z:0,d:'overworld'});
const ack=h.decode().find(x=>x.event==='spawned').data;
assert.equal(ack.definition,def); assert.equal(ack.generation,gen); assert.equal(ack.runtime,gen);
assert(h.players[0].hasTag('endstone_bot_request:'+gen));
// Actual positions batches survive envelope encoding, including identity fields.
vm.runInContext(`flushPositions(Array.from({length:40}, (_,i)=>({n:'Bot'+i, x:i,y:64,z:0,d:'nether',definition:'${def}',generation:'${gen}',runtime:'${gen}'})))`,h.context);
const batches=h.decode().filter(x=>x.event==='positions');
assert(batches.length>1); assert.equal(batches.flatMap(x=>x.data.positions).length,40);
assert(batches.every(x=>x.data.p===3 && x.data.t===token && Array.isArray(x.data.positions)));
const before=h.decode().length; vm.runInContext('flushPositions([])',h.context);assert.equal(h.decode().length,before);
// Pending callbacks cannot run in the replacement authenticated session.
vm.runInContext('globalThis.late = false; sessionTimeout(()=>{globalThis.late=true}, 2)',h.context);
h.send('suspend',{p:3,t:'f'.repeat(32)});h.send('hello',{p:3,t:nextToken});assert(!h.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
h.send('suspend',{p:3,t:token}); assert(h.players[0].isValid);
h.send('hello',{p:3,t:nextToken});h.runTimers();assert.equal(h.context.late,false);
h.send('spawn',{p:3,t:nextToken,n:'Bot',definition:def,generation:'c'.repeat(32),x:0,y:64,z:0});
const adopted=h.decode().filter(x=>x.event==='spawned').at(-1).data;
assert.equal(adopted.runtime,gen);assert.equal(adopted.generation,'c'.repeat(32));
h.send('remove',{p:3,t:nextToken,n:'Bot',definition:'d'.repeat(32)});assert(h.players[0].isValid);
// A human occupying the same name never gains authentication tags.
const human={name:'Human',tags:new Set()};const humanHost=harness([human]);humanHost.send('hello',{p:3,t:token});
humanHost.send('spawn',{p:3,t:token,n:'Human',definition:def,generation:gen});
assert(!humanHost.decode().some(x=>x.event==='spawned'));assert.equal(human.tags.size,0);
// Runtime reset adoption preserves the provenance tag from the live simulated player.
const reset=harness(h.players);reset.send('hello',{p:3,t:token});reset.send('spawn',{p:3,t:token,n:'Bot',definition:def,generation:'e'.repeat(32)});
assert.equal(reset.decode().find(x=>x.event==='spawned').data.runtime,gen);
// Failed disconnect retains ownership and stops retrying at the bounded budget.
const blocked=harness();blocked.send('hello',{p:3,t:token});blocked.send('spawn',{p:3,t:token,n:'Bot',definition:def,generation:gen});
blocked.players[0].disconnect=()=>{};blocked.send('shutdown',{p:3,t:token});blocked.runTimers();
assert(blocked.warnings.some(x=>x.includes('drain blocked')));assert.equal(blocked.timers.length,0);
blocked.send('hello',{p:3,t:nextToken});assert(!blocked.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
// Suspend waits for existing item cleanup, but never spins indefinitely.
const busy=harness();busy.send('hello',{p:3,t:token});
vm.runInContext("tridentBusy.add('Bot')",busy.context);
busy.send('suspend',{p:3,t:token});busy.send('hello',{p:3,t:nextToken});
assert(!busy.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
vm.runInContext("tridentBusy.delete('Bot')",busy.context);busy.runTimers();
busy.send('hello',{p:3,t:nextToken});assert(busy.decode().some(x=>x.event==='hello_ack'&&x.data.t===nextToken));
const stuck=harness();stuck.send('hello',{p:3,t:token});
vm.runInContext("tridentBusy.add('Bot')",stuck.context);stuck.send('suspend',{p:3,t:token});stuck.runTimers();
assert(stuck.warnings.some(x=>x.includes('suspend blocked')));assert.equal(stuck.timers.length,0);
console.log('PASS: runtime identity, human collision, positions batches, retained/reset adoption, authenticated suspend, old callbacks, bounded drain');
// Optional producer fixture consumed by the Python bridge tests.
if (process.argv.includes('--json')) console.log(JSON.stringify(batches.map(x=>x.data)));
