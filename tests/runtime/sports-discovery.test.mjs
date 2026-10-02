import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import * as fixture from '../fixtures/synthetic/sports.mjs';
const built=await build({entryPoints:['workers/sports/discovery.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const d=await import(`data:text/javascript;base64,${Buffer.from(built.outputFiles[0].text).toString('base64')}`);

test('program clocks preserve midnight business-day rollover and reject invalid dates/times',()=>{
 assert.equal(d.programClock('20000101','24:02'),'2000-01-01T15:02:00.000Z');
 assert.equal(d.programClock('20000229','00:05'),'2000-02-28T15:05:00.000Z');
 assert.equal(d.programClock('20000101',null),null);
 for(const [day,clock] of [['20000230','10:00'],['20000101','10:60'],['20000101','30:00'],['20000101','unknown']])assert.throws(()=>d.programClock(day,clock));
});
test('auto discovery uses declared business date and preserves missing/current/cancel state',()=>{
 const rows=d.autoVenues(fixture.autoCatalog);assert.equal(rows.length,1);assert.equal(rows[0].race_date,'20000101');assert.equal(rows[0].current_race,7);
 const zero=JSON.parse(fixture.autoCatalog);zero.body.today[0].oddsRaceNo='0';zero.body.today[0].cancelFlg='1';
 assert.equal(d.autoVenues(JSON.stringify(zero))[0].current_race,null);assert.equal(d.autoVenues(JSON.stringify(zero))[0].cancel_label,'1');
 zero.body.today.push({...zero.body.today[0]});assert.throws(()=>d.autoVenues(JSON.stringify(zero)),/DUPLICATE/);
 assert.throws(()=>d.autoVenues(JSON.stringify({result:'Failure',body:[]})),/NOT_READY/);
});
test('auto program binds the response identity and preserves advertised cutoff semantics',()=>{
 const r=d.autoProgram(fixture.autoProgram,{race_date:'20000101',venue:6,race:7});
 assert.equal(r.race_id,'auto:20000101:6:7');assert.equal(r.close_at,'2000-01-01T15:02:00.000Z');assert.equal(r.final_race_number,8);
 assert.equal(r.time_semantics,'PROVIDER_ADVERTISED_PROGRAM');
 assert.throws(()=>d.autoProgram(fixture.autoProgram,{race_date:'20000101',venue:2,race:7}),/IDENTITY/);
 assert.throws(()=>d.autoProgram(fixture.autoProgram,{race_date:'20000101',venue:6,race:8}),/IDENTITY/);
});
test('boat catalog and per-venue program ignore another date and preserve each advertised time',()=>{
 const venues=d.boatVenues(fixture.boatCatalog,'20000101');assert.equal(venues.length,1);assert.equal(venues[0].venue,2);
 const races=d.boatProgram(fixture.boatProgram,'20000101',2);assert.deepEqual(races.map(r=>r.race),[1,2,3]);
 assert.equal(races[0].close_at,'2000-01-01T01:10:00.000Z');assert.ok(races.every(r=>r.start_at===null));
 assert.throws(()=>d.boatProgram(fixture.boatProgram,'20000102',2),/NOT_READY/);
 assert.throws(()=>d.boatProgram(fixture.boatProgram+fixture.boatProgram,'20000101',2),/DUPLICATE/);
 const missing=d.boatProgram(fixture.boatProgram.replace('10:10','未定'),'20000101',2);assert.equal(missing[0].close_at,null);
});
test('keirin discovery retains cancellation labels and anonymous navigation without inventing zero races',()=>{
 const r=d.keirinVenues(fixture.keirinCatalog)[0];assert.equal(r.current_race,null);assert.equal(r.venue,47);
 assert.equal(r.cancel_label,'synthetic-cancel-label');assert.equal(r.public_navigation,'synthetic-public-navigation');
 const numbered=JSON.parse(fixture.keirinCatalog);numbered.RaceList[0].raceNum='12R';assert.equal(d.keirinVenues(JSON.stringify(numbered))[0].current_race,12);
 const wrong=JSON.parse(fixture.keirinCatalog);wrong.RaceList[0].kaisaiDate='invalid';assert.throws(()=>d.keirinVenues(JSON.stringify(wrong)),/DATE/);
});
test('keirin program uses revised advertised clocks, keeps navigation positions and rejects partial lists',()=>{
 const r=d.keirinProgram(fixture.keirinProgram);assert.equal(r.selected.race_id,'keirin:20000101:47:2');
 assert.equal(r.selected.close_at,'2000-01-01T08:02:00.000Z');assert.equal(r.selected.close_label,'17:02');
 assert.deepEqual(r.navigation.map(x=>x.position),[1,2,3]);assert.equal(r.navigation[0].ended_label,'1');
 assert.equal(r.selected.final_race_number,null);
 assert.throws(()=>d.keirinProgram(fixture.keirinProgram.replace('"selRaceNo":"2"','"selRaceNo":"4"')),/INCOMPLETE/);
 assert.throws(()=>d.keirinProgram('<html>not published</html>'),/NOT_READY/);
 const target={sport:'keirin',kind:'guest',race_id:'keirin:20000101:47:2',url:'https://keirin.jp/pc/racelive'};
 assert.equal(d.parseProgram(fixture.keirinProgram,target).program.selected.race_id,target.race_id);
 assert.throws(()=>d.parseProgram(fixture.keirinProgram,{...target,race_id:'keirin:20000101:47:1'}),/IDENTITY/);
});
test('keirin venue navigation discovers the response race and still rejects another venue/day',()=>{
 const t={sport:'keirin',kind:'guest',race_id:'keirin:20000101:47:0',url:'https://keirin.jp/pc/racelive',discovery_stage:'venue'};
 const p=d.parseProgram(fixture.keirinProgram,t);assert.equal(p.requested_race_id,t.race_id);assert.equal(p.program.selected.race_id,'keirin:20000101:47:2');
 for(const bad of [{race_id:'keirin:20000102:47:0'},{race_id:'keirin:20000101:48:0'},{discovery_stage:'race'},{discovery_stage:undefined}])assert.throws(()=>d.parseProgram(fixture.keirinProgram,{...t,...bad}),/IDENTITY/);
});
test('keirin identity JSON resolves unknown race numbers without inventing cutoff clocks',()=>{
 const t={sport:'keirin',kind:'schedule',race_id:'keirin:20000101:47:0',url:'https://keirin.jp/pc/json?type=JST015&encp=synthetic-public-navigation',discovery_stage:'race'};
 assert.equal(d.supportsProgram(t),true);const p=d.parseProgram(fixture.keirinIdentity,t);
 assert.equal(p.program.selected.race_id,'keirin:20000101:47:2');assert.equal(p.program.selected.close_at,null);assert.equal(p.program.selected.start_at,null);
 for(const id of ['keirin:20000102:47:0','keirin:20000101:48:0','keirin:20000101:47:3'])assert.throws(()=>d.parseProgram(fixture.keirinIdentity,{...t,race_id:id}),/IDENTITY/);
 assert.throws(()=>d.parseProgram(fixture.keirinIdentity.replace('"raceNo":"2"','"raceNo":"0"'),t),/ID/);
});

test('keirin runner support retains declared entrants, frame/withdrawal labels and rejects partial or duplicate support',()=>{
 const t={sport:'keirin',kind:'schedule',race_id:'keirin:20000101:47:2',url:'https://keirin.jp/pc/json?type=JST010&encp=synthetic-public-navigation',context_event:'synthetic-identity'};
 const program=d.parseProgram(fixture.keirinRunners,t).program;assert.equal(program.selected.close_at,null);assert.equal(program.identity_evidence,t.context_event);
 assert.equal(program.runners.declared_count,6);assert.equal(program.runners.entries[2].cancellation_label,'synthetic withdrawal');
 assert.equal(JSON.stringify(program).includes('synthetic unused name'),false);
 const raw=JSON.parse(fixture.keirinRunners);raw.data.sensyuInfoList[0].wakuBan='0';assert.equal(d.keirinRunners(JSON.stringify(raw)).entries[0].frame,null);
 raw.data.sensyuInfoList.pop();assert.throws(()=>d.keirinRunners(JSON.stringify(raw)),/INCOMPLETE/);
 const duplicate=JSON.parse(fixture.keirinRunners);duplicate.data.sensyuInfoList[1].syaban='1';assert.throws(()=>d.keirinRunners(JSON.stringify(duplicate)),/DUPLICATE/);
 assert.throws(()=>d.parseProgram(fixture.keirinRunners,{...t,context_event:undefined}),/CONTEXT/);
 assert.throws(()=>d.parseProgram(fixture.keirinRunners,{...t,race_id:'keirin:20000101:47:0'}),/ID/);
});
