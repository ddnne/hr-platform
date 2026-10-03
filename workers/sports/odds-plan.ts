/** Read-only request recipes from saved race clocks and published runner support. */
import config from '../../configs/sports-collection.json';
import {validateTarget} from './capture';
import {contextNavigation} from './context';
import {programRaces,type Program} from './discovery';
import type {Target} from './types';
type Source={value:Program;target:Target};
export function oddsTargets(source:Source,raceId:string,at:number,runners?:Source,purpose:'INTERMEDIATE'|'CLOSED'='INTERMEDIATE') {
 const {value,target}=source,program=value.program,targets:Target[]=[],deferred:{race_id:string;reason:string;market?:string}[]=[],not_offered:{market:string;source_label:string}[]=[];
 validateTarget(target);if(runners)validateTarget(runners.target);
 const defer=(reason:string,market?:string)=>deferred.push({race_id:raceId,reason,...(market?{market}:{})});
 if(target.sport!==value.sport||target.race_id!==value.requested_race_id)throw new Error('PROGRAM_IDENTITY');
 const races=programRaces(program);
 const race=races.find(r=>r.race_id===raceId);if(!race){defer('RACE_NOT_IN_PROGRAM');return {targets,deferred,not_offered};}
 if(!race.close_at){defer('CLOSE_TIME_UNKNOWN');return {targets,deferred,not_offered};}
 const [,day,venue,no]=raceId.split(':'),base:Target={sport:value.sport,race_id:raceId,kind:'odds',url:''};
 if(value.sport==='auto')targets.push({...base,url:config.sources.auto.origin+config.sources.auto.odds_path,
  body:JSON.stringify({placeCode:Number(venue),raceDate:`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6,8)}`,raceNo:Number(no)})});
 else if(value.sport==='boat')for(const page of config.sources.boat.odds_pages)targets.push({...base,...(page.market?{market:page.market}:{}),
  url:config.sources.boat.origin+page.path+'?'+new URLSearchParams({hd:day,jcd:venue.padStart(2,'0'),rno:no})});
 else {
  const navigation=contextNavigation(target);
  if(target.kind!=='guest'||target.discovery_stage!=='race'||!target.context_event||!navigation){defer('RACE_CONTEXT_REQUIRED');return {targets,deferred,not_offered};}
  if(!runners){defer('RUNNERS_REQUIRED');return {targets,deferred,not_offered};}
  const p=runners.value.program,t=runners.target;
  if(t.sport!=='keirin'||t.race_id!==raceId||runners.value.requested_race_id!==raceId||runners.value.sport!=='keirin'
   ||!('runners' in p)||!p.runners||p.identity_evidence!==target.context_event||t.context_event!==target.context_event
   ||contextNavigation(t)!==navigation)throw new Error('RUNNER_CONTEXT_MISMATCH');
  const entrants=p.runners.entries.map(e=>e.entrant),frames:Record<string,number>={};
  for(const e of p.runners.entries)if(e.frame!==null)frames[e.entrant]=e.frame;
  for(const [market,kake] of Object.entries(config.sources.keirin.odds_market_codes)){
   if(market.startsWith('frame_')){
    const label=p.runners.frame_category_label;
    if(label===config.sources.keirin.frame_markets_disabled_label){not_offered.push({market,source_label:label});continue;}
    if(label!==config.sources.keirin.frame_markets_enabled_label){defer('FRAME_AVAILABILITY_UNKNOWN',market);continue;}
    if(Object.keys(frames).length!==entrants.length){defer('FRAME_SUPPORT_UNKNOWN',market);continue;}
   }
   targets.push({...base,market,entrants,frames,context_event:target.context_event,
    url:config.sources.keirin.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:config.sources.keirin.odds_json_type,encp:navigation,kake,mode:'0'})});
  }
 }
 // The purpose constrains request times; the provider response determines its phase.
 const close=Date.parse(race.close_at),last=at+(targets.length-1)*config.request_spacing_seconds*1000;
 if(!Number.isSafeInteger(at)||(purpose==='INTERMEDIATE'?last>=close:
  at<close+config.daily.final_odds_delay_seconds*1000||last>close+config.daily.final_odds_window_seconds*1000)){
  targets.length=0;defer(purpose==='INTERMEDIATE'?'OUTSIDE_PRE_CLOSE_WINDOW':'OUTSIDE_CLOSED_ODDS_WINDOW');
 }
 for(const t of targets){if(purpose==='INTERMEDIATE')t.deadline_at=close;validateTarget(t);}
 return {targets,deferred,not_offered};
}
