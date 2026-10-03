import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {autoBody,autoCatalog,autoProgram,boatProgram,boatOddsBody,keirinProgram,keirinRunners} from '../fixtures/synthetic/sports.mjs';
import {keirinResult} from '../fixtures/synthetic/sports-results.mjs';
const config=JSON.parse(await readFile('configs/sports-collection.json','utf8'));
const bundle=async contents=>(await build({stdin:{contents,resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'})).outputFiles[0].text;
const pure=await bundle(`export * from './workers/sports/daily-plan';export {parseProgram} from './workers/sports/discovery';export {resourceId,completePayoutMarkets} from './workers/sports/storage';export {parseResult} from './workers/sports/results';export {oddsTargets} from './workers/sports/odds-plan';`);
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
test('parallel daily rounds retain separate race completion and preserve the old action on rollover',async()=>{
 const s=await races('boat',boatProgram,boat),old=p.nextDaily(s,now),entries=p.nextDailyParallel(s,now);
 assert.deepEqual(s.actions[old[0].daily_task].entries,old);assert.equal(entries.length,15);
 const second=entries.filter(e=>e.target.race_id==='boat:20000101:2:2');
 for(const e of second)await p.completeDaily(s,e,'RAW_STORED',now+25000);
 assert.ok(!s.actions[second[0].daily_task]);assert.deepEqual(s.actions[old[0].daily_task].entries,old);
 assert.equal(s.tasks[second[0].daily_task].last_at,now+25000);
 assert.deepEqual(p.nextDailyParallel(s,now+86400000),entries.filter(e=>e.target.race_id!=='boat:20000101:2:2'));
});
test('unchanged program retains overdue odds priority and published result completion',async()=>{
 const s=await races('boat',boatProgram,boat),key='odds:boat:20000101:2:1',due=s.tasks[key].next_at;
 const result=s.tasks['result:boat:20000101:2:1'];result.done=true;
 await p.acceptProgram(s,source(boatProgram,boat,now+10000),now+10000);
 assert.equal(s.tasks[key].next_at,due);assert.equal(result.done,true);
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
test('closed odds have a separate finite window and every offered page must fit',()=>{
 const s=source(boatProgram,boat),race='boat:20000101:2:1',close=Date.parse('2000-01-01T01:10:00Z');
 assert.equal(p.oddsTargets(s,race,close).targets.length,0);
 const earliest=close+config.daily.final_odds_delay_seconds*1000,last=close+config.daily.final_odds_window_seconds*1000;
 assert.equal(p.oddsTargets(s,race,earliest-1,undefined,'CLOSED').targets.length,0);
 assert.equal(p.oddsTargets(s,race,earliest,undefined,'CLOSED').targets.length,5);
 const span=4*config.request_spacing_seconds*1000;
 assert.equal(p.oddsTargets(s,race,last-span,undefined,'CLOSED').targets.length,5);
 assert.equal(p.oddsTargets(s,race,last-span+1,undefined,'CLOSED').targets.length,0);
});
async function closingBoat(){
 const at=Date.parse('2000-01-01T01:10:00Z')+config.daily.final_odds_delay_seconds*1000;
 const s=await races('boat',boatProgram,boat,at),key='final_odds:boat:20000101:2:1';
 s.tasks={[key]:s.tasks[key]};return {s,key,at};
}
test('closing completion requires the same entire closed round; redelivery cannot complete or poison another round',async()=>{
 const {s,key,at}=await closingBoat(),first=p.nextDaily(s,at);
 assert.equal(first.length,5);
 await p.completeDaily(s,first[0],'RAW_STORED',at+1000,true);
 await p.completeDaily(s,first[0],'RAW_STORED',at+2000,false);
 assert.equal(s.action.closed_odds,true);
 for(const [i,e] of first.slice(1).entries())await p.completeDaily(s,e,'RAW_STORED',at+25000,i!==0);
 assert.ok(!s.tasks[key].done);assert.equal(s.action,null);
 const later=s.tasks[key].next_at,second=p.nextDaily(s,later);
 await p.completeDaily(s,first[1],'RAW_STORED',later,false);
 assert.equal(s.action.entries.length,5);assert.equal(s.action.closed_odds,true);
 for(const e of second)await p.completeDaily(s,e,'RAW_STORED',later+25000,true);
 assert.equal(s.tasks[key].done,true);
 await p.acceptProgram(s,source(boatProgram,boat,later+26000),later+26000);
 assert.equal(s.tasks[key].done,true);
 await p.acceptProgram(s,source(boatProgram.replace('10:10','10:30'),boat,later+27000),later+27000);
 assert.equal(s.tasks[key].done,false);assert.equal(s.tasks[key].close_at,Date.parse('2000-01-01T01:30:00Z'));
});
test('finite closing task expires despite missing clock and a later retry appointment',async()=>{
 const {s,key,at}=await closingBoat();delete s.races['boat:20000101:2:1'].clock;
 assert.deepEqual(p.nextDaily(s,at),[]);assert.ok(!s.tasks[key].done);
 const end=s.tasks[key].close_at+config.daily.final_odds_window_seconds*1000;
 s.tasks[key].next_at=end+config.daily.deferred_interval_seconds*1000;
 assert.deepEqual(p.nextDaily(s,end+1),[]);assert.equal(s.tasks[key].done,true);
});
test('closing round yields to preclose odds due during the configured request budget',async()=>{
 const {s,at}=await closingBoat(),next='odds:boat:20000101:2:2';
 s.tasks[next]={kind:'odds',race_id:'boat:20000101:2:2',next_at:at+5000,interval:config.interval_seconds};
 assert.deepEqual(p.nextDaily(s,at),[]);assert.equal(s.wake_at,at+5000);
 const entries=p.nextDaily(s,at+5000);assert.equal(entries.length,5);assert.equal(entries[0].daily_task,next);
});
const doScript=(await build({stdin:{contents:`import {SportsCollector} from './workers/sports/index';
export class DailyTest extends SportsCollector {
 constructor(ctx,env){let failed=false;super(ctx,env.TEST_FAULT==='publish'?{...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),batch:async(...a)=>{if(!failed){failed=true;throw new Error('SYNTHETIC_PUBLISH_FAULT');}return env.INDEX.batch(...a);}}}:env);}
 async seed(s,entries){await this.ctx.storage.put({'daily-state':s,'daily-sport':s.sport});if(entries)await this.schedule(entries);}
 async inspect(){return {state:await this.ctx.storage.get('daily-state'),plan:await this.planState(),guest_received_at:await this.ctx.storage.get('keirin-guest-received-at')};}
 async step(now,enabled,session,advance){const clock=Date.now,start=performance.now();Date.now=()=>now+(advance?Math.floor(performance.now()-start):0);try{if(enabled!==undefined)this.env.SPORTS_DAILY_ENABLED=String(enabled);if(session)await this.ctx.storage.put('guest-session',typeof session==='object'?session:{cookie:'SYNTHETIC',token:'SYNTHETIC',expires:now+3600000});await this.alarm();return this.inspect();}finally{Date.now=clock;}}
}
export default {async fetch(req,env){const v=await req.json(),s=env.SPORTS.get(env.SPORTS.idFromName(v.sport??'auto'));try{if(v.op==='seed'){await s.seed(v.state,v.entries);return Response.json(true);}if(v.op==='step')return Response.json(await s.step(v.now,v.enabled,v.session,v.advance));if(v.op==='ensure')return Response.json(await s.ensureDaily(v.sport));return Response.json(await s.inspect());}catch(e){return Response.json({error:e.message},{status:500});}}};`,resolveDir:process.cwd()},external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'})).outputFiles[0].text;
const schema=(await Promise.all(['0001_capture','0002_processing_metrics','0010_sports_history'].map(n=>readFile('migrations/'+n+'.sql','utf8')))).join('\n');
async function runtime({enabled=true,fault='',body=autoCatalog}={}){
 const requests=[];const mf=new Miniflare(convertV4MiniflareOptions({name:'sports-daily-test',modules:true,script:doScript,compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
 bindings:{SPORTS_ENABLED:'true',SPORTS_DAILY_ENABLED:String(enabled),SPORTS_PROVIDERS_JSON:'["auto","boat","keirin"]',TEST_FAULT:fault},durableObjects:{SPORTS:{className:'DailyTest',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),
 outboundService:async req=>{requests.push(req.url);const value=typeof body==='function'?await body(req):body;return value instanceof Response?value:new Response(value);}}));
 const db=await mf.getD1Database('INDEX');for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=async v=>{const r=await mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)});const result=await r.json();assert.equal(r.status,200,JSON.stringify(result));return result;};
 return {mf,db,call,requests};
}
async function fixture(at=Date.now()+60000,remaining=900000){
 const window=p.businessDay(at);at=Math.max(at,window.start);
 if(at+remaining>=window.end)at=p.businessDay(window.end).start;
 const s=await p.initialDaily('auto',at),e=p.nextDaily(s,at);return {s,e,at:e[0].at};
}
for(const phase of ['FINAL_ONLY','INTERMEDIATE','PARSE_ERROR'])test('DO completes closing task only for a complete provider closed phase: '+phase,async()=>{
 const {s,at}=await fixture(),day=`${s.day.slice(0,4)}-${s.day.slice(4,6)}-${s.day.slice(6,8)}`,race=`auto:${s.day}:6:7`;
 const body=JSON.parse(autoProgram),closeAt=at-config.daily.final_odds_delay_seconds*1000;
 const midnight=Date.parse(day+'T00:00:00Z')-config.discovery.clock_timezone_offset_minutes*60000;
 const minutes=Math.floor((closeAt-midnight)/60000),close=String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');
 body.body.telvoteTime=close;body.body.raceStartTime=close;
 const target={sport:'auto',race_id:race,kind:'schedule',discovery_stage:'venue',url:'https://autorace.jp/race_info/OtherRaceInfo',body:JSON.stringify({placeCode:6,raceDate:day,raceNo:7})};
 const state=await races('auto',JSON.stringify(body),target,at),key='final_odds:'+race;
 state.tasks={[key]:state.tasks[key]};const entries=p.nextDaily(state,at);
 assert.equal(entries.length,1);
 const r=await runtime({body:phase==='PARSE_ERROR'?'changed format':autoBody('4.0',phase==='FINAL_ONLY'?1:0)});
 try{
  await r.call({op:'seed',state,entries});const result=await r.call({op:'step',now:entries[0].at,session:true});
  assert.equal(!!result.state.tasks[key].done,phase==='FINAL_ONLY');assert.equal(r.requests.length,1);
  await r.call({op:'seed',state});const recovered=await r.call({op:'step',now:entries[0].at+(config.request_spacing_seconds+1)*1000,session:true});
  assert.equal(!!recovered.state.tasks[key].done,phase==='FINAL_ONLY');assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
for(const expired of [false,true])test('anonymous Keirin guest without Set-Cookie advances the daily catalog; expired='+expired,async()=>{
 const sample=await fixture(),s=await p.initialDaily('keirin',sample.at-1000),e=p.nextDaily(s,sample.at-1000,false),at=e[0].at,cookies=[];
 const r=await runtime({body:async req=>{cookies.push(req.headers.get('Cookie'));return req.url.endsWith('/top')?'<html>synthetic public top</html>':JSON.stringify({resultCd:0,RaceList:[]});}});
 try{await r.call({op:'seed',sport:'keirin',state:s,entries:e});
 let result=await r.call({op:'step',sport:'keirin',now:at,session:expired?{cookie:'STALE_SYNTHETIC',expires:at-1}:false});
 for(let i=0;i<5&&r.requests.length<2;i++)result=await r.call({op:'step',sport:'keirin',now:result.plan.alarm_at});
 assert.equal(r.requests.length,2);assert.ok(r.requests[1].includes('type=JSJ048'));assert.deepEqual(cookies,[null,null]);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='PROGRAM_PARSED'").first()).n,1);
 }finally{await r.mf.dispose();}
});
for(const delay of [6000,120000])test('Keirin saved guest recovery retains original receipt and old redelivery cannot renew readiness; delay='+delay,async()=>{
 const sample=await fixture(undefined,(config.guest_session_seconds+config.daily.guest_interval_seconds+1)*1000),
  s=await p.initialDaily('keirin',sample.at-1000),e=p.nextDaily(s,sample.at-1000,false),at=e[0].at;
 const r=await runtime({fault:'publish',body:'<html>synthetic public top</html>'});
 try{await r.call({op:'seed',sport:'keirin',state:s,entries:e});
 const failed=await r.call({op:'step',sport:'keirin',now:at});assert.equal(failed.guest_received_at,undefined);assert.equal(r.requests.length,1);
 const recovered=await r.call({op:'step',sport:'keirin',now:at+delay});assert.equal(Date.parse(recovered.guest_received_at),at);assert.equal(r.requests.length,1);
 await r.call({op:'seed',sport:'keirin',state:s});
 const later=await r.call({op:'step',sport:'keirin',now:at+(config.guest_session_seconds+1)*1000});
 assert.equal(later.guest_received_at,recovered.guest_received_at);assert.equal(r.requests.length,1);
 const waiting=await r.call({op:'step',sport:'keirin',now:later.plan.alarm_at});
 assert.equal(waiting.state.report.status,'GUEST_WAIT');assert.equal(r.requests.length,1);
 }finally{await r.mf.dispose();}
});
test('Keirin unsuccessful top keeps catalog waiting and does not record readiness',async()=>{
 const sample=await fixture(),s=await p.initialDaily('keirin',sample.at-1000),e=p.nextDaily(s,sample.at-1000,false),at=e[0].at;
 const r=await runtime({body:()=>new Response('synthetic unavailable',{status:500})});
 try{await r.call({op:'seed',sport:'keirin',state:s,entries:e});const failed=await r.call({op:'step',sport:'keirin',now:at});
 assert.equal(failed.guest_received_at,undefined);const waiting=await r.call({op:'step',sport:'keirin',now:failed.plan.alarm_at});
 assert.equal(waiting.state.report.status,'GUEST_WAIT');assert.equal(r.requests.length,1);assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,0);
 }finally{await r.mf.dispose();}
});
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

test('old closing/result completion cannot finish or postpone a revised deadline',async()=>{
 for(const kind of ['final_odds','result']){
  const {s,at}=await closingBoat(),race='boat:20000101:2:1',key=kind+':'+race;
  if(kind==='result')s.tasks={[key]:{kind,race_id:race,close_at:Date.parse('2000-01-01T01:10:00Z'),next_at:at,interval:300}};
  const entries=p.nextDaily(s,at);assert.ok(entries.length);
  await p.acceptProgram(s,source(boatProgram.replace('10:10','10:30'),boat,at),at);
  const revised=s.tasks[key].next_at;
  for(const entry of entries)await p.completeDaily(s,entry,'RAW_STORED',at+25000,true,true);
  assert.ok(!s.tasks[key].done);assert.equal(s.tasks[key].next_at,revised);assert.equal(s.tasks[key].last_at,undefined);
 }
});
for(const missingRecovery of [false,true])test('partial queue capacity waits for existing finite work, recovery='+missingRecovery,async()=>{
 const {s,e,at}=await fixture(),state=await p.initialDaily('boat',at-1000);state.tasks={};
 const day=state.day,target={...boat,race_id:`boat:${day}:2:0`,url:boat.url.replace('20000101',day)};
 const raw=boatProgram.replaceAll('20000101',day);
 await p.acceptProgram(state,source(raw,target,at-1000),at-1000);
 // Keep one odds task and a future advertised close, independent of the wall-clock hour.
 const key=`odds:boat:${day}:2:1`,clock=state.races[`boat:${day}:2:1`].clock;
 const program=clock.value.program;
 for(const race of program.races??[])race.close_at=new Date(at+1800000).toISOString();
 state.tasks={[key]:{kind:'odds',race_id:`boat:${day}:2:1`,close_at:at+1800000,next_at:at-1000,interval:120}};
 const entries=p.nextDaily(state,at-1000);assert.equal(entries.length,5);
 if(!missingRecovery)state.action=null;state.wake_at=at-1000;
 const finite=Array.from({length:missingRecovery?254:253},(_,i)=>({at:at+240000+i*5000,target:e[0].target})),r=await runtime();
 try{await r.call({op:'seed',sport:'boat',state,entries:finite});const result=await r.call({op:'step',sport:'boat',now:at-1000});
  assert.equal(result.plan.pending,finite.length);assert.equal(result.plan.alarm_at,finite[0].at);assert.equal(result.state.report.status,'CAPACITY_WAIT');assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});
test('a fast race advances its next page while another race response remains held',async()=>{
 const {at}=await fixture(),state=await p.initialDaily('boat',at-1000);state.tasks={};state.wake_at=at+60000;
 const base={sport:'boat',kind:'odds',market:'trifecta'},one={...base,race_id:`boat:${state.day}:2:1`,url:`https://www.boatrace.jp/owpc/pc/race/odds3t?hd=${state.day}&jcd=02&rno=1`},two={...one,race_id:`boat:${state.day}:2:2`,url:one.url.replace('rno=1','rno=2')};
 let release;const hold=new Promise(resolve=>release=resolve);let slowReturned=false;
 const entries=[{at,target:one},{at,target:two},{at:at+5000,target:{...two,market:'trio',url:two.url.replace('odds3t','odds3f')}}];
 const r=await runtime({body:async req=>{if(req.url.includes('rno=1')){await hold;slowReturned=true;}return boatOddsBody([req.url.includes('odds3f')?'trio':'trifecta']);}});
 let work;
 try{await r.call({op:'seed',sport:'boat',state,entries});work=r.call({op:'step',sport:'boat',now:at,advance:true});
  const until=Date.now()+12000;while(r.requests.length<3&&Date.now()<until)await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(r.requests.length,3);assert.equal(slowReturned,false);
  const event=`sports:boat:${entries[2].at}:${await p.resourceId(entries[2].target)}`;
  while(!(await r.db.prepare('SELECT observation_id FROM raw_observations WHERE observation_id=?').bind(event).first())&&Date.now()<until)await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(await r.db.prepare('SELECT observation_id FROM raw_observations WHERE observation_id=?').bind(event).first());assert.equal(slowReturned,false);
  release();const result=await work;assert.equal(result.plan.pending,0);assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,3);
 }finally{release();if(work)await work;await r.mf.dispose();}
});

test('result collection continues for blank payouts and missing offered frame markets',()=>{
 const t={sport:'keirin',race_id:'keirin:20000101:47:2',kind:'result',url:'https://keirin.jp/pc/json?type=JSJ012&encp=synthetic&mode=0'};
 const value=p.parseResult(keirinResult(),t);value.placings.push({entrant:3,rank:3,rank_label:'3',state_label:null});
 const offered=Object.keys(config.sources.keirin.odds_market_codes);
 assert.equal(p.completePayoutMarkets(value,offered),true);
 const removed={...value,payouts:value.payouts.filter(p=>!p.market.startsWith('frame_'))};
 assert.equal(p.completePayoutMarkets(removed,offered),false);
 assert.equal(p.completePayoutMarkets(removed,offered.filter(m=>!m.startsWith('frame_'))),true);
 const blank={...value,payouts:value.payouts.map(p=>p.market==='trifecta'?{...p,status:'DISPLAY_ONLY',amount_yen:null,display:''}:p)};
 assert.equal(p.completePayoutMarkets(blank,offered),false);
});

test('legacy pending action retains its original advertised close before equal or revised programs',async()=>{
 for(const changed of [false,true]){
  const s=await races('boat',boatProgram,boat),entries=p.nextDaily(s,now),key=entries[0].daily_task,old=s.tasks[key].close_at;
  delete s.action.close_at;for(const task of Object.values(s.tasks))delete task.close_at;
  await p.acceptProgram(s,source(changed?boatProgram.replace('10:10','10:30'):boatProgram,boat,now+1000),now+1000);
  assert.equal(s.action.close_at,old);assert.equal(s.tasks[key].close_at,changed?Date.parse('2000-01-01T01:30:00Z'):old);
  p.nextDailyParallel(s,now+2000);assert.deepEqual(s.actions[key].entries,entries);
 }
});
