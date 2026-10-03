import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {autoBody,autoCatalog,autoProgram,boatProgram,keirinProgram,keirinRunners} from '../fixtures/synthetic/sports.mjs';
const config=JSON.parse(await readFile('configs/sports-collection.json','utf8'));
const bundle=async contents=>(await build({stdin:{contents,resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'})).outputFiles[0].text;
const pure=await bundle(`export * from './workers/sports/daily-plan';export {parseProgram} from './workers/sports/discovery';export {resourceId} from './workers/sports/storage';`);
const p=await import(`data:text/javascript;base64,${Buffer.from(pure).toString('base64')}`);
const now=Date.parse('2000-01-01T00:55:00Z');
const boat={sport:'boat',race_id:'boat:20000101:2:0',kind:'schedule',url:'https://www.boatrace.jp/owpc/pc/race/raceindex?hd=20000101&jcd=02'};
function source(raw,target,at=now){return {value:p.parseProgram(raw,target),target,event:'synthetic-program',received_at:new Date(at).toISOString(),available_at:new Date(at).toISOString()};}
async function races(sport,raw,target,at=now){const s=await p.initialDaily(sport,at);s.tasks={};await p.acceptProgram(s,source(raw,target,at),at);return s;}
test('business day covers after midnight and waits through the configured morning gap',async()=>{
 assert.equal(p.businessDay(Date.parse('2000-01-01T16:00:00Z')).day,'20000101');
 const gap=Date.parse('2000-01-01T21:30:00Z'),s=await p.initialDaily('boat',gap);
 assert.equal(s.day,'20000102');assert.deepEqual(p.nextDaily(s,gap),[]);assert.equal(s.wake_at,Date.parse('2000-01-01T22:00:00Z'));
 for(const sport of ['auto','boat','keirin'])assert.ok(p.catalogTargets(sport,'20000101').length);
});
test('whole offered market group has fixed slots, partial completion and redelivery cannot add a round',async()=>{
 const s=await races('boat',boatProgram,boat),e=p.nextDaily(s,now);assert.equal(e.length,5);
 assert.ok(e.every(x=>x.target.race_id==='boat:20000101:2:1'));assert.deepEqual(p.nextDaily(s,now+20000),e);
 const first=e[0];await p.completeDaily(s,first,'RAW_STORED',now+1000);
 assert.equal(s.action.entries.length,4);await p.completeDaily(s,first,'RAW_STORED',now+2000);assert.equal(s.action.entries.length,4);
 for(const x of e.slice(1))await p.completeDaily(s,x,'RAW_STORED',now+25000);
 assert.equal(s.action,null);assert.equal(s.tasks[first.daily_task].next_at,now+25000+config.interval_seconds*1000);
 await p.completeDaily(s,first,'RAW_STORED',now+30000);assert.equal(s.tasks[first.daily_task].last_at,now+25000);
});
test('rollover recovers fixed prior-day action before replacing the business day',async()=>{
 const s=await races('boat',boatProgram,boat),e=p.nextDaily(s,now),tomorrow=now+86400000;
 assert.deepEqual(p.nextDaily(s,tomorrow),e);for(const x of e)await p.completeDaily(s,x,'MISSED_WINDOW',tomorrow);
 assert.throws(()=>p.nextDaily(s,tomorrow),/DAILY_ROLLOVER_REQUIRED/);assert.equal((await p.initialDaily('boat',tomorrow)).day,'20000102');
});
test('fresh successful program replaces disappeared races and failed/stale clocks never generate odds',async()=>{
 const s=await races('boat',boatProgram,boat),reduced=boatProgram.replace(/<tr><td><a[^>]+>1R<\/a>[\s\S]*?<\/tr>/,'');
 await p.acceptProgram(s,source(reduced,boat,now+1),now+1);assert.equal(s.races['boat:20000101:2:1'].clock,undefined);
 assert.ok(p.nextDaily(s,now+1).every(e=>e.target.race_id!=='boat:20000101:2:1'));
 const stale=await races('boat',boatProgram,boat);assert.deepEqual(p.nextDaily(stale,now+(config.discovery.maximum_program_age_seconds+1)*1000),[]);
 const failed=await races('boat',boatProgram,boat);p.rejectProgram(failed,boat);assert.deepEqual(p.nextDaily(failed,now),[]);
});
test('earlier/later advertised close adjusts timing and whole groups stop before close',async()=>{
 const s=await races('boat',boatProgram,boat);await p.acceptProgram(s,source(boatProgram.replace('10:10','09:54'),boat),now);
 const e=p.nextDaily(s,now);assert.ok(e.every(x=>x.target.race_id!=='boat:20000101:2:1'));
 s.action=null;await p.acceptProgram(s,source(boatProgram.replace('10:10','12:10'),boat),now);
 assert.equal(s.tasks['odds:boat:20000101:2:1'].next_at,Date.parse('2000-01-01T02:10:00Z'));
});
test('auto venue programme expands declared race count and results have separate recipes',async()=>{
 const target={sport:'auto',race_id:'auto:20000101:6:7',kind:'schedule',discovery_stage:'venue',url:'https://autorace.jp/race_info/OtherRaceInfo',body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:7})};
 const s=await races('auto',autoProgram,target);assert.equal(Object.values(s.tasks).filter(t=>t.kind==='request').length,7);
 const result=p.resultTarget(source(autoProgram,target),target.race_id);assert.equal(result.kind,'result');assert.ok(result.url.endsWith('/RaceResult'));
});
test('keirin guest failure respects configured retry interval and holds other requests',async()=>{
 const s=await p.initialDaily('keirin',now),e=p.nextDaily(s,now,false);assert.equal(e.length,1);assert.ok(e[0].target.url.endsWith('/top'));
 await p.completeDaily(s,e[0],'RAW_STORED',now+1000);assert.deepEqual(p.nextDaily(s,now+6000,false),[]);
 assert.equal(s.wake_at,now+1000+config.daily.guest_interval_seconds*1000);
});
test('keirin same context runners determine all offered markets; mismatched/unknown support defers whole group',async()=>{
 const target={sport:'keirin',race_id:'keirin:20000101:47:2',kind:'guest',discovery_stage:'race',context_event:'synthetic-context',url:'https://keirin.jp/pc/racelive',form:true,body:'encp=synthetic-navigation'};
 const raw=keirinProgram.replace('17:02','10:02'),s=await races('keirin',raw,target);
 const runners={...target,kind:'schedule',form:undefined,body:undefined,url:'https://keirin.jp/pc/json?type=JST010&encp=synthetic-navigation&url.media.flg=1'};
 const noFrames=keirinRunners.replace('"wakuKbn":"1"','"wakuKbn":"0"');await p.acceptProgram(s,source(noFrames,runners),now);
 assert.equal(p.nextDaily(s,now).length,5);assert.ok(p.resultTarget(source(raw,target),target.race_id).context_event===target.context_event);
 for(const modification of ['mismatch','unknown','stale']){s.action=null;
  const r=source(noFrames,runners);if(modification==='mismatch')r.value.program.identity_evidence='different';
  if(modification==='unknown')r.value.program.runners.frame_category_label='unknown';
  if(modification==='stale')r.received_at=new Date(now-(config.discovery.maximum_program_age_seconds+1)*1000).toISOString();
  s.races[target.race_id].runners=r;s.tasks['odds:'+target.race_id].next_at=now;assert.deepEqual(p.nextDaily(s,now),[]);
 }
});
const doScript=(await build({stdin:{contents:`import {SportsCollector} from './workers/sports/index';
export class DailyTest extends SportsCollector {
 constructor(ctx,env){let failed=false;super(ctx,env.TEST_FAULT==='publish'?{...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),batch:async(...a)=>{if(!failed){failed=true;throw new Error('SYNTHETIC_PUBLISH_FAULT');}return env.INDEX.batch(...a);}}}:env);}
 async seed(s,entries){await this.ctx.storage.put({'daily-state':s,'daily-sport':s.sport});if(entries)await this.schedule(entries);}
 async inspect(){return {state:await this.ctx.storage.get('daily-state'),plan:await this.planState()};}
 async step(now,enabled,session){const clock=Date.now;Date.now=()=>now;try{if(enabled!==undefined)this.env.SPORTS_DAILY_ENABLED=String(enabled);if(session)await this.ctx.storage.put('guest-session',{cookie:'SYNTHETIC',token:'SYNTHETIC',expires:now+3600000});await this.alarm();return this.inspect();}finally{Date.now=clock;}}
}
export default {async fetch(req,env){const v=await req.json(),s=env.SPORTS.get(env.SPORTS.idFromName(v.sport??'auto'));try{if(v.op==='seed'){await s.seed(v.state,v.entries);return Response.json(true);}if(v.op==='step')return Response.json(await s.step(v.now,v.enabled,v.session));if(v.op==='ensure')return Response.json(await s.ensureDaily(v.sport));return Response.json(await s.inspect());}catch(e){return Response.json({error:e.message},{status:500});}}};`,resolveDir:process.cwd()},external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'})).outputFiles[0].text;
const schema=(await Promise.all(['0001_capture','0002_processing_metrics','0010_sports_history'].map(n=>readFile('migrations/'+n+'.sql','utf8')))).join('\n');
async function runtime({enabled=true,fault='',body=autoCatalog}={}){
 const requests=[];const mf=new Miniflare(convertV4MiniflareOptions({name:'sports-daily-test',modules:true,script:doScript,compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
 bindings:{SPORTS_ENABLED:'true',SPORTS_DAILY_ENABLED:String(enabled),SPORTS_PROVIDERS_JSON:'["auto","boat","keirin"]',TEST_FAULT:fault},durableObjects:{SPORTS:{className:'DailyTest',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),
 outboundService:async req=>{requests.push(req.url);return new Response(typeof body==='function'?await body(req):body);}}));
 const db=await mf.getD1Database('INDEX');for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=async v=>{const r=await mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)});const result=await r.json();assert.equal(r.status,200,JSON.stringify(result));return result;};
 return {mf,db,call,requests};
}
async function fixture(at=Date.now()+60000){
 const window=p.businessDay(at);at=Math.max(at,window.start);
 if(at+900000>=window.end)at=p.businessDay(window.end).start;
 const s=await p.initialDaily('auto',at),e=p.nextDaily(s,at);return {s,e,at:e[0].at};
}
test('daily disabled does not arm discovery; finite reservation remains unchanged',async()=>{
 const r=await runtime({enabled:false});try{assert.equal(await r.call({op:'ensure',sport:'auto'}),'DISABLED');assert.equal((await r.call({})).plan.pending,0);assert.equal(r.requests.length,0);}finally{await r.mf.dispose();}
});
test('saved action before queue insertion recovers overdue slots as missing without HTTP',async()=>{
 const r=await runtime();try{const {s,e,at}=await fixture();await r.call({op:'seed',state:s});
 const result=await r.call({op:'step',now:at+(config.capture_window_seconds+1)*1000});
 assert.equal(result.state.action,null);assert.equal(result.state.report.status,'MISSED_WINDOW');assert.equal(r.requests.length,0);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM captures WHERE status='MISSED_WINDOW'").first()).n,1);
 }finally{await r.mf.dispose();}
});
test('stopping daily cancels unsent slots while retaining future finite reservation',async()=>{
 const r=await runtime();try{const {s,e,at}=await fixture();const future={at:at+120000,target:e[0].target};await r.call({op:'seed',state:s,entries:[...e,future]});
 const result=await r.call({op:'step',now:at,enabled:false});assert.equal(result.state.action,null);assert.equal(result.plan.pending,1);assert.equal(result.plan.alarm_at,future.at);assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});
test('daily stop repairs saved raw publication and terminates action with no repeated HTTP',async()=>{
 const {s,e,at}=await fixture(),body=autoCatalog.replaceAll('2000-01-01',`${s.day.slice(0,4)}-${s.day.slice(4,6)}-${s.day.slice(6,8)}`),r=await runtime({fault:'publish',body});
 try{await r.call({op:'seed',state:s,entries:e});assert.equal((await r.call({op:'step',now:at})).plan.pending,1);assert.equal(r.requests.length,1);
 const result=await r.call({op:'step',now:at+6000,enabled:false});assert.equal(result.plan.pending,0);assert.equal(result.state.action,null);assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('future finite reservation allows daily discovery and next alarm is generated inside DO',async()=>{
 const {s,e,at}=await fixture(),body=autoCatalog.replaceAll('2000-01-01',`${s.day.slice(0,4)}-${s.day.slice(4,6)}-${s.day.slice(6,8)}`),r=await runtime({body});
 try{s.action=null;s.wake_at=at-1000;const finite={at:at+240000,target:e[0].target};await r.call({op:'seed',state:s,entries:[finite]});
 const planned=await r.call({op:'step',now:at-1000});assert.equal(planned.plan.pending,2);assert.equal(planned.plan.alarm_at,at);
 const result=await r.call({op:'step',now:at});assert.equal(r.requests.length,1);assert.equal(result.plan.pending,1);
 assert.ok(Object.values(result.state.tasks).some(t=>t.target?.url.endsWith('/OtherRaceInfo')));assert.ok(result.plan.alarm_at<finite.at);
 }finally{await r.mf.dispose();}
});
test('completed capture before action completion recovers the same observation without HTTP',async()=>{
 const {s,e,at}=await fixture(),body=autoCatalog.replaceAll('2000-01-01',`${s.day.slice(0,4)}-${s.day.slice(4,6)}-${s.day.slice(6,8)}`),r=await runtime({body});
 try{await r.call({op:'seed',state:s,entries:e});await r.call({op:'step',now:at});assert.equal(r.requests.length,1);
 await r.call({op:'seed',state:s});const result=await r.call({op:'step',now:at+100000});assert.equal(result.state.action,null);assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('DO generates catalog, per-race programs and odds alarms with no caller schedule after initialization',async()=>{
 const {s,e,at}=await fixture(),day=`${s.day.slice(0,4)}-${s.day.slice(4,6)}-${s.day.slice(6,8)}`;
 const midnight=Date.parse(day+'T00:00:00Z')-config.discovery.clock_timezone_offset_minutes*60000,
  minutes=Math.floor((at+900000-midnight)/60000),close=String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');
 const r=await runtime({body:async req=>{
  if(req.url.endsWith('/Today'))return autoCatalog.replaceAll('2000-01-01',day);
  if(req.url.endsWith('/Odds'))return autoBody();
  const b=JSON.parse(autoProgram);b.body.raceNo=JSON.parse(await req.text()).raceNo;b.body.telvoteTime=close;b.body.raceStartTime=close;return JSON.stringify(b);
 }});
 try{await r.call({op:'seed',state:s,entries:e});let result=await r.call({op:'step',now:at,session:true});
 for(let i=0;i<30&&!r.requests.some(url=>url.endsWith('/Odds'));i++)result=await r.call({op:'step',now:result.plan.alarm_at,session:true});
 assert.ok(r.requests.some(url=>url.endsWith('/OtherRaceInfo')));assert.ok(r.requests.some(url=>url.endsWith('/Odds')));
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='COMPLETE'").first()).n,1);
 assert.ok(result.plan.alarm_at>at);assert.ok(Object.keys(result.state.races).length>0);
 }finally{await r.mf.dispose();}
});
test('full future finite queue defers daily without failed alarms or overwriting reservations',async()=>{
 const {s,e,at}=await fixture(),r=await runtime();try{
 s.action=null;s.wake_at=at-1000;const finite=Array.from({length:config.maximum_pending_requests},(_,i)=>({at:at+240000+i*config.request_spacing_seconds*1000,target:e[0].target}));
 await r.call({op:'seed',state:s,entries:finite});const result=await r.call({op:'step',now:at-1000});
 assert.equal(result.plan.pending,config.maximum_pending_requests);assert.equal(result.plan.alarm_at,finite[0].at);
 assert.equal(result.state.action,null);assert.equal(result.state.report.status,'CAPACITY_WAIT');assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});
