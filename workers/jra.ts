/** Pure JRA odds adapter. Collection uses the shared Sports capture path. */
import config from '../configs/jra-source.json';
import {combinations,market,quote,text} from './sports/parsers';
import {date,programClock} from './sports/program-clock';
import type {Market,Quote,Snapshot} from './sports/types';
export type JraPage = keyof typeof config.tables;
export type JraRunners={entrants:number[];frames:Record<string,number>};
export type JraSnapshot=Omit<Snapshot,'sport'> & {sport:'jra';metadata:{race_date:string;venue:number;scheduled_start_at:string|null;course_label:string|null;race_type_label:string|null;flat:boolean|null};runners:JraRunners|null;place_paid_positions:number|null};
const attribute=(raw:string,name:string)=>{
 const values=[...raw.matchAll(/([^\s=\/"'<>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)].filter(m=>m[1].toLowerCase()===name.toLowerCase());
 return values.length===1?values[0].slice(2).find(x=>x!==undefined)??null:null;
};
const classes=(raw:string)=>(attribute(raw,'class')??'').split(/\s+/);
function tables(raw:string,name:string){return [...raw.matchAll(/<table\b([^>]*)>([\s\S]*?)<\/table>/gi)].filter(m=>classes(m[1]).includes(name));}
function frameSupport(r:JraRunners){return [...new Set(combinations(r.entrants,2).map(c=>c.map(h=>r.frames[h]).sort((a,b)=>a-b).join('-')))].map(k=>k.split('-').map(Number));}
function support(r:JraRunners){
 if(r.entrants.length<3||r.entrants.length>config.maximum_entrants||new Set(r.entrants).size!==r.entrants.length||
  r.entrants.some(h=>!Number.isInteger(h)||h<1||h>config.maximum_entrants||!Number.isInteger(r.frames[h])||r.frames[h]<1||r.frames[h]>config.maximum_frames))throw new Error('JRA_RUNNERS');
 return [...r.entrants].sort((a,b)=>a-b);
}
export function decodeJra(body:Uint8Array):string {return new TextDecoder(config.charset,{fatal:true,ignoreBOM:false}).decode(body);}
export function jraFrame(raw:string|undefined):number|null {
 const frames=[...(raw??'').matchAll(/<img\b([^>]*)>/gi)].map(m=>{
  const label=attribute(m[1],'alt'),entry=Object.entries(config.frame_labels).find(([,labels])=>labels.includes(label??''));
  return entry?Number(entry[0]):null;
 }).filter((frame):frame is number=>frame!==null);
 return frames.length===1&&frames[0]>=1&&frames[0]<=config.maximum_frames?frames[0]:null;
}
export function jraMetadata(raw:string,raceId:string):JraSnapshot['metadata'] {
 const ids=[...raw.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1>/gi)].map(m=>text(m[1]).match(/(\d{4})年(\d{1,2})月(\d{1,2})日[^\d]*\d+回([^\d]+)\d+日\s*(\d+)レース/)).filter(m=>m!==null);
 if(ids.length!==1)throw new Error('JRA_IDENTITY');const id=ids[0];
 const day=date(id[1]+id[2].padStart(2,'0')+id[3].padStart(2,'0'));
 const venue=Number(Object.entries(config.venues).find(([,name])=>name===id[4])?.[0]);
 if(!venue||raceId!==`jra:${day}:${venue}:${Number(id[5])}`)throw new Error('JRA_IDENTITY');
 const courseMatch=[...raw.matchAll(/<div\b([^>]*)>([^]*?)<\/div>/gi)].find(m=>classes(m[1]).includes('course'));
 const course=courseMatch?text(courseMatch[2]):null;
 const name=raw.match(/<span\b[^>]*class=["']race_name["'][^>]*>([\s\S]*?)<\/span>/i)?.[1];
 const category=raw.match(/<div\b[^>]*class=["']cell category["'][^>]*>([\s\S]*?)<\/div>/i)?.[1];
 const type=name&&category?text(name)+' '+text(category):null;
 const flat=course===null||type===null?null:/障害/.test(course+' '+type)?false:/芝|ダート/.test(course)?true:null;
 const start=raw.match(/発走時刻[：:]\s*<strong\b[^>]*>(\d{1,2})時(\d{2})分<\/strong>/)?.slice(1);
 return {race_date:day,venue,scheduled_start_at:start?programClock(day,start.join(':')):null,course_label:course,race_type_label:type,flat};
}
export function parseJra(raw:string,raceId:string,page:JraPage,known?:JraRunners):JraSnapshot {
 if(!Object.hasOwn(config.tables,page))throw new Error('JRA_PAGE');
 const metadata=jraMetadata(raw,raceId);
 const time=raw.match(/<div\b[^>]*class=["']refresh_line["'][^>]*>[\s\S]*?<div\b[^>]*class=["']cell time["'][^>]*>([\s\S]*?)<\/div>/i)?.[1];
 const label=time?text(time):null,phase:Snapshot['phase']=label==='最終オッズ'?'FINAL_ONLY':label&&/\d+時\d+分.*現在/.test(label)?'INTERMEDIATE':'UNKNOWN';
 let runners: JraRunners|null=known??null,place_paid_positions:number|null=null;const markets:Market[]=[];
 if(page==='win_place'){
  const ts=tables(raw,config.tables.win_place);if(ts.length!==1)throw new Error('JRA_TABLE');
  const paid=[...ts[0][2].matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/gi)].map(m=>text(m[1]).match(/^複勝\s*[（(]\s*([23])着払い\s*[）)]$/)).filter(m=>m);
  if(paid.length===1)place_paid_positions=Number(paid[0]![1]);
  const entrants:number[]=[],frames:Record<string,number>={},win:Quote[]=[],place:Quote[]=[];let frame:number|null=null,remaining=0;
  for(const row of ts[0][2].matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
   const tags=[...row[1].matchAll(/<td\b([^>]*)>([\s\S]*?)<\/td>/gi)],cells=new Map(tags.map(c=>[attribute(c[1],'class'),c[2]]));
   if(!cells.has('num'))continue;const horse=Number(text(cells.get('num')!));
   if(cells.has('waku')){
    if(remaining)throw new Error('JRA_FRAME_SPAN');frame=jraFrame(cells.get('waku'));
    remaining=Number(attribute(tags.find(c=>attribute(c[1],'class')==='waku')![1],'rowspan')??1);
   }
   if(!frame||!Number.isInteger(remaining)||remaining<1||remaining>config.maximum_entrants||!cells.has('odds_tan')||!cells.has('odds_fuku'))throw new Error('JRA_RUNNERS');
   remaining--;
   entrants.push(horse);frames[horse]=frame;win.push(quote([horse],cells.get('odds_tan')));place.push(quote([horse],cells.get('odds_fuku')));
  }
  if(remaining)throw new Error('JRA_FRAME_SPAN');runners={entrants,frames};const ids=support(runners);
  if(known&&(JSON.stringify(support(known))!==JSON.stringify(ids)||ids.some(h=>known.frames[h]!==frames[h])))throw new Error('JRA_RUNNERS');
  markets.push(market('win',win,combinations(ids,1),label),market('place',place,combinations(ids,1),label));
 }else{
  if(!runners)throw new Error('JRA_RUNNERS');const ids=support(runners),qs:Quote[]=[];
  const size=page==='trifecta'||page==='trio'?3:2,ordered=page==='trifecta'||page==='exacta';
  const expected=page==='frame_quinella'?frameSupport(runners):combinations(ids,size,ordered);
  const ts=tables(raw,config.tables[page]);if(!ts.length)throw new Error('JRA_TABLE');let previous=0;
  for(const table of ts){
   const cap=table[2].match(/<caption\b([^>]*)>([\s\S]*?)<\/caption>/i);if(!cap)throw new Error('JRA_TABLE');let prefix:number[];
   if(page==='trifecta'){
    const context=raw.slice(previous,table.index),first=context.match(/1着<\/span><\/div>\s*<div\b[^>]*class=["']num["'][^>]*>(\d+)<\/div>/),second=context.match(/2着<\/span><\/div>\s*<div\b[^>]*class=["']num["'][^>]*>(\d+)<\/div>/);
    if(!first||!second)throw new Error('JRA_TABLE');prefix=[Number(first[1]),Number(second[1])];
   }else prefix=page==='frame_quinella'?[Number(cap[2].match(/alt=["']枠(\d+)/)?.[1])]:text(cap[2]).split('-').map(Number);
   previous=table.index!+table[0].length;
   for(const row of table[2].matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
    const a=row[1].match(/<th\b[^>]*>([\s\S]*?)<\/th>/i),b=row[1].match(/<td\b[^>]*>([\s\S]*?)<\/td>/i);if(!a||!b)throw new Error('JRA_TABLE');
    const c=[...prefix,Number(text(a[1]))];if(!ordered)c.sort((a,b)=>a-b);
    if(!text(b[1])&&!expected.some(e=>e.join('-')===c.join('-')))continue;
    qs.push(quote(c,b[1]));
   }
  }
  markets.push(market(page,qs,expected,label));
 }
 return {schema:'sports-odds-v1',sport:'jra',race_id:raceId,phase,source_updated_at:null,source_published_at:null,
  time_semantics:'JRA display time label; update/publish semantics unverified',markets,runners,
  metadata,place_paid_positions};
}
