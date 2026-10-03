import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {jraBody,race,roster} from '../fixtures/synthetic/jra.mjs';
const b=await build({entryPoints:['workers/jra.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const {parseJra,decodeJra}=await import(`data:text/javascript;base64,${Buffer.from(b.outputFiles[0].text).toString('base64')}`);
test('eight JRA markets retain complete support, frame self-pairs and ranged prices',()=>{
 const single=parseJra(jraBody('win_place'),race,'win_place');
 assert.deepEqual(single.runners,roster);assert.equal(single.metadata.flat,true);assert.equal(single.metadata.scheduled_start_at,'2000-01-01T01:05:00.000Z');
 assert.equal(single.markets[1].quotes[0].upper,2);
 for(const [page,count] of [['frame_quinella',4],['quinella',6],['wide',6],['exacta',12],['trio',4],['trifecta',24]]){
  const s=parseJra(jraBody(page),race,page,single.runners);assert.equal(s.phase,'FINAL_ONLY');assert.equal(s.source_updated_at,null);
  assert.equal(s.markets[0].expected,count);assert.equal(s.markets[0].complete,true);assert.equal(s.markets[0].quotes.length,count);
  if(page==='wide')assert.equal(s.markets[0].quotes[0].upper,3);
 }
});
test('missing combination is incomplete; an explicit no-vote cell stays unavailable',()=>{
 const absent=parseJra(jraBody('trifecta',{omit:'1-2-10'}),race,'trifecta',roster);assert.equal(absent.markets[0].complete,false);
 assert.equal(absent.markets[0].quotes.length,23);
 const empty=parseJra(jraBody('trifecta',{value:'票数なし'}),race,'trifecta',roster);
 assert.equal(empty.markets[0].complete,true);assert.ok(empty.markets[0].quotes.every(q=>q.lower===null&&q.status==='UNAVAILABLE'));
});
test('page identity, advertised flat scope and time phase cannot be inferred from request or footer',()=>{
 assert.throws(()=>parseJra(jraBody('quinella',{venue:'京都'}),race,'quinella',roster),/IDENTITY/);
 assert.equal(parseJra(jraBody('quinella',{course:'障害・芝'}),race,'quinella',roster).metadata.flat,false);
 assert.equal(parseJra(jraBody('quinella',{raceName:'障害未勝利'}),race,'quinella',roster).metadata.flat,false);
 assert.equal(parseJra(jraBody('quinella',{category:'障害3歳以上'}),race,'quinella',roster).metadata.flat,false);
 const before=parseJra(jraBody('quinella',{phase:'10時04分現在'}),race,'quinella',roster);assert.equal(before.phase,'INTERMEDIATE');assert.equal(before.source_updated_at,null);
 const unknown=parseJra(jraBody('quinella',{phase:'表示不明'})+'<footer>最終オッズ</footer>',race,'quinella',roster);assert.equal(unknown.phase,'UNKNOWN');
 assert.throws(()=>parseJra(jraBody('quinella'),race,'quinella'),/RUNNERS/);
});
test('frame spans carry exactly their advertised rows and reject an omitted frame outside the span',()=>{
 const body=jraBody('win_place'),last=body.replace('<td class="waku"><img alt="枠3" /></td><td class="num">10</td>','<td class="waku" rowspan="2"><img alt="枠3" /></td><td class="num">10</td>')
  .replace('<td class="waku"><img alt="枠3" /></td><td class="num">11</td>','<td class="num">11</td>');
 assert.deepEqual(parseJra(last,race,'win_place').runners,roster);
 assert.throws(()=>parseJra(last.replace('rowspan="2"','rowspan="1"'),race,'win_place'),/RUNNERS/);
 assert.throws(()=>parseJra(last.replace('rowspan="2"','rowspan="3"'),race,'win_place'),/FRAME_SPAN/);
});
test('malformed runner support and quote identities are refused, Shift JIS is decoded',()=>{
 assert.throws(()=>parseJra(jraBody('quinella'),race,'quinella',{...roster,entrants:[1,1,10]}),/RUNNERS/);
 assert.throws(()=>parseJra(jraBody('quinella').replace('<caption>1</caption>','<caption>99</caption>'),race,'quinella',roster),/IDENTITY/);
 assert.equal(decodeJra(new Uint8Array([0x8d,0xc5,0x8f,0x49])),'最終');
 assert.throws(()=>decodeJra(new Uint8Array([0x81])),/encoded|encoding|decode/i);
});
