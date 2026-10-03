import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {catalogTarget,jraCatalog,jraProgram,resultCatalogTarget,jraResultCatalog,jraResultProgram} from '../fixtures/synthetic/jra.mjs';
import {readFile} from 'node:fs/promises';
const config=JSON.parse(await readFile('configs/jra-source.json','utf8'));
const b=await build({stdin:{contents:`export {parseProgram} from './workers/sports/discovery';export {validateTarget} from './workers/sports/capture';
export {discoveryTargets} from './workers/sports/discovery-plan';export {oddsTargets} from './workers/sports/odds-plan';
export * from './workers/sports/daily-plan';`,resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'});
const p=await import('data:text/javascript;base64,'+Buffer.from(b.outputFiles[0].text).toString('base64'));
const catalog=p.parseProgram(jraCatalog,catalogTarget),venue=p.discoveryTargets(catalog).targets[0],now=Date.parse('2000-01-01T00:55:00Z');
const source=(raw,target,at=now)=>({value:p.parseProgram(raw,target),target,event:'synthetic-program',received_at:new Date(at).toISOString(),available_at:new Date(at).toISOString()});
test('official catalog selects only requested day and saved venue links retain full market navigation',()=>{
 p.validateTarget(catalogTarget);p.validateTarget(venue);assert.equal(catalog.program.venues.length,1);assert.equal(venue.race_id,'jra:20000101:5:0');
 const value=p.parseProgram(jraProgram(),venue),race=value.program.races[0];assert.equal(race.race_id,'jra:20000101:5:1');
 assert.equal(race.start_at,'2000-01-01T01:05:00.000Z');assert.equal(race.close_at,null);
 assert.equal(Object.keys(value.program.odds_navigation[race.race_id]).length,7);assert.deepEqual(p.discoveryTargets(value).targets,[]);
 assert.equal(value.source_updated_at,null);assert.equal(value.source_published_at,null);
});
test('mismatched link/header/request identities and ambiguous duplicate recipes fail closed',()=>{
 assert.throws(()=>p.validateTarget({...venue,race_id:'jra:20000102:5:0'}),/IDENTITY/);
 assert.throws(()=>p.parseProgram(jraProgram({day:'20000102'}),venue),/IDENTITY/);
 assert.throws(()=>p.parseProgram(jraProgram({venue:8}),venue),/IDENTITY/);
 assert.throws(()=>p.parseProgram(jraProgram().replace('2000年1月1日','2000年1月2日'),venue),/IDENTITY/);
 assert.throws(()=>p.parseProgram(jraProgram().replace('/AA\');','/AB\');'),venue),/DUPLICATE/);
 assert.throws(()=>p.parseProgram(jraCatalog.replaceAll('東京','京都'),catalogTarget),/IDENTITY/);
 assert.throws(()=>p.parseProgram('<html>unexpected body</html>',catalogTarget),/NOT_READY/);
});
test('the separate venue navigation state digit never becomes part of the venue number',()=>{
 const raw=jraCatalog.replace('pw15orl0005','pw15orl1005'),value=p.parseProgram(raw,catalogTarget),child=p.discoveryTargets(value).targets[0];
 assert.equal(value.program.venues[0].venue,5);assert.equal(child.race_id,'jra:20000101:5:0');p.validateTarget(child);
 assert.equal(p.parseProgram(jraProgram(),child).program.races[0].venue,5);
 assert.throws(()=>p.parseProgram(raw.replace('pw15orl1005','pw15orl2005'),catalogTarget),/NAVIGATION/);
});
test('final-display navigation retains complete recipes without inventing a start or deadline',()=>{
 const value=p.parseProgram(jraProgram({navigationMode:'final',clock:'発走済'}),venue),r=value.program.races[0];
 assert.equal(r.race_id,'jra:20000101:5:1');assert.equal(r.start_at,null);assert.equal(r.close_at,null);
 assert.equal(Object.keys(value.program.odds_navigation[r.race_id]).length,7);
 assert.ok(value.program.odds_navigation[r.race_id].win_place.startsWith('pw151ou1005'));
 assert.throws(()=>p.parseProgram(jraProgram({navigationMode:'final',venue:8}),venue),/IDENTITY/);
});
test('ended/cancelled/unknown labels never become a scheduled start or observed deadline',()=>{
 for(const label of ['発走済','中止','変更確認中']){
  const r=p.parseProgram(jraProgram({clock:label}),venue).program.races[0];assert.equal(r.start_label,label);assert.equal(r.start_at,null);assert.equal(r.close_at,null);
 }
 assert.throws(()=>p.parseProgram(jraProgram({clock:'99:05'}),venue),/CLOCK/);
});
test('Japanese advertised start uses the shared clock while preserving its original label',()=>{
 const r=p.parseProgram(jraProgram({clock:'10時05分'}),venue).program.races[0];
 assert.equal(r.start_at,'2000-01-01T01:05:00.000Z');assert.equal(r.start_label,'10時05分');assert.equal(r.close_at,null);
 assert.throws(()=>p.parseProgram(jraProgram({clock:'99時05分'}),venue),/CLOCK/);
 assert.equal(p.parseProgram(jraProgram({clock:'未定'}),venue).program.races[0].start_at,null);
});
test('shared daily discovery permits one JRA action, every odds page and no inferred result deadline',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraCatalog,catalogTarget),now);
 let entries=p.nextDailyParallel(state,now);assert.equal(entries.length,1);assert.equal(entries[0].target.kind,'schedule');
 await p.acceptProgram(state,source(jraProgram(),venue),now+1000);await p.completeDaily(state,entries[0],'RAW_STORED',now+1000);
 assert.equal(state.races['jra:20000101:5:1'].clock.value.program.races[0].start_at,'2000-01-01T01:05:00.000Z');
 assert.equal(Object.values(state.tasks).filter(t=>t.kind==='odds').length,7);assert.equal(Object.keys(state.actions).length,0);
 assert.ok(Object.values(state.tasks).every(t=>['request','odds'].includes(t.kind)));
 const s=source(jraProgram(),venue);assert.equal(p.oddsTargets(s,'jra:20000101:5:1',now).deferred[0].reason,'JRA_ODDS_PAGE_REQUIRED');
 assert.throws(()=>p.resultTarget(s,'jra:20000101:5:1'),/JRA_RESULT_UNSUPPORTED/);
 p.rejectProgram(state,venue);assert.equal(state.races['jra:20000101:5:1'].clock,undefined);
});
test('changed venue navigation replaces only the periodic task and cannot restore old clocks',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraCatalog,catalogTarget),now);
 const old=p.nextDailyParallel(state,now)[0];await p.acceptProgram(state,source(jraProgram(),venue),now);
 await p.acceptProgram(state,source(jraCatalog.replace('pw15orl0005','pw15orl1005'),catalogTarget),now+1);
 assert.equal(state.tasks[old.daily_task].done,true);assert.equal(state.races['jra:20000101:5:1'].clock,undefined);
 await p.acceptProgram(state,source(jraProgram(),venue),now+2);assert.equal(state.races['jra:20000101:5:1'].clock,undefined);
 await p.completeDaily(state,old,'RAW_STORED',now+2);const next=p.nextDailyParallel(state,now+2)[0];
 assert.ok(next.target.body.includes('pw15orl1005'));assert.equal(Object.values(state.tasks).filter(t=>t.kind==='request'&&!t.done).length,1);
 await p.acceptProgram(state,source(jraCatalog,catalogTarget),now+3);
 assert.equal(Object.values(state.tasks).filter(t=>t.kind==='request'&&!t.done).length,1);assert.equal(state.tasks[old.daily_task].done,false);
});
test('JRA pages use bounded parallel actions with spaced starts and an available intermediate roster',async()=>{
 let at=now-10*60000;const state=await p.initialDaily('jra',at);state.tasks={};
 await p.acceptProgram(state,source(jraProgram(),venue,at),at);
 const pages=[],first=p.nextDailyParallel(state,at)[0];assert.equal(first.target.page,'win_place');
 const context={event:'synthetic-win',race_id:first.target.race_id,received_at:new Date(first.at).toISOString(),available_at:new Date(first.at).toISOString()};
 const finish=async e=>{assert.equal(state.actions[e.daily_task].entries.length,1);pages.push(e.target.page);
  assert.equal(e.target.deadline_at,Date.parse('2000-01-01T01:05:00Z'));assert.equal(state.tasks[e.daily_task].close_at,undefined);
  if(e!==first){assert.equal(e.target.context_event,context.event);assert.equal(e.target.context_phase,'INTERMEDIATE');}
  await p.completeDaily(state,e,'RAW_STORED',e.at,false,false,e===first?context:null);};
 await finish(first);at=first.at+config.daily_request_spacing_seconds*1000;
 for(let attempts=0;pages.length<7&&attempts<20;attempts++){const entries=p.nextDailyParallel(state,at);if(!entries.length){at=state.wake_at;continue;}assert.ok(Object.keys(state.actions).length<=config.daily_maximum_parallel_requests);
  assert.ok(entries.slice(1).every((e,i)=>e.at-entries[i].at>=config.daily_request_spacing_seconds*1000));
  for(const e of entries)await finish(e);at=entries.at(-1).at+config.daily_request_spacing_seconds*1000;}
 await p.completeDaily(state,first,'RAW_STORED',at,false,false,null);assert.deepEqual(state.races[first.target.race_id].win_context,context);
 assert.equal(Object.keys(state.actions).length,0);
 assert.deepEqual(pages.sort(),Object.keys(config.tables).sort());
});
test('JRA missing, stale, future or failed roster prevents other pages but not a fresh win request',async()=>{
 for(const mode of ['missing','stale','future','wrong-race']){
  const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraProgram(),venue),now);
  state.races['jra:20000101:5:1'].win_context=mode==='missing'?undefined:{event:'synthetic-win',race_id:mode==='wrong-race'?'jra:20000101:5:2':'jra:20000101:5:1',
   received_at:new Date(now-(mode==='stale'?config.maximum_context_age_seconds+1:0)*1000).toISOString(),available_at:new Date(now+(mode==='future'?1:0)).toISOString()};
  const key='odds:jra:20000101:5:1:win_place';state.tasks[key].next_at=now+1000;
  assert.deepEqual(p.nextDailyParallel(state,now),[]);const e=p.nextDailyParallel(state,now+1000)[0];assert.equal(e.target.page,'win_place');
  await p.completeDaily(state,e,'RAW_STORED',e.at,false,false,null);assert.equal(state.races[e.target.race_id].win_context,undefined);
 }
});
test('changed or removed JRA start invalidates the old action, preserves cutoff null and reschedules collection',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraProgram(),venue),now);
 const old=p.nextDailyParallel(state,now)[0],last=state.tasks[old.daily_task].next_at;
 await p.acceptProgram(state,source(jraProgram({clock:'11:05'}),venue,now+1),now+1);
 const revised=state.tasks[old.daily_task].next_at;assert.ok(revised>last);
 await p.completeDaily(state,old,'CLOSE_CHANGED',now+2);assert.equal(state.tasks[old.daily_task].next_at,revised);
 assert.equal(state.races[old.target.race_id].clock.value.program.races[0].close_at,null);
 await p.acceptProgram(state,source(jraProgram({clock:'中止'}),venue,now+3),now+3);
 assert.ok(Object.values(state.tasks).every(t=>t.done));assert.deepEqual(p.nextDailyParallel(state,now+3),[]);
});
const resultCatalog=p.parseProgram(jraResultCatalog,resultCatalogTarget),resultVenue=p.discoveryTargets(resultCatalog).targets[0];
test('result discovery uses only published official identities and retains final-price links without clocks',()=>{
 p.validateTarget(resultCatalogTarget);p.validateTarget(resultVenue);assert.equal(resultVenue.program_kind,'results');
 const v=p.parseProgram(jraResultProgram(),resultVenue).program;
 assert.deepEqual(Object.keys(v.result_navigation),['jra:20000101:5:1']);assert.ok(v.final_odds_navigation['jra:20000101:5:1']);
 assert.equal(v.races,undefined);assert.equal(p.parseProgram(jraResultProgram({empty:true}),resultVenue).program.result_navigation['jra:20000101:5:1'],undefined);
 assert.equal(p.parseProgram(jraResultCatalog,resultCatalogTarget).program.venues[0].venue,5);
 assert.deepEqual(p.parseProgram(jraResultCatalog.replaceAll('20000101','20000102'),resultCatalogTarget).program.venues,[]);
 assert.throws(()=>p.parseProgram(jraResultProgram().replace('class="race_num"','class="changed"'),resultVenue),/NOT_READY/);
 assert.throws(()=>p.parseProgram(jraResultProgram().replace('pw01sde1005','pw01sde1008'),resultVenue),/IDENTITY/);
 assert.throws(()=>p.parseProgram(jraResultProgram().replace('pw151ou1005','pw151ou1008'),resultVenue),/IDENTITY/);
 assert.throws(()=>p.validateTarget({...resultCatalogTarget,program_kind:undefined}),/ORIGIN/);
});
test('result programs and navigation replacement preserve odds clocks, actions and input roster',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraCatalog,catalogTarget),now);
 await p.acceptProgram(state,source(jraProgram(),venue),now);const clock=structuredClone(state.races['jra:20000101:5:1'].clock);
 await p.acceptProgram(state,source(jraResultCatalog,resultCatalogTarget),now);
 assert.equal(Object.values(state.tasks).filter(t=>t.target?.discovery_stage==='venue'&&!t.done).length,2);
 await p.acceptProgram(state,source(jraResultProgram(),resultVenue),now);
 assert.deepEqual(state.races['jra:20000101:5:1'].clock,clock);assert.ok(state.races['jra:20000101:5:1'].results);
 await p.acceptProgram(state,source(jraResultCatalog.replace('/AA','/AB'),resultCatalogTarget),now+1);
 assert.deepEqual(state.races['jra:20000101:5:1'].clock,clock);assert.equal(state.races['jra:20000101:5:1'].results,undefined);
 assert.equal(Object.values(state.tasks).filter(t=>t.target?.discovery_stage==='venue'&&!t.done).length,2);
 assert.equal(Object.values(state.tasks).filter(t=>t.kind==='odds').length,7);
});
test('result tasks need published fresh navigation, retry incomplete payouts and finish independently of a cutoff',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraResultProgram(),resultVenue),now);
 const key='result:jra:20000101:5:1',first=p.nextDailyParallel(state,now)[0];assert.equal(first.target.kind,'result');
 assert.equal(state.tasks[key].close_at,undefined);assert.equal(state.tasks[key].start_at,undefined);assert.equal(state.races[first.target.race_id].clock,undefined);
 await p.completeDaily(state,first,'RAW_STORED',first.at,false,false);assert.equal(state.tasks[key].done,undefined);
 const at=first.at+state.tasks[key].interval*1000,next=p.nextDailyParallel(state,at).find(e=>e.daily_task===key);assert.ok(next);
 await p.completeDaily(state,next,'RAW_STORED',next.at,false,true);assert.equal(state.tasks[key].done,true);
 await p.completeDaily(state,first,'RAW_STORED',next.at,false,false);assert.equal(state.tasks[key].done,true);
});
test('a replaced result recipe cannot be completed by the old action and stale/missing sources expire',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraResultProgram(),resultVenue),now);
 const key='result:jra:20000101:5:1',old=p.nextDailyParallel(state,now)[0];
 await p.acceptProgram(state,source(jraResultProgram({name:'pw01sde1005200001010120000101/AB'}),resultVenue),now+1);
 await p.completeDaily(state,old,'RAW_STORED',now+2,false,true);assert.equal(state.tasks[key].done,false);
 const current=p.nextDailyParallel(state,now+2).find(e=>e.daily_task===key);assert.ok(current.target.body.includes('AB'));
 await p.completeDaily(state,current,'RAW_STORED',now+3,false,false);p.rejectProgram(state,resultVenue);
 assert.ok(p.nextDailyParallel(state,state.tasks[key].next_at).every(e=>e.daily_task!==key));
 assert.ok(p.nextDailyParallel(state,state.tasks[key].expires_at+1).every(e=>e.daily_task!==key));assert.equal(state.tasks[key].done,true);
});
test('saved daily migration adds the result catalog once and leaves queued action state unchanged',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};state.actions={'synthetic-in-flight':{task:'synthetic-in-flight',entries:[]}};
 assert.equal(await p.ensureDailyCatalogs(state,now),true);assert.equal(Object.keys(state.tasks).length,2);
 const before=structuredClone(state);assert.equal(await p.ensureDailyCatalogs(state,now+1),false);assert.deepEqual(state,before);
});
test('published final navigation runs all pages without an inferred cutoff and keeps intermediate context separate',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};const id='jra:20000101:5:1';
 await p.acceptProgram(state,source(jraProgram({clock:'発走済',navigationMode:'final'}),venue),now);
 const intermediate={event:'synthetic-intermediate',race_id:id,received_at:new Date(now).toISOString(),available_at:new Date(now).toISOString()};
 state.races[id].win_context=intermediate;
 await p.acceptProgram(state,source(jraResultProgram(),resultVenue),now);state.tasks['result:'+id].done=true;
 const first=p.nextDailyParallel(state,now).find(e=>e.daily_task===`final_odds:${id}:win_place`);assert.ok(first);assert.equal(first.target.deadline_at,undefined);
 const final={...intermediate,event:'synthetic-final',received_at:new Date(first.at).toISOString(),available_at:new Date(first.at).toISOString()};
 await p.completeDaily(state,first,'RAW_STORED',first.at,true,false,final);
 const pages=['win_place'];let at=state.wake_at;
 for(let i=0;i<20&&pages.length<7;i++){
  const entries=p.nextDailyParallel(state,at);for(const e of entries){assert.equal(e.target.context_event,final.event);assert.equal(e.target.context_phase,'FINAL_ONLY');assert.equal(e.target.deadline_at,undefined);
   pages.push(e.target.page);await p.completeDaily(state,e,'RAW_STORED',e.at,true);}
  at=Math.max(at+config.daily_request_spacing_seconds*1000,state.wake_at);
 }
 assert.deepEqual(pages.sort(),Object.keys(config.tables).sort());assert.deepEqual(state.races[id].win_context,intermediate);
 assert.deepEqual(state.races[id].final_context,final);assert.ok(Object.values(state.tasks).filter(t=>t.kind==='final_odds').every(t=>t.done));
});
test('unfinished final pages refresh an expired final roster without extending the original window',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};const id='jra:20000101:5:1';
 await p.acceptProgram(state,source(jraProgram({clock:'発走済',navigationMode:'final'}),venue),now);
 await p.acceptProgram(state,source(jraResultProgram(),resultVenue),now);state.tasks['result:'+id].done=true;
 const key=`final_odds:${id}:win_place`,first=p.nextDailyParallel(state,now).find(e=>e.daily_task===key);
 await p.completeDaily(state,first,'RAW_STORED',first.at,true,false,{event:'synthetic-old-final',race_id:id,received_at:new Date(first.at).toISOString(),available_at:new Date(first.at).toISOString()});
 const expiry=state.tasks[key].expires_at,later=first.at+(config.maximum_context_age_seconds+1)*1000;
 await p.acceptProgram(state,source(jraProgram({clock:'発走済',navigationMode:'final'}),venue,later),later);
 await p.acceptProgram(state,source(jraResultProgram(),resultVenue,later),later);
 assert.equal(state.tasks[key].done,false);assert.equal(state.tasks[key].expires_at,expiry);assert.equal(state.races[id].final_context,undefined);
 const fresh=p.nextDailyParallel(state,later).find(e=>e.daily_task===key);assert.ok(fresh);
 const context={event:'synthetic-new-final',race_id:id,received_at:new Date(fresh.at).toISOString(),available_at:new Date(fresh.at).toISOString()};
 await p.completeDaily(state,fresh,'RAW_STORED',fresh.at,true,false,context);
 let entries=[];for(let i=0;i<8&&!entries.length;i++)entries=p.nextDailyParallel(state,state.wake_at);
 assert.ok(entries.some(e=>e.target.page!=='win_place'&&e.target.context_event===context.event));
});
