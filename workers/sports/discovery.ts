/** Pure discovery adapters. A program clock is an advertised time, not an observed deadline. */
import config from '../../configs/sports-collection.json';
import {text} from './parsers';
import type {Sport,Target} from './types';

export type Venue = {sport:Sport;race_date:string;venue:number;current_race:number|null;
 public_navigation:string|null;cancel_label:string|null};
export type Race = {race_id:string;race_date:string;venue:number;race:number;
 start_at:string|null;close_at:string|null;start_label:string|null;close_label:string|null;
 time_semantics:'PROVIDER_ADVERTISED_PROGRAM';final_race_number:number|null};
const integer=(v:unknown,max:number)=>{const n=Number(v);if(!Number.isInteger(n)||n<1||n>max)throw new Error('PROGRAM_ID');return n;};
const raceNumber=(v:unknown)=>integer(String(v).replace(/^(\d+)R$/,'$1'),config.discovery.maximum_race_number);
export function date(value:unknown):string {
 const s=String(value).replaceAll('-','');if(!/^\d{8}$/.test(s))throw new Error('PROGRAM_DATE');
 const iso=`${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;
 const milliseconds=Date.parse(iso+'T00:00:00Z');
 if(!Number.isFinite(milliseconds)||new Date(milliseconds).toISOString().slice(0,10)!==iso)throw new Error('PROGRAM_DATE');
 return s;
}
export function programClock(day:string,label:unknown):string|null {
 const d=date(day);if(label===null||label===undefined||label==='')return null;
 const m=String(label).match(/^(\d{1,2}):(\d{2})$/);if(!m)throw new Error('PROGRAM_CLOCK');
 const hour=Number(m[1]),minute=Number(m[2]);if(hour>config.discovery.maximum_clock_hour||minute>=60)throw new Error('PROGRAM_CLOCK');
 const start=Date.parse(`${d.slice(0,4)}-${d.slice(4,6)}-${d.slice(6,8)}T00:00:00Z`)-config.discovery.clock_timezone_offset_minutes*60_000;
 return new Date(start+(hour*60+minute)*60_000).toISOString();
}
const label=(value:unknown)=>value===undefined||value===null||value===''?null:String(value);
function race(sport:Sport,day:string,venue:number,no:number,start:unknown,close:unknown,last:unknown=null):Race {
 const start_label=label(start),close_label=label(close);
 if(last!==null&&integer(last,config.discovery.maximum_race_number)<no)throw new Error('PROGRAM_INCOMPLETE');
 return {race_id:`${sport}:${day}:${venue}:${no}`,race_date:day,venue,race:no,
  start_at:programClock(day,start_label),close_at:programClock(day,close_label),start_label,close_label,
  time_semantics:'PROVIDER_ADVERTISED_PROGRAM',final_race_number:last===null?null:integer(last,config.discovery.maximum_race_number)};
}

export function autoVenues(raw:string):Venue[] {
 const v=JSON.parse(raw);if(v.result!=='Success'||!Array.isArray(v.body?.today))throw new Error('PROGRAM_NOT_READY');
 // The provider's business day can still be yesterday after local midnight.
 const day=date(String(v.body.date).slice(0,10));
 const rows=v.body.today.map((p:any)=>({sport:'auto' as const,race_date:day,venue:integer(p.placeCode,config.discovery.maximum_venue_number),
  current_race:p.oddsRaceNo===undefined||p.oddsRaceNo===null||Number(p.oddsRaceNo)===0?null:integer(p.oddsRaceNo,config.discovery.maximum_race_number),
  public_navigation:null,cancel_label:label(p.cancelFlg)}));
 if(new Set(rows.map((p:Venue)=>p.venue)).size!==rows.length)throw new Error('PROGRAM_DUPLICATE');return rows;
}
export function autoProgram(raw:string,expected:{race_date:string;venue:number;race:number}):Race {
 const v=JSON.parse(raw),b=v.body;if(v.result!=='Success'||!b)throw new Error('PROGRAM_NOT_READY');
 const day=date(expected.race_date),venue=integer(b.placeCode,config.discovery.maximum_venue_number),no=integer(b.raceNo,config.discovery.maximum_race_number);
 if(venue!==expected.venue||no!==expected.race)throw new Error('PROGRAM_IDENTITY');
 return race('auto',day,venue,no,b.raceStartTime,b.telvoteTime,b.finalRaceNo);
}

export function boatVenues(raw:string,day:string):Venue[] {
 const requested=date(day),rows=new Map<number,Venue>();
 for(const m of raw.matchAll(/href=["']([^"']*\/race\/raceindex\?[^"']+)["']/gi)){
  const u=new URL(m[1].replaceAll('&amp;','&'),config.sources.boat.origin);
  if(u.origin!==config.sources.boat.origin||u.pathname!=='/owpc/pc/race/raceindex'||u.searchParams.get('hd')!==requested)continue;
  const venue=integer(u.searchParams.get('jcd'),config.discovery.maximum_venue_number);
  rows.set(venue,{sport:'boat',race_date:requested,venue,current_race:null,public_navigation:u.href,cancel_label:null});
 }
 return [...rows.values()].sort((a,b)=>a.venue-b.venue);
}
export function boatProgram(raw:string,day:string,venue:number):Race[] {
 const requested=date(day);integer(venue,config.discovery.maximum_venue_number);const rows=new Map<number,Race>();
 for(const m of raw.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)) {
  const links=[...m[1].matchAll(/href=["']([^"']+)["']/gi)].map(x=>new URL(x[1].replaceAll('&amp;','&'),config.sources.boat.origin));
  const ids=new Set(links.filter(u=>u.origin===config.sources.boat.origin&&u.pathname.startsWith('/owpc/pc/race/')&&u.searchParams.has('rno'))
   .filter(u=>u.searchParams.get('hd')===requested&&Number(u.searchParams.get('jcd'))===venue).map(u=>integer(u.searchParams.get('rno'),config.discovery.maximum_race_number)));
  if(ids.size!==1)continue;const no=[...ids][0];
  // raceindex labels this first program clock as the planned sales cutoff.
  const cells=[...m[1].matchAll(/<t[dh]\b[^>]*>([\s\S]*?)<\/t[dh]>/gi)].map(x=>text(x[1]));
  if(!cells[0]?.match(new RegExp(`^${no}R$`)))continue;
  const close=cells[1]?.match(/^\d{1,2}:\d{2}$/)?.[0]??null;
  const row=race('boat',requested,venue,no,null,close);
  if(rows.has(no))throw new Error('PROGRAM_DUPLICATE');rows.set(no,row);
 }
 if(!rows.size)throw new Error('PROGRAM_NOT_READY');return [...rows.values()].sort((a,b)=>a.race-b.race);
}

export function keirinVenues(raw:string):Venue[] {
 const v=JSON.parse(raw);if(v.resultCd!==0||!Array.isArray(v.RaceList))throw new Error('PROGRAM_NOT_READY');
 const rows=v.RaceList.map((p:any)=>({sport:'keirin' as const,race_date:date(p.kaisaiDate),venue:integer(p.naibuKeirinCd,config.discovery.maximum_venue_number),
  current_race:p.raceNum===undefined||p.raceNum===null||Number(p.raceNum)===0?null:raceNumber(p.raceNum),public_navigation:label(p.touhyouLivePara),cancel_label:label(p.tyusiKbn)}));
 if(new Set(rows.map((p:Venue)=>`${p.race_date}:${p.venue}`)).size!==rows.length)throw new Error('PROGRAM_DUPLICATE');return rows;
}
export function keirinProgram(raw:string):{selected:Race;navigation:{position:number;public_navigation:string;ended_label:string|null}[]} {
 const encoded=raw.match(/jsonData\["PC0201"\]\s*=\s*(.*?);/s)?.[1];if(!encoded)throw new Error('PROGRAM_NOT_READY');
 const v=JSON.parse(encoded),d=v.C0201data;if(v.resultCd!==0||!d||!Array.isArray(d.C0201race)||!d.C0201racedtl)throw new Error('PROGRAM_NOT_READY');
 const day=date(d.selKaisai),venue=integer(d.selKjyoCd,config.discovery.maximum_venue_number),no=integer(d.selRaceNo,config.discovery.maximum_race_number),times=d.C0201racedtl;
 const selected=race('keirin',day,venue,no,times.aftStartTime||times.bfrStartTime,times.aftBetTime||times.bfrBetTime);
 // cntRace was zero in an actual twelve-link program. Its meaning is unverified.
 if(d.C0201race.length<no||d.C0201race.length>config.discovery.maximum_race_number)throw new Error('PROGRAM_INCOMPLETE');
 // Retain positions. The selected race's identity is confirmed by subsequent context JSON.
 const navigation=d.C0201race.map((p:any,i:number)=>{if(typeof p.encParaR!=='string'||!p.encParaR)throw new Error('PROGRAM_NAVIGATION');
  return {position:i+1,public_navigation:p.encParaR,ended_label:label(p.flgRaceEnd)};});
 return {selected,navigation};
}

export function keirinIdentity(raw:string,expected:{race_date:string;venue:number;race:number}):Race {
 const v=JSON.parse(raw),d=v.data;if(v.resultCd!==0||!d)throw new Error('PROGRAM_NOT_READY');
 const day=date(d.kaisaiDate),venue=integer(d.keirinJyoCd,config.discovery.maximum_venue_number),no=raceNumber(d.raceNo);
 if(day!==date(expected.race_date)||venue!==expected.venue||expected.race!==0&&no!==expected.race)throw new Error('PROGRAM_IDENTITY');
 // JST015 confirms the identity; it contains no advertised start or cutoff clocks.
 return race('keirin',day,venue,no,null,null);
}

export function supportsProgram(t:Target):boolean {
 const u=new URL(t.url);
 return t.kind==='schedule'&&(t.sport==='auto'&&['/race_info/XML/Hold/Today','/race_info/OtherRaceInfo'].includes(u.pathname)
  ||t.sport==='boat'&&['/owpc/pc/race/index','/owpc/pc/race/raceindex'].includes(u.pathname)
  ||t.sport==='keirin'&&u.pathname==='/pc/json'&&['JSJ048',config.sources.keirin.identity_json_type].includes(u.searchParams.get('type')??''))
  ||t.sport==='keirin'&&t.kind==='guest'&&u.pathname==='/pc/racelive';
}
export function parseProgram(raw:string,t:Target) {
 if(!supportsProgram(t))throw new Error('PROGRAM_RESOURCE');const u=new URL(t.url),[,day,venue]=t.race_id.split(':');
 let program;
 if(t.sport==='auto')program=u.pathname.endsWith('/Today')?{venues:autoVenues(raw)}:{races:[autoProgram(raw,{race_date:day,venue:Number(venue),race:Number(t.race_id.split(':')[3])})]};
 else if(t.sport==='boat'){
  if(u.searchParams.get('hd')!==day||u.pathname.endsWith('/raceindex')&&Number(u.searchParams.get('jcd'))!==Number(venue))throw new Error('PROGRAM_IDENTITY');
  program=u.pathname.endsWith('/raceindex')?{races:boatProgram(raw,day,Number(venue))}:{venues:boatVenues(raw,day)};
 }else if(u.pathname==='/pc/json'){
  if(u.searchParams.get('type')==='JSJ048')program={venues:keirinVenues(raw)};
  else {const navigation=u.searchParams.get('encp');if(!navigation)throw new Error('PROGRAM_NAVIGATION');
   program={selected:keirinIdentity(raw,{race_date:day,venue:Number(venue),race:Number(t.race_id.split(':')[3])}),public_navigation:navigation};}
 }else {program=keirinProgram(raw);
  const selected=program.selected,venueScope=t.race_id===`keirin:${selected.race_date}:${selected.venue}:0`&&t.discovery_stage==='venue';
  if(selected.race_id!==t.race_id&&!venueScope)throw new Error('PROGRAM_IDENTITY');}
 return {schema:'sports-program-v1' as const,sport:t.sport,requested_race_id:t.race_id,discovery_stage:t.discovery_stage??'venue',source_updated_at:null,source_published_at:null,program};
}
export type Program = ReturnType<typeof parseProgram>;
