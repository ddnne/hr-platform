/** Daily request timing from saved program facts. No HTTP, DB or Paper calls. */
import config from '../../configs/sports-collection.json';
import {discoveryTargets} from './discovery-plan';
import {programRaces} from './discovery';
import {oddsTargets} from './odds-plan';
import {contextNavigation} from './context';
import {validateTarget} from './capture';
import {resourceId} from './storage';
import type {savedProgram} from './storage';
import type {Sport,Target} from './types';

type Source=Awaited<ReturnType<typeof savedProgram>> & {event:string};
type Task={kind:'request'|'odds'|'result';target?:Target;race_id?:string;next_at:number;interval:number;last_at?:number;done?:boolean};
export type DailyEntry={at:number;target:Target;daily_task?:string;daily_day?:string};
export type DailyState={sport:Sport;day:string;wake_at:number;tasks:Record<string,Task>;
 races:Record<string,{clock?:Source;runners?:Source}>;
 action:{task:string;entries:DailyEntry[]}|null;
 report:{at:number;status:string;planned:number;deferred:number;reason?:string}};

const settings=config.daily;
export function businessDay(now:number):{day:string;start:number;end:number} {
 if(!Number.isSafeInteger(now))throw new Error('DAILY_CLOCK');
 const shifted=new Date(now+config.discovery.clock_timezone_offset_minutes*60_000);
 if(shifted.getUTCHours()<settings.rollover_hour)shifted.setUTCDate(shifted.getUTCDate()-1);
 const iso=shifted.toISOString().slice(0,10),midnight=Date.parse(iso+'T00:00:00Z')-config.discovery.clock_timezone_offset_minutes*60_000;
 return {day:iso.replaceAll('-',''),start:midnight+settings.start_hour*3600_000,end:midnight+settings.end_hour*3600_000};
}
export function catalogTargets(sport:Sport,day:string):Target[] {
 const source=config.sources[sport],base={sport,race_id:`${sport}:${day}:0:0`,kind:'schedule' as const};
 const targets:Target[]=sport==='auto'?[{...base,url:source.origin+config.sources.auto.catalog_path}]:
  sport==='boat'?[{...base,url:source.origin+config.sources.boat.catalog_path+'?'+new URLSearchParams({hd:day})}]:
  [{...base,kind:'guest',url:source.origin+config.sources.keirin.guest_path},
   {...base,url:source.origin+config.sources.keirin.json_path+'?'+new URLSearchParams({type:config.sources.keirin.catalog_json_type})}];
 targets.forEach(validateTarget);return targets;
}
async function requestTask(state:DailyState,target:Target,now:number,interval:number) {
 const key='request:'+await resourceId(target),prior=state.tasks[key];
 if(!prior&&Object.keys(state.tasks).length>=settings.maximum_tasks)throw new Error('DAILY_TASK_CAPACITY');
 // A newly observed identity must refresh the clock and runners together.
 const changed=prior&&JSON.stringify(prior.target)!==JSON.stringify(target);
 state.tasks[key]={kind:'request',target,next_at:changed?now:prior?.next_at??now,interval};
}
export async function initialDaily(sport:Sport,now:number):Promise<DailyState> {
 const clock=businessDay(now),state:DailyState={sport,day:clock.day,wake_at:Math.max(now,clock.start),tasks:{},races:{},action:null,
  report:{at:now,status:'INITIALIZED',planned:0,deferred:0}};
 for(const t of catalogTargets(sport,clock.day))await requestTask(state,t,clock.start,
  t.kind==='guest'?settings.guest_interval_seconds:settings.catalog_interval_seconds);
 return state;
}
function raceTask(state:DailyState,key:string,task:Task) {
 if(!state.tasks[key]&&Object.keys(state.tasks).length>=settings.maximum_tasks)throw new Error('DAILY_TASK_CAPACITY');
 state.tasks[key]??=task;
}
export async function acceptProgram(state:DailyState,source:Source,now:number) {
 if(source.value.sport!==state.sport||source.value.requested_race_id.split(':')[1]!==state.day)throw new Error('DAILY_PROGRAM_IDENTITY');
 rejectProgram(state,source.target);
 const children=discoveryTargets(source.value,source.event);
 for(const t of children.targets)await requestTask(state,t,now,settings.program_interval_seconds);
 const program=source.value.program;
 if('runners' in program&&program.runners){
  const scope=state.races[source.target.race_id]??={};scope.runners=source;return;
 }
 const races=programRaces(program);
 for(const race of races){
  // Venue-level Keirin LIVE is discovery, without the individual identity reference.
  if(state.sport==='keirin'&&(!source.target.context_event||source.target.discovery_stage!=='race'))continue;
  const item=state.races[race.race_id]??={};item.clock=source;
  if(!race.close_at)continue;
  const close=Date.parse(race.close_at),odds='odds:'+race.race_id,result='result:'+race.race_id;
  raceTask(state,odds,{kind:'odds',race_id:race.race_id,next_at:close-settings.odds_lead_seconds*1000,interval:config.interval_seconds});
  raceTask(state,result,{kind:'result',race_id:race.race_id,next_at:close+settings.result_delay_seconds*1000,interval:settings.result_interval_seconds});
  // Follow changed advertisements before generating any further request group.
  const oddsTask=state.tasks[odds],resultTask=state.tasks[result];
  oddsTask.done=false;oddsTask.next_at=Math.max(now,close-settings.odds_lead_seconds*1000,(oddsTask.last_at??0)+oddsTask.interval*1000);
  resultTask.done=false;resultTask.next_at=Math.max(now,close+settings.result_delay_seconds*1000,(resultTask.last_at??0)+resultTask.interval*1000);
 }
 if(children.deferred.length)state.report={at:now,status:'PROGRAM_DEFERRED',planned:0,deferred:children.deferred.length,reason:children.deferred[0].reason};
}
export function rejectProgram(state:DailyState,target:Target) {
 // Invalidate the matching latest resource; an error never revives its old normal copy.
 for(const facts of Object.values(state.races))for(const field of ['clock','runners'] as const)
  if(facts[field]&&facts[field]!.target.url===target.url&&facts[field]!.target.body===target.body)delete facts[field];
}
export function resultTarget(source:Source,raceId:string):Target {
 const [sport,day,venue,no]=raceId.split(':'),base={sport:sport as Sport,race_id:raceId,kind:'result' as const};
 let target:Target;
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
  if(task.next_at>now)continue;
  const at=now+settings.planning_offset_seconds*1000;let targets:Target[]=[];
  if(task.kind==='request'){
   const race=task.target?state.races[task.target.race_id]?.clock?.value.program:undefined;
   const known=race?programRaces(race).find(r=>r.race_id===task.target!.race_id):undefined;
   if(task.target?.discovery_stage!=='venue'&&known?.close_at&&now>Date.parse(known.close_at)+settings.result_window_seconds*1000){task.done=true;continue;}
   targets=[task.target!];
  }else {
   const facts=state.races[task.race_id!],source=facts?.clock;
   const races=source?programRaces(source.value.program):[];
   const race=races?.find(r=>r.race_id===task.race_id),close=race?.close_at&&Date.parse(race.close_at);
   if(!source||!close){task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;}
   if(task.kind==='result'){
    if(now>close+settings.result_window_seconds*1000){task.done=true;continue;}
    targets=[resultTarget(source,task.race_id!)];
   }else {
    if(at>=close){task.done=true;continue;}
    if(now-Date.parse(source.received_at)>config.discovery.maximum_program_age_seconds*1000||state.sport==='keirin'&&(!facts.runners||now-Date.parse(facts.runners.received_at)>config.discovery.maximum_program_age_seconds*1000)){
     task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;
    }
    try {const plan=oddsTargets(source,task.race_id!,at,facts.runners);if(!plan.deferred.length)targets=plan.targets;}catch {targets=[];}
    if(!targets.length){task.next_at=now+settings.deferred_interval_seconds*1000;deferred++;continue;}
   }
  }
  const entries=targets.map((target,i)=>({at:at+i*config.request_spacing_seconds*1000,target,daily_task:key,daily_day:state.day}));
  if(entries.length>config.maximum_plan_entries)throw new Error('DAILY_PLAN_CAPACITY');
  state.action={task:key,entries};state.wake_at=at;
  state.report={at:now,status:'PLANNED',planned:entries.length,deferred};return entries;
 }
 const future=Object.values(state.tasks).filter(t=>!t.done).map(t=>t.next_at);
 state.wake_at=future.length?Math.max(now+1,Math.min(...future)):window.end;
 state.report={at:now,status:'WAITING',planned:0,deferred};return [];
}
export async function completeDaily(state:DailyState,entry:DailyEntry,status:string,now:number) {
 if(entry.daily_day!==state.day||!state.action||state.action.task!==entry.daily_task)return;
 const resource=await resourceId(entry.target);
 state.action.entries=await Promise.all(state.action.entries.map(async e=>({e,match:e.at===entry.at&&await resourceId(e.target)===resource})))
  .then(rows=>rows.filter(r=>!r.match).map(r=>r.e));
 if(!state.action.entries.length){const task=state.tasks[state.action.task];task.last_at=now;task.next_at=now+task.interval*1000;
  state.action=null;state.wake_at=now+config.request_spacing_seconds*1000;}
 state.report={at:now,status,planned:state.action?.entries.length??0,deferred:0};
}
