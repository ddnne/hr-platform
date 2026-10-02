import test from 'node:test';import assert from 'node:assert/strict';import {build} from 'esbuild';
import * as fixture from '../fixtures/synthetic/sports.mjs';
const built=await build({stdin:{contents:"export {parseProgram} from './workers/sports/discovery';export {discoveryTargets} from './workers/sports/discovery-plan';export {validateTarget} from './workers/sports/capture';",resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'});
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
