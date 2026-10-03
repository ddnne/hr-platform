/** Daily request timing from saved program facts. No HTTP, DB or Paper calls. */
import config from '../../configs/sports-collection.json';
import jra from '../../configs/jra-source.json';
import {discoveryTargets} from './discovery-plan';
import {programRaces} from './discovery';
import {oddsTargets} from './odds-plan';
import {contextNavigation} from './context';
import {validateTarget,concurrencyFor,spacingFor} from './capture';
import {resourceId} from './storage';
import {recipeJson} from '../capture-storage';
import type {savedProgram,JraWinContext} from './storage';
import type {JraPage} from '../jra';
import type {CaptureSport,CaptureTarget,Target} from './types';

type Source=Awaited<ReturnType<typeof savedProgram>> & {event:string};
type Task={kind:'request'|'odds'|'result'|'final_odds';target?:CaptureTarget;race_id?:string;next_at:number;interval:number;last_at?:number;done?:boolean;close_at?:number;start_at?:number;expires_at?:number;page?:JraPage};
type Action={task:string;entries:DailyEntry[];closed_odds?:boolean;close_at?:number;start_at?:number};
export type DailyEntry={at:number;target:CaptureTarget;daily_task?:string;daily_day?:string};
export type DailyState={sport:CaptureSport;day:string;wake_at:number;tasks:Record<string,Task>;
 races:Record<string,{clock?:Source;runners?:Source;results?:Source;win_context?:JraWinContext}>;
 action:Action|null;actions?:Record<string,Action>;
 report:{at:number;status:string;planned:number;deferred:number;reason?:string}};

const settings=config.daily;
export function businessDay(now:number):{day:string;start:number;end:number} {
 if(!Number.isSafeInteger(now))throw new Error('DAILY_CLOCK');
 const shifted=new Date(now+config.discovery.clock_timezone_offset_minutes*60_000);
 if(shifted.getUTCHours()<settings.rollover_hour)shifted.setUTCDate(shifted.getUTCDate()-1);
 const iso=shifted.toISOString().slice(0,10),midnight=Date.parse(iso+'T00:00:00Z')-config.discovery.clock_timezone_offset_minutes*60_000;
 return {day:iso.replaceAll('-',''),start:midnight+settings.start_hour*3600_000,end:midnight+settings.end_hour*3600_000};
}
export function catalogTargets(sport:CaptureSport,day:string):CaptureTarget[] {
 if(sport==='jra'){const target:CaptureTarget={sport,race_id:`jra:${day}:0:0`,kind:'schedule',discovery_stage:'catalog',form:true,
  url:jra.origin+jra.odds_path,body:new URLSearchParams({cname:jra.catalog_navigation}).toString()};
  const results:CaptureTarget={...target,program_kind:'results' as const,url:jra.origin+jra.result_path,body:new URLSearchParams({cname:jra.result_catalog_navigation}).toString()};
  [target,results].forEach(validateTarget);return [target,results];}
 const source=config.sources[sport],base={sport,race_id:`${sport}:${day}:0:0`,kind:'schedule' as const};
 const targets:Target[]=sport==='auto'?[{...base,url:source.origin+config.sources.auto.catalog_path}]:
  sport==='boat'?[{...base,url:source.origin+config.sources.boat.catalog_path+'?'+new URLSearchParams({hd:day})}]:
  [{...base,kind:'guest',url:source.origin+config.sources.keirin.guest_path},
   {...base,url:source.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:config.sources.keirin.catalog_json_type})}];
 targets.forEach(validateTarget);return targets;
}
async function requestTask(state:DailyState,target:CaptureTarget,now:number,interval:number) {
 const key='request:'+await resourceId(target),prior=state.tasks[key];
 let replaced=false;
 if(target.sport==='jra'&&target.kind==='schedule'&&target.discovery_stage==='venue')
  for(const [oldKey,old] of Object.entries(state.tasks))if(oldKey!==key&&old.target?.sport==='jra'&&old.target.kind==='schedule'
   &&old.target.discovery_stage==='venue'&&old.target.race_id===target.race_id&&old.target.program_kind===target.program_kind){
   replaced||=!old.done;old.done=true;rejectProgram(state,old.target);
  }
 if(!prior&&Object.keys(state.tasks).length>=settings.maximum_tasks)throw new Error('DAILY_TASK_CAPACITY');
 // A newly observed identity must refresh the clock and runners together.
 const changed=prior&&(replaced||recipeJson(prior.target)!==recipeJson(target));
 state.tasks[key]={kind:'request',target,next_at:changed?now:prior?.next_at??now,interval,done:changed?false:prior?.done};
}
export async function initialDaily(sport:CaptureSport,now:number):Promise<DailyState> {
 const clock=businessDay(now),state:DailyState={sport,day:clock.day,wake_at:Math.max(now,clock.start),tasks:{},races:{},action:null,
  report:{at:now,status:'INITIALIZED',planned:0,deferred:0}};
 for(const t of catalogTargets(sport,clock.day))await requestTask(state,t,clock.start,
  t.kind==='guest'?settings.guest_interval_seconds:settings.catalog_interval_seconds);
 return state;
}
/** Add newly supported catalogs to a saved day without replacing its races or actions. */
export async function ensureDailyCatalogs(state:DailyState,now:number):Promise<boolean> {
 let changed=false;
 for(const target of catalogTargets(state.sport,state.day))if(!state.tasks['request:'+await resourceId(target)]){
  await requestTask(state,target,now,settings.catalog_interval_seconds);changed=true;
 }
 return changed;
}
function raceTask(state:DailyState,key:string,task:Task) {
 if(!state.tasks[key]&&Object.keys(state.tasks).length>=settings.maximum_tasks)throw new Error('DAILY_TASK_CAPACITY');
 state.tasks[key]??=task;
}
// Recover the old advertised close BEFORE replacing its saved program evidence.
export function migrateDailyCloses(state:DailyState):boolean {
 let changed=false;
 for(const action of [...(state.action?[state.action]:[]),...Object.values(state.actions??{})]){
  const task=state.tasks[action.task];if(!task?.race_id)continue;
  const clock=state.races[task.race_id]?.clock;
  const label=clock&&programRaces(clock.value.program).find(r=>r.race_id===task.race_id)?.close_at;
  const close=task.close_at??(label?Date.parse(label):undefined);
  if(close===undefined)continue;
  if(action.close_at===undefined){action.close_at=close;changed=true;}
  if(task.close_at===undefined){task.close_at=close;changed=true;}
 }
 return changed;
}
export async function acceptProgram(state:DailyState,source:Source,now:number) {
 if(source.value.sport!==state.sport||source.value.requested_race_id.split(':')[1]!==state.day)throw new Error('DAILY_PROGRAM_IDENTITY');
 // A completed old navigation cannot restore clocks after its replacement.
 if(source.target.sport==='jra'&&source.target.kind==='schedule'&&source.target.discovery_stage==='venue'
  &&state.tasks['request:'+await resourceId(source.target)]?.done)return;
 migrateDailyCloses(state);rejectProgram(state,source.target);
 const children=discoveryTargets(source.value,source.event);
 for(const t of children.targets)await requestTask(state,t,now,settings.program_interval_seconds);
 const program=source.value.program;
 if(state.sport==='jra'&&source.target.sport==='jra'&&source.target.kind==='schedule'&&source.target.program_kind==='results'){
  if('result_navigation' in program)for(const [race_id,name] of Object.entries(program.result_navigation)){
   const item=state.races[race_id]??={};item.results=source;
   const target:CaptureTarget={sport:'jra',race_id,kind:'result',form:true,url:jra.origin+jra.result_path,body:new URLSearchParams({cname:name}).toString()};
   validateTarget(target);const key='result:'+race_id;
   raceTask(state,key,{kind:'result',race_id,target,next_at:now,interval:settings.result_interval_seconds,expires_at:now+settings.result_window_seconds*1000});
   const task=state.tasks[key];if(recipeJson(task.target)!==recipeJson(target)){
    task.target=target;task.done=false;task.next_at=now;task.expires_at=now+settings.result_window_seconds*1000;
   }
  }
  return;
 }
 if('runners' in program&&program.runners){
  const scope=state.races[source.target.race_id]??={};scope.runners=source;return;
 }
 const races=programRaces(program);
 for(const race of races){
  // Venue-level Keirin LIVE is discovery, without the individual identity reference.
  if(state.sport==='keirin'&&(!source.target.context_event||source.target.discovery_stage!=='race'))continue;
  const item=state.races[race.race_id]??={};item.clock=source;
  if(state.sport==='jra'){
   const start=race.start_at?Date.parse(race.start_at):undefined;
   for(const [i,page] of (Object.keys(jra.tables) as JraPage[]).entries()){
    const key=`odds:${race.race_id}:${page}`;
    if(start===undefined){const task=state.tasks[key];if(task){task.start_at=undefined;task.done=true;}continue;}
    const due=start-jra.daily_odds_lead_seconds*1000+i*spacingFor('jra')*1000;
    raceTask(state,key,{kind:'odds',race_id:race.race_id,page,start_at:start,next_at:due,interval:config.interval_seconds});
    const task=state.tasks[key];if(task.start_at!==start){task.done=false;task.start_at=start;task.next_at=Math.max(now,due,(task.last_at??0)+task.interval*1000);}
   }
   continue;
  }
  if(!race.close_at)continue;
  const close=Date.parse(race.close_at),odds='odds:'+race.race_id,result='result:'+race.race_id,final='final_odds:'+race.race_id;
  raceTask(state,odds,{kind:'odds',race_id:race.race_id,close_at:close,next_at:close-settings.odds_lead_seconds*1000,interval:config.interval_seconds});
  raceTask(state,result,{kind:'result',race_id:race.race_id,close_at:close,next_at:close+settings.result_delay_seconds*1000,interval:settings.result_interval_seconds});
  raceTask(state,final,{kind:'final_odds',race_id:race.race_id,close_at:close,
   next_at:close+settings.final_odds_delay_seconds*1000,interval:settings.final_odds_interval_seconds});
  // Follow changed advertisements before generating any further request group.
  const oddsTask=state.tasks[odds],resultTask=state.tasks[result];
  if(oddsTask.close_at!==close){oddsTask.done=false;oddsTask.close_at=close;oddsTask.next_at=Math.max(now,close-settings.odds_lead_seconds*1000,(oddsTask.last_at??0)+oddsTask.interval*1000);}
  if(resultTask.close_at!==close){resultTask.done=false;resultTask.close_at=close;resultTask.next_at=Math.max(now,close+settings.result_delay_seconds*1000,(resultTask.last_at??0)+resultTask.interval*1000);}
  const finalTask=state.tasks[final];
  if(finalTask.close_at!==close){finalTask.done=false;finalTask.close_at=close;}
  if(!finalTask.done)finalTask.next_at=Math.max(now,close+settings.final_odds_delay_seconds*1000,(finalTask.last_at??0)+finalTask.interval*1000);
 }
 if(children.deferred.length)state.report={at:now,status:'PROGRAM_DEFERRED',planned:0,deferred:children.deferred.length,reason:children.deferred[0].reason};
}
export function rejectProgram(state:DailyState,target:CaptureTarget) {
 // Invalidate the matching latest resource; an error never revives its old normal copy.
 for(const facts of Object.values(state.races))for(const field of ['clock','runners','results'] as const)
  if(facts[field]&&facts[field]!.target.url===target.url&&facts[field]!.target.body===target.body)delete facts[field];
}
export function resultTarget(source:Source,raceId:string):Target {
 if(source.target.sport==='jra'||raceId.startsWith('jra:'))throw new Error('JRA_RESULT_UNSUPPORTED');
 const [sport,day,venue,no]=raceId.split(':'),base={sport:sport as Target['sport'],race_id:raceId,kind:'result' as const};
 let target:CaptureTarget;
 if(sport==='auto')target={...base,url:config.sources.auto.origin+config.sources.auto.result_path,
  body:JSON.stringify({placeCode:Number(venue),raceDate:`${day.slice(0,4)}-${day.slice(4,6)}-${day.slice(6,8)}`,raceNo:Number(no)})};
 else if(sport==='boat')target={...base,url:config.sources.boat.origin+config.sources.boat.result_path+'?'+new URLSearchParams({hd:day,jcd:venue.padStart(2,'0'),rno:no})};
 else {
  const navigation=contextNavigation(source.target);if(!navigation||!source.target.context_event)throw new Error('RACE_CONTEXT_REQUIRED');
  target={...base,context_event:source.target.context_event,url:config.sources.keirin.origin+config.sources.keirin.json_path+'?'+
   new URLSearchParams({type:config.sources.keirin.result_json_type,encp:navigation,mode:'0'})};
 }
 validateTarget(target);return target;
}
export function nextDaily(state:DailyState,now:number,guestReady=true):DailyEntry[] {
 if(state.action)return state.action.entries;
 const window=businessDay(now);
 if(window.day!==state.day)throw new Error('DAILY_ROLLOVER_REQUIRED');
 if(now<window.start||now>=window.end){state.wake_at=now<window.start?window.start:window.end+3600_000;return [];}
 let tasks=Object.entries(state.tasks).filter(([,t])=>!t.done).sort((a,b)=>a[1].next_at-b[1].next_at||a[0].localeCompare(b[0]));
 // Anonymous Keirin initialization precedes JSON/LIVE when its session expired.
 if(state.sport==='keirin'&&!guestReady){const guest=tasks.find(([,t])=>t.target?.url===config.sources.keirin.origin+config.sources.keirin.guest_path);
  if(!guest)throw new Error('DAILY_GUEST_REQUIRED');
  if(guest[1].next_at>now){state.wake_at=guest[1].next_at;state.report={at:now,status:'GUEST_WAIT',planned:0,deferred:tasks.length-1};return [];}
  tasks=[guest];}
 let deferred=0;
 for(const [key,task] of tasks){
  // Expire finite closing tasks even if their clock/roster resource disappeared.
  if(task.kind==='final_odds'&&task.close_at!==undefined&&now>task.close_at+settings.final_odds_window_seconds*1000){task.done=true;continue;}
  if(task.kind==='result'&&task.expires_at!==undefined&&now>task.expires_at){task.done=true;continue;}
  if(task.next_at>now)continue;
  if(state.sport==='jra'&&task.kind==='odds'&&task.start_at!==undefined&&now>=task.start_at){task.done=true;continue;}
  // Due preclose/discovery/result work retains precedence over closing price samples.
  if(task.kind==='final_odds'&&tasks.some(([,t])=>t.kind!=='final_odds'&&!t.done&&t.next_at<=now))continue;
  const at=now+settings.planning_offset_seconds*1000;let targets:CaptureTarget[]=[];
  if(task.kind==='request'){
   const race=task.target?state.races[task.target.race_id]?.clock?.value.program:undefined;
   const known=race?programRaces(race).find(r=>r.race_id===task.target!.race_id):undefined;
   if(task.target?.discovery_stage!=='venue'&&known?.close_at&&now>Date.parse(known.close_at)+settings.result_window_seconds*1000){task.done=true;continue;}
   if(task.target?.discovery_stage==='race'&&known?.close_at&&now<Date.parse(known.close_at)-settings.odds_lead_seconds*1000){task.next_at=Date.parse(known.close_at)-settings.odds_lead_seconds*1000;continue;}
   targets=[task.target!];
  }else if(state.sport==='jra'&&task.kind==='result'&&task.target){
   const source=state.races[task.race_id!]?.results,program=source?.value.program;
   const name=program&&'result_navigation' in program?program.result_navigation[task.race_id!]:undefined;
   if(!source||now-Date.parse(source.received_at)>config.discovery.maximum_program_age_seconds*1000||
    name!==new URLSearchParams(task.target.body).get('cname')){
    task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;
   }
   targets=[task.target];
  }else {
   const facts=state.races[task.race_id!],source=facts?.clock;
   const races=source?programRaces(source.value.program):[];
   const race=races?.find(r=>r.race_id===task.race_id),close=race?.close_at&&Date.parse(race.close_at);
   if(state.sport==='jra'&&task.kind==='odds'){
    const context=facts?.win_context;
    if(!source||!race?.start_at||!task.page||now-Date.parse(source.received_at)>config.discovery.maximum_program_age_seconds*1000||
     task.page!=='win_place'&&(!context||context.race_id!==task.race_id||Date.parse(context.available_at)>now||Date.parse(context.received_at)>now||now-Date.parse(context.received_at)>jra.maximum_context_age_seconds*1000)){
     task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;
    }
    const plan=oddsTargets(source,task.race_id!,at,undefined,'INTERMEDIATE',{page:task.page,context_event:context?.event});
    if(plan.deferred.length){task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;}targets=plan.targets;
   }else {
   if(!source||!close){task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;}
   if(task.kind==='result'){
    if(now>close+settings.result_window_seconds*1000){task.done=true;continue;}
    targets=[resultTarget(source,task.race_id!)];
   }else {
    if(task.kind==='odds'&&at>=close){task.done=true;continue;}
    if(now-Date.parse(source.received_at)>config.discovery.maximum_program_age_seconds*1000||state.sport==='keirin'&&(!facts.runners||now-Date.parse(facts.runners.received_at)>config.discovery.maximum_program_age_seconds*1000)){
     task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;
    }
    try {const plan=oddsTargets(source,task.race_id!,at,facts.runners,task.kind==='final_odds'?'CLOSED':'INTERMEDIATE');if(!plan.deferred.length)targets=plan.targets;}catch {targets=[];}
    if(!targets.length){task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;}
   }
   }
  }
  if(task.kind==='final_odds'){
   // Reserve time for every request's configured timeout and source spacing.
   // Do not begin a closing round ahead of preclose work due inside that span.
   const finish=at+targets.length*(config.request_timeout_seconds+config.request_spacing_seconds)*1000;
   const earlier=tasks.filter(([,t])=>t.kind==='odds'&&!t.done&&t.next_at<=finish);
   if(earlier.length){task.next_at=Math.max(now+1,Math.min(...earlier.map(([,t])=>t.next_at)));deferred++;continue;}
  }
  const entries=targets.map((target,i)=>({at:at+i*spacingFor(target.sport)*1000,target,daily_task:key,daily_day:state.day}));
  if(entries.length>config.maximum_plan_entries)throw new Error('DAILY_PLAN_CAPACITY');
  state.action={task:key,entries,close_at:task.close_at,start_at:task.start_at,...(task.kind==='final_odds'?{closed_odds:true}:{})};state.wake_at=at;
  state.report={at:now,status:'PLANNED',planned:entries.length,deferred};return entries;
 }
 const future=Object.values(state.tasks).filter(t=>!t.done).map(t=>t.next_at);
 state.wake_at=future.length?Math.max(now+1,Math.min(...future)):window.end;
 state.report={at:now,status:'WAITING',planned:0,deferred};return [];
}
export function nextDailyParallel(state:DailyState,now:number,guestReady=true,capacity=config.maximum_pending_requests):DailyEntry[] {
 migrateDailyCloses(state);const actions=state.actions??={};
 if(state.action){actions[state.action.task]=state.action;state.action=null;}
 if(state.day!==businessDay(now).day&&Object.keys(actions).length)return Object.values(actions).flatMap(a=>a.entries);
 if(state.sport==='keirin'&&!guestReady&&Object.keys(actions).length)return Object.values(actions).flatMap(a=>a.entries);
 const working:DailyState={...state,tasks:Object.fromEntries(Object.entries(state.tasks).filter(([key])=>!actions[key])),action:null};
 let added=0;
 while(Object.keys(actions).length<(state.sport==='keirin'&&!guestReady?1:concurrencyFor(state.sport))){
  const entries=nextDaily(working,now,guestReady);if(!entries.length)break;
  if(entries.length>capacity-added){working.action=null;working.report={at:now,status:'CAPACITY_WAIT',planned:added,deferred:1};break;}
  const action=working.action!;actions[action.task]=action;working.action=null;delete working.tasks[action.task];added+=entries.length;
 }
 state.wake_at=working.wake_at;state.report=working.report;
 return Object.values(actions).flatMap(a=>a.entries);
}
export async function completeDaily(state:DailyState,entry:DailyEntry,status:string,now:number,closedOdds=false,resultPublished=false,winContext:JraWinContext|null=null) {
 const action=entry.daily_task?state.actions?.[entry.daily_task]??(state.action?.task===entry.daily_task?state.action:null):null;
 if(entry.daily_day!==state.day||!action)return;
 const resource=await resourceId(entry.target);
 const matched=await Promise.all(action.entries.map(async e=>({e,match:e.at===entry.at&&await resourceId(e.target)===resource})));
 if(!matched.some(r=>r.match))return;
 const task=state.tasks[action.task];
 if(entry.target.sport==='jra'&&entry.target.kind==='odds'&&entry.target.page==='win_place'&&action.start_at===task.start_at){
  if(winContext&&(winContext.race_id!==entry.target.race_id||Date.parse(winContext.available_at)>now||Date.parse(winContext.received_at)>now))throw new Error('JRA_CONTEXT_IDENTITY');
  const facts=state.races[entry.target.race_id];if(facts)facts.win_context=winContext??undefined;
 }
 if(task.kind==='final_odds')action.closed_odds=action.closed_odds===true&&closedOdds;
 action.entries=matched.filter(r=>!r.match).map(r=>r.e);
 if(!action.entries.length){
  // A response from an earlier advertised close cannot finish the revised task.
  if(action.close_at===task.close_at&&action.start_at===task.start_at&&(!task.target||resource===await resourceId(task.target))){
  if(task.kind==='final_odds'&&action.closed_odds||task.kind==='result'&&resultPublished)task.done=true;
  if(task.kind==='request'&&status==='RAW_STORED'&&task.target?.sport==='keirin'&&new URL(task.target.url).searchParams.get('type')===config.sources.keirin.identity_json_type)task.done=true;
  task.last_at=now;task.next_at=now+task.interval*1000;
  }
  if(state.action===action)state.action=null;else delete state.actions![action.task];
  state.wake_at=now+config.request_spacing_seconds*1000;}
 state.report={at:now,status,planned:action.entries.length,deferred:0};
}
