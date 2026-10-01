import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare, Log, LogLevel, convertV4MiniflareOptions} from 'miniflare';

// Only this synthetic harness exposes drive/state. Production HTTP remains 404.
const code = await build({stdin:{contents:`
import {NarCollector,nextJob} from './workers/ingestion/daily.ts';
import {registerPage} from './workers/ingestion/pages.ts';
export class TestCollector extends NarCollector {
 constructor(ctx,env){let failures=0;const index={batch:env.INDEX.batch.bind(env.INDEX),prepare(sql){
  const st=env.INDEX.prepare(sql);
  if(env.FAULT==='preflight' && sql.startsWith('SELECT etag'))return {bind:(...args)=>({first:async()=>{
   if(failures++===0)throw new Error('injected preflight failure');return st.bind(...args).first();}})};
  if((env.FAULT==='claim' && sql.startsWith('INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,'))
    || (env.FAULT==='refusal' && sql.startsWith('UPDATE source_control SET blocked=1'))) {
   return {bind:(...args)=>{const bound=st.bind(...args);return {run:async()=>{if(failures++===0)throw new Error('injected write failure');return bound.run();}}}};
  }return st;}};super(ctx,{...env,INDEX:index});}
 async drive(at,kind,now){const page=['state','payout'].includes(kind)?await this.env.INDEX.prepare('SELECT * FROM page_capture_plans WHERE event_id=?').bind('nar-daily-'+kind+':'+at).first():undefined;
 await this.ctx.storage.put('job',{at,kind,date:'SYNTHETIC',...(page?{page}:{})});let error=null;const realNow=Date.now;
 if(now)Date.now=()=>now;
 try{await this.alarm()}catch(e){error=e.message}finally{Date.now=realNow}
 return {error,job:await this.ctx.storage.get('job'),lastRaceAt:await this.ctx.storage.get('lastRaceAt'),alarm:await this.ctx.storage.getAlarm()};}
}
export default {async fetch(request,env){const u=new URL(request.url);
 if(u.pathname==='/plan'){const {at,target}=await request.json();try{return Response.json(await registerPage(env,at,target));}catch(e){return new Response(e.message,{status:400});}}
 if(u.pathname==='/next')return Response.json(nextJob(Number(u.searchParams.get('at')),u.searchParams.has('race')?Number(u.searchParams.get('race')):null));
 const stub=env.COLLECTOR.getByName('synthetic');return Response.json(await stub.drive(Number(u.searchParams.get('at')),u.searchParams.get('kind'),Number(u.searchParams.get('now'))));}};
`,sourcefile:'daily-harness.ts',resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const schema = await readFile('migrations/0001_capture.sql','utf8') + await readFile('migrations/0002_processing_metrics.sql','utf8') + await readFile('migrations/0007_page_evidence.sql','utf8');
async function runtime(responses, enabled=true, fault="") {
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:code.outputFiles[0].text,
  compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
  durableObjects:{COLLECTOR:{className:'TestCollector',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],
  bindings:{COLLECTION_ENABLED:'true',SOURCE_APPROVED:'true',DAILY_COLLECTION_ENABLED:String(enabled),CAPTURE_SLOTS_JSON:'[]',FAULT:fault},
  log:new Log(LogLevel.NONE),outboundService:async req=>{requests.push({url:req.url,etag:req.headers.get('if-none-match')});
   const r=responses.shift();if(!r)throw new Error('unexpected provider request');
   return new Response(r.body??null,{status:r.status??200,headers:r.headers});}}));
 const db=await mf.getD1Database('INDEX');
 for(const statement of schema.replace(/^--.*$/gm,'').split(';').map(x=>x.trim()).filter(Boolean))await db.prepare(statement).run();
 return {mf,db,requests,drive:async(at,kind='odds',now=0)=>{
  const response=await mf.dispatchFetch('http://local/drive?at='+at+'&kind='+kind+'&now='+now);
  assert.equal(response.status,200);return response.json();}};
}
const zip=new Uint8Array([0x50,0x4b,3,4]);

test('daily alarms retain raw captures and arm future work without any parser binding',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"odds"'}}]);
 try{
  const at=Date.now();const first=await r.drive(at);
  assert.ok(first.alarm>=at+120000);
  await r.drive(at); // redelivered alarm with the same persisted job
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});

test('race ZIP uses the same source control but a separate ETag and dataset kind',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"odds"'}},{body:zip}]);
 try{
  await r.drive(Date.now());await r.db.prepare('UPDATE source_control SET next_allowed_at=0').run();
  await r.drive(Date.now(),'race');
  assert.ok(r.requests[0].url.includes('OddsDataDownload'));
  assert.ok(r.requests[1].url.includes('RaceDataDownload'));
  assert.equal(r.requests[1].etag,null);
  assert.equal((await r.db.prepare("SELECT count(*) n FROM raw_observations WHERE dataset_kind='NAR_RACE_BUNDLE'").first()).n,1);
 }finally{await r.mf.dispose();}
});

test('late daily job is a gap, 429 extends the next alarm, and refusal stops it',async()=>{
 const r=await runtime([{status:429,headers:{'retry-after':'600'}},{status:403}]);
 try{
  const missed=Date.now()-91000;await r.drive(missed);
  assert.equal(r.requests.length,0);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'MISSED_WINDOW');
  const now=Date.now();const rate=await r.drive(now);
  assert.ok(rate.alarm>=now+600000);
  await r.db.prepare('UPDATE source_control SET next_allowed_at=0').run();
  await r.drive(Date.now(),'race');
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
  await r.drive(Date.now()+1);
  assert.equal(r.requests.length,2);
 }finally{await r.mf.dispose();}
});

test('day rollover discovers the new race list; no Mac clock or hard-coded date',async()=>{
 const r=await runtime([],false);
 try{
  const at=Date.parse('2026-10-01T14:01:00Z');
  const result=await(await r.mf.dispatchFetch('http://local/next?at='+at+'&race='+(at-60000))).json();
  assert.equal(result.at,Date.parse('2026-10-01T22:00:00Z'));
  assert.equal(result.date,'20261002');assert.equal(result.kind,'race');
  await r.drive(Date.now());assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});


test('refusal stop survives its first D1 write failure',async()=>{
 const r=await runtime([{status:403}],true,'refusal');
 try{
  const at=Date.now();await r.drive(at);await r.drive(at);
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
 }finally{await r.mf.dispose();}
});

test('claim without a capture row keeps the job until its missed window is recorded',async()=>{
 const r=await runtime([],true,'claim');
 try{
  const at=Date.now();const failed=await r.drive(at);
  assert.equal(failed.error,'injected write failure');assert.equal(failed.job.at,at);
  const retry=await r.drive(at);
  assert.equal(retry.error,'CAPTURE_NOT_RECORDED');assert.equal(retry.job.at,at);
  const expired=await r.drive(at,'odds',at+91000);
  assert.equal(expired.error,null);assert.ok(expired.job.at>at);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'MISSED_WINDOW');
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('a previous-day race capture replay preserves the receipt date',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()-86400000;const receipt=new Date(at).toISOString().replace('Z','000+00:00');
  await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status,collector_received_at) VALUES(?,?,'RAW_STORED',?)")
   .bind('nar-daily-race:'+at,receipt,receipt).run();
  const replay=await r.drive(at,'race');
  assert.equal(replay.lastRaceAt,at);assert.equal(replay.job.kind,'race');assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});


test('interrupted preflight reservation becomes a gap without sending HTTP',async()=>{
 const r=await runtime([],true,'preflight');
 try{
  const at=Date.now();assert.equal((await r.drive(at)).error,'injected preflight failure');
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'RESERVED');
  const expired=await r.drive(at,'odds',at+91000);
  assert.equal(expired.error,null);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'MISSED_WINDOW');
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('unknown interrupted HTTP is terminally recorded and stops the provider',async()=>{
 for(const phase of ['PENDING','FETCHING']){
  const r=await runtime([]);
  try{
   const at=Date.now()-120000;
   await r.db.prepare('INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,?)')
    .bind('nar-daily-odds:'+at,new Date(at).toISOString(),phase).run();
   assert.equal((await r.drive(at)).error,null);
   assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'INCOMPLETE_FETCH');
   assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
   assert.equal(r.requests.length,0);
  }finally{await r.mf.dispose();}
 }
});

test('interrupted raw write with an intent manifest records a gap and advances',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()-120000,event='nar-daily-odds:'+at;
  const receipt=new Date(at+1000).toISOString().replace('Z','000+00:00');
  await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'FETCHING')")
   .bind(event,new Date(at).toISOString()).run();
  const bucket=await r.mf.getR2Bucket('RAW');
  await bucket.put('manifests/'+event+'.json',JSON.stringify({event_id:event,raw_sha256:'0'.repeat(64),
   http_status:200,headers_received_at:receipt,collector_received_at:receipt,raw_saved_at:null}));
  const result=await r.drive(at);
  assert.equal(result.error,null);assert.ok(result.job.at>at);
  const stored=await r.db.prepare('SELECT status,error_code,collector_received_at FROM captures').first();
  assert.equal(stored.status,'STORAGE_ERROR');assert.equal(stored.error_code,'RAW_MISSING');
  assert.equal(stored.collector_received_at,receipt);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,0);
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

const target=(kind='state',race=1)=>({kind,race_id:'20000101:SYNTHETIC:'+race,
 url:'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/'+(kind==='state'?'OddsTanFuku':'RaceMarkTable')+
 '?k_raceDate=2000%2F01%2F01&k_raceNo='+race+'&k_babaCode=19'});
const plan=(r,at,t=target())=>r.mf.dispatchFetch('http://local/plan',{method:'POST',body:JSON.stringify({at,target:t})});

test('planned pages share interval, raw history and replay controls; consume one daily slot',async()=>{
 const r=await runtime([{body:zip},{body:'<!doctype html><html>SYNTHETIC</html>',headers:{'content-type':'text/html'}}]);
 try{
  const now=Date.now(), at=now+360000;
  assert.equal((await plan(r,at)).status,200);
  assert.equal((await plan(r,at)).status,200);
  assert.equal((await plan(r,at,target('state',2))).status,400);
  const first=await r.drive(at-200000,'odds',at-200000);
  assert.equal(first.job.at,at);assert.equal(first.job.kind,'state');
  const state=await r.drive(at,'state',at);
  assert.equal(state.error,null);assert.ok(state.alarm>=at+120000);
  await r.drive(at,'state',at);
  assert.equal(r.requests.length,2);
  assert.equal(r.requests[1].etag,null);assert.ok(r.requests[1].url.includes('OddsTanFuku'));
  const obs=await r.db.prepare("SELECT * FROM raw_observations WHERE dataset_kind='NAR_PAGE_STATE'").first();
  assert.ok(obs);assert.equal(obs.source_updated_at,null);
  const bucket=await r.mf.getR2Bucket('RAW');const m=await(await bucket.get('manifests/'+obs.observation_id+'.json')).json();
  assert.equal(m.url,target().url);assert.equal(m.race_id,target().race_id);
 }finally{await r.mf.dispose();}
});

test('page targets reject another host, duplicate parameters and late plans before HTTP',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()+360000;
  for(const t of [{...target(),url:target().url.replace('www.keiba.go.jp','invalid.example')},
                  {...target(),url:target().url+'&k_babaCode=19'},target('state',13)]){
   assert.equal((await plan(r,at,t)).status,400);
  }
  assert.equal((await plan(r,Date.now())).status,400);
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('a page challenge stops both page and odds collection',async()=>{
 const r=await runtime([{body:'<!doctype html><html>CAPTCHA</html>',headers:{'content-type':'text/html'}}]);
 try{
  const at=Date.now()+360000;assert.equal((await plan(r,at)).status,200);
  await r.drive(at,'state',at);
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
  await r.drive(at+120000,'odds',at+120000);
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
 }finally{await r.mf.dispose();}
});

test('expired page slot remains a gap instead of fetching a newer page',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()+360000;await plan(r,at);
  await r.drive(at,'state',at+91000);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'MISSED_WINDOW');
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('late previous receipt delays page execution within its original window, without consuming it',async()=>{
 const r=await runtime([{body:'<html>SYNTHETIC</html>',headers:{'content-type':'text/html'}}]);
 try{
  const at=Date.now()+360000;await plan(r,at);
  await r.db.prepare('UPDATE source_control SET next_allowed_at=?').bind(at+5000).run();
  const wait=await r.drive(at,'state',at);
  assert.equal(wait.alarm,at+5000);assert.equal(wait.job.at,at);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM captures').first()).n,0);
  await r.drive(at,'state',at+5000);
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'RAW_STORED');
 }finally{await r.mf.dispose();}
});
