/** Read-only request recipes from saved race clocks and published runner support. */
import config from '../../configs/sports-collection.json';
import {validateTarget} from './capture';
import {contextNavigation} from './context';
import type {Program} from './discovery';
import type {Target} from './types';
type Source={value:Program;target:Target};
export function oddsTargets(source:Source,raceId:string,at:number,runners?:Source) {
 const {value,target}=source,program=value.program,targets:Target[]=[],deferred:{race_id:string;reason:string;market?:string}[]=[];
 validateTarget(target);if(runners)validateTarget(runners.target);
 const defer=(reason:string,market?:string)=>deferred.push({race_id:raceId,reason,...(market?{market}:{})});
 if(target.sport!==value.sport||target.race_id!==value.requested_race_id)throw new Error('PROGRAM_IDENTITY');
 const races='races' in program&&program.races?program.races:'selected' in program&&program.selected?[program.selected]:[];
 const race=races.find(r=>r.race_id===raceId);if(!race){defer('RACE_NOT_IN_PROGRAM');return {targets,deferred};}
 if(!race.close_at){defer('CLOSE_TIME_UNKNOWN');return {targets,deferred};}
 const [,day,venue,no]=raceId.split(':'),base:Target={sport:value.sport,race_id:raceId,kind:'odds',url:''};
 if(value.sport==='auto')targets.push({...base,url:config.sources.auto.origin+config.sources.auto.odds_path,
  body:JSON.stringify({placeCode:Number(venue),raceDate:`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6,8)}`,raceNo:Number(no)})});
 else if(value.sport==='boat')for(const page of config.sources.boat.odds_pages)targets.push({...base,...(page.market?{market:page.market}:{}),
  url:config.sources.boat.origin+page.path+'?'+new URLSearchParams({hd:day,jcd:venue.padStart(2,'0'),rno:no})});
 else {
  const navigation=contextNavigation(target);
  if(target.kind!=='guest'||target.discovery_stage!=='race'||!target.context_event||!navigation){defer('RACE_CONTEXT_REQUIRED');return {targets,deferred};}
  if(!runners){defer('RUNNERS_REQUIRED');return {targets,deferred};}
  const p=runners.value.program,t=runners.target;
  if(t.sport!=='keirin'||t.race_id!==raceId||runners.value.requested_race_id!==raceId||runners.value.sport!=='keirin'
   ||!('runners' in p)||!p.runners||p.identity_evidence!==target.context_event||t.context_event!==target.context_event
   ||contextNavigation(t)!==navigation)throw new Error('RUNNER_CONTEXT_MISMATCH');
  const entrants=p.runners.entries.map(e=>e.entrant),frames:Record<string,number>={};
  for(const e of p.runners.entries)if(e.frame!==null)frames[e.entrant]=e.frame;
  for(const [market,kake] of Object.entries(config.sources.keirin.odds_market_codes)){
   if(market.startsWith('frame_')&&Object.keys(frames).length!==entrants.length){defer('FRAME_SUPPORT_UNKNOWN',market);continue;}
   targets.push({...base,market,entrants,frames,context_event:target.context_event,
    url:config.sources.keirin.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:config.sources.keirin.odds_json_type,encp:navigation,kake,mode:'0'})});
  }
 }
 // Leave the whole race unbooked if even one page would be scheduled after the advertised cutoff.
 if(!Number.isSafeInteger(at)||at+(targets.length-1)*config.request_spacing_seconds*1000>=Date.parse(race.close_at)){
  targets.length=0;defer('OUTSIDE_PRE_CLOSE_WINDOW');
 }
 for(const t of targets)validateTarget(t);
 return {targets,deferred};
}
