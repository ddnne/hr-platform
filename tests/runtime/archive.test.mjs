import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare, Log, LogLevel, convertV4MiniflareOptions} from 'miniflare';

const bundle = await build({stdin:{contents:`import {collectArchive,inspectPage} from './workers/archive/index.ts';
export default {async fetch(request,env){const u=new URL(request.url);
 if(u.pathname==='/inspect') return Response.json(inspectPage(await request.json(),{day:'2026-09-29',page:1}));
 const now=Number(u.searchParams.get('at')),previous=Date.now;let clock=now;Date.now=()=>clock;
 let calls=0;const delay=Number(u.searchParams.get('delay')||0);
 const bindings={...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),batch:async statements=>{
  const result=await env.INDEX.batch(statements);if(++calls===1)clock+=delay;return result;}}};
 try{return Response.json(await collectArchive(now,bindings));}finally{Date.now=previous;}}};`,
 resolveDir:process.cwd(),sourcefile:'archive-harness.ts'},bundle:true,write:false,format:'esm',
 platform:'browser',target:'es2022',external:['cloudflare:workers']});
const start = Date.parse('2026-09-30T00:00:00Z');
const page = (day='2026-09-29',numbers=[1,2],total=3) => ({race_kind:'nar',race_type:'NAR_TODAY',race_date:day,
 total_count:total,nar_info:{race_date:day,race_info:numbers.map(n=>({race_type:'NAR',
 race_info:{race_date:day,track:'合成競馬場',race:n,course:'ダート 1400ｍ（右）'},
 horse_info:{1:{},2:{},3:{},4:{}},odds_info:{time_odds_times:{umaren:['最終','13:50']},
 time_pops:{umaren:[{'1-2':4.0},{'1-2':4.2}]}}}))}});

async function runtime(responses) {
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:bundle.outputFiles[0].text,
  compatibilityDate:'2026-09-30',bindings:{ARCHIVE_ENABLED:'true',BACKFILL_DAYS:'7'},
  d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),outboundService:async req=>{
   requests.push(req.url);const r=responses.shift();assert.ok(r,'unexpected external access');
   return new Response(typeof r.body==='string'?r.body:JSON.stringify(r.body??{}),
    {status:r.status??200,headers:{'content-type':'application/json',...r.headers}});}
 }));
 const db=await mf.getD1Database('INDEX');
 const schema=await readFile('migrations/0001_capture.sql','utf8')+await readFile('migrations/0004_archive.sql','utf8');
 for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean)) await db.prepare(s).run();
 const tick=async(at=start,delay=0)=>(await mf.dispatchFetch('http://test/tick?at='+at+'&delay='+delay)).json();
 return {mf,db,requests,tick,raw:await mf.getR2Bucket('RAW')};
}

test('closed-day pagination stores originals; cadence and event replay do not fetch twice',async()=>{
 const r=await runtime([{body:page()},{body:page('2026-09-29',[3])}]);
 try {
  assert.equal((await r.tick(start,20000)).status,'STORED');
  assert.equal((await r.tick()).status,'REPLAY');
  assert.equal((await r.tick(start+120000)).status,'WAIT_OR_STOPPED');
  assert.equal((await r.tick(start+300000)).status,'WAIT_OR_STOPPED');
  assert.equal((await r.tick(start+600000)).status,'STORED');
  assert.equal(r.requests.length,2);
  assert.ok(r.requests[0].includes('race_date=2026-09-29')&&r.requests[1].includes('page=2'));
  assert.ok(r.requests.every(url=>new URL(url).searchParams.get('time')==='00:00'));
  const attempts=(await r.db.prepare('SELECT * FROM archive_attempts ORDER BY scheduled_at').all()).results;
  assert.equal(attempts.length,2);
  assert.equal(attempts[0].fetch_started_at,new Date(start+20000).toISOString());
  for(const attempt of attempts){
   assert.equal(attempt.status,'STORED');assert.equal(attempt.source_updated_at,null);assert.equal(attempt.historical_available_at,null);
   assert.ok(attempt.available_at>=attempt.received_at);
   const raw=await (await r.raw.get('archive/raw/'+attempt.raw_sha256)).json();
   assert.equal(raw.race_date,'2026-09-29');
   const manifest=await (await r.raw.get('archive/manifests/'+attempt.event_id+'.json')).json();
   assert.equal(manifest.paper_eligible,false);
   assert.deepEqual(manifest.races[0].markets.umaren.map(x=>x.kind),['FINAL_ONLY','CLOCK_ONLY']);
  }
  assert.equal((await r.db.prepare('SELECT count(*) n FROM raw_observations').first()).n,0);
  await r.db.prepare("UPDATE archive_jobs SET status='DONE'").run();
  const migration=await readFile('migrations/0005_archive_day_window.sql','utf8');
  await r.db.prepare(migration.replace(/^--.*$/gm,'')).run();
  assert.equal((await r.db.prepare("SELECT count(*) n FROM archive_jobs WHERE status='DONE'").first()).n,0);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM archive_attempts').first()).n,2);
 }finally{await r.mf.dispose();}
});

test('refusal stops only archive source; 429 retains attempt and waits before recovery',async()=>{
 for(const first of [{status:403},{status:429,headers:{'retry-after':'600'}}]){
  const r=await runtime([first,{body:page()}]);
  try{
   assert.equal((await r.tick()).status,first.status===403?'SOURCE_DENIED':'RATE_LIMITED');
   assert.equal((await r.tick(start+300000)).status,'WAIT_OR_STOPPED');
   assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='nar-daily-odds'").first()).blocked,0);
   if(first.status===429) assert.equal((await r.tick(start+600000)).status,'STORED');
   else assert.equal((await r.tick(start+600000)).status,'WAIT_OR_STOPPED');
   assert.equal(r.requests.length,first.status===429?2:1);
  }finally{await r.mf.dispose();}
 }
});

test('unexpected day is preserved for inspection and never marked complete',async()=>{
 const r=await runtime([{body:page('2026-09-28')}]);
 try{
  assert.equal((await r.tick()).status,'PARSE_ERROR');
  const attempt=await r.db.prepare('SELECT * FROM archive_attempts').first();
  assert.equal(attempt.available_at,null);assert.ok(attempt.raw_sha256);
  assert.ok(await r.raw.get('archive/raw/'+attempt.raw_sha256));
  assert.equal((await r.tick(start+300000)).status,'WAIT_OR_STOPPED');
 }finally{await r.mf.dispose();}
});
