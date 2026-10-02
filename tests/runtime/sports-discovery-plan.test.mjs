import test from 'node:test';import assert from 'node:assert/strict';import {build} from 'esbuild';
import * as fixture from '../fixtures/synthetic/sports.mjs';
const built=await build({stdin:{contents:"export {parseProgram} from './workers/sports/discovery';export {discoveryTargets} from './workers/sports/discovery-plan';export {oddsTargets} from './workers/sports/odds-plan';export {validateTarget} from './workers/sports/capture';export {parseOdds} from './workers/sports/parsers';",resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'});
const d=await import('data:text/javascript;base64,'+Buffer.from(built.outputFiles[0].text).toString('base64'));
const target=(sport,scope,url)=>({sport,race_id:sport+':20000101:'+scope,kind:'schedule',url});
test('three catalogs use declared days and common validated request recipes',()=>{
 const samples=[['auto',fixture.autoCatalog,target('auto','0:0','https://autorace.jp/race_info/XML/Hold/Today')],['boat',fixture.boatCatalog,target('boat','0:0','https://www.boatrace.jp/owpc/pc/race/index?hd=20000101')],['keirin',fixture.keirinCatalog,target('keirin','0:0','https://keirin.jp/pc/json?type=JSJ048')]];
 for(const [sport,raw,t] of samples){const p=d.parseProgram(raw,t),plan=d.discoveryTargets(p);assert.equal(plan.targets.length,1);assert.equal(plan.deferred.length,0);d.validateTarget(plan.targets[0]);
  if(sport==='auto')assert.equal(JSON.parse(plan.targets[0].body).raceNo,7);
  if(sport==='keirin'){assert.equal(plan.targets[0].race_id,'keirin:20000101:47:0');assert.equal(plan.targets[0].discovery_stage,'venue');}
  p.requested_race_id=sport+':20000102:0:0';assert.equal(d.discoveryTargets(p).targets.length,0);assert.equal(d.discoveryTargets(p).deferred[0].reason,'BUSINESS_DAY_MISMATCH');
 }
 const zero=JSON.parse(fixture.autoCatalog);zero.body.today[0].oddsRaceNo=0;
 assert.equal(d.discoveryTargets(d.parseProgram(JSON.stringify(zero),samples[0][2])).deferred[0].reason,'CURRENT_RACE_UNKNOWN');
});
test('auto expands the declared final race once and leaves unknown totals unresolved',()=>{
 const t={...target('auto','6:7','https://autorace.jp/race_info/OtherRaceInfo'),body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:7}),discovery_stage:'venue'};
 const plan=d.discoveryTargets(d.parseProgram(fixture.autoProgram,t));assert.deepEqual(plan.targets.map(t=>JSON.parse(t.body).raceNo),[1,2,3,4,5,6,8]);
 for(const child of plan.targets){const v=JSON.parse(fixture.autoProgram);v.body.raceNo=JSON.parse(child.body).raceNo;assert.deepEqual(d.discoveryTargets(d.parseProgram(JSON.stringify(v),child)).targets,[]);}
 const unknown=JSON.parse(fixture.autoProgram);delete unknown.body.finalRaceNo;assert.equal(d.discoveryTargets(d.parseProgram(JSON.stringify(unknown),t)).deferred[0].reason,'FINAL_RACE_NUMBER_UNKNOWN');
 assert.throws(()=>d.parseProgram(fixture.autoProgram.replace('"finalRaceNo":8','"finalRaceNo":6'),t),/INCOMPLETE/);
});
test('keirin links first resolve a response identity then obtain that race clock without recursive fanout',()=>{
 const t={...target('keirin','47:0','https://keirin.jp/pc/racelive'),kind:'guest',form:true,body:'encp=synthetic-public-navigation',discovery_stage:'venue'};
 const p=d.parseProgram(fixture.keirinProgram,t),plan=d.discoveryTargets(p);assert.equal(plan.targets.length,3);
 assert.ok(plan.targets.every(t=>t.race_id==='keirin:20000101:47:0'));assert.deepEqual(plan.targets.map(t=>new URL(t.url).searchParams.get('encp')),['synthetic-public-navigation-1','synthetic-public-navigation-2','synthetic-public-navigation-3']);
 const resolved=d.parseProgram(fixture.keirinIdentity,plan.targets[0]);const children=d.discoveryTargets(resolved,'synthetic-identity-observation').targets;assert.equal(children.length,2);const child=children[0];
 assert.equal(new URL(children[1].url).searchParams.get('type'),'JST010');assert.equal(children[1].context_event,'synthetic-identity-observation');
 assert.equal(new URL(children[1].url).searchParams.get('url.media.flg'),'1');assert.equal(new URL(children[1].url).searchParams.has('mode'),false);
 assert.throws(()=>d.validateTarget({...children[1],url:children[1].url.replace('url.media.flg=1','mode=0')}),/READ_API_PARAMS/);
 assert.equal(d.discoveryTargets(resolved).deferred[0].reason,'RACE_CONTEXT_REQUIRED');
 assert.equal(child.race_id,'keirin:20000101:47:2');assert.equal(child.context_event,'synthetic-identity-observation');assert.equal(new URLSearchParams(child.body).get('encp'),'synthetic-public-navigation-1');
 assert.deepEqual(d.discoveryTargets(d.parseProgram(fixture.keirinProgram,child)).targets,[]);
 const duplicate=structuredClone(p);duplicate.program.navigation[1].public_navigation=duplicate.program.navigation[0].public_navigation;assert.throws(()=>d.discoveryTargets(duplicate),/NAVIGATION/);
});
test('invalid dates and discovery scopes cannot become odds or result requests',()=>{
 const t={sport:'auto',race_id:'auto:20000101:6:8',kind:'odds',url:'https://autorace.jp/race_info/Odds',body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:8})};
 for(const id of ['auto:20000230:6:8','auto:20000101:0:8','auto:20000101:6:0'])assert.throws(()=>d.validateTarget({...t,race_id:id}));
 for(const discovery_stage of ['venue','race','unknown'])assert.throws(()=>d.validateTarget({...t,discovery_stage}));
});
test('saved auto and boat race clocks build one/seven-market recipes without inventing a cutoff',()=>{
 const auto={...target('auto','6:7','https://autorace.jp/race_info/OtherRaceInfo'),body:JSON.stringify({placeCode:6,raceDate:'2000-01-01',raceNo:7})};
 const boat=target('boat','2:0','https://www.boatrace.jp/owpc/pc/race/raceindex?jcd=02&hd=20000101');
 for(const [raw,t,id,count] of [[fixture.autoProgram,auto,'auto:20000101:6:7',1],[fixture.boatProgram,boat,'boat:20000101:2:2',5]]){
  const source={value:d.parseProgram(raw,t),target:t},at=Date.parse('2000-01-01T00:00:00Z'),p=d.oddsTargets(source,id,at);
  assert.equal(p.targets.length,count);assert.deepEqual(p.deferred,[]);assert.ok(p.targets.every(t=>t.race_id===id&&t.kind==='odds'));p.targets.forEach(d.validateTarget);
  const names=['trifecta','trio','exacta,quinella','wide','win,place'];
  const parsed=p.targets.flatMap((q,i)=>d.parseOdds(t.sport==='auto'?fixture.autoBody():fixture.boatOddsBody(names[i].split(',')),q).markets);
  assert.equal(parsed.length,7);assert.ok(parsed.every(m=>m.complete));
  const race=source.value.program.races.find(r=>r.race_id===id),close=Date.parse(race.close_at);
  assert.equal(d.oddsTargets(source,id,close).targets.length,0);
  assert.equal(d.oddsTargets(source,id,close-(count-1)*5000).deferred[0].reason,'OUTSIDE_PRE_CLOSE_WINDOW');
  race.close_at=null;assert.equal(d.oddsTargets(source,id,at).deferred[0].reason,'CLOSE_TIME_UNKNOWN');
  assert.equal(d.oddsTargets(source,id.replace(/\d+$/,'99'),at).deferred[0].reason,'RACE_NOT_IN_PROGRAM');
 }
});
test('keirin odds recipes use the same fixed identity and published support, retaining withdrawals and deferring unknown frames',()=>{
 const t={...target('keirin','47:2','https://keirin.jp/pc/racelive'),kind:'guest',form:true,body:'encp=synthetic-public-navigation',discovery_stage:'race',context_event:'synthetic-identity'};
 const r={...t,kind:'schedule',form:undefined,body:undefined,url:'https://keirin.jp/pc/json?type=JST010&encp=synthetic-public-navigation&url.media.flg=1'};
 const clock={value:d.parseProgram(fixture.keirinProgram,t),target:t},runners={value:d.parseProgram(fixture.keirinRunners,r),target:r},at=Date.parse('2000-01-01T00:00:00Z');
 const p=d.oddsTargets(clock,t.race_id,at,runners);assert.equal(p.targets.length,7);assert.deepEqual(p.deferred,[]);
 assert.ok(p.targets.every(q=>q.context_event===t.context_event&&q.entrants.join(',')==='1,2,3,4,5,6'));
 assert.deepEqual(p.targets.map(q=>new URL(q.url).searchParams.get('kake')),['6','2','7','4','3','1','5']);
 assert.equal(d.oddsTargets(clock,t.race_id,at).deferred[0].reason,'RUNNERS_REQUIRED');
 for(const other of [{...r,context_event:'other'},{...r,race_id:'keirin:20000101:47:3'},{...r,url:r.url.replace('synthetic-public-navigation','different-navigation')}])
  assert.throws(()=>d.oddsTargets(clock,t.race_id,at,{...runners,target:other}),/RUNNER_CONTEXT_MISMATCH/);
 const unknown=structuredClone(runners);unknown.value.program.runners.entries[0].frame=null;
 const partial=d.oddsTargets(clock,t.race_id,at,unknown);assert.equal(partial.targets.length,5);assert.deepEqual(partial.deferred.map(q=>q.market),['frame_exacta','frame_quinella']);
 assert.ok(partial.targets.every(q=>q.entrants.includes(3)));assert.equal(partial.targets[0].frames[1],undefined);
 const venue={...t,race_id:'keirin:20000101:47:0',discovery_stage:'venue',context_event:undefined};
 assert.equal(d.oddsTargets({target:venue,value:d.parseProgram(fixture.keirinProgram,venue)},t.race_id,at,runners).deferred[0].reason,'RACE_CONTEXT_REQUIRED');
});
test('keirin frame availability follows the saved provider flag even when dummy frame numbers exist',()=>{
 const t={...target('keirin','47:2','https://keirin.jp/pc/racelive'),kind:'guest',form:true,body:'encp=synthetic-public-navigation',discovery_stage:'race',context_event:'synthetic-identity'};
 const r={...t,kind:'schedule',form:undefined,body:undefined,url:'https://keirin.jp/pc/json?type=JST010&encp=synthetic-public-navigation&url.media.flg=1'};
 const clock={value:d.parseProgram(fixture.keirinProgram,t),target:t},at=Date.parse('2000-01-01T00:00:00Z');
 const raw=JSON.parse(fixture.keirinRunners);raw.data.wakuKbn='0';
 const runners={value:d.parseProgram(JSON.stringify(raw),r),target:r};
 const absent=d.oddsTargets(clock,t.race_id,at,runners);
 assert.deepEqual(absent.targets.map(q=>q.market),['trifecta','exacta','trio','quinella','wide']);
 assert.deepEqual(absent.deferred,[]);
 assert.deepEqual(absent.not_offered,[{market:'frame_exacta',source_label:'0'},{market:'frame_quinella',source_label:'0'}]);
 assert.ok(absent.targets.every(q=>q.entrants.join(',')==='1,2,3,4,5,6'));
 for(const label of [null,'2']){
  const unknown=structuredClone(runners);unknown.value.program.runners.frame_category_label=label;
  const p=d.oddsTargets(clock,t.race_id,at,unknown);
  assert.equal(p.targets.length,5);assert.deepEqual(p.not_offered,[]);
  assert.deepEqual(p.deferred.map(q=>q.reason),['FRAME_AVAILABILITY_UNKNOWN','FRAME_AVAILABILITY_UNKNOWN']);
 }
});
