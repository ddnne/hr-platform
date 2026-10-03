/** Published results are evaluation-only. A payout table does not certify refund coverage. */
import {text} from './parsers';
import jra from '../../configs/jra-source.json';
import {jraMetadata,type JraSnapshot} from '../jra';
import {jraLinks,jraNavigationIdentity} from '../jra-program';
import type {ResultSnapshot,CaptureTarget,JraResultTarget} from './types';
export type JraResultSnapshot=ResultSnapshot & {sport:'jra';metadata:JraSnapshot['metadata'];result_navigation:Record<string,string>};
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
function base(t:CaptureTarget):ResultSnapshot {
 return {schema:'sports-result-v1',sport:t.sport,race_id:t.race_id,phase:'RESULT_ONLY',publication:'PENDING',
  source_updated_at:null,source_published_at:null,source_time_label:null,identity_status:'REQUEST_BOUND',identity_evidence:null,
  payout_unit_yen:null,settlement_qualified:false,placings:[],payouts:[],refund_evidence:{display:null,source_flags:{}}};
}
export function supportsResult(t:CaptureTarget):boolean {
 const u=new URL(t.url);return t.kind==='result'&&(t.sport==='auto'&&u.pathname==='/race_info/RaceResult'
  ||t.sport==='boat'&&u.pathname==='/owpc/pc/race/raceresult'
  ||t.sport==='keirin'&&u.pathname==='/pc/json'&&u.searchParams.get('type')==='JSJ012'
  ||t.sport==='jra'&&u.pathname===jra.result_path);
}
function divBlocks(raw:string,cls:string):string[] {
 const blocks:string[]=[];
 for(const open of raw.matchAll(/<div\b([^>]*)>/gi)){
  const classes=open[1].match(/\bclass=["']([^"']*)["']/i)?.[1].split(/\s+/)??[];
  if(!classes.includes(cls))continue;
  const tail=raw.slice(open.index!+open[0].length);let depth=1,end:number|undefined;
  for(const tag of tail.matchAll(/<\/?div\b[^>]*>/gi)){depth+=tag[0].startsWith('</')?-1:1;if(!depth){end=tag.index!;break;}}
  if(end===undefined)throw new Error('RESULT_MARKET');blocks.push(tail.slice(0,end));
 }
 return blocks;
}
function jraResult(raw:string,t:JraResultTarget,result:ResultSnapshot):JraResultSnapshot {
 const name=new URLSearchParams(t.body).get('cname')??'',id=jraNavigationIdentity(name,'result');
 if(t.race_id!==`jra:${id.day}:${id.venue}:${id.race}`)throw new Error('RESULT_IDENTITY');
 const metadata=jraMetadata(raw,t.race_id);
 const ranks=[...raw.matchAll(/<table\b[^>]*>([\s\S]*?)<\/table>/gi)].filter(m=>
  /<th\b[^>]*class=["']place["'][^>]*>着順<\/th>/.test(m[1])&&/<th\b[^>]*class=["']num["'][^>]*>馬(?:<br\s*\/?>)?番<\/th>/.test(m[1]));
 if(ranks.length!==1)throw new Error('RESULT_NOT_READY');
 for(const row of ranks[0][1].matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
  const cells=new Map([...row[1].matchAll(/<td\b[^>]*class=["']([^"']+)["'][^>]*>([\s\S]*?)<\/td>/gi)].map(m=>[m[1],m[2]]));
  if(!cells.has('num'))continue;
  if(!cells.has('place'))throw new Error('RESULT_PLACINGS');
  const p=placing(text(cells.get('num')!),text(cells.get('place')!));
  if(p.entrant>jra.maximum_entrants)throw new Error('RESULT_ENTRANT');result.placings.push(p);
 }
 if(!result.placings.length)throw new Error('RESULT_NOT_READY');
 // Scope to the payout unit. Generic footer notes about special payouts are not events.
 const units=divBlocks(raw,'refund_unit');if(units.length!==1)throw new Error('RESULT_MARKET');const unit=units[0];
 const names:Record<string,string>={'単勝':'win','複勝':'place','枠連':'frame_quinella','馬連':'quinella','ワイド':'wide','馬単':'exacta','3連複':'trio','3連単':'trifecta'};
 const seen=new Set<string>();
 for(const dl of unit.matchAll(/<dl\b[^>]*>([\s\S]*?)<\/dl>/gi)){
  const label=text(dl[1].match(/<dt\b[^>]*>([\s\S]*?)<\/dt>/i)?.[1]??''),market=names[label];
  if(!market||seen.has(market))throw new Error('RESULT_MARKET');seen.add(market);
  const lines=divBlocks(dl[1],'line');if(!lines.length)throw new Error('RESULT_MARKET');
  for(const line of lines){
   const nums=divBlocks(line,'num'),values=divBlocks(line,'yen');
   if(nums.length!==1||values.length!==1)throw new Error('RESULT_MARKET');
   const p=payout(market,nums[0],values[0]);
   if(p.combination?.some(n=>n>(market==='frame_quinella'?jra.maximum_frames:jra.maximum_entrants)))throw new Error('RESULT_ENTRANT');
   result.payouts.push(p);
  }
 }
 if(!seen.size)throw new Error('RESULT_MARKET');
 result.identity_status='DOCUMENT_VERIFIED';result.identity_evidence=name;
 result.refund_evidence.display=/返還|取消|除外|特払/.test(text(unit))?text(unit):null;
 result.refund_evidence.source_flags.refund_coverage_verified=false;
 const result_navigation:Record<string,string>={};
 for(const link of jraLinks(raw,jra.result_path).filter(l=>l.name.startsWith('pw01sde'))){
  const id=jraNavigationIdentity(link.name,'result');if(id.day!==metadata.race_date)continue;
  const race=`jra:${id.day}:${id.venue}:${id.race}`;
  if(result_navigation[race]&&result_navigation[race]!==link.name)throw new Error('RESULT_DUPLICATE');result_navigation[race]=link.name;
 }
 return {...result,sport:'jra',metadata,result_navigation};
}
export function parseResult(raw:string,t:CaptureTarget):ResultSnapshot|JraResultSnapshot {
 if(!supportsResult(t))throw new Error('RESULT_RESOURCE');const result=base(t);
 let parsed:ResultSnapshot|JraResultSnapshot=result;
 if(t.sport==='jra'){
  if(t.kind!=='result')throw new Error('RESULT_RESOURCE');parsed=jraResult(raw,t,result);
 }else if(t.sport==='auto'){
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
 if(new Set(parsed.placings.map(p=>p.entrant)).size!==parsed.placings.length)throw new Error('RESULT_DUPLICATE');
 const keys=parsed.payouts.filter(p=>p.status==='NUMERIC').map(p=>p.market+':'+p.combination!.join('-'));
 if(new Set(keys).size!==keys.length)throw new Error('RESULT_DUPLICATE');
 if(parsed.placings.length||parsed.payouts.some(p=>p.amount_yen!==null))parsed.publication='PUBLISHED';
 return parsed;
}
