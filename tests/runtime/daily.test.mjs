import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare, Log, LogLevel, convertV4MiniflareOptions} from 'miniflare';

// Only this synthetic harness exposes drive/state. Production HTTP remains 404.
const code = await build({stdin:{contents:`
import {NarCollector,nextJob,nextMonthly} from './workers/ingestion/daily.ts';
import {monthlyTarget} from './workers/ingestion/capture.ts';
import {registerPage,registerEvidenceBatch} from './workers/ingestion/pages.ts';
export class TestCollector extends NarCollector {
 constructor(ctx,env){
  // drive() owns the synthetic clock. Real workerd alarms must not run between
  // manual deliveries when a fixture date has passed on the host clock.
  const storage=ctx.storage;
  storage.getAlarm=async()=>await storage.get('syntheticAlarm')??null;
  storage.setAlarm=at=>storage.put('syntheticAlarm',Number(at));
  storage.deleteAlarm=()=>storage.delete('syntheticAlarm');
  let failures=0,notification=Promise.resolve(null);const index={batch:env.INDEX.batch.bind(env.INDEX),prepare(sql){
  const st=env.INDEX.prepare(sql);
  if(env.FAULT==='preflight' && sql.startsWith('SELECT etag'))return {bind:(...args)=>({first:async()=>{
   if(failures++===0)throw new Error('injected preflight failure');return st.bind(...args).first();}})};
  if((env.FAULT==='claim' && sql.startsWith('INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,'))
    || (['refusal','refusal-persistent'].includes(env.FAULT) && sql.startsWith('UPDATE source_control SET blocked=1'))) {
   return {bind:(...args)=>{const bound=st.bind(...args);return {run:async()=>{if(env.FAULT==='refusal-persistent'||failures++===0)throw new Error('injected write failure');return bound.run();}}}};
  }return st;}};
  const research=env.FAULT==='notification'?{normalize_saved(){notification=(async()=>{
    const alarm=await ctx.storage.getAlarm();const job=await ctx.storage.get('job');
    await ctx.storage.put('notification',{alarm,job});throw new Error('synthetic parser failure');
  })();return notification;}}:undefined;
  super(ctx,{...env,INDEX:index,RESEARCH:research});this.notification=()=>notification.catch(()=>null);}
 async wake(now,preserve=false){const realNow=Date.now;Date.now=()=>now;
  try{if(!preserve)await this.ctx.storage.deleteAlarm();await this.ensure();return {job:await this.ctx.storage.get('job'),alarm:await this.ctx.storage.getAlarm()};}
  finally{Date.now=realNow}}
 async drive(at,kind,now){const plan=await this.env.INDEX.prepare('SELECT * FROM page_capture_plans WHERE event_id=?').bind('nar-daily-'+kind+':'+at).first();
 const page=['state','payout'].includes(kind)?plan:undefined;
 await this.ctx.storage.put('job',{at,kind,date:'SYNTHETIC',...(plan?{planned:true}:{}),...(page?{page}:{})});let error=null;const realNow=Date.now;
 if(now)Date.now=()=>now;
 try{await this.alarm()}catch(e){error=e.message}finally{Date.now=realNow}
 await this.notification();
 return {error,notification:await this.ctx.storage.get('notification'),job:await this.ctx.storage.get('job'),lastRaceAt:await this.ctx.storage.get('lastRaceAt'),alarm:await this.ctx.storage.getAlarm()};}
}
export default {async fetch(request,env){const u=new URL(request.url);
 if(u.pathname==='/plan'){const {at,target}=await request.json();try{return Response.json(await registerPage(env,at,target));}catch(e){return new Response(e.message,{status:400});}}
 if(u.pathname==='/batch'){try{
   const {entries:rows,revision}=await request.json();const index={prepare:env.INDEX.prepare.bind(env.INDEX),batch:async statements=>{
     if(env.FAULT==='plan-conflict'){const r=rows[0],t={...r,...{race_id:'20000101:SYNTHETIC:2',url:r.url.replace('k_raceNo=1','k_raceNo=2')}};
       await env.INDEX.prepare('INSERT OR IGNORE INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
         .bind('nar-daily-'+t.kind+':'+t.at,t.at,t.kind,t.url,t.race_id,new Date().toISOString()).run();}
     return env.INDEX.batch(statements);}};
   return Response.json(await registerEvidenceBatch({...env,INDEX:index},rows,revision));
 }catch(e){return new Response(e.message,{status:400});}}
 if(u.pathname==='/next')return Response.json(nextJob(Number(u.searchParams.get('at')),u.searchParams.has('race')?Number(u.searchParams.get('race')):null));
 if(u.pathname==='/monthly')return Response.json({...nextMonthly(Number(u.searchParams.get('at')),u.searchParams.has('previous')?Number(u.searchParams.get('previous')):null),...monthlyTarget(Number(u.searchParams.get('at')))});
 const stub=env.COLLECTOR.getByName('synthetic');
 if(u.pathname==='/wake')return Response.json(await stub.wake(Number(u.searchParams.get('now'))));
 if(u.pathname==='/ensure')return Response.json(await stub.wake(Number(u.searchParams.get('now')),true));
 return Response.json(await stub.drive(Number(u.searchParams.get('at')),u.searchParams.get('kind'),Number(u.searchParams.get('now'))));}};
`,sourcefile:'daily-harness.ts',resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const schema = await readFile('migrations/0001_capture.sql','utf8') + await readFile('migrations/0002_processing_metrics.sql','utf8') + await readFile('migrations/0007_page_evidence.sql','utf8') + await readFile('migrations/0009_evidence_packet_owner.sql','utf8');
async function runtime(responses, enabled=true, fault="", monthly=false) {
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:code.outputFiles[0].text,
  compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
  durableObjects:{COLLECTOR:{className:'TestCollector',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],
  bindings:{COLLECTION_ENABLED:'true',SOURCE_APPROVED:'true',DAILY_COLLECTION_ENABLED:String(enabled),MONTHLY_COLLECTION_ENABLED:String(monthly),CAPTURE_SLOTS_JSON:'[]',FAULT:fault},
  log:new Log(LogLevel.NONE),outboundService:async req=>{requests.push({url:req.url,etag:req.headers.get('if-none-match')});
   const r=responses.shift();if(!r)throw new Error('unexpected provider request');
   return new Response(r.body??null,{status:r.status??200,headers:r.headers});}}));
 const db=await mf.getD1Database('INDEX');
 for(const statement of schema.replace(/^--.*$/gm,'').split(';').map(x=>x.trim()).filter(Boolean))await db.prepare(statement).run();
 return {mf,db,requests,drive:async(at,kind='odds',now=0)=>{
  const response=await mf.dispatchFetch('http://local/drive?at='+at+'&kind='+kind+'&now='+now);
  if(response.status!==200)throw new Error(await response.text());
  assert.equal(response.status,200);return response.json();}};
}
const zip=new Uint8Array([0x50,0x4b,3,4]);

test('new night payout reservation advances the existing morning alarm without fetching early',async()=>{
 const r=await runtime([{body:zip}]);
 try{
  const at=Date.parse('2000-01-01T13:59:00Z'),pageAt=at+600000;
  const old=await r.drive(at,'odds',at);
  assert.ok(old.alarm>pageAt);
  const unchanged=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
  assert.deepEqual(unchanged.job,old.job);assert.equal(unchanged.alarm,old.alarm);
  // An expired queue head must not hide a later, usable new reservation.
  await r.db.prepare('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
   .bind('nar-daily-payout:'+(at-600000),at-600000,'payout',
    'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=2000/01/01&k_raceNo=2&k_babaCode=03',
    '20000101:SYNTHETIC:2',new Date(at-900000).toISOString()).run();
  await r.db.prepare('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
   .bind('nar-daily-payout:'+pageAt,pageAt,'payout',
    'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=2000/01/01&k_raceNo=1&k_babaCode=03',
    '20000101:SYNTHETIC:1',new Date(at).toISOString()).run();
  await r.db.prepare("UPDATE source_control SET blocked=1").run();
  const blocked=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
  assert.deepEqual(blocked.job,old.job);assert.equal(blocked.alarm,old.alarm);
  await r.db.prepare("UPDATE source_control SET blocked=0").run();
  const next=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
  assert.equal(next.job.kind,'payout');assert.equal(next.job.at,pageAt);assert.equal(next.alarm,pageAt);
  const repeated=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
  assert.deepEqual(repeated,next);
  assert.equal(r.requests.length,1);
 }finally{await r.mf.dispose();}
});

test('reservation updates preserve a due page waiting for the provider gate',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.parse('2000-01-01T04:12:00Z');
  for(const [kind,time] of [['state',at],['payout',at-600000]]){
   await r.db.prepare('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
    .bind('nar-daily-'+kind+':'+time,time,kind,
     'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/'+(kind==='state'?'OddsTanFuku':'RaceMarkTable')+'?k_raceDate=2000/01/01&k_raceNo=1&k_babaCode=03',
     '20000101:SYNTHETIC:1',new Date(at-900000).toISOString()).run();
  }
  await r.db.prepare('UPDATE source_control SET next_allowed_at=?').bind(at+5000).run();
  const old=await r.drive(at,'state',at);
  const ensured=await(await r.mf.dispatchFetch('http://local/ensure?now='+at)).json();
  assert.deepEqual(ensured.job,old.job);assert.equal(ensured.alarm,at+5000);assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('reservation updates preserve regular work during 429 waits and ignore expired pages',async()=>{
 for(const response of [{status:429,headers:{'retry-after':'1200'}},{body:zip}]){
  const r=await runtime([response]);
  try{
   const at=Date.parse('2000-01-01T04:12:00Z');
   const old=await r.drive(at,'odds',at);
   const pageAt=response.status===429?at+300000:at-600000;
   await r.db.prepare('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
    .bind('nar-daily-payout:'+pageAt,pageAt,'payout',
     'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=2000/01/01&k_raceNo=1&k_babaCode=03',
     '20000101:SYNTHETIC:1',new Date(at-900000).toISOString()).run();
   const ensured=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
   assert.deepEqual(ensured.job,old.job);assert.equal(ensured.alarm,old.alarm);assert.equal(r.requests.length,1);
  }finally{await r.mf.dispose();}
 }
});

test('a newly reserved page can wait within its own window and take the regular slot',async()=>{
 for(const time of ['2000-01-01T04:12:00Z','2000-01-01T13:59:00Z']){
  const r=await runtime([{status:429,headers:{'retry-after':'1200'}},
   {body:'<html>SYNTHETIC payout</html>',headers:{'content-type':'text/html'}}]);
  try{
   const at=Date.parse(time),pageAt=at+1140000,allowedAt=at+1200000;
   await r.drive(at,'odds',at);
   await r.db.prepare('INSERT INTO page_capture_plans(event_id,at,kind,url,race_id,registered_at) VALUES(?,?,?,?,?,?)')
    .bind('nar-daily-payout:'+pageAt,pageAt,'payout',
     'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RaceMarkTable?k_raceDate=2000/01/01&k_raceNo=1&k_babaCode=03',
     '20000101:SYNTHETIC:1',new Date(at).toISOString()).run();
   const ensured=await(await r.mf.dispatchFetch('http://local/ensure?now='+(at+60000))).json();
   assert.equal(ensured.job.at,pageAt);assert.equal(ensured.job.kind,'payout');assert.equal(ensured.alarm,allowedAt);
   assert.equal(r.requests.length,1);
   const captured=await r.drive(pageAt,'payout',allowedAt);
   assert.equal(captured.error,null);assert.equal(r.requests.length,2);
   assert.equal((await r.db.prepare('SELECT status FROM captures WHERE event_id=?')
    .bind('nar-daily-payout:'+pageAt).first()).status,'RAW_STORED');
  }finally{await r.mf.dispose();}
 }
});

test('failed stop writes are never mistaken for an authorized release',async()=>{
 const r=await runtime([{status:403}],true,'refusal-persistent');
 try{
  const at=new Date(Date.now()+86400000).setUTCHours(4,12,0,0);
  const failed=await r.drive(at,'odds',at);
  assert.ok(failed.error);
  assert.equal((await r.db.prepare('SELECT error_code FROM captures').first()).error_code,'STOP_WRITE_FAILED');
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,0);
  const wake=await(await r.mf.dispatchFetch('http://local/wake?now='+(at+900000))).json();
  assert.equal(wake.job.at,at);assert.equal(r.requests.length,1);
 }finally{await r.mf.dispose();}
});

test('a stopped failed event stays stopped; an explicit release starts a new observation',async()=>{
 const r=await runtime([{body:'<html>SYNTHETIC download error</html>',headers:{'content-type':'text/html'}},{body:zip}]);
 try{
  const at=new Date(Date.now()+86400000).setUTCHours(4,12,0,0);
  await r.drive(at,'odds',at);
  const failed=await r.db.prepare('SELECT * FROM captures').first();
  assert.equal(failed.error_code,'NON_ZIP_OR_CHALLENGE');
  const wake=async now=>(await r.mf.dispatchFetch('http://local/wake?now='+now)).json();
  const held=await wake(at+900000);
  assert.equal(held.alarm,null);assert.equal(held.job.at,at);assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
  // Simulate a separately authorized release, not an automatic collector action.
  await r.db.prepare('UPDATE source_control SET blocked=0,next_allowed_at=?').bind(at+960000).run();
  const resumed=await wake(at+900000);
  assert.equal(resumed.job.at,at+960000);assert.equal(resumed.alarm,resumed.job.at);
  assert.equal(r.requests.length,1); // ensure only arms, it never fetches early.
  await r.drive(resumed.job.at,resumed.job.kind,resumed.job.at);
  assert.equal(r.requests.length,2);
  assert.deepEqual(await r.db.prepare('SELECT * FROM captures WHERE event_id=?').bind(failed.event_id).first(),failed);
  const rows=(await r.db.prepare('SELECT * FROM raw_observations').all()).results;
  assert.equal(rows.length,1);assert.notEqual(rows[0].observation_id,failed.event_id);
  assert.equal(Date.parse(rows[0].received_at),resumed.job.at);
 }finally{await r.mf.dispose();}
});

test('monthly archive replaces one daily slot, shares raw history and stays FINAL_ONLY',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"daily"'}},{body:zip},{body:zip}],true,'',true);
 try{
  const at=Date.parse('2026-10-02T05:10:00Z');
  const first=await r.drive(at-120000,'odds',at-120000);
  assert.equal(first.job.kind,'monthly');assert.equal(first.job.at,at);
  const monthly=await r.drive(at,'monthly',at);assert.equal(monthly.error,null);
  await r.drive(at,'monthly',at);assert.equal(r.requests.length,2);
  assert.equal(r.requests[1].url,'https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=monthly&k_year=2026&k_month=10');
  assert.equal(r.requests[1].etag,null);
  const rows=(await r.db.prepare('SELECT * FROM raw_observations ORDER BY received_at').all()).results;
  assert.equal(rows.length,2);assert.equal(rows[1].dataset_kind,'FINAL_ONLY');
  assert.equal(rows[0].raw_sha256,rows[1].raw_sha256);
  assert.equal(rows[1].source_updated_at,null);
  const bucket=await r.mf.getR2Bucket('RAW');
  const manifest=await(await bucket.get('manifests/'+rows[1].observation_id+'.json')).json();
  assert.equal(manifest.dataset_kind,'FINAL_ONLY');assert.equal(manifest.url,r.requests[1].url);
  assert.ok(monthly.alarm>=at+120000);
  await r.db.prepare('UPDATE source_control SET next_allowed_at=0').run();
  await r.drive(at+120000,'monthly',at+120000);assert.equal(r.requests.length,2);
  assert.equal((await r.db.prepare('SELECT status FROM captures WHERE event_id=?').bind('nar-daily-monthly:'+(at+120000)).first()).status,'WAIT_OR_BLOCKED');
  await r.drive(at+240000,'race',at+240000); // monthly cooldown does not disable daily collection
  assert.equal(r.requests.length,3);
 }finally{await r.mf.dispose();}
});

test('monthly first-day scope and rolling interval survive month/year boundaries',async()=>{
 const r=await runtime([],false);
 try{
  const jan=Date.parse('2027-01-01T00:00:00Z');
  const first=await(await r.mf.dispatchFetch('http://local/monthly?at='+jan)).json();
  assert.equal(first.month,'202612');assert.equal(first.at,Date.parse('2027-01-01T01:00:00Z'));
  const previous=Date.parse('2026-12-31T05:12:00Z');
  const delayed=await(await r.mf.dispatchFetch('http://local/monthly?at='+jan+'&previous='+previous)).json();
  assert.equal(delayed.at,Date.parse('2027-01-01T05:12:00Z'));
 }finally{await r.mf.dispose();}
});

test('failed monthly requests retain the daily wait/stop and the 24-hour archive limit',async()=>{
 for(const status of [429,403]){
  const r=await runtime([{status,headers:{'retry-after':'600'}}],true,'',true);
  try{
   const at=Date.parse('2026-10-02T05:10:00Z');
   const result=await r.drive(at,'monthly',at);assert.equal(result.error,null);
   const control=await r.db.prepare('SELECT * FROM source_control').first();
   if(status===429){assert.equal(control.blocked,0);assert.ok(result.alarm>=at+600000);}
   else assert.equal(control.blocked,1);
   await r.drive(at+120000,'monthly',at+120000);
   assert.equal(r.requests.length,1);
   assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  }finally{await r.mf.dispose();}
 }
});

test('disabling an already armed monthly job advances without HTTP',async()=>{
 const r=await runtime([]);
 try{
  const result=await r.drive(Date.now(),'monthly');
  assert.equal(result.error,null);assert.notEqual(result.job.kind,'monthly');
  assert.equal(r.requests.length,0);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'SKIPPED_DISABLED');
 }finally{await r.mf.dispose();}
});

test('monthly insertion preserves the first odds slot after Paper evidence',async()=>{
 const r=await runtime([{body:zip},{body:zip}],true,'',true);
 try{
  const raceAt=Date.parse('2026-10-02T05:08:00Z');
  const evidence=await r.drive(raceAt,'race',raceAt);
  assert.equal(evidence.job.kind,'odds');assert.equal(evidence.job.at,raceAt+120000);
  const odds=await r.drive(evidence.job.at,'odds',evidence.job.at);
  assert.equal(odds.job.kind,'monthly');assert.ok(odds.job.at>=raceAt+240000);
 }finally{await r.mf.dispose();}
});

test('archive cooldown includes the previous HTTP/preflight duration',async()=>{
 const r=await runtime([{body:zip},{body:zip}],true,'',true);
 try{
  const at=Date.parse('2026-10-02T05:10:00Z');
  await r.drive(at,'monthly',at);
  await r.db.prepare('UPDATE captures SET duration_ms=10000 WHERE event_id=?').bind('nar-daily-monthly:'+at).run();
  await r.drive(at+86400000,'monthly',at+86400000);
  assert.equal(r.requests.length,1);
  await r.drive(at+86410000,'monthly',at+86410000);
  assert.equal(r.requests.length,2);
 }finally{await r.mf.dispose();}
});

test('monthly disable first recovers or terminates an interrupted event',async()=>{
 for(const phase of ['RESERVED','FETCHING']){
  const r=await runtime([]);
  try{
   const at=Date.now()-120000;
   await r.db.prepare('INSERT INTO captures(event_id,scheduled_capture_at,fetch_started_at,status) VALUES(?,?,?,?)')
    .bind('nar-daily-monthly:'+at,new Date(at).toISOString(),new Date(at).toISOString(),phase).run();
   const result=await r.drive(at,'monthly');assert.equal(result.error,null);
   const stored=await r.db.prepare('SELECT status FROM captures').first();
   assert.ok(['MISSED_WINDOW','SKIPPED_DISABLED','INCOMPLETE_FETCH'].includes(stored.status));
   assert.equal(r.requests.length,0);
   if(phase==='FETCHING')assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
   else assert.notEqual(result.job.kind,'monthly');
  }finally{await r.mf.dispose();}
 }
});

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

test('normalizer wakeup sees the armed next capture and its failure cannot stop collection',async()=>{
 const r=await runtime([{body:zip},{body:zip}],true,'notification');
 try{
  const at=Date.now();const first=await r.drive(at);
  assert.equal(first.error,null);assert.equal(first.notification.alarm,first.alarm);
  assert.equal(first.notification.job.at,first.job.at);assert.ok(first.alarm>=at+120000);
  await r.db.prepare('UPDATE source_control SET next_allowed_at=0').run();
  const next=await r.drive(Date.now(),'race');assert.equal(next.error,null);
  assert.equal(r.requests.length,2);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,2);
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

const batch=(r,rows,revision=1)=>r.mf.dispatchFetch('http://local/batch',{method:'POST',body:JSON.stringify({entries:rows,
 revision:'2000-01-01T00:00:00.000000+00:00|2000-01-01T00:00:0'+revision+'.000000+00:00|'+'0'.repeat(64)})});
const packet=(at,race=1)=>[
 {at,...target('state',race)},
 {at:at+120000,kind:'race',race_id:target('state',race).race_id,url:'https://www.keiba.go.jp/KeibaWeb/DataDownload/RaceDataDownload?type=daily'},
 {at:at+1200000,...target('payout',race)}];

test('input packets are reserved completely or rejected without partial rows',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()+360000;
  assert.equal((await batch(r,packet(at))).status,200);
  assert.equal((await batch(r,packet(at))).status,200);
  assert.equal((await batch(r,packet(at+2000000,2))).status,200);
  assert.equal((await batch(r,packet(at+4000000,3))).status,400);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM page_capture_plans').first()).n,6);
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('a reserved race refresh waits for the shared receipt interval and retains its original slot',async()=>{
 const r=await runtime([{body:zip}]);
 try{
  const at=Date.now()+360000;
  assert.equal((await batch(r,packet(at))).status,200);
  const raceAt=at+120000;
  await r.db.prepare('UPDATE source_control SET next_allowed_at=?').bind(raceAt+5000).run();
  const wait=await r.drive(raceAt,'race',raceAt);
  assert.equal(wait.job.at,raceAt);assert.equal(wait.alarm,raceAt+5000);assert.equal(r.requests.length,0);
  const stored=await r.drive(raceAt,'race',raceAt+5000);
  assert.equal(stored.error,null);assert.equal(r.requests.length,1);
  assert.ok(r.requests[0].url.includes('RaceDataDownload'));
  assert.equal((await r.db.prepare("SELECT dataset_kind FROM raw_observations").first()).dataset_kind,'NAR_RACE_BUNDLE');
 }finally{await r.mf.dispose();}
});

test('a conflicting registration after precheck cannot leave a partial packet',async()=>{
 const r=await runtime([],true,'plan-conflict');
 try{
  assert.equal((await batch(r,packet(Date.now()+360000))).status,400);
  const rows=(await r.db.prepare('SELECT * FROM page_capture_plans').all()).results;
  assert.equal(rows.length,1);assert.equal(rows[0].race_id,'20000101:SYNTHETIC:2');
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('schedule revisions supersede only unstarted automatic slots and do not invent a provider wait',async()=>{
 const r=await runtime([{body:'<html>SYNTHETIC</html>',headers:{'content-type':'text/html'}}]);
 try{
  const at=Date.now()+360000, old=packet(at);
  assert.equal((await batch(r,old)).status,200);
  await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'RAW_STORED')")
    .bind('nar-daily-state:'+at,new Date(at).toISOString()).run();
  const manual=at+2000000;assert.equal((await plan(r,manual,target('payout'))).status,200);
  assert.equal((await batch(r,packet(at+120000),2)).status,200);
  assert.equal((await batch(r,packet(at+120000),3)).status,200); // same slots, newer observation
  assert.equal((await batch(r,packet(at),2)).status,400);
  const status=await r.db.prepare('SELECT event_id,status FROM captures ORDER BY event_id').all();
  assert.equal(status.results.filter(r=>r.status==='SUPERSEDED_PLAN').length,2);
  assert.equal((await r.db.prepare('SELECT status FROM captures WHERE event_id=?').bind('nar-daily-state:'+at).first()).status,'RAW_STORED');
  assert.equal(await r.db.prepare('SELECT status FROM captures WHERE event_id=?').bind('nar-daily-payout:'+manual).first(),null);
  const canceled=await r.drive(at+120000,'race',at+120000);
  assert.equal(canceled.error,null);assert.equal(canceled.job.kind,'state');assert.equal(canceled.job.at,at+120000);
  assert.equal(canceled.alarm,at+120000);assert.equal(r.requests.length,0);
  await r.drive(at+120000,'state',at+120000);assert.equal(r.requests.length,1);
  // A superseded event cannot be revived by delivery of an old registration.
  assert.equal((await batch(r,old)).status,400);
 }finally{await r.mf.dispose();}
});

test('late initial registration of an older schedule cannot supersede newer reservations',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()+360000;
  assert.equal((await batch(r,packet(at+120000),2)).status,200);
  assert.equal((await batch(r,packet(at+120000),3)).status,200);
  assert.equal((await batch(r,packet(at),2)).status,400);
  assert.equal((await batch(r,packet(at),1)).status,400);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM captures').first()).n,0);
  const rows=(await r.db.prepare('SELECT * FROM page_capture_plans').all()).results;
  assert.equal(rows.length,3);assert.ok(rows.every(x=>x.packet_revision.includes('00:00:03.')));
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('post-decision delay can retain both input slots and replace only the payout reservation',async()=>{
 const r=await runtime([]);
 try{
  const at=Date.now()+360000, old=packet(at);
  assert.equal((await batch(r,old)).status,200);
  for(const entry of old.slice(0,2)) {
   await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'RAW_STORED')")
    .bind(`nar-daily-${entry.kind}:${entry.at}`,new Date(entry.at).toISOString()).run();
  }
  const revised=[...old.slice(0,2),{...old[2],at:old[2].at+1200000}];
  assert.equal((await batch(r,revised,2)).status,200);
  assert.equal((await batch(r,revised,2)).status,200);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM page_capture_plans').first()).n,4);
  assert.equal((await r.db.prepare('SELECT status FROM captures WHERE event_id=?')
   .bind('nar-daily-payout:'+old[2].at).first()).status,'SUPERSEDED_PLAN');
  assert.equal((await r.db.prepare("SELECT count(*) n FROM captures WHERE status='RAW_STORED'").first()).n,2);
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});
