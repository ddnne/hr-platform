/** Published results are evaluation-only. A payout table does not certify refund coverage. */
import {text} from './parsers';
import type {ResultSnapshot,Target} from './types';
const specs:Record<string,[number,boolean]>={win:[1,false],place:[1,false],exacta:[2,true],quinella:[2,false],wide:[2,false],
 trifecta:[3,true],trio:[3,false],frame_exacta:[2,true],frame_quinella:[2,false]};
const keirin={frame_quinella:'WH2',frame_exacta:'WT2',quinella:'SH2',exacta:'ST2',trio:'RH3',trifecta:'RT3',wide:'W'};
const auto={exacta:'rtw',quinella:'rfw',trifecta:'rt3',trio:'rf3',wide:'wid',win:'tns',place:'fns'};
const boat:Record<string,string>={'3連単':'trifecta','3連複':'trio','2連単':'exacta','2連複':'quinella','拡連複':'wide','単勝':'win','複勝':'place'};
function amount(value:unknown):number|null {
 const s=String(value??'').normalize('NFKC').replaceAll('&yen;','¥').replaceAll(',','').replace(/\s/g,'');
 if(!/^[¥￥]?\d+(?:円)?$/.test(s))return null;
 const n=Number(s.replace(/[¥￥円]/g,''));return Number.isSafeInteger(n)?n:null;
}
function payout(market:string,label:unknown,value:unknown):ResultSnapshot['payouts'][number] {
 const combination_label=text(String(label??'')).normalize('NFKC'),display=text(String(value??'')).replaceAll('&yen;','¥');
 const [size,ordered]=specs[market],ids=combination_label.split(/\s*[-=]\s*/).map(Number);
 const valid=ids.length===size&&ids.every(id=>Number.isInteger(id)&&id>0)&&
  (market.startsWith('frame_')||new Set(ids).size===ids.length);
 const combination=valid?(ordered?ids:ids.sort((a,b)=>a-b)):null,amount_yen=amount(display);
 return {market,combination,combination_label,display,amount_yen,status:combination&&amount_yen!==null&&amount_yen>0?'NUMERIC':'DISPLAY_ONLY'};
}
function placing(entrant:unknown,rankLabel:unknown,state:unknown=null):ResultSnapshot['placings'][number] {
 const id=Number(entrant);if(!Number.isInteger(id)||id<1)throw new Error('RESULT_ENTRANT');
 const rank_label=text(String(rankLabel??'')).normalize('NFKC'),rank=/^[1-9]\d*$/.test(rank_label)?Number(rank_label):null;
 return {entrant:id,rank_label,rank,state_label:state===null?null:String(state)};
}
function base(t:Target):ResultSnapshot {
 return {schema:'sports-result-v1',sport:t.sport,race_id:t.race_id,phase:'RESULT_ONLY',publication:'PENDING',
  source_updated_at:null,source_published_at:null,source_time_label:null,identity_status:'REQUEST_BOUND',identity_evidence:null,
  payout_unit_yen:null,settlement_qualified:false,placings:[],payouts:[],refund_evidence:{display:null,source_flags:{}}};
}
export function supportsResult(t:Target):boolean {
 const u=new URL(t.url);return t.kind==='result'&&(t.sport==='auto'&&u.pathname==='/race_info/RaceResult'
  ||t.sport==='boat'&&u.pathname==='/owpc/pc/race/raceresult'
  ||t.sport==='keirin'&&u.pathname==='/pc/json'&&u.searchParams.get('type')==='JSJ012');
}
export function parseResult(raw:string,t:Target):ResultSnapshot {
 if(!supportsResult(t))throw new Error('RESULT_RESOURCE');const result=base(t);
 if(t.sport==='auto'){
  const v=JSON.parse(raw),b=v.body;if(v.result!=='Success'||!b||!Array.isArray(b.raceResult)||!b.refundInfo)throw new Error('RESULT_NOT_READY');
  result.placings=b.raceResult.map((p:any)=>placing(p.carNo,p.order,p.accidentName??p.accidentCode));
  for(const [market,key] of Object.entries(auto)){
   const m=b.refundInfo[key];if(!m||!Number.isInteger(m.typeCode)||!Array.isArray(m.list))throw new Error('RESULT_MARKET');
   result.refund_evidence.source_flags[market+'.type_code']=String(m.typeCode);
   result.refund_evidence.source_flags[market+'.type_name']=m.typeName??null;
   if(![0,5].includes(m.typeCode)){
    const display=text(String(m.typeName??''))+(m.typeCode===1&&m.list.length?' '+String(m.list[0].refund??'')+'円':'');
    result.payouts.push({...payout(market,'',display),combination:null,status:'DISPLAY_ONLY'});continue;
   }
   for(const p of m.list){const [size]=specs[market];const ids=size===1?[p.carNo]:[p['1thCarNo'],p['2thCarNo'],...(size===3?[p['3thCarNo']]:[])];
    result.payouts.push(payout(market,ids.join('-'),p.refund));}
  }
 }else if(t.sport==='keirin'){
  const v=JSON.parse(raw);if(v.resultCd!==0||typeof v.haraiGakuDispFlg!=='boolean'||typeof v.tyakujyunDispFlg!=='boolean')throw new Error('RESULT_NOT_READY');
  result.source_time_label=typeof v.lastUpdateTime==='string'?v.lastUpdateTime:null;
  if(v.tyakujyunDispFlg){if(!Array.isArray(v.tyakujyunItemSubData))throw new Error('RESULT_PLACINGS');
   result.placings=v.tyakujyunItemSubData.map((p:any)=>placing(p.syaban,p.tyaku));}
  if(v.haraiGakuDispFlg){for(const [market,key] of Object.entries(keirin)){
   const items=v.haraiGakuSubData?.[key+'HaraiGakuDispItemSubData'];if(!Array.isArray(items))throw new Error('RESULT_MARKET');
   for(const p of items)result.payouts.push(payout(market,p.kumiDispFlg?p.kumiBan:'',p.haraiGaku));}
   const flag=v.haraiGakuSubData.APartReturnDispFlg;result.refund_evidence.source_flags.APartReturnDispFlg=typeof flag==='boolean'?flag:null;
  }
 }else {
  const tables=[...raw.matchAll(/<table\b[^>]*>([\s\S]*?)<\/table>/gi)].map(m=>m[1]);
  const ranks=tables.find(s=>/<th[^>]*>着<\/th>/.test(s));if(!ranks)throw new Error('RESULT_NOT_READY');
  for(const m of ranks.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
   const cells=[...m[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/gi)];if(cells.length>=2)result.placings.push(placing(text(cells[1][1]),text(cells[0][1])));
  }
  const table=tables.find(s=>s.includes('is-payout1'));if(!table)throw new Error('RESULT_MARKET');
  let market='';const seen=new Set<string>();
  for(const m of table.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
   const cells=[...m[1].matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/gi)].map(x=>x[1]);if(!cells.length)continue;
   if(boat[text(cells[0])]){market=boat[text(cells.shift()!)];seen.add(market);}
   const value=m[1].match(/<span\b[^>]*class="is-payout1"[^>]*>([\s\S]*?)<\/span>/)?.[1];
   if(market&&value!==undefined&&text(value))result.payouts.push(payout(market,text(cells[0]),text(value)));
  }
  if(seen.size!==Object.keys(boat).length)throw new Error('RESULT_MARKET');
  const refund=tables.find(s=>/<th[^>]*>返還<\/th>/.test(s));result.refund_evidence.display=refund?text(refund.replace(/<thead\b[^>]*>[\s\S]*?<\/thead>/gi,'')):null;
 }
 if(new Set(result.placings.map(p=>p.entrant)).size!==result.placings.length)throw new Error('RESULT_DUPLICATE');
 const keys=result.payouts.filter(p=>p.status==='NUMERIC').map(p=>p.market+':'+p.combination!.join('-'));
 if(new Set(keys).size!==keys.length)throw new Error('RESULT_DUPLICATE');
 if(result.placings.length||result.payouts.some(p=>p.amount_yen!==null))result.publication='PUBLISHED';
 return result;
}
