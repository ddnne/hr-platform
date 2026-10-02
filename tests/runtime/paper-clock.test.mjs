import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';

// Exercise the real class with synthetic storage and a separate awaited service.
const bundle = await build({entryPoints:['workers/ingestion/paper-clock.ts'], bundle:true,
  write:false, format:'esm', plugins:[{name:'base', setup(b){
    b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'base',namespace:'stub'}));
    b.onLoad({filter:/.*/,namespace:'stub'},()=>({contents:
      'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env;}}'}));
  }}]});
const {PaperClock} = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);

function fixture() {
  let stored=null, next=1000, ticks=0, failure=false, release;
  const calls=[];
  const research={paper_next_alarm:async()=>next, paper_tick:async()=>{
    ticks++; if(failure) throw new Error('synthetic');
    if(release) await release.promise;
  }};
  const storage={getAlarm:async()=>stored, setAlarm:async at=>{calls.push(at);stored=at;},
    deleteAlarm:async()=>{calls.push(null);stored=null;}};
  const timer=new PaperClock({storage},{RESEARCH:research});
  return {timer,calls,get stored(){return stored;},get ticks(){return ticks;},
    set next(v){next=v;},set failure(v){failure=v;},
    wait(){let resolve;const promise=new Promise(r=>{resolve=r;});release={promise,resolve};return resolve;}};
}

test('one alarm follows revised plans; null disables; bad timestamps are rejected',async()=>{
  const f=fixture();
  await Promise.all([f.timer.sync(),f.timer.sync()]);
  assert.deepEqual(f.calls,[1000]);
  f.next=2000; await f.timer.sync();assert.equal(f.stored,2000);
  f.next=null; await f.timer.sync();assert.equal(f.stored,null);
  f.next=NaN; await assert.rejects(f.timer.sync(),/PAPER_ALARM_TIME/);
  f.next=3000; await f.timer.sync();assert.equal(f.stored,3000);
});

test('timer awaits calculation, ignores in-flight sync, then re-reads the plan',async()=>{
  const f=fixture();await f.timer.sync();
  const release=f.wait();const alarm=f.timer.alarm();
  while(!f.ticks) await Promise.resolve();
  f.next=4000;await f.timer.sync();assert.equal(f.stored,1000);
  release();await alarm;assert.equal(f.stored,4000);
});

test('RPC failure propagates for retry and releases the local running flag',async()=>{
  const f=fixture();f.failure=true;
  await assert.rejects(f.timer.alarm(),/synthetic/);
  f.next=5000;await f.timer.sync();assert.equal(f.stored,5000);
  f.failure=false;f.next=null;await f.timer.alarm();assert.equal(f.stored,null);
});
