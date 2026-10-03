/** Pure follow-up requests. This builds a plan, never performs or enables collection. */
import config from '../../configs/sports-collection.json';
import jra from '../../configs/jra-source.json';
import {validateTarget} from './capture';
import {date,type Program} from './discovery';
import type {Sport,Target,CaptureTarget} from './types';
type Deferred={race_id:string;reason:string};
export function discoveryTargets(value:Program,parentObservation?:string):{targets:CaptureTarget[];deferred:Deferred[]} {
 const targets:CaptureTarget[]=[],deferred:Deferred[]=[],[,day]=value.requested_race_id.split(':');date(day);
 const defer=(race_id:string,reason:string)=>deferred.push({race_id,reason});
 const program=value.program;
 const make=(sport:Sport,venue:number,no:number):Target=>({sport,race_id:`${sport}:${day}:${venue}:${no}`,kind:'schedule',url:''});
 const auto=(venue:number,no:number,stage:'venue'|'race'='race'):Target=>({...make('auto',venue,no),discovery_stage:stage,
  url:config.sources.auto.origin+config.sources.auto.program_path,
  body:JSON.stringify({placeCode:venue,raceDate:`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6,8)}`,raceNo:no})});
 const live=(venue:number,no:number,navigation:string,stage:'venue'|'race'):Target=>({...make('keirin',venue,no),kind:'guest',discovery_stage:stage,
  url:config.sources.keirin.origin+config.sources.keirin.live_path,form:true,body:new URLSearchParams({encp:navigation}).toString()});
 const identity=(venue:number,navigation:string):Target=>({...make('keirin',venue,0),discovery_stage:'race',
  url:config.sources.keirin.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:config.sources.keirin.identity_json_type,encp:navigation,mode:'0'})});
 if(value.sport==='jra'){
  if('venues' in program&&program.venues)for(const v of program.venues){
   if(v.sport!=='jra'||v.race_date!==day||!v.public_navigation){defer(`jra:${day}:${v.venue}:0`,'BUSINESS_DAY_MISMATCH');continue;}
   targets.push({sport:'jra',race_id:`jra:${day}:${v.venue}:0`,kind:'schedule',discovery_stage:'venue',form:true,
    url:jra.origin+jra.odds_path,body:new URLSearchParams({cname:v.public_navigation}).toString()});
  }
 }else if('venues' in program&&program.venues)for(const v of program.venues){
  const scope=`${value.sport}:${day}:${v.venue}:0`;
  if(v.sport!==value.sport||v.race_date!==day){defer(scope,'BUSINESS_DAY_MISMATCH');continue;}
  if(v.sport==='auto'){
   if(v.current_race===null)defer(scope,'CURRENT_RACE_UNKNOWN');else targets.push(auto(v.venue,v.current_race,'venue'));
  }else if(!v.public_navigation)defer(scope,'PUBLIC_NAVIGATION_MISSING');
  else if(v.sport==='boat')targets.push({...make('boat',v.venue,0),url:v.public_navigation});
  // A venue request accepts the provider's selected identity, never the link position.
  else targets.push(live(v.venue,0,v.public_navigation,'venue'));
 }else if('races' in program&&program.races&&value.sport==='auto'&&(value.discovery_stage??'venue')==='venue')for(const r of program.races){
  if(r.race_date!==day)throw new Error('PROGRAM_IDENTITY');
  if(r.final_race_number===null){defer(r.race_id,'FINAL_RACE_NUMBER_UNKNOWN');continue;}
  for(let no=1;no<=r.final_race_number;no++)if(no!==r.race)targets.push(auto(r.venue,no));
 }else if('selected' in program&&program.selected&&value.sport==='keirin'){
  const r=program.selected;if(r.race_date!==day)throw new Error('PROGRAM_IDENTITY');
  if('navigation' in program&&program.navigation&&(value.discovery_stage??'venue')==='venue'){
   if(new Set(program.navigation.map(n=>n.public_navigation)).size!==program.navigation.length)throw new Error('PROGRAM_NAVIGATION');
   for(const n of program.navigation)targets.push(identity(r.venue,n.public_navigation));
  }else if('public_navigation' in program&&program.public_navigation){
   if(!parentObservation)defer(r.race_id,'RACE_CONTEXT_REQUIRED');
   else {targets.push({...live(r.venue,r.race,program.public_navigation,'race'),context_event:parentObservation});
    targets.push({...make('keirin',r.venue,r.race),discovery_stage:'race',context_event:parentObservation,
     url:config.sources.keirin.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:'JST010',encp:program.public_navigation,'url.media.flg':config.sources.keirin.runners_media_flag})});}
  }
 }
 if(targets.length>config.maximum_plan_entries)throw new Error('PLAN_COUNT');
 for(const t of targets)validateTarget(t);
 return {targets,deferred};
}
