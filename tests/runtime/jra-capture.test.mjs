import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {jraBody,race,catalogTarget,jraCatalog,jraProgram} from '../fixtures/synthetic/jra.mjs';
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
 if(v.op==='history')return Response.json(await history(env,'jra',v.race??'${race}',v.at,100));
 if(v.op==='programHistory')return Response.json(await history(env,'jra',v.race,v.at,100,'','program'));
 if(v.op==='day')return Response.json(businessDay(Date.now()+86400000));
 if(v.op==='dailyState')return Response.json(JSON.parse(await env.CONTROL.dailyState('jra')));
 if(v.op==='alarm'){await env.SPORTS.get(env.SPORTS.idFromName('jra')).step(v.now);return Response.json(true);}
 if(v.op==='reparse'){try{return Response.json(await normalize(storageEnv,v.event,v.target,v.version));}catch(e){return Response.json({error:e.message});}}
 if(v.op==='validate'){try{validateTarget(v.target);return Response.json(true);}catch(e){return Response.json(e.message);}}
 if(v.op==='schedule')return Response.json(await env.CONTROL.schedule(JSON.stringify(v.entries)));
 if(v.op==='planState')return Response.json(JSON.parse(await env.CONTROL.planState('jra')));
 if(v.op==='daily'){try{return Response.json(await env.CONTROL.ensureDaily('jra'));}catch(e){return Response.json(e.message);}}
 if(v.op==='cron'){const called=[];await sports.scheduled({}, {...env,SPORTS_DAILY_ENABLED:'true',SPORTS:{idFromName:s=>s,get:s=>({ensureDaily:async()=>called.push(s)})}});return Response.json(called);}
 return Response.json(await collect(v.at,storageEnv,v.target,undefined,!!v.parallel));}};`,resolveDir:process.cwd()},
 external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'});
const schema=(await Promise.all(['0001_capture','0002_processing_metrics','0010_sports_history'].map(n=>readFile('migrations/'+n+'.sql','utf8')))).join('\n');
async function runtime(responses=[],daily=false){
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({name:'jra-test',modules:true,script:script.outputFiles[0].text,
 compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],log:new Log(LogLevel.NONE),
 bindings:{SPORTS_ENABLED:'true',SPORTS_DAILY_ENABLED:String(daily),SPORTS_PROVIDERS_JSON:'["jra","boat","auto","keirin"]'},d1Databases:['INDEX'],r2Buckets:['RAW'],
 durableObjects:{SPORTS:{className:'JraTest',useSQLite:true}},serviceBindings:{CONTROL:{name:'jra-test',entrypoint:'SportsControl'}},
 outboundService:async req=>{requests.push({method:req.method,contentType:req.headers.get('Content-Type'),body:await req.text()});
 const response=responses.shift();assert.ok(response,'unexpected HTTP');if(typeof response==='function')return response(req);
 return new Response(response.body??encoded(jraBody(response.page??'win_place')),{status:response.status??200,headers:response.headers});}}));
 const db=await mf.getD1Database('INDEX'),raw=await mf.getR2Bucket('RAW');
 for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=async v=>{const r=await mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)});if(r.status!==200)assert.fail(await r.text());return r.json();};
 const tick=async(t=target(),at=Date.now(),parallel=false)=>call({target:t,at,parallel});
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
 assert.deepEqual(await read(cutoff),past);assert.equal(await r.call({op:'reparse',event:first.event_id,target:catalogTarget,version:'sports-program-v2'}),'PROGRAM_PARSED');
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
 responses.push(...[catalog,renamed(jraProgram())].map(s=>({body:encoded(s)})));
 for(let i=0;i<6;i++){
  await r.call({op:'alarm',now:now+i*70000});const state=await r.call({op:'dailyState'});
  if(state.races[`jra:${day}:5:1`]?.clock&&Object.keys(state.actions??{}).length===0)break;
 }
 const state=await r.call({op:'dailyState'});assert.ok(state.races[`jra:${day}:5:1`]?.clock,JSON.stringify({state,requests:r.requests,parses:(await r.db.prepare('SELECT status,error_code FROM sports_parses').all()).results}));assert.equal(state.races[`jra:${day}:5:1`].clock.value.program.races[0].close_at,null);
 assert.equal(Object.keys(state.actions??{}).length,0);assert.ok(Object.keys(state.tasks).every(k=>k.startsWith('request:')));assert.equal(r.requests.length,2);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM raw_observations WHERE dataset_kind='SPORT_JRA_SCHEDULE'").first()).n,2);
 }finally{await r.mf.dispose();}
});
