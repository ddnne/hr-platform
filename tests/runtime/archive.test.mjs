import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare, Log, LogLevel, convertV4MiniflareOptions} from 'miniflare';

const configured = JSON.parse(await readFile('wrangler.archive.jsonc','utf8')).vars;
const bundle = await build({stdin:{contents:`import {collectArchive,inspectPage} from './workers/archive/index.ts';
export default {async fetch(request,env){const u=new URL(request.url);
 if(u.pathname==='/inspect') return Response.json(inspectPage(await request.json(),{day:'2026-09-29',page:1},Number(env.ARCHIVE_DISPLAY_TIMES)));
 const now=Number(u.searchParams.get('at')),previous=Date.now;let clock=now;Date.now=()=>clock;
 let calls=0,puts=0,nestedStatus;const delay=Number(u.searchParams.get('delay')||0);
 const saveDelay=Number(u.searchParams.get('save_delay')||0);
 const bindings={...env,RAW:{head:env.RAW.head.bind(env.RAW),put:async(...args)=>{
  const result=await env.RAW.put(...args);if(++puts===1)clock+=saveDelay;return result;}},
 INDEX:{prepare:sql=>env.INDEX.prepare(sql.replaceAll("'now'", "'"+new Date(clock).toISOString()+"'")),batch:async statements=>{
  const result=await env.INDEX.batch(statements);if(++calls===1)clock+=delay;
  if(calls===2&&saveDelay)nestedStatus=(await collectArchive(Math.floor(clock/60000)*60000,bindings)).status;
  return result;}}};
 try{const result=await collectArchive(now,bindings,u.searchParams.get('day')??undefined);
 return Response.json({...result,nestedStatus});}finally{Date.now=previous;}}};`,
 resolveDir:process.cwd(),sourcefile:'archive-harness.ts'},bundle:true,write:false,format:'esm',
 platform:'browser',target:'es2022',external:['cloudflare:workers']});
const start = Date.parse('2026-09-30T00:00:00Z');
const page = (day='2026-09-29',numbers=[1,2],total=3) => ({race_kind:'nar',race_type:'NAR_TODAY',race_date:day,
 total_count:total,nar_info:{race_date:day,race_info:numbers.map(n=>({race_type:'NAR',
 race_info:{race_date:day,track:'合成競馬場',race:n,course:'ダート 1400ｍ（右）'},
 horse_info:{1:{},2:{},3:{},4:{}},odds_info:{time_odds_times:{umaren:['最終','13:50']},
 time_pops:{umaren:[{'1-2':4.0},{'1-2':4.2}]}}}))}});

async function runtime(responses, backfill='7', enabled='true') {
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:bundle.outputFiles[0].text,
  compatibilityDate:'2026-09-30',bindings:{...configured,BACKFILL_DAYS:backfill,ARCHIVE_ENABLED:enabled},
  d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),outboundService:async req=>{
   requests.push(req.url);const r=responses.shift();assert.ok(r,'unexpected external access');
   return new Response(typeof r.body==='string'?r.body:JSON.stringify(r.body??{}),
    {status:r.status??200,headers:{'content-type':'application/json',...r.headers}});}
 }));
 const db=await mf.getD1Database('INDEX');
 const schema=await readFile('migrations/0001_capture.sql','utf8')+await readFile('migrations/0004_archive.sql','utf8');
 for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean)) await db.prepare(s).run();
 const tick=async(at=start,delay=0,day,saveDelay=0)=>(await mf.dispatchFetch('http://test/tick?at='+at+'&delay='+delay+(day?'&day='+day:'')+'&save_delay='+saveDelay)).json();
 return {mf,db,requests,tick,raw:await mf.getR2Bucket('RAW')};
}

test('disabled source neither requests data nor recreates deleted archive jobs',async()=>{
 const r=await runtime([],'7',configured.ARCHIVE_ENABLED);
 try {
  assert.equal((await r.tick()).status,'DISABLED');
  assert.equal(r.requests.length,0);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM archive_jobs').first()).n,0);
  assert.equal((await r.db.prepare('SELECT count(*) n FROM archive_attempts').first()).n,0);
 } finally {await r.mf.dispose();}
});

test('closed-day pagination stores originals; cadence and event replay do not fetch twice',async()=>{
 const r=await runtime([{body:page()},{body:page('2026-09-29',[3])}]);
 try {
  assert.equal((await r.tick(start,20000)).status,'STORED');
  assert.equal((await r.tick()).status,'REPLAY');
  assert.equal((await r.tick(start+60000)).status,'WAIT_OR_STOPPED');
  assert.equal((await r.tick(start+120000)).status,'STORED');
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

test('180 closed days are seeded once; a short Retry-After survives lease release',async()=>{
 const r=await runtime([{status:429,headers:{'retry-after':'120'}},{body:page()}],'180');
 try{
  assert.equal((await r.tick()).status,'RATE_LIMITED');
  assert.equal((await r.tick(start+60000)).status,'WAIT_OR_STOPPED');
  assert.equal((await r.tick(start+120000)).status,'STORED');
  const jobs=await r.db.prepare('SELECT count(*) n,min(day) oldest,max(day) newest FROM archive_jobs WHERE page=1').first();
  assert.deepEqual(jobs,{n:180,oldest:'2026-04-03',newest:'2026-09-29'});
  assert.equal((await r.db.prepare("SELECT status FROM archive_jobs WHERE day='2026-09-29' AND page=1").first()).status,'DONE');
  assert.equal(r.requests.length,2);
 }finally{await r.mf.dispose();}
});

test('twenty-year range supports explicit closed-day inspection without a second fetch path',async()=>{
 const r=await runtime([{body:page('2006-09-30',[],0)}],'7305');
 try{
  assert.equal((await r.tick(start,0,'2006-09-30')).status,'STORED');
  const jobs=await r.db.prepare('SELECT count(*) n,min(day) oldest,max(day) newest FROM archive_jobs WHERE page=1').first();
  assert.deepEqual(jobs,{n:7305,oldest:'2006-09-30',newest:'2026-09-29'});
  assert.equal((await r.db.prepare("SELECT status FROM archive_jobs WHERE day='2026-09-29' AND page=1").first()).status,'PENDING');
  assert.equal((await r.tick(start+120000,0,'2006-09-29')).status,'IDLE');
  assert.equal(r.requests.length,1);
 }finally{await r.mf.dispose();}
});

test('an unfinished owner prevents another request after its timed lease expires',async()=>{
 const r=await runtime([]);
 try{
  await r.db.prepare("INSERT INTO archive_attempts(event_id,day,page,status,scheduled_at,reserved_at) VALUES('slow-save','2026-09-29',1,'PENDING',?,?)").bind(new Date(start).toISOString(),new Date(start).toISOString()).run();
  await r.db.prepare("UPDATE source_control SET owner_event_id='slow-save',next_allowed_at=? WHERE source='keibaodds-history'").bind(start+180000).run();
  assert.equal((await r.tick(start+240000)).status,'WAIT_OR_STOPPED');
  assert.equal(r.requests.length,0);
 }finally{await r.mf.dispose();}
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


test('terminal publication atomically waits after a save that outlives the lease',async()=>{
 const r=await runtime([{body:page()}]);
 try{
  const result=await r.tick(start,0,undefined,185000);
  assert.equal(result.status,'STORED');
  assert.equal(result.nestedStatus,'WAIT_OR_STOPPED');
  assert.equal(r.requests.length,1);
  const control=await r.db.prepare("SELECT next_allowed_at FROM source_control WHERE source='keibaodds-history'").first();
  assert.ok(control.next_allowed_at>=start+245000-1);
 }finally{await r.mf.dispose();}
});


test('an empty-date null array is retained without claiming historical coverage',async()=>{
 const empty={...page('2006-09-30',[],0),race_type:null};
 Object.assign(empty.nar_info,{race_info:null,races:[],date_info:[],track_info:[]});
 for(const total of [0,1]){
  const r=await runtime([{body:{...empty,total_count:total}}],'7305');
  try{
   const result=await r.tick(start,0,'2006-09-30');
   assert.equal(result.status,total===0?'STORED':'PARSE_ERROR');
   const attempt=await r.db.prepare('SELECT * FROM archive_attempts').first();
   assert.ok(await r.raw.get('archive/raw/'+attempt.raw_sha256));
   assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='keibaodds-history'").first()).blocked,total===0?0:1);
   if(total===0){
    const manifest=await (await r.raw.get('archive/manifests/'+attempt.event_id+'.json')).json();
    assert.equal(manifest.coverage_status,'NO_RACES_RETURNED');
    assert.equal(manifest.historical_available_at,null);assert.equal(manifest.races.length,0);
   }
  }finally{await r.mf.dispose();}
 }
});


test('variable page sizes are followed until an empty page without assuming two races per page',async()=>{
 const r=await runtime([{body:page('2026-09-29',[1])},{body:page('2026-09-29',[2])},
  {body:page('2026-09-29',[3])},{body:page('2026-09-29',[])}]);
 try{
  for(let i=0;i<4;i++)assert.equal((await r.tick(start+i*120000)).status,'STORED');
  const jobs=(await r.db.prepare("SELECT page,status FROM archive_jobs WHERE day='2026-09-29' ORDER BY page").all()).results;
  assert.deepEqual(jobs,[1,2,3,4].map(page=>({page,status:'DONE'})));
  assert.equal(r.requests.length,4);
 }finally{await r.mf.dispose();}
});

test('an empty tail with navigation ends the day and the next date continues',async()=>{
 const empty={race_kind:'nar',race_type:null,race_date:'2026-09-29',total_count:0,
  nar_info:{race_date:'2026-09-29',race_info:null,races:[],date_info:['2026-09-29'],track_info:['合成競馬場']}};
 const r=await runtime([{body:page('2026-09-29',[1],1)},{body:empty},{body:page('2026-09-28',[1],1)}]);
 try{
  assert.equal((await r.tick()).status,'STORED');
  assert.equal((await r.tick(start+120000)).status,'STORED');
  const attempt=await r.db.prepare("SELECT * FROM archive_attempts WHERE day='2026-09-29' AND page=2").first();
  const manifest=await (await r.raw.get('archive/manifests/'+attempt.event_id+'.json')).json();
  assert.equal(manifest.coverage_status,'NO_RACES_RETURNED');assert.equal(manifest.has_more,false);
  assert.equal(manifest.pagination_completeness,'UNVERIFIED');
  assert.equal((await r.db.prepare("SELECT count(*) n FROM archive_jobs WHERE day='2026-09-29' AND page=3").first()).n,0);
  assert.equal((await r.tick(start+240000)).status,'STORED');
  assert.equal(new URL(r.requests[2]).searchParams.get('race_date'),'2026-09-28');
 }finally{await r.mf.dispose();}
});

test('a known metadata-only archive preserves missing odds and queues the next page',async()=>{
 const data=page('2026-09-29',[1],3);
 data.nar_info.race_info[0].odds_info={sikis:null,siki_odds_times:null};
 const r=await runtime([{body:data}]);
 try{
  assert.equal((await r.tick()).status,'STORED');
  const attempt=await r.db.prepare('SELECT * FROM archive_attempts').first();
  const manifest=await (await r.raw.get('archive/manifests/'+attempt.event_id+'.json')).json();
  assert.equal(manifest.races[0].odds_status,'ODDS_NOT_RETURNED');
  assert.deepEqual(manifest.races[0].markets,{});
  assert.equal(manifest.historical_available_at,null);assert.equal(manifest.paper_eligible,false);
  assert.equal((await r.db.prepare("SELECT status FROM archive_jobs WHERE day='2026-09-29' AND page=2").first()).status,'PENDING');
  assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='keibaodds-history'").first()).blocked,0);
 }finally{await r.mf.dispose();}
});
