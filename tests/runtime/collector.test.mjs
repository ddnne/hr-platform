import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';

// The test-only HTTP harness is never the production Worker entrypoint.
const bundle = await build({stdin: {contents: `import production,{capture,retryAfter,sampleSlotAllowed} from './workers/ingestion/index.ts';
export default {async fetch(req,env){const u=new URL(req.url); if(u.pathname==='/tick'){
 const fault=u.searchParams.get('fault');
 if(fault==='cancel403'||fault==='cancel429') globalThis.fetch=async()=>new Response(
 new ReadableStream({start(controller){controller.error(new Error('stream failed'));}}),
 {status:fault==='cancel403'?403:429,headers:{'retry-after':'600'}});
 let manifestWrites=0;
 const bindings=fault==='publish' ? {...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),
 batch:async()=>{throw new Error('injected index publication failure');}}}
 : fault==='metrics' ? {...env,INDEX:{batch:env.INDEX.batch.bind(env.INDEX),
 prepare:(sql)=>sql.startsWith('UPDATE captures SET processing_ms')
 ? {bind:()=>({run:async()=>{throw new Error('metric failure');}})} : env.INDEX.prepare(sql)}}
 : fault==='manifest' ? {...env,RAW:{head:env.RAW.head.bind(env.RAW),get:env.RAW.get.bind(env.RAW),
 put:async(key,value)=>{if(key.startsWith('manifests/') && ++manifestWrites===2) throw new Error('manifest failure');
 return env.RAW.put(key,value);}}}
 : fault==='failureEvidence' ? {...env,RAW:{head:env.RAW.head.bind(env.RAW),get:env.RAW.get.bind(env.RAW),
 put:async(key,value)=>{if(key.startsWith('failure-metadata/')) throw new Error('evidence failure');
 return env.RAW.put(key,value);}}} : env;
 await capture(Number(u.searchParams.get('t')),bindings); return new Response('ok');}
 if(u.pathname==='/scheduled'){
 const t=Number(u.searchParams.get('t')),originalNow=Date.now;
 const late=u.searchParams.get('late')==='true';
 const bindings=late?{...env,INDEX:{batch:env.INDEX.batch.bind(env.INDEX),prepare(sql){
 const statement=env.INDEX.prepare(sql);
 if(sql.startsWith('SELECT etag')) return {first:async()=>{const value=await statement.first();Date.now=()=>t+120001;return value;}};
 return statement;}}}:env;
 if(u.searchParams.get('expired')==='true') Date.now=()=>t+120001;
 if(u.searchParams.has('now')) Date.now=()=>Number(u.searchParams.get('now'));
 try{await production.scheduled({scheduledTime:t},bindings);}finally{Date.now=originalNow;}
 return new Response('ok');}
 if(u.pathname==='/sample-check') return Response.json(sampleSlotAllowed(u.searchParams.get('plan'),Number(u.searchParams.get('t')),Number(u.searchParams.get('now'))));
 if(u.pathname==='/retry') return Response.json(retryAfter(u.searchParams.get('v'),1000));
 return production.fetch();}};`, resolveDir: process.cwd(), sourcefile: 'harness.ts'},
 bundle: true, write: false, format: 'esm', platform: 'browser', target: 'es2022'});
const schema = await readFile('migrations/0001_capture.sql', 'utf8') + await readFile('migrations/0002_processing_metrics.sql', 'utf8');
const zip = new Uint8Array([0x50, 0x4b, 3, 4, 1, 2, 3]); // raw capture only, not a parser success fixture

async function runtime(responses, enabled=true, extraBindings={}) {
 let requests = [];
 const mf = new Miniflare(convertV4MiniflareOptions({modules: true, script: bundle.outputFiles[0].text,
  compatibilityDate: '2026-09-28', compatibilityFlags: ['nodejs_compat'],
  bindings: {COLLECTION_ENABLED: String(enabled), SOURCE_APPROVED: String(enabled), ...extraBindings},
  d1Databases: ['INDEX'], r2Buckets: ['RAW'], log: new Log(LogLevel.NONE),
  outboundService: async req => {requests.push({url:req.url,etag:req.headers.get('if-none-match'),
    userAgent:req.headers.get('user-agent'),accept:req.headers.get('accept')});
    const item = responses.shift(); if(!item) throw new Error('unexpected outbound request');
    return new Response(item.body ?? null, {status:item.status ?? 200, headers:item.headers});}
 }));
 const db = await mf.getD1Database('INDEX');
 try {for (const statement of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean)) await db.prepare(statement).run();}
 catch(error){await mf.dispose();throw error;}
 const tick = async t => {const r=await mf.dispatchFetch(`http://local/tick?t=${t}`); assert.equal(r.status,200);};
 const reset = () => db.prepare('UPDATE source_control SET next_allowed_at=0').run();
 return {mf,db,requests,tick,reset};
}

test('disabled gate has no network and public endpoint returns 404', async()=>{
 const r=await runtime([],false);
 try {await r.tick(1);assert.equal(r.requests.length,0); assert.equal((await r.mf.dispatchFetch('http://local/')).status,404);}
 finally{await r.mf.dispose();}
});

test('same bytes at two times retain observations; same event and concurrent delivery do not',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"a"'}},{body:zip,headers:{etag:'"a"'}}]);
 try {
  await Promise.all([r.tick(1000),r.tick(1000)]);
  await r.reset(); await r.tick(121000); await r.tick(121000);
  const rows=await r.db.prepare('SELECT * FROM raw_observations ORDER BY observation_id').all();
  assert.equal(rows.results.length,2);assert.equal(r.requests.length,2);
  assert.equal(rows.results[0].raw_sha256,rows.results[1].raw_sha256);
  assert.equal(rows.results[0].source_updated_at,null);
  const raw=await r.mf.getR2Bucket('RAW'); assert.equal((await raw.list({prefix:'raw/'})).objects.length,1);
  const captures=await r.db.prepare('SELECT available_at,parsed_at FROM captures').all();
  assert.ok(captures.results.every(x=>x.available_at===null&&x.parsed_at===null));
 }finally{await r.mf.dispose();}
});

test('validated 304 records observation and never reuses an old ETag for a changed 200',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"a"'}},{status:304},
  {body:new Uint8Array([0x50,0x4b,3,4,9])},{status:304}]);
 try{
  await r.tick(1);await r.reset();await r.tick(2);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,2);
  await r.reset();await r.tick(3);
  const third=await r.db.prepare('SELECT etag FROM captures WHERE event_id=?').bind('nar-daily-odds:3').first();
  assert.equal(third.etag,null);
  // Current implementation must select the newest successful body, even if it lacks a validator.
  await r.reset();await r.tick(4);
  assert.equal(r.requests[3].etag,null);
  assert.equal((await r.db.prepare('SELECT error_code FROM captures WHERE event_id=?').bind('nar-daily-odds:4').first()).error_code,'UNBOUND_304');
 }finally{await r.mf.dispose();}
});

test('403 and HTML challenge stop further requests',async()=>{
 for(const response of [{status:403},{body:'<html>captcha</html>'},{status:503,body:'CAPTCHA verification'},{status:503,body:'challenge',headers:{'cf-mitigated':'challenge'}}]){
  const r=await runtime([response]);
  try{await r.tick(1);await r.reset();await r.tick(2);
   assert.equal(r.requests.length,1);assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
   assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  }finally{await r.mf.dispose();}
 }
});

test('429 honors Retry-After durably; failure and recovery retain separate attempts',async()=>{
 const r=await runtime([{status:429,headers:{'retry-after':'600'}},{body:zip}]);
 try{
  const before=Date.now();await r.tick(1);await r.tick(2);
  assert.equal(r.requests.length,1);
  assert.ok((await r.db.prepare('SELECT next_allowed_at FROM source_control').first()).next_allowed_at >= before+600000);
  await r.reset();await r.tick(3);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM captures').first()).n,3);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,1);
  assert.equal(await (await r.mf.dispatchFetch('http://local/retry?v=60')).json(),121000);
 }finally{await r.mf.dispose();}
});

test('oversized response cannot publish an observation',async()=>{
 const r=await runtime([{body:zip,headers:{'content-length':String(17*1024*1024)}}]);
 try{await r.tick(1);assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);}
 finally{await r.mf.dispose();}
});

test('R2 manifest repairs D1 publication failure without re-fetch or duplicate observation',async()=>{
 const r=await runtime([{body:zip}]);
 try{
  assert.equal((await r.mf.dispatchFetch('http://local/tick?t=1&fault=publish')).status,200);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'STORAGE_ERROR');
  await r.tick(1);await r.tick(1);
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,1);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'RAW_STORED');
 }finally{await r.mf.dispose();}
});

test('raw write followed by completion-manifest failure recovers original receipt from intent',async()=>{
 const r=await runtime([{body:zip}]);
 try{
  assert.equal((await r.mf.dispatchFetch('http://local/tick?t=1&fault=manifest')).status,200);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  const raw=await r.mf.getR2Bucket('RAW');
  const before=await (await raw.get('manifests/nar-daily-odds:1.json')).json();
  assert.equal(before.raw_saved_at,null);
  await r.tick(1);
  assert.equal(r.requests.length,1);
  const after=await r.db.prepare('SELECT * FROM raw_observations').first();
  assert.equal(after.received_at,before.collector_received_at);
  assert.ok(after.raw_saved_at>=after.received_at);
 }finally{await r.mf.dispose();}
});

test('errored body cancellation cannot bypass refusal or Retry-After',async()=>{
 for(const fault of ['cancel403','cancel429']){
  const r=await runtime([]);
  try{
   const now=Date.now();await r.mf.dispatchFetch(`http://local/tick?t=1&fault=${fault}`);
   const control=await r.db.prepare('SELECT * FROM source_control').first();
   if(fault==='cancel403') assert.equal(control.blocked,1);
   else assert.ok(control.next_allowed_at>=now+600000);
   assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  }finally{await r.mf.dispose();}
 }
});

test('ordinary HTML 429 waits; a 429 CAPTCHA stops; Retry-After is retained in both',async()=>{
 for(const challenge of [false,true]){
  const r=await runtime([{status:429,body:challenge?'<html>CAPTCHA</html>':'<html>Too many requests</html>',
   headers:{'content-type':'text/html','retry-after':'600'}}]);
  try{
   const before=Date.now();await r.tick(1);await r.tick(2);
   const control=await r.db.prepare('SELECT * FROM source_control').first();
   assert.equal(control.blocked,Number(challenge));assert.ok(control.next_allowed_at>=before+600000);
   const record=await r.db.prepare('SELECT retry_after_at,error_code FROM captures WHERE event_id=?').bind('nar-daily-odds:1').first();
   assert.ok(record.retry_after_at);assert.equal(record.error_code,challenge?'NON_ZIP_OR_CHALLENGE':'RATE_LIMITED');
   assert.equal(r.requests.length,1);
  }finally{await r.mf.dispose();}
 }
});

test('successful publication records processing wall time separately from receipt duration',async()=>{
 const r=await runtime([{body:zip}]);
 try{
  await r.tick(1);
  const record=await r.db.prepare('SELECT duration_ms,processing_ms FROM captures').first();
  assert.ok(record.processing_ms>=record.duration_ms);assert.ok(record.duration_ms>=0);
 }finally{await r.mf.dispose();}
});

test('failed metric write does not invalidate a published observation or its validator',async()=>{
 const r=await runtime([{body:zip,headers:{etag:'"v1"'}},{status:304}]);
 try{
  await r.mf.dispatchFetch('http://local/tick?t=1&fault=metrics');
  const record=await r.db.prepare('SELECT status,processing_ms FROM captures').first();
  assert.equal(record.status,'RAW_STORED');assert.equal(record.processing_ms,null);
  await r.reset();await r.tick(2);
  assert.equal(r.requests[1].etag,'"v1"');
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,2);
 }finally{await r.mf.dispose();}
});

test('finite sample schedule validates dates, spacing and late delivery',async()=>{
 const r=await runtime([]);const start=1_800_000_000_000;
 const check=async(plan,t=start,now=start)=>{
  const u=new URL('http://local/sample-check');u.searchParams.set('plan',typeof plan==='string'?plan:JSON.stringify(plan));
  u.searchParams.set('t',t);u.searchParams.set('now',now);
  return (await r.mf.dispatchFetch(u)).json();
 };
 try{
  assert.equal(await check([start,start+300_000]),true);
  assert.equal(await check([start,start+300_000],start+300_000,start+300_001),true);
  assert.equal(await check([start],start,start+120_000),true);
  for(const plan of [[],[start,start+300_000,start+600_000],[start,start],
    [start,start+120_000],[start+300_000,start],[start+1],[''+start],{},'bad',null]){
    assert.equal(await check(plan),false);
  }
  assert.equal(await check([start],start,start-1),false);
  assert.equal(await check([start],start,start+120_001),false);
  assert.equal(await check([start],start+300_000,start+300_000),false);
  assert.equal(await check([start],start,NaN),false);
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
});

test('production scheduled handler blocks empty plan and limits delivery to approved slot',async()=>{
 const start=Math.floor(Date.now()/60_000)*60_000;
 for(const plan of ['[]',JSON.stringify([start,start+300_000])]){
  const r=await runtime([{body:zip}],true,{CAPTURE_SLOTS_JSON:plan});
  try{
   const scheduled=async t=>assert.equal((await r.mf.dispatchFetch(`http://local/scheduled?t=${t}`)).status,200);
   await scheduled(start-300_000);await scheduled(start+600_000);
   assert.equal(r.requests.length,0);
   await scheduled(start);await scheduled(start);
   assert.equal(r.requests.length,plan==='[]'?0:1);
   assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,plan==='[]'?0:1);
  }finally{await r.mf.dispose();}
 }
});

test('D1 delay cannot start HTTP beyond the finite sample window',async()=>{
 const start=Math.floor(Date.now()/60_000)*60_000;
 const r=await runtime([],true,{CAPTURE_SLOTS_JSON:JSON.stringify([start])});
 try{
  assert.equal((await r.mf.dispatchFetch(`http://local/scheduled?t=${start}&late=true`)).status,200);
  assert.equal(r.requests.length,0);
  const record=await r.db.prepare('SELECT status,error_code FROM captures').first();
  assert.equal(record.status,'FAILED');assert.equal(record.error_code,'SAMPLE_WINDOW_EXPIRED');
 }finally{await r.mf.dispose();}
});

test('expired approved slot repairs stored manifest without another request',async()=>{
 const start=Math.floor(Date.now()/60_000)*60_000;
 const r=await runtime([{body:zip}],true,{CAPTURE_SLOTS_JSON:JSON.stringify([start])});
 try{
  assert.equal((await r.mf.dispatchFetch(`http://local/tick?t=${start}&fault=publish`)).status,200);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'STORAGE_ERROR');
  assert.equal((await r.mf.dispatchFetch(`http://local/scheduled?t=${start}&expired=true`)).status,200);
  assert.equal(r.requests.length,1);
  assert.equal((await r.db.prepare('SELECT status FROM captures').first()).status,'RAW_STORED');
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});

test('Cron second offsets share one planned minute and keep actual receipt clocks',async()=>{
 const start=Math.floor(Date.now()/60_000)*60_000;
 const r=await runtime([{body:zip}],true,{CAPTURE_SLOTS_JSON:JSON.stringify([start])});
 try{
  const scheduled=async(t,now)=>r.mf.dispatchFetch(`http://local/scheduled?t=${t}&now=${now}`);
  await scheduled(start-1,start+57_000);
  await scheduled(start+60_000,start+61_000);
  await scheduled(start+56_000,start+55_999);
  assert.equal(r.requests.length,0);
  await scheduled(start+56_000,start+57_000);
  await scheduled(start+58_000,start+59_000);
  assert.equal(r.requests.length,1);
  const row=await r.db.prepare('SELECT * FROM captures').first();
  assert.equal(row.event_id,`nar-daily-odds:${start}`);
  assert.equal(row.scheduled_capture_at,new Date(start).toISOString());
  assert.equal(row.fetch_started_at,new Date(start+57_000).toISOString());
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});

test('Cron offset does not extend the planned minute deadline',async()=>{
 const start=Math.floor(Date.now()/60_000)*60_000;
 const r=await runtime([],true,{CAPTURE_SLOTS_JSON:JSON.stringify([start])});
 try{
  await r.mf.dispatchFetch(`http://local/scheduled?t=${start+56_000}&now=${start+120001}`);
  assert.equal(r.requests.length,0);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM captures').first()).n,0);
 }finally{await r.mf.dispose();}
});

test('identified ZIP request and failure metadata distinguish HTML from explicit challenge',async()=>{
 for(const challenge of [false,true]){
  const headers={'content-type':'text/html','set-cookie':'SYNTHETIC_COOKIE_MUST_NOT_BE_RECORDED'};
  if(challenge) headers['cf-mitigated']='challenge';
  const r=await runtime([{status:404,headers,body:'SYNTHETIC_PRIVATE_ERROR_BODY'}]);
  try{
   await r.tick(1);await r.reset();await r.tick(1);await r.tick(2);
   assert.equal(r.requests.length,1);
   assert.equal(r.requests[0].userAgent,'hr-platform-personal-research/0.1');
   assert.equal(r.requests[0].accept,'application/zip');
   const raw=await r.mf.getR2Bucket('RAW');
   const text=await (await raw.get('failure-metadata/nar-daily-odds:1.json')).text();
   const report=JSON.parse(text);
   assert.equal(report.http_status,404);
   assert.equal(report.denial_basis,challenge?'CHALLENGE_HEADER':'NON_SUCCESS_HTML');
   assert.equal(report.content_type,'text/html');assert.equal(report.body_prefix_truncated,false);
   assert.equal(Buffer.from(report.body_prefix_base64,'base64').toString(),'SYNTHETIC_PRIVATE_ERROR_BODY');
   assert.ok(!text.includes('SYNTHETIC_COOKIE')&&!text.includes('SYNTHETIC_PRIVATE_ERROR_BODY'));
   assert.equal((await raw.list({prefix:'failure-metadata/'})).objects.length,1);
   assert.equal((await raw.list({prefix:'raw/'})).objects.length,0);
   assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
   assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
  }finally{await r.mf.dispose();}
 }
});

test('failed diagnostic metadata write cannot remove the source stop',async()=>{
 const r=await runtime([{status:403}]);
 try{
  await r.mf.dispatchFetch('http://local/tick?t=1&fault=failureEvidence');
  await r.reset();await r.tick(2);
  assert.equal(r.requests.length,1);
  const row=await r.db.prepare('SELECT status,error_code FROM captures WHERE event_id=?').bind('nar-daily-odds:1').first();
  assert.equal(row.status,'FAILED');assert.equal(row.error_code,'SOURCE_DENIED');
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
 }finally{await r.mf.dispose();}
});

test('private failure evidence retains at most 8 KiB and reports truncation',async()=>{
 const r=await runtime([{status:404,headers:{'content-type':'text/html'},body:'X'.repeat(9000)}]);
 try{
  await r.tick(1);
  const raw=await r.mf.getR2Bucket('RAW');
  const report=await (await raw.get('failure-metadata/nar-daily-odds:1.json')).json();
  assert.equal(report.body_bytes,9000);
  assert.equal(report.body_prefix_truncated,true);
  assert.equal(Buffer.from(report.body_prefix_base64,'base64').byteLength,8192);
  assert.equal((await r.db.prepare('SELECT blocked FROM source_control').first()).blocked,1);
 }finally{await r.mf.dispose();}
});
