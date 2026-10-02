import test from 'node:test';import assert from 'node:assert/strict';import {build} from 'esbuild';
import {autoResult,keirinResult,boatResult} from '../fixtures/synthetic/sports-results.mjs';
const b=await build({entryPoints:['workers/sports/results.ts'],bundle:true,write:false,format:'esm',platform:'node'});
const p=await import('data:text/javascript;base64,'+Buffer.from(b.outputFiles[0].text).toString('base64'));
const targets={auto:{sport:'auto',race_id:'auto:20000101:6:8',kind:'result',url:'https://autorace.jp/race_info/RaceResult'},
 keirin:{sport:'keirin',race_id:'keirin:20000101:47:2',kind:'result',url:'https://keirin.jp/pc/json?type=JSJ012&encp=synthetic-public-navigation'},
 boat:{sport:'boat',race_id:'boat:20000101:2:1',kind:'result',url:'https://www.boatrace.jp/owpc/pc/race/raceresult?hd=20000101&jcd=02&rno=1'}};
test('result adapters preserve all seven payout displays, ties/nonfinish labels and unverified settlement conditions',()=>{
 for(const [sport,body] of [['auto',autoResult()],['keirin',keirinResult()],['boat',boatResult]]){
  const r=p.parseResult(body,targets[sport]);assert.equal(r.phase,'RESULT_ONLY');assert.equal(r.publication,'PUBLISHED');assert.equal(r.payouts.length,7);
  assert.ok(r.payouts.every(v=>v.amount_yen===1230&&v.status==='NUMERIC'));assert.equal(r.source_updated_at,null);
  assert.equal(r.settlement_qualified,false);assert.equal(r.payout_unit_yen,null);assert.equal(r.identity_status,'REQUEST_BOUND');
 }
 const a=p.parseResult(autoResult(),targets.auto);assert.deepEqual(a.placings.map(v=>v.rank),[1,1,null]);
 const k=p.parseResult(keirinResult(),targets.keirin);assert.deepEqual(k.payouts[0].combination,[4,4]);assert.equal(k.refund_evidence.source_flags.APartReturnDispFlg,true);
 const boat=p.parseResult(boatResult,targets.boat);assert.equal(boat.placings[1].rank_label,'F');assert.equal(boat.refund_evidence.display,'2');
});
test('pending results and special payout displays never become fabricated winning combinations',()=>{
 assert.equal(p.parseResult(autoResult(true),targets.auto).publication,'PENDING');assert.equal(p.parseResult(keirinResult(true),targets.keirin).publication,'PENDING');
 const v=JSON.parse(keirinResult());v.haraiGakuSubData.RT3HaraiGakuDispItemSubData=[{kumiDispFlg:false,haraiGaku:'特払 70円'}];
 const r=p.parseResult(JSON.stringify(v),targets.keirin),special=r.payouts.find(v=>v.market==='trifecta');
 const a=JSON.parse(autoResult());a.body.refundInfo.rt3.typeCode=1;a.body.refundInfo.rt3.typeName='synthetic special';
 a.body.refundInfo.rf3.typeCode=2;a.body.refundInfo.rf3.typeName='synthetic refund';a.body.refundInfo.rf3.list=[];
 const entries=p.parseResult(JSON.stringify(a),targets.auto).payouts.filter(v=>['trifecta','trio'].includes(v.market));
 assert.ok(entries.every(v=>v.status==='DISPLAY_ONLY'&&v.combination===null));
 assert.deepEqual(entries.map(v=>v.display),['synthetic special 1,230円','synthetic refund']);
 assert.equal(special.status,'DISPLAY_ONLY');assert.equal(special.combination,null);assert.equal(special.amount_yen,null);assert.equal(special.display,'特払 70円');
});
test('missing markets and duplicate numeric payouts are rejected without reducing expected coverage',()=>{
 const v=JSON.parse(autoResult());delete v.body.refundInfo.rt3;assert.throws(()=>p.parseResult(JSON.stringify(v),targets.auto),/RESULT_MARKET/);
 const k=JSON.parse(keirinResult());k.haraiGakuSubData.ST2HaraiGakuDispItemSubData.push(k.haraiGakuSubData.ST2HaraiGakuDispItemSubData[0]);
 assert.throws(()=>p.parseResult(JSON.stringify(k),targets.keirin),/RESULT_DUPLICATE/);
 assert.throws(()=>p.parseResult(boatResult.replace('3連単','unknown'),targets.boat),/RESULT_MARKET/);
});
