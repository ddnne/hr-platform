import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {catalogTarget,jraCatalog,jraProgram} from '../fixtures/synthetic/jra.mjs';
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
test('ended/cancelled/unknown labels never become a scheduled start or observed deadline',()=>{
 for(const label of ['発走済','中止','変更確認中']){
  const r=p.parseProgram(jraProgram({clock:label}),venue).program.races[0];assert.equal(r.start_label,label);assert.equal(r.start_at,null);assert.equal(r.close_at,null);
 }
 assert.throws(()=>p.parseProgram(jraProgram({clock:'99:05'}),venue),/CLOCK/);
});
test('shared daily discovery permits one JRA action and no odds/result task or legacy fallback',async()=>{
 const state=await p.initialDaily('jra',now);state.tasks={};await p.acceptProgram(state,source(jraCatalog,catalogTarget),now);
 let entries=p.nextDailyParallel(state,now);assert.equal(entries.length,1);assert.equal(entries[0].target.kind,'schedule');
 await p.acceptProgram(state,source(jraProgram(),venue),now+1000);await p.completeDaily(state,entries[0],'RAW_STORED',now+1000);
 assert.equal(state.races['jra:20000101:5:1'].clock.value.program.races[0].start_at,'2000-01-01T01:05:00.000Z');
 assert.ok(Object.keys(state.tasks).every(k=>k.startsWith('request:')));assert.equal(Object.keys(state.actions).length,0);
 const s=source(jraProgram(),venue);assert.equal(p.oddsTargets(s,'jra:20000101:5:1',now).deferred[0].reason,'JRA_DAILY_ODDS_UNQUALIFIED');
 assert.throws(()=>p.resultTarget(s,'jra:20000101:5:1'),/JRA_RESULT_UNSUPPORTED/);
 p.rejectProgram(state,venue);assert.equal(state.races['jra:20000101:5:1'].clock,undefined);
});
