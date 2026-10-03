import test from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {jraResult,resultTarget} from '../fixtures/synthetic/jra.mjs';
const b=await build({stdin:{contents:`export {parseResult} from './workers/sports/results';export {validateTarget} from './workers/sports/capture';export {completePayoutMarkets} from './workers/sports/storage';`,resolveDir:process.cwd()},bundle:true,write:false,format:'esm',platform:'node'});
const p=await import('data:text/javascript;base64,'+Buffer.from(b.outputFiles[0].text).toString('base64'));
const markets=['win','place','frame_quinella','quinella','wide','exacta','trio','trifecta'];
test('official result structure preserves eight payout markets, placings and published navigation',()=>{
 p.validateTarget(resultTarget);const v=p.parseResult(jraResult(),resultTarget);
 assert.equal(v.phase,'RESULT_ONLY');assert.equal(v.publication,'PUBLISHED');assert.equal(v.identity_status,'DOCUMENT_VERIFIED');
 assert.deepEqual(v.placings.map(x=>x.rank),[1,2,3,null]);assert.equal(v.placings[3].rank_label,'取消');
 assert.deepEqual(v.placings.map(x=>x.frame),[1,2,3,3]);
 assert.equal(v.payouts.length,12);assert.ok(markets.every(m=>v.payouts.some(x=>x.market===m&&x.amount_yen===1230)));
 assert.equal(v.metadata.flat,true);assert.equal(v.source_updated_at,null);assert.equal(v.source_published_at,null);
 assert.equal(v.settlement_qualified,false);assert.equal(v.payout_unit_yen,null);assert.equal(v.refund_evidence.source_flags.refund_coverage_verified,false);
 assert.equal(v.refund_evidence.display,null);assert.equal(p.completePayoutMarkets(v,markets),true);
 assert.deepEqual(Object.keys(v.result_navigation),['jra:20000101:5:1','jra:20000101:5:2']);
});
test('frame payout matches the leading frames, including the same-frame combination',()=>{
 const good=p.parseResult(jraResult(),resultTarget),wrong=structuredClone(good);
 const colored=p.parseResult(jraResult({frameColors:true}),resultTarget);
 assert.deepEqual(colored.placings,good.placings);assert.equal(p.completePayoutMarkets(colored,markets),true);
 wrong.payouts.find(x=>x.market==='frame_quinella').combination=[1,3];assert.equal(p.completePayoutMarkets(wrong,markets),false);
 const same=structuredClone(good);same.placings[1].frame=1;same.payouts.find(x=>x.market==='frame_quinella').combination=[1,1];
 assert.equal(p.completePayoutMarkets(same,markets),true);
 const duplicate=structuredClone(good);duplicate.payouts.push({...duplicate.payouts.find(x=>x.market==='frame_quinella')});
 assert.equal(p.completePayoutMarkets(duplicate,markets),false);
 for(const label of ['','<img alt="枠9" />','<img alt="枠1黒" />','<img alt="枠1" /><img alt="枠2" />','<img data-alt="枠1" />',`<img data-note="note alt='枠1'" />`]){
  const incomplete=p.parseResult(jraResult().replace('<img alt="枠1" />',label),resultTarget);
  assert.equal(incomplete.placings[0].frame,null);assert.equal(incomplete.publication,'PUBLISHED');
  assert.equal(p.completePayoutMarkets(incomplete,markets),false);assert.equal(incomplete.settlement_qualified,false);
 }
});
test('ties, special payouts and missing markets retain uncertainty without inventing combinations',()=>{
 const tie=p.parseResult(jraResult({tie:true}),resultTarget);assert.deepEqual(tie.placings.map(x=>x.rank),[1,1,3,null]);
 const special=p.parseResult(jraResult({special:true}),resultTarget),s=special.payouts.find(x=>x.market==='trifecta');
 assert.equal(s.status,'DISPLAY_ONLY');assert.equal(s.combination,null);assert.equal(s.combination_label,'特払');assert.equal(s.amount_yen,70);
 assert.equal(p.completePayoutMarkets(special,markets),false);assert.ok(special.refund_evidence.display.includes('特払'));
 const missing=p.parseResult(jraResult({missing:true}),resultTarget);assert.equal(p.completePayoutMarkets(missing,markets),false);
});
test('whole missing or incorrect winning rows cannot complete daily result collection',()=>{
 const good=p.parseResult(jraResult(),resultTarget);
 const missing=structuredClone(good);missing.payouts=missing.payouts.filter(x=>x.market!=='wide'||x.combination.join('-')!=='2-10');
 assert.equal(p.completePayoutMarkets(missing,markets),false);
 const wrong=structuredClone(good);wrong.payouts.find(x=>x.market==='quinella').combination=[1,11];
 assert.equal(p.completePayoutMarkets(wrong,markets),false);
 const tied=structuredClone(good);tied.placings[3].rank=3;assert.equal(p.completePayoutMarkets(tied,markets),false);
 const paidTwo=structuredClone(good);paidTwo.payouts=paidTwo.payouts.filter(x=>x.market!=='place'||x.combination[0]!==10);
 assert.equal(p.completePayoutMarkets(paidTwo,markets),false);
 assert.equal(p.completePayoutMarkets(paidTwo,markets,2),true);
 assert.equal(p.completePayoutMarkets(paidTwo,markets,3),false);
 assert.equal(p.completePayoutMarkets(good,markets,2),false);
});
test('incorrect identity, malformed payout rows and duplicate results fail before publication',()=>{
 assert.throws(()=>p.validateTarget({...resultTarget,race_id:'jra:20000101:5:2'}),/IDENTITY/);
 assert.throws(()=>p.parseResult(jraResult().replace('2000年1月1日','2000年1月2日'),resultTarget),/IDENTITY/);
 assert.throws(()=>p.validateTarget({...resultTarget,url:resultTarget.url.replace('accessS','accessO')}),/ORIGIN/);
 assert.throws(()=>p.validateTarget({...resultTarget,deadline_at:1}),/RESOURCE/);
 assert.throws(()=>p.parseResult(jraResult().replace('<div class="yen">','<div class="unknown">'),resultTarget),/MARKET/);
 const broken=jraResult().replace(/<dl><dt>複勝<\/dt>[\s\S]*?<\/dl>/,part=>
  part.replace('<div class="yen">1,230<span class="unit">円</span></div>','').replace('<div class="num">2</div>',''));
 assert.throws(()=>p.parseResult(broken,resultTarget),/MARKET/);
 assert.throws(()=>p.parseResult(jraResult().replace('class="num">2</td>','class="num">1</td>'),resultTarget),/DUPLICATE/);
 assert.throws(()=>p.parseResult(jraResult().replace('20000101/BB','20000101/CC')+'<a href="/JRADB/accessS.html?CNAME=pw01sde1005200001010220000101/BB">合成重複</a>',resultTarget),/DUPLICATE/);
});
