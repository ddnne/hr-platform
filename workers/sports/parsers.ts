/** Pure provider adapters. Missing combinations remain missing; zero is a display, not a probability. */
import type {Market,Quote,Snapshot,Target} from './types';
export const text = (s:string) => s.replace(/<br\s*\/?\s*>/gi,' ').replace(/<[^>]*>/g,'').replace(/&nbsp;|&#160;/g,' ').replace(/&amp;/g,'&').replace(/\s+/g,' ').trim();
export function quote(combination:number[], value:unknown): Quote {
 const display = typeof value === 'object' && value !== null
   ? `${(value as any).min} - ${(value as any).max}` : text(String(value ?? ''));
 const range = display.match(/^(\d+(?:\.\d+)?)\s*(?:-|～|~)\s*(\d+(?:\.\d+)?)$/);
 const scalar = /^\d+(?:\.\d+)?$/.test(display) ? Number(display) : null;
 const lower = range ? Number(range[1]) : scalar, upper = range ? Number(range[2]) : scalar;
 if(lower !== null && upper !== null && lower > upper) throw new Error('REVERSED_RANGE');
 return {combination,display,lower,upper,status:lower === 0 || upper === 0 ? 'ZERO_DISPLAY' : lower === null ? 'UNAVAILABLE' : 'NUMERIC'};
}
export function combinations(ids:number[], size:number, ordered=false):number[][] {
 if(size === 0) return [[]];
 return ids.flatMap((id,i)=>combinations(ordered ? ids.filter(x=>x!==id) : ids.slice(i+1),size-1,ordered).map(t=>[id,...t]));
}
export function market(name:string, quotes:Quote[], support:number[][], label:string|null=null):Market {
 const keys=quotes.map(q=>q.combination.join('-')), expected=new Set(support.map(c=>c.join('-')));
 if(new Set(keys).size !== keys.length || keys.some(k=>!expected.has(k))) throw new Error('COMBINATION_IDENTITY');
 return {market:name,quotes,expected:expected.size,complete:keys.length === expected.size,source_time_label:label};
}
function snapshot(t:Target, phase:Snapshot['phase'], markets:Market[], label:string|null, semantics:string):Snapshot {
 return {schema:'sports-odds-v1',sport:t.sport,race_id:t.race_id,phase,source_updated_at:null,
   source_published_at:null,time_semantics:semantics,markets};
}
const maps = {exacta:['rtwOddsList',2,true],quinella:['rfwOddsList',2,false],trifecta:['rt3OddsList',3,true],trio:['rf3OddsList',3,false],wide:['widOddsList',2,false],win:['tnsOddsList',1,false],place:['fnsOddsList',1,false]} as const;
export function parseAuto(raw:string,t:Target):Snapshot {
 const v=JSON.parse(raw); if(v.result!=='Success' || !v.body || ![0,1].includes(v.body.statusCode)) throw new Error('PROVIDER_NOT_READY');
 const b=v.body, ids=b.playerList.map((p:any)=>Number(p.carNo)).sort((a:number,b:number)=>a-b);
 if(ids.length<3 || ids.some((x:number)=>!Number.isInteger(x)||x<1||x>8) || new Set(ids).size!==ids.length) throw new Error('ENTRANTS');
 const label=typeof b.salesInfo?.updateDate==='string'?b.salesInfo.updateDate:null;
 const markets=Object.entries(maps).map(([name,[key,size,ordered]])=>{
  const q:Quote[]=[];
  const walk=(obj:any,path:number[])=>{if(path.length===size){q.push(quote(path,obj));return;}
   if(!obj || typeof obj!=='object') return;
   for(const [id,x] of Object.entries(obj)) if(/^\d+$/.test(id)) walk(x,[...path,Number(id)]);};
  walk(b[key],[]);return market(name,q,combinations(ids,size,ordered),label);
 });
 return snapshot(t,b.statusCode===1?'FINAL_ONLY':'INTERMEDIATE',markets,label,'provider display salesInfo.updateDate; update/publish semantics unverified');
}
const keirinMarkets:Record<string,[string,number,boolean]>={trifecta:['ozz3RentanData',3,true],exacta:['ozz2SyatanData',2,true],trio:['ozz3RenhukuData',3,false],quinella:['ozz2SyahukuData',2,false],frame_exacta:['ozz2WakutanData',2,true],frame_quinella:['ozz2WakuhukuData',2,false],wide:['ozzWideData',2,false]};
export function parseKeirin(raw:string,t:Target):Snapshot {
 const v=JSON.parse(raw), spec=keirinMarkets[t.market??''];
 if(v.resultCd!==0 || !v.data || v.data.karaGamenFlg!=='0' || !spec || !t.entrants) throw new Error('PROVIDER_NOT_READY');
 const [key,size,ordered]=spec, values=v.data[key];if(!values || typeof values!=='object') throw new Error('MARKET_MISSING');
 let support=combinations(t.entrants,size,ordered);
 if(t.market?.startsWith('frame_')) {
  if(!t.frames || t.entrants.some(id=>!t.frames![String(id)])) throw new Error('FRAMES');
  const keys=new Set(combinations(t.entrants,2,ordered).map(c=>{const f=c.map(id=>t.frames![String(id)]);if(!ordered)f.sort((a,b)=>a-b);return f.join('-');}));
  support=[...keys].map(k=>k.split('-').map(Number));
 }
 const q=t.market==='wide' ? Object.entries(values).filter(([k])=>/^DN_OZZ\d+$/.test(k)).map(([k,value])=>quote([...k.slice(6)].map(Number),{min:value,max:values['TP_'+k.slice(3)]}))
  : Object.entries(values).filter(([k])=>/^OZZ\d+$/.test(k)).map(([k,value])=>quote([...k.slice(3)].map(Number),value));
 const label=typeof values.UP_DATE==='string'?values.UP_DATE:null;
 // endFlg indicates ended, not proof of final payout/refunds.
 return snapshot(t,v.data.endFlg==='1'?'CLOSE_ONLY':v.data.endFlg==='0'?'INTERMEDIATE':'UNKNOWN',
  [market(t.market!,q,support,label)],label,'provider display UP_DATE; update/publish semantics unverified');
}
type Cell={value:string; odds:boolean};
function grid(section:string):Cell[][] {
 const rows:Cell[][]=[];
 for(const [ri,row] of [...section.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)].entries()) {
  rows[ri]??=[];let col=0;
  for(const m of row[1].matchAll(/<(td|th)\b([^>]*)>([\s\S]*?)<\/\1>/gi)) {
   while(rows[ri][col])col++;
   const rs=Number(m[2].match(/rowspan=["']?(\d+)/i)?.[1]??1),cs=Number(m[2].match(/colspan=["']?(\d+)/i)?.[1]??1);
   if(rs>30||cs>24)throw new Error('TABLE_SHAPE');
   const cell={value:text(m[3]),odds:/\boddsPoint\b/.test(m[2])};
   for(let y=ri;y<ri+rs;y++){rows[y]??=[];for(let x=col;x<col+cs;x++)rows[y][x]=cell;}
   col+=cs;
  }
 }
 return rows;
}
const boatLabels:Record<string,string>={trifecta:'3連単オッズ',trio:'3連複オッズ',exacta:'2連単オッズ',quinella:'2連複オッズ',wide:'拡連複オッズ',win:'単勝オッズ',place:'複勝オッズ'};
export function parseBoat(raw:string,t:Target):Snapshot {
 const names=t.url.includes('odds2tf')?['exacta','quinella']:t.url.includes('oddstf')?['win','place']:[t.market??''];
 const timeParagraph=[...raw.matchAll(/<p\b([^>]*)>([\s\S]*?)<\/p>/gi)].find(m=>{
  const attributes=[...m[1].matchAll(/([^\s="'<>/]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))/g)];
  const classes=attributes.find(a=>a[1].toLowerCase()==='class');
  return (classes?.[2]??classes?.[3]??classes?.[4]??'').split(/\s+/).some(c=>c==='tab4_time'||c==='tab4_refreshText');
 });
 const label=text(timeParagraph?.[2]??'')||null;
 const phase=label?.includes('締切時')?'CLOSE_ONLY':label?.includes('オッズ更新時間')?'INTERMEDIATE':'UNKNOWN';
 const ids=[1,2,3,4,5,6];
 const markets=names.map(name=>{
  const start=raw.indexOf(`>${boatLabels[name]}<`);if(start<0)throw new Error('MARKET_MISSING');
  const table=raw.slice(start).match(/<table\b[^>]*>([\s\S]*?)<\/table>/i)?.[1];if(!table)throw new Error('MARKET_MISSING');
  const head=grid(table.match(/<thead\b[^>]*>([\s\S]*?)<\/thead>/i)?.[1]??'')[0];
  const body=grid(table.replace(/<thead\b[^>]*>[\s\S]*?<\/thead>/gi,''));
  const size=name==='trifecta'||name==='trio'?3:name==='win'||name==='place'?1:2;
  const ordered=name==='trifecta'||name==='exacta', q:Quote[]=[];
  for(const row of body)for(let col=0;col<row.length;col++)if(row[col]?.odds){
   let combination:number[];
   if(size===1) combination=[Number(row[0].value)];
   else {const width=size===3?3:2, group=Math.floor(col/width)*width;
    combination=[Number(head?.[group]?.value),...(size===3?[Number(row[col-2]?.value)]:[]),Number(row[col-1]?.value)];}
   // Blank cells represent nonexistent permutations in triangular tables.
   if(combination.some(x=>!Number.isInteger(x)||x<1||x>6))throw new Error('TABLE_IDENTITY');
   q.push(quote(combination,row[col].value));
  }
  return market(name,q,combinations(ids,size,ordered),label);
 });
 return snapshot(t,phase,markets,null,'odds time paragraph is a provider display label; exact update/publish time unknown');
}
export function parseOdds(raw:string,target:Target):Snapshot {
 if(target.sport==='auto')return parseAuto(raw,target);
 if(target.sport==='keirin')return parseKeirin(raw,target);
 return parseBoat(raw,target);
}
