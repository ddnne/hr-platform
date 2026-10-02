import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
const pure=await build({entryPoints:['workers/sports/parsers.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const parser=await import(`data:text/javascript;base64,${Buffer.from(pure.outputFiles[0].text).toString('base64')}`);
import {ids,autoBody,autoProgram,keirinIdentity,keirinRunners,keirinProgram} from '../fixtures/synthetic/sports.mjs';
import {autoResult,keirinResult} from '../fixtures/synthetic/sports-results.mjs';
const target={sport:'auto',race_id:'auto:20000101:6:8',url:'https://autorace.jp/race_info/Odds',kind:'odds',body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:8})};
test('synthetic full seven markets, ranges and explicit zero; unknown source timestamp stays null',()=>{
 const s=parser.parseAuto(autoBody('0.0'),target);
 assert.deepEqual(s.markets.map(m=>m.quotes.length),[30,15,120,20,15,6,6]);assert.ok(s.markets.every(m=>m.complete));
 assert.equal(s.source_updated_at,null);assert.equal(s.source_published_at,null);assert.equal(s.markets[0].quotes[0].status,'ZERO_DISPLAY');
 assert.equal(parser.parseAuto(autoBody('4.0',1),target).phase,'FINAL_ONLY');
 const missing=JSON.parse(autoBody());delete missing.body.rt3OddsList[1][2][3];
 assert.equal(parser.parseAuto(JSON.stringify(missing),target).markets.find(m=>m.market==='trifecta').complete,false);
});
test('keirin full frame support includes same frame only where two entrants exist; wide uses min and max',()=>{
 const entrants=[1,2,3,4,5,6,7,8,9],frames={1:1,2:2,3:3,4:4,5:4,6:5,7:5,8:6,9:6};
 const data={karaGamenFlg:'0',endFlg:'0',ozzWideData:{UP_DATE:'200001011001'}};
 for(const c of parser.combinations(entrants,2)){data.ozzWideData['DN_OZZ'+c.join('')]='1.0';data.ozzWideData['TP_OZZ'+c.join('')]='2.0';}
 const wide=parser.parseKeirin(JSON.stringify({resultCd:0,data}),{...target,sport:'keirin',market:'wide',entrants,frames});
 assert.equal(wide.markets[0].quotes.length,36);assert.equal(wide.markets[0].complete,true);assert.equal(wide.markets[0].quotes[0].upper,2);
 const support=new Set(parser.combinations(entrants,2,true).map(c=>c.map(id=>frames[id]).join('')));
 const odds=Object.fromEntries([...support].map(c=>['OZZ'+c,'3.0']));
 const r=parser.parseKeirin(JSON.stringify({resultCd:0,data:{karaGamenFlg:'0',endFlg:'1',ozz2WakutanData:odds}}),{...target,sport:'keirin',market:'frame_exacta',entrants,frames});
 assert.equal(r.phase,'CLOSE_ONLY');assert.equal(r.markets[0].complete,true);assert.equal(r.markets[0].expected,33);
});
test('boat rowspan table maps every trifecta to the correct runner identities',()=>{
 let table='<thead><tr>'+ids.map(i=>`<th>${i}</th><th colspan="2">synthetic</th>`).join('')+'</tr></thead><tbody>';
 for(let row=0;row<20;row++){table+='<tr>';for(const first of ids){const seconds=ids.filter(x=>x!==first),second=seconds[Math.floor(row/4)],third=ids.filter(x=>x!==first&&x!==second)[row%4];
 if(row%4===0)table+=`<td rowspan="4">${second}</td>`;table+=`<td>${third}</td><td class="oddsPoint">${first*100+second*10+third}.0</td>`;}table+='</tr>';}table+='</tbody>';
 const r=parser.parseBoat('<p class="tab4_time">締切時オッズ</p><span>3連単オッズ</span><table>'+table+'</table>',{sport:'boat',race_id:'boat:20000101:1:1',url:'odds3t',market:'trifecta'});
 assert.equal(r.phase,'CLOSE_ONLY');assert.equal(r.markets[0].complete,true);assert.equal(r.markets[0].quotes.length,120);
 for(const q of r.markets[0].quotes)assert.equal(q.lower,q.combination[0]*100+q.combination[1]*10+q.combination[2]);
});
const bundle=await build({stdin:{contents:`import {collect,validateTarget} from './workers/sports/capture';import {history,normalize,savedProgram} from './workers/sports/storage';import {discoveryTargets} from './workers/sports/discovery-plan';import {capture} from './workers/ingestion/capture';
export default {async fetch(req,env){const v=await req.json();
 if(v.op==='nar'){await capture(v.at,env);return new Response('ok');}
 if(v.op==='scopedHistory')return Response.json(await history(env,v.sport,v.race_id,v.cutoff,100,'',v.kind));
 if(v.op==='history')return Response.json(await history(env,'auto','auto:20000101:6:8',v.cutoff,100,'',v.kind??'odds'));
 if(v.op==='reparse'){const store=v.fault==='context'?{...env,RAW:{get:async(k)=>{if(k==='manifests/'+v.context+'.json')throw new Error('R2_TEMPORARY');return env.RAW.get(k);},put:env.RAW.put.bind(env.RAW)}}:env;
 try{return new Response(await normalize(store,v.event,v.target,v.version));}catch(e){return new Response(e.message,{status:503});}}
 if(v.op==='programPlan'){try{const s=await savedProgram(env,v.event,v.now??Date.now());return Response.json({...s,plan:discoveryTargets(s.value,v.event)});}catch(e){return Response.json({error:e.message});}}
 const bindings=v.fault==='publish'?{...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),batch:async()=>{throw new Error('INDEX_FAILED');}}}:v.fault==='normalized'?{...env,RAW:{head:env.RAW.head.bind(env.RAW),get:env.RAW.get.bind(env.RAW),put:async(k,b)=>{if(k.startsWith('sports/normalized/'))throw new Error('R2_FAILED');return env.RAW.put(k,b);}}}:env;
 return Response.json(await collect(v.at,bindings,v.target));}};`,resolveDir:process.cwd(),sourcefile:'sports-harness.ts'},external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'});
const schema=(await Promise.all(['0001_capture','0002_processing_metrics','0010_sports_history'].map(n=>readFile('migrations/'+n+'.sql','utf8')))).join('\n');
const settings=JSON.parse(await readFile('configs/sports-collection.json','utf8'));
const nextProgramVersion=settings.program_parser_version.replace(/\d+$/,v=>String(Number(v)+1));
async function runtime(responses=[],enabled=true,doClass=null){
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({name:'sports',modules:true,script:(doClass?doBundle:bundle).outputFiles[0].text,compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
 ...(doClass?{durableObjects:{SPORTS:{className:doClass,useSQLite:true}}}:{}),
 bindings:{SPORTS_ENABLED:String(enabled),SPORTS_PROVIDERS_JSON:'["auto","boat","keirin"]',COLLECTION_ENABLED:'true',SOURCE_APPROVED:'true'},d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),
 outboundService:async req=>{requests.push({url:req.url,method:req.method,body:await req.text(),cookie:req.headers.get('Cookie')});const matching=responses.findIndex(r=>r.route&&req.url.includes(r.route));const r=responses.splice(matching<0?0:matching,1)[0];if(r instanceof Error)throw r;if(!r)throw new Error('UNEXPECTED_FETCH');return new Response(r.body??autoBody(),{status:r.status??200,headers:r.headers});}}));
 const db=await mf.getD1Database('INDEX');for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=async v=>{const r=await mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)});assert.equal(r.status,200);return r;};
 const tick=async(at=Date.now(),t=target)=>(await call({at,target:t})).json();
 const reset=()=>db.prepare("UPDATE source_control SET next_allowed_at=0 WHERE source LIKE 'sports-%'").run();
 return {mf,db,requests,tick,reset,call};
}
test('append same body at a later time, redelivery is silent, changed recovery and as-of stay immutable',async()=>{
 const r=await runtime([{},{},{status:500},{body:autoBody('5.0')}]);try{
 const one=await r.tick();await r.reset();const two=await r.tick();await r.reset();const cut=new Date().toISOString();
 await r.tick(Date.now(),{...target,kind:'result',url:'https://autorace.jp/race_info/RaceResult'});await r.reset();
 await r.tick();await r.tick(Number(one.event_id.split(':')[2]));
 assert.equal(r.requests.length,4);const rows=(await r.db.prepare('SELECT * FROM raw_observations').all()).results;
 assert.equal(rows.length,3);assert.equal(rows[0].raw_sha256,rows[1].raw_sha256);assert.notEqual(rows[1].raw_sha256,rows[2].raw_sha256);
 const before=await (await r.call({op:'history',cutoff:cut})).json();assert.equal(before.length,2);
 await r.call({op:'reparse',event:one.event_id,target,version:'sports-odds-v2'});
 assert.deepEqual(await (await r.call({op:'history',cutoff:cut})).json(),before);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM captures WHERE status='FAILED'").first()).n,1);
 }finally{await r.mf.dispose();}
});
for(const status of [403,419,429])test('provider '+status+' is isolated from NAR and stops or waits',async()=>{
 const r=await runtime([{status,headers:{'retry-after':'600'}}]);try{const result=await r.tick();const source=await r.db.prepare("SELECT * FROM source_control WHERE source='sports-auto'").first();
 assert.equal(result.status,status===429?'RATE_LIMITED':'SOURCE_DENIED');assert.equal(source.blocked,status===429?0:1);assert.ok(source.next_allowed_at>Date.now());
 await r.tick();assert.equal(r.requests.length,1);assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='nar-daily-odds'").first()).blocked,0);
 }finally{await r.mf.dispose();}
});
test('program versions share persistence but never enter odds history; later programs/reparse cannot change a past cutoff',async()=>{
 const program=JSON.parse(autoProgram);program.body.raceNo=8;
 const changed=structuredClone(program);changed.body.telvoteTime='24:01';
 const r=await runtime([{},{body:JSON.stringify(program)},{body:JSON.stringify(changed)}]);try{
 await r.tick();await r.reset();const t={...target,kind:'schedule',url:'https://autorace.jp/race_info/OtherRaceInfo'};
 const one=await r.tick(Date.now(),t);await r.reset();const cutoff=new Date().toISOString();
 const past=await (await r.call({op:'history',kind:'program',cutoff})).json();assert.equal(past.length,1);assert.equal(past[0].status,'PROGRAM_PARSED');
 assert.equal((await (await r.call({op:'history',cutoff})).json()).length,1);
 await new Promise(resolve=>setTimeout(resolve,2));await r.tick(Date.now(),t);await r.call({op:'reparse',event:one.event_id,target:t,version:nextProgramVersion});
 assert.deepEqual(await (await r.call({op:'history',kind:'program',cutoff})).json(),past);
 assert.equal((await (await r.call({op:'history',cutoff:new Date().toISOString()})).json()).length,1);
 assert.equal((await (await r.call({op:'history',kind:'program',cutoff:new Date().toISOString()})).json()).length,3);
 }finally{await r.mf.dispose();}
});
test('saved program plans use availability and receipt freshness; reparsing does not refresh old input',async()=>{
 const r=await runtime([{body:keirinIdentity}]);try{
 const before=Date.now()-1,t={sport:'keirin',race_id:'keirin:20000101:47:0',kind:'schedule',discovery_stage:'race',url:'https://keirin.jp/pc/json?type=JST015&encp=synthetic-public-navigation'};
 const one=await r.tick(Date.now(),t);
 assert.equal((await (await r.call({op:'programPlan',event:one.event_id,now:before})).json()).error,'PROGRAM_UNAVAILABLE');
 const saved=await (await r.call({op:'programPlan',event:one.event_id})).json();assert.equal(saved.value.program.selected.race_id,'keirin:20000101:47:2');assert.equal(saved.plan.targets[0].race_id,'keirin:20000101:47:2');
 await r.call({op:'reparse',event:one.event_id,target:t,version:nextProgramVersion});
 const staleAt=Date.parse(saved.received_at)+settings.discovery.maximum_program_age_seconds*1000+1;
 assert.equal((await (await r.call({op:'programPlan',event:one.event_id,now:staleAt})).json()).error,'PROGRAM_STALE');
 assert.equal(r.requests.length,1);assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('a newer failed program revision rejects preview instead of reviving an old success; past cutoffs stay usable',async()=>{
 const r=await runtime([{body:keirinIdentity}]);try{
 const t={sport:'keirin',race_id:'keirin:20000101:47:0',kind:'schedule',discovery_stage:'race',url:'https://keirin.jp/pc/json?type=JST015&encp=synthetic-public-navigation'};
 const one=await r.tick(Date.now(),t),past=await (await r.call({op:'programPlan',event:one.event_id})).json();
 const earlier=Date.parse(past.available_at),later=earlier+1,available=new Date(later).toISOString().replace('Z','000+00:00');
 // Fabricated later parser outcomes for the same immutable observation, including a same-time numeric version tie.
 for(const [version,status,key] of [['sports-program-v9','PROGRAM_PARSED','normalized_key'],['sports-program-v10','PARSE_ERROR','NULL']])
  await r.db.prepare(`INSERT INTO sports_parses SELECT observation_id,?,sport,race_id,resource_id,?,?,?,${key},NULL FROM sports_parses WHERE observation_id=? AND parser_version=?`)
   .bind(version,available,available,status,one.event_id,settings.program_parser_version).run();
 assert.deepEqual(await (await r.call({op:'programPlan',event:one.event_id,now:earlier})).json(),past);
 assert.equal((await (await r.call({op:'programPlan',event:one.event_id,now:later})).json()).error,'PROGRAM_UNAVAILABLE');
 assert.equal(r.requests.length,1);assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('result pending/published history is separated from odds and remains immutable across later observations and reparse',async()=>{
 const r=await runtime([{},{body:autoResult(true)},{body:autoResult()},{body:autoResult()}]);try{
 await r.tick();await r.reset();const t={...target,kind:'result',url:'https://autorace.jp/race_info/RaceResult'};
 const pending=await r.tick(Date.now(),t);await r.reset();const cutoff=new Date().toISOString();
 const past=await (await r.call({op:'history',kind:'result',cutoff})).json();assert.deepEqual(past.map(v=>v.status),['RESULT_PENDING']);
 await new Promise(resolve=>setTimeout(resolve,2));const one=await r.tick(Date.now(),t);await r.reset();await r.tick(Date.now(),t);
 await r.call({op:'reparse',event:pending.event_id,target,version:'sports-result-v2'});
 assert.deepEqual(await (await r.call({op:'history',kind:'result',cutoff})).json(),past);
 const now=new Date().toISOString();assert.equal((await (await r.call({op:'history',cutoff:now})).json()).length,1);
 assert.equal((await (await r.call({op:'history',kind:'program',cutoff:now})).json()).length,0);
 const rows=(await (await r.call({op:'history',kind:'result',cutoff:now})).json()).filter(v=>v.status==='RESULT_PARSED');assert.equal(rows.length,2);
 assert.equal(rows[0].raw_sha256,rows[1].raw_sha256);await r.tick(Number(one.event_id.split(':')[2]),t);assert.equal(r.requests.length,4);
 }finally{await r.mf.dispose();}
});
test('keirin result collection verifies the same public navigation context before HTTP and again during normalization',async()=>{
 const r=await runtime([{body:keirinIdentity},{body:keirinResult()}]);try{
 const ctx={sport:'keirin',race_id:'keirin:20000101:47:2',kind:'schedule',url:'https://keirin.jp/pc/json?type=JST015&encp=synthetic-public-navigation'};
 const evidence=await r.tick(Date.now(),ctx);await r.reset();const t={...ctx,kind:'result',url:ctx.url.replace('JST015','JSJ012'),context_event:evidence.event_id};
 assert.equal((await r.tick(Date.now(),{...t,race_id:'keirin:20000101:47:3'})).status,'INVALID_CONTEXT');assert.equal(r.requests.length,1);
 const result=await r.tick(Date.now(),t),row=await r.db.prepare("SELECT * FROM sports_parses WHERE observation_id=? AND status='RESULT_PARSED'").bind(result.event_id).first();
 assert.ok(row);const bucket=await r.mf.getR2Bucket('RAW'),normalized=await (await bucket.get(row.normalized_key)).json();
 assert.equal(normalized.identity_status,'CONTEXT_VERIFIED');assert.equal(normalized.identity_evidence,evidence.event_id);assert.equal(normalized.settlement_qualified,false);assert.equal(r.requests.length,2);
 // A temporary context read failure must not freeze an invalid parse version.
 const failed=await r.mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify({op:'reparse',event:result.event_id,target:t,version:'sports-result-v2',fault:'context',context:evidence.event_id})});
 assert.equal(failed.status,503);assert.equal(await failed.text(),'R2_TEMPORARY');
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE parser_version='sports-result-v2'").first()).n,0);
 assert.equal(await (await r.call({op:'reparse',event:result.event_id,target:t,version:'sports-result-v2'})).text(),'RESULT_PARSED');assert.equal(r.requests.length,2);
 // An older raw capture without identity provenance must fail even if the caller supplies a valid target now.
 const legacy='sports:keirin:1:'+'0'.repeat(64),raw=await r.db.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=?').bind(result.event_id).first();
 await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,'2000-01-01T00:00:00.000000+00:00','RAW_STORED')").bind(legacy).run();
 await r.db.prepare("INSERT INTO raw_observations VALUES(?,?,'2000-01-01T00:00:00.000000+00:00','2000-01-01T00:00:00.000000+00:00','body_200','SPORT_KEIRIN_RESULT',NULL,NULL)").bind(legacy,raw.raw_sha256).run();
 await bucket.put('manifests/'+legacy+'.json',JSON.stringify({target:{...t,context_event:undefined}}));
 assert.equal(await (await r.call({op:'reparse',event:legacy,target:t,version:'sports-result-v1'})).text(),'PARSE_ERROR');assert.equal(r.requests.length,2);
 }finally{await r.mf.dispose();}
});
test('keirin runners reuse identity/storage/history and recover a temporary context read without another HTTP',async()=>{
 const r=await runtime([{body:keirinIdentity},{body:keirinRunners},{body:keirinProgram}]);try{
 const ctx={sport:'keirin',race_id:'keirin:20000101:47:2',kind:'schedule',url:'https://keirin.jp/pc/json?type=JST015&encp=synthetic-public-navigation'};
 const parent=await r.tick(Date.now(),ctx);await r.reset();const t={...ctx,url:ctx.url.replace('JST015','JST010')+'&url.media.flg=1',context_event:parent.event_id};
 assert.equal((await r.tick(Date.now(),{...t,race_id:'keirin:20000101:47:3'})).status,'INVALID_CONTEXT');assert.equal(r.requests.length,1);
 const clock={...t,kind:'guest',url:'https://keirin.jp/pc/racelive',form:true,body:'encp=synthetic-public-navigation',discovery_stage:'race'};
 assert.equal((await r.tick(Date.now(),{...clock,race_id:'keirin:20000101:47:3'})).status,'INVALID_CONTEXT');assert.equal(r.requests.length,1);
 const cutoff=new Date(Date.now()-1).toISOString(),at=Date.now(),capture=await r.tick(at,t);
 const q={op:'scopedHistory',kind:'program',sport:t.sport,race_id:t.race_id},before=await (await r.call({...q,cutoff})).json();assert.deepEqual(before.map(v=>v.observation_id),[parent.event_id]);
 const row=await r.db.prepare('SELECT * FROM sports_parses WHERE observation_id=?').bind(capture.event_id).first();assert.equal(row.status,'PROGRAM_PARSED');
 const bucket=await r.mf.getR2Bucket('RAW'),value=await (await bucket.get(row.normalized_key)).json();assert.equal(value.program.runners.declared_count,6);assert.equal(value.program.identity_evidence,parent.event_id);assert.equal(value.program.runners.entries[2].cancellation_label,'synthetic withdrawal');
 await r.reset();assert.equal((await r.tick(Date.now(),clock)).status,'RAW_STORED');
 assert.equal((await r.tick(at,t)).status,'RAW_STORED');assert.equal(r.requests.length,3);
 const failed=await r.mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify({op:'reparse',event:capture.event_id,target:t,version:nextProgramVersion,fault:'context',context:parent.event_id})});assert.equal(failed.status,503);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM sports_parses WHERE observation_id=? AND parser_version=?').bind(capture.event_id,nextProgramVersion).first()).n,0);
 assert.equal(await (await r.call({op:'reparse',event:capture.event_id,target:t,version:nextProgramVersion})).text(),'PROGRAM_PARSED');assert.equal(r.requests.length,3);
 assert.deepEqual(await (await r.call({...q,cutoff})).json(),before);
 }finally{await r.mf.dispose();}
});
test('result normalized-storage failure recovers from the original raw without a second HTTP or observation',async()=>{
 const r=await runtime([{body:autoResult()}]);try{
 const at=Date.now(),t={...target,kind:'result',url:'https://autorace.jp/race_info/RaceResult'};
 assert.equal((await (await r.call({at,target:t,fault:'normalized'})).json()).status,'STORAGE_ERROR');
 assert.equal((await r.tick(at,t)).status,'RAW_STORED');assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='RESULT_PARSED'").first()).n,1);
 }finally{await r.mf.dispose();}
});
test('NAR and extra sports can publish concurrently in shared D1/R2; NAR history query excludes all sports rows',async()=>{
 const zip=new Uint8Array([0x50,0x4b,3,4,1]);const r=await runtime([{body:zip,route:'keiba.go.jp'},{body:autoBody(),route:'autorace.jp'}]);try{
 // Representative many-sport index population; NAR must still select only its own datasets.
 await r.db.prepare("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<5000) INSERT INTO captures(event_id,scheduled_capture_at,status) SELECT 'synthetic-sport-'||x,'2000-01-01T00:00:00.000000+00:00','RAW_STORED' FROM n").run();
 await r.db.prepare("INSERT INTO raw_observations SELECT event_id,'synthetic-hash','2000-01-01T00:00:00.000000+00:00','2000-01-01T00:00:00.000000+00:00','body_200','SPORT_AUTO_ODDS',NULL,NULL FROM captures").run();
 // Dispatch both while the first body is processed. Outbound routing follows resource identity.
 const at=Date.now();await Promise.all([r.call({op:'nar',at}),r.tick(at)]);
 const nar=(await r.db.prepare("SELECT o.* FROM raw_observations o WHERE o.dataset_kind IN ('DAILY_SNAPSHOT','NAR_RACE_BUNDLE','NAR_PAGE_STATE','NAR_PAGE_PAYOUT')").all()).results;
 assert.equal(nar.length,1);assert.equal(nar[0].observation_id,'nar-daily-odds:'+at);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='COMPLETE'").first()).n,1);
 assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='nar-daily-odds'").first()).blocked,0);
 const plan=await r.db.prepare("EXPLAIN QUERY PLAN SELECT * FROM raw_observations WHERE dataset_kind='DAILY_SNAPSHOT' AND received_at>'2000'").all();
 assert.ok(plan.results.some(x=>x.detail.includes('observation_kind_history')));
 }finally{await r.mf.dispose();}
});
test('disabled sports have no network; expired observations are gaps',async()=>{
 const r=await runtime([],false);try{assert.equal((await r.tick()).status,'DISABLED');assert.equal(r.requests.length,0);}finally{await r.mf.dispose();}
 const active=await runtime([]);try{assert.equal((await active.tick(Date.now()-100000)).status,'MISSED_WINDOW');assert.equal(active.requests.length,0);}finally{await active.mf.dispose();}
});
test('same-time races have independent event identities; a rate-limit challenge blocks the source',async()=>{
 const r=await runtime([{},{},{status:429,body:'<title>CAPTCHA challenge</title>',headers:{'retry-after':'900'}}]);try{
 const at=Date.now(),one=await r.tick(at);await r.reset();const other={...target,race_id:'auto:20000101:6:9',body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:9})};
 const two=await r.tick(at,other);assert.notEqual(one.event_id,two.event_id);await r.reset();
 const refusal=await r.tick();assert.equal(refusal.status,'CHALLENGE');assert.equal((await r.db.prepare("SELECT blocked FROM source_control WHERE source='sports-auto'").first()).blocked,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,2);
 }finally{await r.mf.dispose();}
});
for(const fault of ['publish','normalized'])test('partial '+fault+' storage recovers from immutable raw without new HTTP',async()=>{
 const r=await runtime([{}]);try{
 const at=Date.now(),bad=await (await r.call({at,target,fault})).json();assert.equal(bad.status,'STORAGE_ERROR');
 const recovered=await r.tick(at);assert.equal(recovered.status,'RAW_STORED');assert.equal(r.requests.length,1);
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 assert.equal((await r.db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='COMPLETE'").first()).n,1);
 }finally{await r.mf.dispose();}
});
const doBundle=await build({stdin:{contents:`import {SportsCollector} from './workers/sports/index';
export class TestSports extends SportsCollector {
 async runForTest(now){const clock=Date.now;Date.now=()=>now;try{await this.ctx.storage.put('guest-session',{cookie:'SYNTHETIC_PUBLIC_SESSION',token:'SYNTHETIC_TOKEN',expires:now+3600000});await this.alarm();}finally{Date.now=clock;}}
}
export class TestGuest extends SportsCollector {
 constructor(ctx,env){let failed=false;super(ctx,{...env,INDEX:{prepare:env.INDEX.prepare.bind(env.INDEX),batch:async(...args)=>{if(!failed){failed=true;throw new Error('GUEST_PUBLICATION_FAILED');}return env.INDEX.batch(...args);}}});}
 async runForTest(now){const clock=Date.now;Date.now=()=>now;try{await this.alarm();}finally{Date.now=clock;}}
}
export default {async fetch(req,env){const v=await req.json(),stub=env.SPORTS.get(env.SPORTS.idFromName('auto'));if(v.op==='schedule'){try{return new Response(await stub.schedule(v.entries));}catch(e){return new Response(e.message);}}if(v.op==='alarm'){await stub.runForTest(v.now);return new Response('ok');}return Response.json(v.op==='state'?await stub.planState():await stub.pending());}};`,resolveDir:process.cwd(),sourcefile:'do-sports-harness.ts'},external:['cloudflare:workers'],bundle:true,write:false,format:'esm',platform:'browser',target:'es2022'});
test('auto POST resources without a body are rejected before queue or provider access',async()=>{
 const r=await runtime([],true,'TestSports');try{const at=Date.now()+60000,{body,...missing}=target;
 for(const [path,kind] of [['Odds','odds'],['OtherRaceInfo','schedule']]){
  const t={...missing,url:'https://autorace.jp/race_info/'+path,kind};
  assert.equal(await (await r.call({op:'schedule',entries:[{at,target:t}]})).text(),'READ_POST_REQUIRED');
 }
 assert.equal(await (await r.call({op:'pending'})).json(),0);assert.equal(r.requests.length,0);
 assert.deepEqual(await (await r.call({op:'state'})).json(),{pending:0,alarm_at:null});
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM captures').first()).n,0);
 assert.equal(await (await r.call({op:'schedule',entries:[{at,target}]})).text(),'REGISTERED');
 assert.equal(await (await r.call({op:'pending'})).json(),1);
 assert.deepEqual(await (await r.call({op:'state'})).json(),{pending:1,alarm_at:at});
 }finally{await r.mf.dispose();}
});
test('a batch with conflicting recipes is atomic; identical duplicates keep one reservation',async()=>{
 const r=await runtime([],true,'TestSports');try{const at=Date.now()+60000;
 const event='synthetic-keirin-context',hash='synthetic-keirin-context-hash',url='https://keirin.jp/pc/json?type=JST015&encp=synthetic-navigation';
 await r.db.prepare("INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,'2000-01-01T00:00:00.000000+00:00','RAW_STORED')").bind(event).run();
 await r.db.prepare("INSERT INTO raw_observations VALUES(?,?,'2000-01-01T00:00:00.000000+00:00','2000-01-01T00:00:00.000000+00:00','body_200','SPORT_KEIRIN_SCHEDULE',NULL,NULL)").bind(event,hash).run();
 const bucket=await r.mf.getR2Bucket('RAW');await bucket.put('manifests/'+event+'.json',JSON.stringify({url}));
 await bucket.put('raw/'+hash,JSON.stringify({resultCd:0,data:{kaisaiDate:'20000101',keirinJyoCd:'47',raceNo:'2'}}));
 const t={sport:'keirin',race_id:'keirin:20000101:47:2',kind:'odds',market:'trifecta',context_event:event,entrants:[1,2,3],url:'https://keirin.jp/pc/json?type=JST011&encp=synthetic-navigation&kake=6&mode=0'};
 await r.call({op:'schedule',entries:[{at:at+120000,target}]});
 for(const changed of [{entrants:[1,2,3,4]},{frames:{1:1,2:2,3:3}},{market:'exacta'}]){
  const batch=[{at:at+240000,target},{at,target:t},{at,target:{...t,...changed}}];
  assert.equal(await (await r.call({op:'schedule',entries:batch})).text(),'PLAN_CONFLICT');
  assert.equal(await (await r.call({op:'pending'})).json(),1);
  assert.deepEqual(await (await r.call({op:'state'})).json(),{pending:1,alarm_at:at+120000});
 }
 assert.equal(await (await r.call({op:'schedule',entries:[{at,target:t},{at,target:t}]})).text(),'REGISTERED');
 assert.equal(await (await r.call({op:'pending'})).json(),2);assert.equal(r.requests.length,0);
 assert.deepEqual(await (await r.call({op:'state'})).json(),{pending:2,alarm_at:at});
 assert.equal((await r.db.prepare('SELECT count(*) AS n FROM captures').first()).n,1);
 }finally{await r.mf.dispose();}
});
test('DO concurrent reservations survive an in-flight capture and are drained separately',async()=>{
 let fetching,release;const fetchingPromise=new Promise(r=>fetching=r),hold=new Promise(r=>release=r);
 const mf=new Miniflare(convertV4MiniflareOptions({name:'sports-do',modules:true,script:doBundle.outputFiles[0].text,compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
 bindings:{SPORTS_ENABLED:'true',SPORTS_PROVIDERS_JSON:'["auto"]'},durableObjects:{SPORTS:{className:'TestSports',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),
 outboundService:async()=>{fetching();await hold;return new Response(autoBody());}}));
 try{const db=await mf.getD1Database('INDEX');for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=v=>mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)}),at=Date.now()+60000;
 await call({op:'schedule',entries:[{at,target}]});const active=call({op:'alarm',now:at});await fetchingPromise;
 await Promise.all([call({op:'schedule',entries:[{at:at+120000,target}]}),call({op:'schedule',entries:[{at:at+240000,target}]})]);
 assert.equal(await (await call({op:'pending'})).json(),3);release();await active;
 assert.equal(await (await call({op:'pending'})).json(),2);
 assert.equal((await db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,1);
 }finally{release();await mf.dispose();}
});
for(const kind of ['odds','schedule'])for(const delay of [6000,100000])test('auto '+kind+' guest publication failure recovers at delay '+delay+' without duplicate guest HTTP',async()=>{
 const requests=[];
 const mf=new Miniflare(convertV4MiniflareOptions({name:'guest-do',modules:true,script:doBundle.outputFiles[0].text,compatibilityDate:'2026-09-28',compatibilityFlags:['nodejs_compat'],
 bindings:{SPORTS_ENABLED:'true',SPORTS_PROVIDERS_JSON:'["auto"]'},durableObjects:{SPORTS:{className:'TestGuest',useSQLite:true}},d1Databases:['INDEX'],r2Buckets:['RAW'],log:new Log(LogLevel.NONE),
 outboundService:async req=>{requests.push({method:req.method,url:req.url});const p=JSON.parse(autoProgram);p.body.raceNo=8;return req.method==='GET'?new Response('<meta name="csrf-token" content="SYNTHETIC_TOKEN">',{headers:{'set-cookie':'guest=SYNTHETIC; Path=/'}}):new Response(kind==='odds'?autoBody():JSON.stringify(p));}}));
 try{const db=await mf.getD1Database('INDEX');for(const s of schema.replace(/^--.*$/gm,'').split(';').map(s=>s.trim()).filter(Boolean))await db.prepare(s).run();
 const call=v=>mf.dispatchFetch('http://test/',{method:'POST',body:JSON.stringify(v)}),at=Date.now()+60000;
 const scheduled=kind==='odds'?target:{...target,kind:'schedule',url:'https://autorace.jp/race_info/OtherRaceInfo'};
 await call({op:'schedule',entries:[{at,target:scheduled}]});await call({op:'alarm',now:at});
 assert.equal(await (await call({op:'pending'})).json(),1);assert.equal(requests.length,1);
 await call({op:'alarm',now:at+delay});assert.deepEqual(requests.map(r=>r.method),delay<90000?['GET','POST']:['GET']);
 assert.equal(await (await call({op:'pending'})).json(),0);assert.equal((await db.prepare('SELECT count(*) AS n FROM raw_observations').first()).n,delay<90000?2:1);
 assert.equal((await db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='COMPLETE'").first()).n,delay<90000&&kind==='odds'?1:0);
 assert.equal((await db.prepare("SELECT count(*) AS n FROM sports_parses WHERE status='PROGRAM_PARSED'").first()).n,delay<90000&&kind==='schedule'?1:0);
 }finally{await mf.dispose();}
});
