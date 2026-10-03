import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {jraBody,race,catalogTarget,jraCatalog,jraProgram,jraResult,resultTarget,jraResultCatalog,jraResultProgram} from '../fixtures/synthetic/jra.mjs';
const config=JSON.parse(await readFile('configs/jra-source.json','utf8'));
const encoded=body=>{const result=spawnSync('python3',['-c','import sys;sys.stdout.buffer.write(sys.stdin.read().encode("shift_jis"))'],{input:body});assert.equal(result.status,0);return result.stdout;};
const target=(page='win_place',context_event)=>({sport:'jra',race_id:race,kind:'odds',page,form:true,
 url:config.origin+config.odds_path,body:new URLSearchParams({cname:config.navigation_prefixes[page]+'S300200001010120000101Z/AA'}).toString(),...(context_event?{context_event}:{})});
const script=await build({stdin:{contents:`import {collect,validateTarget} from './workers/sports/capture';
import {normalize,history} from './workers/sports/storage';import sports from './workers/sports/index';import {businessDay} from './workers/sports/daily-plan';
export {SportsCollector,SportsControl} from './workers/sports/index';
import {SportsCollector} from './workers/sports/index';export class JraTest extends SportsCollector {
 async step(now){const clock=Date.now;Date.now=()=>now;try{await this.ensureDaily('jra');await this.alarm();}finally{Date.now=clock;}}
}
let releaseRead,waiting=false;
export default {async fetch(req,env){const v=await req.json();
 if(v.op==='readState')return Response.json(waiting);
 if(v.op==='releaseRead'){releaseRead?.();return Response.json(true);}
 let storageEnv=env;
 if(v.bodyIO||v.holdRead){const hold=v.holdRead?new Promise(resolve=>releaseRead=resolve):null;
 storageEnv={...env,RAW:{get:async(...args)=>{const o=await env.RAW.get(...args);if(!o||!args[0].startsWith('raw/'))return o;
 const read=async method=>{if(v.bodyIO)throw new Error('SYNTHETIC_R2_IO');if(hold){waiting=true;await hold;waiting=false;}return o[method]();};
 return {...o,arrayBuffer:()=>read('arrayBuffer'),text:()=>read('text')};},head:(...args)=>env.RAW.head(...args),put:(...args)=>env.RAW.put(...args)}};}
 if(v.batchLag)storageEnv={...storageEnv,INDEX:{prepare:s=>env.INDEX.prepare(s),batch:async rows=>{const result=await env.INDEX.batch(rows);await new Promise(resolve=>setTimeout(resolve,v.batchLag));return result;}}};
 if(v.op==='history')return Response.json(await history(env,'jra',v.race??'${race}',v.at,100));
 if(v.op==='programHistory')return Response.json(await history(env,'jra',v.race,v.at,100,'','program'));
 if(v.op==='resultHistory')return Response.json(JSON.parse(await env.CONTROL.resultHistory('jra',v.race??'${race}',v.at,100)));
 if(v.op==='resultPlan'){try{return Response.json(JSON.parse(await env.CONTROL.resultPlan(v.event,v.race,v.at)));}catch(e){return Response.json({error:e.message});}}
 if(v.op==='day')return Response.json(businessDay(Date.now()+86400000));
 if(v.op==='dailyState')return Response.json(JSON.parse(await env.CONTROL.dailyState('jra')));
 if(v.op==='alarm'){await env.SPORTS.get(env.SPORTS.idFromName('jra')).step(v.now);return Response.json(true);}
 if(v.op==='reparse'){try{return Response.json(await normalize(storageEnv,v.event,v.target,v.version));}catch(e){return Response.json({error:e.message});}}
 if(v.op==='validate'){try{validateTarget(v.target);return Response.json(true);}catch(e){return Response.json(e.message);}}
 if(v.op==='schedule')return Response.json(await env.CONTROL.schedule(JSON.stringify(v.entries)));
 if(v.op==='planState')return Response.json(JSON.parse(await env.CONTROL.planState('jra')));
 if(v.op==='daily'){try{return Response.json(await env.CONTROL.ensureDaily('jra'));}catch(e){return Response.json(e.message);}}
 if(v.op==='cron'){const called=[];await sports.scheduled({}, {...env,SPORTS_DAILY_ENABLED:'true',SPORTS:{idFromName:s=>s,get:s=>({ensureDaily:async()=>called.push(s)})}});return Response.json(called);}
 return Response.json(await collect(v.at,storageEnv,v.target,undefined,!!v.parallel,undefined,!!v.daily));}};`,resolveDir:process.cwd()},
 external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'});
const schema=(await Promise.all(['0001_capture','0002_processing_metrics','0010_sports_history'].map(n=>readFile('migrations/'+n+'.sql','utf8')))).join('\n');
async function runtime(responses=[],daily=false){
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({name:'jra-test',modules:true,script:script.outputFiles[0].text,
 compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],log:new Log(LogLevel.NONE),
 bindings:{SPORTS_ENABLED:'true',SPORTS_DAILY_ENABLED:String(daily),SPORTS_PROVIDERS_JSON:'["jra","boat","auto","keirin"]'},d1Databases:['INDEX'],r2Buckets:['RAW'],
 durableObjects:{SPORTS:{className:'JraTest',useSQLite:true}},serviceBindings:{CONTROL:{name:'jra-test',entrypoint:'SportsControl'}},
 outboundService:async req=>{requests.push({method:req.method,contentType:req.headers.get('Content-Type'),body:await req.text(),started:performance.now()});
 const response=responses.shift();assert.ok(response,'unexpected HTTP');if(typeof response==='function')return response(req);
 return new Response(response.body??encoded(jraBody(response.page??'win_place')),{status:response.status??200,headers:response.headers});}}));
 const db=await mf.getD1Database('INDEX'),raw=await mf.getR2Bucket('RAW');
 for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=async v=>{const r=await mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)});if(r.status!==200)assert.fail(await r.text());return r.json();};
 const tick=async(t=target(),at=Date.now(),parallel=false,daily=false)=>call({target:t,at,parallel,daily});
 const reset=()=>db.prepare("UPDATE source_control SET next_allowed_at=0 WHERE source='sports-jra'").run();
 const parsed=async event=>{const p=await db.prepare('SELECT * FROM sports_parses WHERE observation_id=? ORDER BY available_at DESC').bind(event).first();return {index:p,value:p.normalized_key?await (await raw.get(p.normalized_key)).json():null};};
 return {mf,db,raw,requests,call,tick,reset,parsed};
}
test('all eight JRA markets use shared capture, Shift JIS, original storage and readback',async()=>{
 const pages=Object.keys(config.tables),r=await runtime(pages.map(page=>({page})));try{
 const first=await r.tick();assert.equal(first.status,'RAW_STORED');const single=await r.parsed(first.event_id);
 assert.equal(single.index.status,'COMPLETE');assert.equal(single.value.phase,'FINAL_ONLY');assert.equal(single.value.source_updated_at,null);
 const markets=[...single.value.markets];
 for(const page of pages.slice(1)){await r.reset();const observed=await r.tick(target(page,first.event_id));assert.equal(observed.status,'RAW_STORED');
 const p=await r.parsed(observed.event_id);assert.equal(p.index.status,'COMPLETE');markets.push(...p.value.markets);}
 assert.equal(markets.length,8);assert.ok(markets.every(m=>m.complete));assert.equal(r.requests.length,7);
 assert.ok(r.requests.every(q=>q.method==='POST'&&q.contentType==='application/x-www-form-urlencoded'&&new URLSearchParams(q.body).size===1));
 const saved=await r.call({op:'history',at:new Date().toISOString()});assert.equal(saved.length,7);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM raw_observations WHERE dataset_kind='SPORT_JRA_ODDS'").first()).n,7);
 }finally{await r.mf.dispose();}
});
test('same-content observations remain separate, delivery is idempotent and past as-of stays fixed',async()=>{
 const r=await runtime([{},{}]);try{
 const at=Date.now(),first=await r.tick(target(),at),cutoff=new Date().toISOString();
 const past=await r.call({op:'history',at:cutoff});assert.equal(past.length,1);
 assert.equal((await r.tick(target(),at)).status,'RAW_STORED');assert.equal(r.requests.length,1);
 assert.equal((await r.tick(target(),at+1)).status,'WAIT_OR_BLOCKED');await r.reset();
 await new Promise(resolve=>setTimeout(resolve,3));const second=await r.tick();assert.notEqual(first.event_id,second.event_id);
 const all=await r.call({op:'history',at:new Date().toISOString()});assert.equal(all.length,2);
 assert.equal(all[0].raw_sha256,all[1].raw_sha256);assert.deepEqual(await r.call({op:'history',at:cutoff}),past);
 }finally{await r.mf.dispose();}
});
test('JRA results share immutable originals, as-of history, redelivery and saved-link planning',async()=>{
 const r=await runtime([{body:encoded(jraResult())},{body:encoded(jraResult())}]);try{
  const at=Date.now(),first=await r.tick(resultTarget,at),cutoff=new Date().toISOString();
  assert.equal(first.status,'RAW_STORED');const parsed=await r.parsed(first.event_id);assert.equal(parsed.index.status,'RESULT_PARSED');
  assert.equal(parsed.value.phase,'RESULT_ONLY');assert.equal(parsed.value.payouts.length,12);
  const past=await r.call({op:'resultHistory',at:cutoff});assert.equal(past.length,1);
  assert.deepEqual(await r.call({op:'history',at:cutoff}),[]);
  await r.tick(resultTarget,at);assert.equal(r.requests.length,1);
  await r.reset();await new Promise(resolve=>setTimeout(resolve,3));await r.tick(resultTarget);
  const all=await r.call({op:'resultHistory',at:new Date().toISOString()});assert.equal(all.length,2);assert.equal(all[0].raw_sha256,all[1].raw_sha256);
  const plan=await r.call({op:'resultPlan',event:first.event_id,race:'jra:20000101:5:2',at:Date.now()+60000});
  assert.equal(plan.entries.length,1);assert.equal(plan.entries[0].target.kind,'result');
  assert.equal(new URLSearchParams(plan.entries[0].target.body).get('cname'),'pw01sde1005200001010220000101/BB');
  assert.equal((await r.call({op:'resultPlan',event:first.event_id,race:'jra:20000101:5:3',at:Date.now()+60000})).deferred[0].reason,'RESULT_NAVIGATION_MISSING');
  assert.equal(await r.call({op:'reparse',event:first.event_id,target:resultTarget,version:'sports-result-v2'}),'RESULT_PARSED');
  assert.deepEqual(await r.call({op:'resultHistory',at:cutoff}),past);assert.equal(r.requests.length,2);
  const later=new Date().toISOString().replace('Z','000+00:00');
  await r.db.prepare("INSERT INTO sports_parses SELECT observation_id,'sports-result-v3',sport,race_id,resource_id,?,?,'PARSE_ERROR',NULL,'SYNTHETIC_FAILURE' FROM sports_parses WHERE observation_id=? AND parser_version=?")
   .bind(later,later,first.event_id,config.result_parser_version).run();
  assert.deepEqual(await r.call({op:'resultPlan',event:first.event_id,race:'jra:20000101:5:2',at:Date.now()+60000}),{error:'RESULT_UNAVAILABLE'});
  assert.deepEqual(await r.call({op:'resultHistory',at:cutoff}),past);
 }finally{await r.mf.dispose();}
});
test('context is fixed to original fetch start, latest failure blocks new fetches but preserves raw recovery',async()=>{
 const r=await runtime([{},{page:'quinella'}]);try{
 const first=await r.tick();await r.reset();const t=target('quinella',first.event_id),at=Date.now();
 const second=await r.tick(t,at);assert.equal(second.status,'RAW_STORED');
 await new Promise(resolve=>setTimeout(resolve,3));
 const future=new Date().toISOString().replace('Z','000+00:00');
 await r.db.prepare("INSERT INTO sports_parses SELECT observation_id,'sports-odds-v2',sport,race_id,resource_id,?,?,'PARSE_ERROR',NULL,'SYNTHETIC_PARSER_FAILURE' FROM sports_parses WHERE observation_id=? AND parser_version=?")
  .bind(future,future,first.event_id,config.parser_version).run();
 assert.equal(await r.call({op:'reparse',event:second.event_id,target:t,version:'sports-odds-v2'}),'COMPLETE');
 await r.reset();assert.equal((await r.tick(t)).status,'INVALID_CONTEXT');assert.equal(r.requests.length,2);
 await r.db.prepare('DELETE FROM sports_parses WHERE observation_id=?').bind(second.event_id).run();
 await r.db.prepare('DELETE FROM raw_observations WHERE observation_id=?').bind(second.event_id).run();
 await r.db.prepare("UPDATE captures SET status='STORAGE_ERROR' WHERE event_id=?").bind(second.event_id).run();
 assert.equal((await r.tick(t,at)).status,'RAW_STORED');assert.equal(r.requests.length,2);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations WHERE observation_id=?').bind(second.event_id).first()).n,1);
 }finally{await r.mf.dispose();}
});
for(const change of ['phase','flat'])test('daily context rechecks latest intermediate flat support while finite final collection remains usable: '+change,async()=>{
 const r=await runtime([{body:encoded(jraBody('win_place',{phase:'9時45分現在'}))},{page:'quinella'}]);try{
  const first=await r.tick(),original=await r.parsed(first.event_id),value=structuredClone(original.value);
  if(change==='phase')value.phase='FINAL_ONLY';else value.metadata.flat=false;
  const key='synthetic/reparsed-'+change+'.json';await r.raw.put(key,JSON.stringify(value));
  await new Promise(resolve=>setTimeout(resolve,3));const later=new Date().toISOString().replace('Z','000+00:00');
  await r.db.prepare("INSERT INTO sports_parses SELECT observation_id,'sports-odds-v2',sport,race_id,resource_id,?,?,'COMPLETE',?,NULL FROM sports_parses WHERE observation_id=? AND parser_version=?")
   .bind(later,later,key,first.event_id,config.parser_version).run();
  await r.reset();assert.equal((await r.tick({...target('quinella',first.event_id),context_phase:'INTERMEDIATE'})).status,'INVALID_CONTEXT');assert.equal(r.requests.length,1);
  await r.reset();const finite=await r.tick(target('quinella',first.event_id));
  assert.equal(finite.status,change==='phase'?'RAW_STORED':'INVALID_CONTEXT');assert.equal(r.requests.length,change==='phase'?2:1);
 }finally{await r.mf.dispose();}
});
test('invalid context and malformed Shift JIS are recorded without inventing odds or losing the original',async()=>{
 const r=await runtime([{body:new Uint8Array([0x81])}]);try{
 assert.equal((await r.tick(target('quinella','missing-synthetic-context'))).status,'INVALID_CONTEXT');assert.equal(r.requests.length,0);
 const fetched=await r.tick();assert.equal(fetched.status,'RAW_STORED');assert.equal((await r.parsed(fetched.event_id)).index.status,'PARSE_ERROR');
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('daily enablement is separate and central finite enablement preserves every sports cron',async()=>{
 const r=await runtime();try{
 assert.equal(await r.call({op:'daily'}),'DISABLED');
 assert.deepEqual(await r.call({op:'cron'}),['jra','boat','auto','keirin']);
 assert.equal(await r.call({op:'schedule',entries:[{at:Date.now()+60000,target:target(),daily_task:'synthetic-invalid-daily'}]}),'INPUT_OR_CAPACITY_ERROR');
 assert.equal(await r.call({op:'validate',target:{...target(),body:'cname=synthetic&cname=other'}}),'READ_FORM_REQUIRED');
 assert.equal(await r.call({op:'validate',target:{...target(),url:config.origin+config.odds_path+'?unexpected=1'}}),'TARGET_ORIGIN');
 assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});
test('JRA refusal and rate-limit gates stay independent from NAR and retain their wait',async()=>{
 for(const status of [403,429]){
 const r=await runtime([{status,body:'synthetic refusal',headers:{'Retry-After':'600'}}]);try{
 const at=Date.now(),failed=await r.tick(target(),at);assert.equal(failed.status,status===403?'SOURCE_DENIED':'RATE_LIMITED');
 const gate=await r.db.prepare("SELECT * FROM source_control WHERE source='sports-jra'").first();
 assert.equal(gate.blocked,status===403?1:0);assert.ok(gate.next_allowed_at>=at+(status===429?600:config.finite_request_spacing_seconds)*1000);
 assert.equal((await r.tick(target(),at+1)).status,'WAIT_OR_BLOCKED');assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='nar-daily-odds'").first()).blocked,0);
 }finally{await r.mf.dispose();}
 }
});
test('a held JRA request occupies its sole finite slot without allowing a second race fetch',async()=>{
 let release;const hold=new Promise(resolve=>release=resolve);
 const r=await runtime([async()=>{await hold;return new Response(encoded(jraBody('win_place')));}]);
 const at=Date.now(),first=r.tick(target(),at,true);
 try{
 for(let i=0;i<500&&!r.requests.length;i++)await new Promise(resolve=>setTimeout(resolve,2));assert.equal(r.requests.length,1);
 assert.equal((await r.tick({...target(),race_id:'jra:20000101:5:2'},at,true)).status,'WAIT_OR_BLOCKED');assert.equal(r.requests.length,1);
 release();assert.equal((await first).status,'RAW_STORED');
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM captures WHERE status='FETCHING'").first()).n,0);
 }finally{release();await first;await r.mf.dispose();}
});
test('temporary raw body I/O failure does not poison a parser version and remains retryable',async()=>{
 const r=await runtime([{}]);try{
 const first=await r.tick(),version='sports-odds-v3';
 assert.deepEqual(await r.call({op:'reparse',event:first.event_id,target:target(),version,bodyIO:true}),{error:'SYNTHETIC_R2_IO'});
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM sports_parses WHERE observation_id=? AND parser_version=?').bind(first.event_id,version).first()).n,0);
 assert.equal(await r.call({op:'reparse',event:first.event_id,target:target(),version}),'COMPLETE');assert.equal(r.requests.length,1);
 }finally{await r.mf.dispose();}
});
test('JRA wait is durable after raw publication while normalization is still reading',async()=>{
 const r=await runtime([{}]),at=Date.now(),first=r.call({target:target(),at,parallel:true,holdRead:true});
 try{
 let waiting=false;for(let i=0;i<500&&!waiting;i++){waiting=await r.call({op:'readState'});if(!waiting)await new Promise(resolve=>setTimeout(resolve,2));}assert.ok(waiting);
 const saved=await r.db.prepare("SELECT status,collector_received_at FROM captures WHERE status='RAW_STORED'").first();assert.ok(saved);
 const gate=await r.db.prepare("SELECT next_allowed_at FROM source_control WHERE source='sports-jra'").first();
 assert.ok(gate.next_allowed_at>=Date.parse(saved.collector_received_at)+config.finite_request_spacing_seconds*1000);
 assert.equal((await r.tick({...target(),race_id:'jra:20000101:5:2'},at,true)).status,'WAIT_OR_BLOCKED');assert.equal(r.requests.length,1);
 await r.call({op:'releaseRead'});assert.equal((await first).status,'RAW_STORED');
 }finally{await r.call({op:'releaseRead'});await first;await r.mf.dispose();}
});
test('object key order does not create a conflicting reservation or a second observation',async()=>{
 const reverse=v=>Array.isArray(v)?v.map(reverse):v&&typeof v==='object'?Object.fromEntries(Object.entries(v).reverse().map(([k,x])=>[k,reverse(x)])):v;
 const r=await runtime([{}]);try{
 const pending={at:Date.now()+60000,target:target()};
 assert.equal(await r.call({op:'schedule',entries:[pending,reverse(pending)]}),'REGISTERED');
 assert.equal(await r.call({op:'schedule',entries:[reverse(pending)]}),'REGISTERED');assert.equal((await r.call({op:'planState'})).pending,1);
 assert.equal(await r.call({op:'schedule',entries:[{...pending,target:{...pending.target,deadline_at:pending.at+60000}}]}),'INPUT_OR_CAPACITY_ERROR');
 const at=Date.now(),observed=await r.tick(target(),at);assert.equal(observed.status,'RAW_STORED');
 assert.equal(await r.call({op:'schedule',entries:[reverse({at,target:target()})]}),'REGISTERED');
 assert.equal((await r.call({op:'planState'})).pending,1);assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('JRA programs use shared immutable capture, parser versions and as-of history',async()=>{
 const r=await runtime([{body:encoded(jraCatalog)},{body:encoded(jraCatalog)}]);try{
 const first=await r.tick(catalogTarget),value=await r.parsed(first.event_id);assert.equal(first.status,'RAW_STORED');assert.equal(value.index.status,'PROGRAM_PARSED');
 assert.equal(value.index.parser_version,config.program_parser_version);assert.equal(value.value.program.venues.length,1);
 const cutoff=new Date().toISOString(),read=at=>r.call({op:'programHistory',race:catalogTarget.race_id,at});
 const past=await read(cutoff);assert.equal(past.length,1);await r.reset();await new Promise(resolve=>setTimeout(resolve,3));
 const second=await r.tick(catalogTarget);assert.notEqual(second.event_id,first.event_id);assert.equal((await read(new Date().toISOString())).length,2);
 const newer='sports-program-v'+(Number(config.program_parser_version.match(/\d+$/)[0])+1);
 assert.deepEqual(await read(cutoff),past);assert.equal(await r.call({op:'reparse',event:first.event_id,target:catalogTarget,version:newer}),'PROGRAM_PARSED');
 assert.equal((await read(new Date().toISOString())).length,3);
 assert.deepEqual(await read(cutoff),past);assert.equal(r.requests.length,2);
 assert.equal((await r.db.prepare("SELECT count(DISTINCT raw_sha256) AS n FROM raw_observations WHERE dataset_kind='SPORT_JRA_SCHEDULE'").first()).n,1);
 assert.equal((await r.call({op:'history',race:catalogTarget.race_id,at:new Date().toISOString()})).length,0);
 }finally{await r.mf.dispose();}
});
test('JRA daily alarm completes catalog/venue actions and preserves clock evidence without generating bets',async()=>{
 const responses=[],r=await runtime(responses,true);try{
 // Future business-day morning avoids rollover and matches D1's real publication clock.
 const window=await r.call({op:'day'}),day=window.day,now=window.start+3600000,year=day.slice(0,4),month=Number(day.slice(4,6)),date=Number(day.slice(6,8));
 const renamed=s=>s.replaceAll('20000101',day).replaceAll('20000102',day).replaceAll('2000年1月1日',`${year}年${month}月${date}日`).replaceAll('2000',year);
 // Keep a single official venue link in the synthetic catalog for this alarm test.
 const catalog=renamed(jraCatalog.slice(0,jraCatalog.indexOf('<h3>1月2日')));
 const respond=async()=>{const name=new URLSearchParams(r.requests.at(-1).body).get('cname');
  return new Response(encoded(name.startsWith('pw01sli')?jraResultCatalog:name.startsWith('pw15oli')?catalog:renamed(jraProgram())));};
 responses.push(...Array(10).fill(respond));
 let at=now;
 for(let i=0;i<20;i++){
  await r.call({op:'alarm',now:at});const state=await r.call({op:'dailyState'});
  if(state.races[`jra:${day}:5:1`]?.clock&&Object.keys(state.actions??{}).length===0)break;
  at=(await r.call({op:'planState'})).alarm_at;
 }
 const state=await r.call({op:'dailyState'});assert.ok(state.races[`jra:${day}:5:1`]?.clock,JSON.stringify({state,requests:r.requests,parses:(await r.db.prepare('SELECT status,error_code FROM sports_parses').all()).results}));assert.equal(state.races[`jra:${day}:5:1`].clock.value.program.races[0].close_at,null);
 assert.equal(Object.keys(state.actions??{}).length,0);assert.equal(Object.values(state.tasks).filter(t=>t.kind==='odds').length,7);assert.equal(r.requests.length,3);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM raw_observations WHERE dataset_kind='SPORT_JRA_SCHEDULE'").first()).n,3);
 }finally{await r.mf.dispose();}
});
for(const phase of ['9時45分現在','最終オッズ'])test('daily JRA roster qualifies each independently scheduled page only from intermediate odds: '+phase,async()=>{
 const responses=[],r=await runtime(responses,true);try{
  const window=await r.call({op:'day'}),day=window.day,year=day.slice(0,4),month=Number(day.slice(4,6)),date=Number(day.slice(6,8));
  const rename=s=>s.replaceAll('20000101',day).replaceAll('20000102',day).replaceAll('2000年1月1日',`${year}年${month}月${date}日`).replaceAll('2000',year);
  const respond=async()=>{
   const name=new URLSearchParams(r.requests.at(-1).body).get('cname');
   if(name.startsWith('pw01sli'))return new Response(encoded(jraResultCatalog));
   const body=name.startsWith('pw15oli')?jraCatalog.slice(0,jraCatalog.indexOf('<h3>1月2日')):
    name.startsWith('pw15orl')?jraProgram():jraBody(Object.keys(config.tables).find(p=>name.startsWith(config.navigation_prefixes[p])),{phase});
   return new Response(encoded(rename(body)));
  };
  responses.push(...Array(40).fill(respond));let at=window.start+2*3600000+45*60000;
  for(let i=0;i<40;i++){
   await r.call({op:'alarm',now:at});const plan=await r.call({op:'planState'});assert.ok(plan.alarm_at);at=plan.alarm_at;
   const rows=(await r.db.prepare("SELECT normalized_key FROM sports_parses WHERE sport='jra' AND parser_version LIKE 'sports-odds-v%' AND status='COMPLETE'").all()).results;
   const values=await Promise.all(rows.map(async x=>(await r.raw.get(x.normalized_key)).json()));
   const markets=new Set(values.flatMap(v=>v.markets.map(m=>m.market)));
   if(phase==='最終オッズ'&&rows.length||markets.size===8)break;
  }
  const state=await r.call({op:'dailyState'}),facts=state.races[`jra:${day}:5:1`];assert.ok(facts?.clock);
  assert.equal(facts.clock.value.program.races[0].close_at,null);assert.ok(Object.values(state.tasks).every(t=>['request','odds'].includes(t.kind)));
  if(phase==='最終オッズ'){
   assert.equal(facts.win_context,undefined);
   for(let i=0;i<4;i++){await r.call({op:'alarm',now:at});at=(await r.call({op:'planState'})).alarm_at;}
   assert.ok(r.requests.filter(q=>/^pw15[1345678]ou/.test(new URLSearchParams(q.body).get('cname'))).every(q=>new URLSearchParams(q.body).get('cname').startsWith(config.navigation_prefixes.win_place)));
  }else {
   assert.ok(facts.win_context);
   const rows=(await r.db.prepare("SELECT * FROM sports_parses WHERE parser_version LIKE 'sports-odds-v%'").all()).results;
   const values=await Promise.all(rows.map(async x=>(await r.raw.get(x.normalized_key)).json()));
   assert.equal(new Set(values.flatMap(v=>v.markets.map(m=>m.market))).size,8);assert.ok(rows.every(x=>x.status==='COMPLETE'));
   const manifests=await Promise.all(rows.map(async x=>(await r.raw.get('manifests/'+x.observation_id+'.json')).json()));
   assert.ok(manifests.filter(m=>m.target.page!=='win_place').every(m=>m.target.context_event));
   const starts=manifests.map(m=>Date.parse(m.fetch_started_at)).sort((a,b)=>a-b);
   assert.ok(starts.slice(1).every((t,i)=>t-starts[i]>=config.daily_request_spacing_seconds*1000));
  }
 }finally{await r.mf.dispose();}
});
for(const missingWide of [false,true])test(`JRA daily results retain odds clocks${missingWide?' and retry a whole missing payout row':''}`,async()=>{
 const responses=[],r=await runtime(responses,true);try{
  const window=await r.call({op:'day'}),day=window.day,year=day.slice(0,4),month=Number(day.slice(4,6)),date=Number(day.slice(6,8));
  const rename=s=>s.replaceAll('20000101',day).replaceAll('20000102',day).replaceAll('2000年1月1日',`${year}年${month}月${date}日`).replaceAll('2000',year);
  let resultRequests=0,retried=false;
  const respond=async()=>{const name=new URLSearchParams(r.requests.at(-1).body).get('cname');
   const body=name.startsWith('pw01sli')?jraResultCatalog:name.startsWith('pw01srl')?jraResultProgram():name.startsWith('pw01sde')?jraResult({missingWide:missingWide&&++resultRequests===1}):
    name.startsWith('pw15oli')?jraCatalog.slice(0,jraCatalog.indexOf('<h3>1月2日')):name.startsWith('pw15orl')?jraProgram():jraBody(Object.keys(config.tables).find(p=>name.startsWith(config.navigation_prefixes[p])));
   return new Response(encoded(rename(body)));};
  responses.push(...Array(32).fill(respond));let at=window.start+3600000;
  for(let i=0;i<32;i++){
   await r.call({op:'alarm',now:at});const state=await r.call({op:'dailyState'});
   if(missingWide&&resultRequests===1&&state.tasks[`result:jra:${day}:5:1`]?.last_at){
    assert.notEqual(state.tasks[`result:jra:${day}:5:1`].done,true);retried=true;
   }
   if(state.races[`jra:${day}:5:1`]?.clock&&state.tasks[`result:jra:${day}:5:1`]?.done&&Object.values(state.tasks).filter(t=>t.kind==='final_odds').every(t=>t.done)&&Object.keys(state.actions??{}).length===0)break;
   at=(await r.call({op:'planState'})).alarm_at;
  }
  const state=await r.call({op:'dailyState'}),id=`jra:${day}:5:1`,facts=state.races[id];
  assert.ok(facts.clock&&facts.results);assert.equal(facts.clock.target.program_kind,undefined);assert.equal(facts.results.target.program_kind,'results');
  assert.equal(facts.clock.value.program.races[0].close_at,null);assert.equal(state.tasks['result:'+id].done,true);
  assert.equal(Object.values(state.tasks).filter(t=>t.kind==='odds').length,7);
  assert.equal(r.requests.filter(q=>new URLSearchParams(q.body).get('cname').startsWith('pw01sde')).length,missingWide?2:1);
  const rows=await r.call({op:'resultHistory',race:id,at:new Date(at).toISOString()});assert.equal(rows.length,missingWide?2:1);assert.ok(rows.every(x=>x.status==='RESULT_PARSED'));
  if(missingWide){assert.ok(retried);assert.equal((await r.parsed(rows[0].observation_id)).value.payouts.length,11);}
  const v=(await r.parsed(rows.at(-1).observation_id)).value;assert.equal(v.phase,'RESULT_ONLY');assert.equal(v.payouts.length,12);assert.equal(v.settlement_qualified,false);
  const odds=await r.call({op:'history',race:id,at:new Date(at).toISOString()});assert.equal(odds.length,7);assert.ok(odds.every(x=>x.status==='COMPLETE'));
  const values=await Promise.all(odds.map(async x=>(await r.parsed(x.observation_id)).value));assert.ok(values.every(v=>v.phase==='FINAL_ONLY'));
  assert.equal(new Set(values.flatMap(v=>v.markets.map(m=>m.market))).size,8);assert.equal(facts.win_context,undefined);assert.ok(facts.final_context);
 }finally{await r.mf.dispose();}
});
test('overdue JRA daily claims share an atomic start gate and a held request allows a later daily slot',async()=>{
 let release;const held=new Promise(resolve=>release=resolve),r=await runtime([async()=>{await held;return new Response(encoded(jraBody('win_place')));},{}]);
 try{
  const at=Date.now()-1000,first=r.tick(target(),at,true,true);for(let i=0;i<100&&!r.requests.length;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(r.requests.length,1);
  const rest=await Promise.all([1,2,3].map(i=>r.tick(target(),at-i,true,true)));assert.ok(rest.every(v=>v.status==='WAIT_OR_BLOCKED'));assert.equal(r.requests.length,1);
  await new Promise(resolve=>setTimeout(resolve,(config.daily_request_spacing_seconds+0.1)*1000));
  const next=await r.tick(target(),at-4,true,true);assert.equal(next.status,'RAW_STORED');assert.equal(r.requests.length,2);
  release();assert.equal((await first).status,'RAW_STORED');
 }finally{release();await r.mf.dispose();}
});
test('a held finite JRA lease remains exclusive even if the start timer elapsed for daily requests',async()=>{
 let release;const held=new Promise(resolve=>release=resolve),r=await runtime([async()=>{await held;return new Response(encoded(jraBody('win_place')));}]);
 try{
  const at=Date.now()-1000,first=r.tick(target(),at,true);for(let i=0;i<100&&!r.requests.length;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(r.requests.length,1);
  await r.reset();const next=await r.tick(target(),at-1,true,true);assert.equal(next.status,'WAIT_OR_BLOCKED');assert.equal(r.requests.length,1);
  release();assert.equal((await first).status,'RAW_STORED');
  const gate=await r.db.prepare("SELECT next_allowed_at FROM source_control WHERE source='sports-jra'").first();assert.ok(gate.next_allowed_at>Date.now()+59000);
 }finally{release();await r.mf.dispose();}
});
test('delayed D1 grant responses cannot bunch JRA HTTP starts after the durable timer passed',async()=>{
 let release;const held=new Promise(resolve=>release=resolve),r=await runtime([async()=>{await held;return new Response(encoded(jraBody()));},{}]);try{
  const at=Date.now()-1000,spacing=config.daily_request_spacing_seconds*1000;
  const delayed=r.call({target:target(),at,parallel:true,daily:true,batchLag:spacing+1000});
  await new Promise(resolve=>setTimeout(resolve,spacing+200));
  const next=r.tick(target(),at-1,true,true);assert.equal((await delayed).status,'RAW_STORED');release();assert.equal((await next).status,'RAW_STORED');
  assert.equal(r.requests.length,2);assert.ok(r.requests[1].started-r.requests[0].started>=spacing-20);
 }finally{release();await r.mf.dispose();}
});
for(const status of [403,429])test(`a delayed granted JRA request respects a later ${status} before sending HTTP`,async()=>{
 const r=await runtime([{status,headers:status===429?{'retry-after':'120'}:undefined}]);try{
  const at=Date.now()-1000,spacing=config.daily_request_spacing_seconds*1000;
  const delayed=r.call({target:target(),at,parallel:true,daily:true,batchLag:spacing+1000});
  await new Promise(resolve=>setTimeout(resolve,spacing+200));
  const response=await r.tick(target(),at-1,true,true);assert.equal(response.status,status===403?'SOURCE_DENIED':'RATE_LIMITED');
  assert.equal((await delayed).status,status===403?'SOURCE_DENIED':'SOURCE_WAIT');assert.equal(r.requests.length,1);
  const gate=await r.db.prepare("SELECT blocked,next_allowed_at FROM source_control WHERE source='sports-jra'").first();
  assert.equal(gate.blocked,status===403?1:0);if(status===429)assert.ok(gate.next_allowed_at>Date.now()+110000);
  assert.equal((await r.db.prepare("SELECT count(*) n FROM captures WHERE status='FETCHING'").first()).n,0);
 }finally{await r.mf.dispose();}
});
