/** Official anonymous navigation only. No generated checksums or inferred cutoff. */
import config from '../configs/jra-source.json';
import {text} from './sports/parsers';
import {date,programClock} from './sports/program-clock';
import type {Race,Venue} from './sports/discovery';
import type {JraScheduleTarget} from './sports/types';
import type {JraPage} from './jra';
type Identity={day:string;venue:number;race?:number};
export type JraProgram={venues:Venue[];result_catalog?:true}|{races:Race[];odds_navigation:Record<string,Partial<Record<JraPage,string>>>}|
 {result_navigation:Record<string,string>;final_odds_navigation:Record<string,string>};
export function jraNavigationIdentity(name:string,kind:'venue'|'odds'|'result'|'result_venue'):Identity {
 const m=kind==='venue'?name.match(/^pw15orl[01](\d{3})(\d{4})(\d{2})(\d{2})(\d{8})\/[A-Za-z0-9+]+$/):
  kind==='result_venue'?name.match(/^pw01srl(?:01|10)(\d{2})(\d{4})(\d{2})(\d{2})(\d{8})\/[A-Za-z0-9+]+$/):
  kind==='result'?name.match(/^pw01sde(?:01|10)(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})\/[A-Za-z0-9+]+$/):
  name.match(/^pw15[1345678]ou(?:S3|10)(\d{2})(\d{4})(\d{2})(\d{2})(\d{2})(\d{8})Z(?:99)?\/[A-Za-z0-9+]+$/);
 if(!m)throw new Error('PROGRAM_NAVIGATION');
 const venueScope=kind==='venue'||kind==='result_venue',venue=Number(m[1]),day=date(m[venueScope?5:6]);
 if(!Object.hasOwn(config.venues,String(venue))||m[2]!==day.slice(0,4))throw new Error('PROGRAM_IDENTITY');
 if(venueScope)return {day,venue};
 const race=Number(m[5]);if(race<1||race>config.maximum_race_number)throw new Error('PROGRAM_IDENTITY');
 return {day,venue,race};
}
export function jraLinks(raw:string,path:string=config.odds_path) {
 return [...raw.matchAll(/<a\b([^>]*)>([\s\S]*?)<\/a>/gi)].flatMap(a=>{
  const action=a[1].match(/\bonclick\s*=\s*"([^"]*)"/i)?.[1]??a[1].match(/\bonclick\s*=\s*'([^']*)'/i)?.[1];
  const m=action?.match(/^\s*return\s+doAction\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]\s*\)\s*;?\s*$/);
  if(m&&m[1]===path)return [{name:m[2],label:text(a[2])}];
  // Result detail pages use GET links; preserve the existing program parser behavior.
  if(path!==config.result_path)return [];
  const href=a[1].match(/\bhref\s*=\s*"([^"]*)"/i)?.[1]??a[1].match(/\bhref\s*=\s*'([^']*)'/i)?.[1];
  if(!href)return [];
  const url=new URL(href.replaceAll('&amp;','&'),config.origin);
  const name=url.searchParams.get('CNAME');
  return url.origin===config.origin&&url.pathname===path&&url.searchParams.size===1&&name?[{name,label:text(a[2])}]:[];
 });
}
export function parseJraProgram(raw:string,t:JraScheduleTarget):JraProgram {
 const [,requested,scope]=t.race_id.split(':'),day=date(requested);
 const results=t.program_kind==='results',path=results?config.result_path:config.odds_path;
 if(t.discovery_stage==='catalog'){
  if(results&&![...raw.matchAll(/<h1\b[^>]*>([\s\S]*?)<\/h1>/gi)].some(m=>text(m[1])==='レース結果 開催選択'))throw new Error('PROGRAM_NOT_READY');
  const rows=new Map<number,Venue>();let seen=0;
  for(const link of jraLinks(raw,path).filter(l=>l.name.startsWith(results?config.result_venue_navigation_prefix:config.venue_navigation_prefix))){
   const id=jraNavigationIdentity(link.name,results?'result_venue':'venue');seen++;
   if(!link.label.includes(config.venues[String(id.venue) as keyof typeof config.venues]))throw new Error('PROGRAM_IDENTITY');
   if(id.day!==day)continue;
   if(rows.has(id.venue)&&rows.get(id.venue)!.public_navigation!==link.name)throw new Error('PROGRAM_DUPLICATE');
   rows.set(id.venue,{sport:'jra',race_date:day,venue:id.venue,current_race:null,public_navigation:link.name,cancel_label:null});
  }
  if(!seen)throw new Error('PROGRAM_NOT_READY');
  return {venues:[...rows.values()].sort((a,b)=>a.venue-b.venue),...(results?{result_catalog:true as const}:{})};
 }
 const identity=jraNavigationIdentity(new URLSearchParams(t.body).get('cname')??'',results?'result_venue':'venue');
 if(identity.day!==day||identity.venue!==Number(scope))throw new Error('PROGRAM_IDENTITY');
 const headers=[...raw.matchAll(/<h[123]\b[^>]*>([\s\S]*?)<\/h[123]>/gi)].map(m=>text(m[1]));
 const title=new RegExp(`${Number(day.slice(0,4))}年${Number(day.slice(4,6))}月${Number(day.slice(6,8))}日.*${config.venues[String(identity.venue) as keyof typeof config.venues]}`);
 if(!headers.some(h=>title.test(h)))throw new Error('PROGRAM_IDENTITY');
 if(results){
  const result_navigation:Record<string,string>={},final_odds_navigation:Record<string,string>={};
  const tables=[...raw.matchAll(/<table\b[^>]*>([\s\S]*?)<\/table>/gi)].filter(m=>
   /<th\b[^>]*class=["']race_num["'][^>]*>レース結果<\/th>/.test(m[1])&&/<th\b[^>]*class=["']odds["'][^>]*>最終/.test(m[1]));
  if(tables.length!==1)throw new Error('PROGRAM_NOT_READY');
  for(const row of tables[0][1].matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
   const links=jraLinks(row[1],config.result_path).filter(l=>l.name.startsWith('pw01sde'));
   if(!links.length)continue;if(links.length!==1)throw new Error('PROGRAM_DUPLICATE');
   const id=jraNavigationIdentity(links[0].name,'result');
   if(id.day!==day||id.venue!==identity.venue)throw new Error('PROGRAM_IDENTITY');
   const race_id=`jra:${day}:${id.venue}:${id.race}`;if(result_navigation[race_id])throw new Error('PROGRAM_DUPLICATE');
   result_navigation[race_id]=links[0].name;
   const odds=jraLinks(row[1]).filter(l=>l.name.startsWith(config.navigation_prefixes.win_place));
   if(odds.length>1)throw new Error('PROGRAM_DUPLICATE');
   if(odds.length){const o=jraNavigationIdentity(odds[0].name,'odds');
    if(o.day!==day||o.venue!==id.venue||o.race!==id.race)throw new Error('PROGRAM_IDENTITY');final_odds_navigation[race_id]=odds[0].name;}
  }
  // An identified venue may have no published results yet.
  return {result_navigation,final_odds_navigation};
 }
 const races:Race[]=[],odds_navigation:Record<string,Partial<Record<JraPage,string>>>={};
 for(const row of raw.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)){
  const navigation:Partial<Record<JraPage,string>>={};let no:number|undefined;
  for(const link of jraLinks(row[1])){
   const page=Object.entries(config.navigation_prefixes).find(([,prefix])=>link.name.startsWith(prefix))?.[0] as JraPage|undefined;
   if(!page)continue;
   const id=jraNavigationIdentity(link.name,'odds');
   if(id.day!==day||id.venue!==identity.venue||no!==undefined&&no!==id.race)throw new Error('PROGRAM_IDENTITY');
   no=id.race;if(navigation[page]&&navigation[page]!==link.name)throw new Error('PROGRAM_DUPLICATE');navigation[page]=link.name;
  }
  if(no===undefined)continue;
  const label=row[1].match(/<td\b[^>]*class=["']time["'][^>]*>([\s\S]*?)<\/td>/i)?.[1];
  const start_label=label?text(label):null;
  // Ended/cancelled/unknown labels remain evidence, without manufacturing a clock.
  const japanese=start_label?.match(/^(\d{1,2})時(\d{2})分$/);
  const clock=japanese?`${japanese[1]}:${japanese[2]}`:start_label;
  const start_at=clock&&/^\d{1,2}:\d{2}$/.test(clock)?programClock(day,clock):null;
  const race_id=`jra:${day}:${identity.venue}:${no}`;
  if(odds_navigation[race_id])throw new Error('PROGRAM_DUPLICATE');odds_navigation[race_id]=navigation;
  races.push({race_id,race_date:day,venue:identity.venue,race:no,start_at,start_label,close_at:null,close_label:null,
   time_semantics:'PROVIDER_ADVERTISED_PROGRAM',final_race_number:null});
 }
 if(!races.length)throw new Error('PROGRAM_NOT_READY');
 return {races:races.sort((a,b)=>a.race-b.race),odds_navigation};
}
