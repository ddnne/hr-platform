/** Separate private Worker. No horse, research, Paper or wagering bindings. */
import {DurableObject,WorkerEntrypoint} from 'cloudflare:workers';
import config from '../../configs/sports-collection.json';
import {collect,validateTarget,validateContext,sourceFor} from './capture';
import {history,resourceId,normalize,savedProgram,closedOddsSaved,publishedResultSaved} from './storage';
import {discoveryTargets} from './discovery-plan';
import {oddsTargets} from './odds-plan';
import {businessDay,initialDaily,nextDailyParallel,migrateDailyCloses,acceptProgram,rejectProgram,completeDaily,type DailyState,type DailyEntry} from './daily-plan';
import {supportsProgram} from './discovery';
import type {Sport,CaptureSport,SportsEnv,Target,CaptureTarget} from './types';
import type {CaptureManifest} from '../capture-storage';
type Entry=Omit<DailyEntry,'target'> & {target:CaptureTarget};
type Session={cookie:string;token?:string;expires:number};
function planTime(event:string,at:number):number {
 const now=Date.now();if(!/^sports:(?:auto|boat|keirin):\d+:[0-9a-f]{64}$/.test(event)||!Number.isSafeInteger(at)||at<now||at>now+config.plan_horizon_seconds*1000)throw new Error('PLAN_WINDOW');return now;
}
function planEntries(targets:Target[],at:number,now:number):Entry[] {
 const entries=targets.map((target,i)=>({at:at+i*config.request_spacing_seconds*1000,target}));
 if(entries.length>config.maximum_plan_entries||entries.some(e=>e.at>now+config.plan_horizon_seconds*1000))throw new Error('PLAN_WINDOW');return entries;
}
export class SportsCollector extends DurableObject<SportsEnv> {
 private dailyEnabled(sport:CaptureSport):boolean {
  return sport!=='jra'&&this.env.SPORTS_DAILY_ENABLED==='true'&&this.env.SPORTS_ENABLED==='true'&&JSON.parse(this.env.SPORTS_PROVIDERS_JSON).includes(sport);
 }
 async ensureDaily(sport:CaptureSport):Promise<string> {
  if(sport==='jra')throw new Error('DAILY_UNSUPPORTED');
  if(!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
  if(!this.dailyEnabled(sport))return 'DISABLED';
  const saved=await this.ctx.storage.get<Sport>('daily-sport');if(saved&&saved!==sport)throw new Error('ONE_SOURCE');
  await this.ctx.storage.put('daily-sport',sport);await this.daily();await this.arm();return 'ARMED';
 }
 private async daily():Promise<DailyState|null> {
  const sport=await this.ctx.storage.get<Sport>('daily-sport');if(!sport||!this.dailyEnabled(sport))return null;
  let state=await this.ctx.storage.get<DailyState>('daily-state');
  if(!state||state.day!==businessDay(Date.now()).day&&!state.action&&!Object.keys(state.actions??{}).length){state=await initialDaily(sport,Date.now());await this.ctx.storage.put('daily-state',state);}
  return state;
 }
 async dailyState():Promise<DailyState|null> {return this.env.SPORTS_DAILY_ENABLED==='true'?await this.ctx.storage.get<DailyState>('daily-state')??null:null;}
 private async planDaily():Promise<void> {
  const current=await this.daily();if(!current)return;
  const gate=await this.env.INDEX.prepare('SELECT blocked,next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(current.sport)).first<{blocked:number;next_allowed_at:number}>();
  // Planning and completion share one transaction; neither can erase the other.
  await this.ctx.storage.transaction(async store=>{
   const state=await store.get<DailyState>('daily-state'),now=Date.now();if(!state||now<state.wake_at)return;
   const queue=await store.list<Entry>({prefix:'plan:'}),free=config.maximum_pending_requests-queue.size;
   const capacityWait=()=>{state.wake_at=Math.max(now+1,Math.min(...[...queue.values()].map(e=>e.at)));state.report={at:now,status:'CAPACITY_WAIT',planned:0,deferred:1};};
   const recovering=[...(state.action?[state.action]:[]),...Object.values(state.actions??{})].flatMap(a=>a.entries);
   let missing=0;for(const e of recovering)if(!queue.has(`plan:${e.at}:${await resourceId(e.target)}`))missing++;
   if(!free||missing>free){capacityWait();await store.put('daily-state',state);return;}
   if(gate?.blocked||gate&&gate.next_allowed_at>now){
    state.wake_at=gate.blocked?now+config.daily.blocked_interval_seconds*1000:gate.next_allowed_at;
    state.report={at:now,status:gate.blocked?'SOURCE_BLOCKED':'SOURCE_WAIT',planned:0,deferred:0};
    await store.put('daily-state',state);return;
   }
   const guest=await store.get<Session>('guest-session'),received=await store.get<string>('keirin-guest-received-at'),age=received?now-Date.parse(received):Infinity;
   const entries=nextDailyParallel(state,now,!!guest&&guest.expires>now||age>=0&&age<config.guest_session_seconds*1000,free-missing);
   const items:Record<string,Entry>={};for(const entry of entries){const key=`plan:${entry.at}:${await resourceId(entry.target)}`;
    if(queue.has(key)&&JSON.stringify(queue.get(key))!==JSON.stringify(entry))throw new Error('PLAN_CONFLICT');items[key]=entry;}
   if(state.report.status==='CAPACITY_WAIT'&&queue.size)capacityWait();
   await store.put('daily-state',state);if(entries.length)await store.put(items);
  });
 }
 async schedule(entries:Entry[]):Promise<string> {
  return this.enqueue(entries);
 }
 private async enqueue(entries:Entry[],recovery=false):Promise<string> {
  if(this.env.SPORTS_ENABLED!=='true')return 'DISABLED';
  const items:Record<string,Entry>={};
  for(const entry of entries){validateTarget(entry.target);
   if(entry.target.sport==='jra'&&(entry.daily_task!==undefined||entry.daily_day!==undefined))throw new Error('DAILY_UNSUPPORTED');
   if(!recovery)await validateContext(entry.target,this.env);
   if(entry.target.headers||!Number.isSafeInteger(entry.at)||!recovery&&entry.at<Date.now()-config.capture_window_seconds*1000||entry.at>Date.now()+config.plan_horizon_seconds*1000)throw new Error('PLAN_WINDOW');
   const resource=await resourceId(entry.target),key=`plan:${entry.at}:${resource}`;
   const recorded=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(`sports:${entry.target.sport}:${entry.at}:${resource}`).first();
   if(recorded){const saved=await this.env.RAW.get(`manifests/sports:${entry.target.sport}:${entry.at}:${resource}.json`);if(saved){const m=await saved.json<{target:CaptureTarget}>();if(JSON.stringify(m.target)!==JSON.stringify(entry.target))throw new Error('PLAN_CONFLICT');}}
   if(!recorded||recovery){if(items[key]&&JSON.stringify(items[key])!==JSON.stringify(entry))throw new Error('PLAN_CONFLICT');items[key]=entry;}
  }
  // Transactional insertion cannot overwrite another concurrent registration.
  await this.ctx.storage.transaction(async store=>{
   const queue=await store.list<Entry>({prefix:'plan:'});
   for(const [key,entry] of Object.entries(items))if(queue.has(key)&&JSON.stringify(queue.get(key))!==JSON.stringify(entry))throw new Error('PLAN_CONFLICT');
   const additions=Object.keys(items).filter(k=>!queue.has(k));
   if(queue.size+additions.length>config.maximum_pending_requests)throw new Error('PLAN_CAPACITY');
   await store.put(items);
  });
  await this.arm();return 'REGISTERED';
 }
 async pending():Promise<number> {return (await this.ctx.storage.list({prefix:'plan:'})).size;}
 async planState():Promise<{pending:number;alarm_at:number|null}> {
  return {pending:await this.pending(),alarm_at:await this.ctx.storage.getAlarm()};
 }
 private async rememberKeirinGuest(t:CaptureTarget,result:Awaited<ReturnType<typeof collect>>):Promise<void> {
  if(t.sport!=='keirin'||t.kind!=='guest'||t.url!==config.sources.keirin.origin+config.sources.keirin.guest_path||result.status!=='RAW_STORED'||!result.received_at)return;
  const received=Date.parse(result.received_at),prior=await this.ctx.storage.get<string>('keirin-guest-received-at');
  if(Number.isFinite(received)&&received<=Date.now()&&(!prior||received>Date.parse(prior)))
   await this.ctx.storage.put('keirin-guest-received-at',result.received_at);
 }
 private async arm() {
  const daily=await this.daily();
  const sport=await this.ctx.storage.get<Sport>('daily-sport')??[...(await this.ctx.storage.list<Entry>({prefix:'plan:'})).values()][0]?.target.sport,
   gate=sport?await this.env.INDEX.prepare('SELECT next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(sport)).first<{next_allowed_at:number}>():null;
  await this.ctx.storage.transaction(async store=>{
   const queue=[...(await store.list<Entry>({prefix:'plan:'})).values()],times=queue.map(e=>e.at);
   const current=daily?await store.get<DailyState>('daily-state'):null;
   if(current&&Object.keys(current.actions??{}).length<config.maximum_parallel_requests)times.push(current.wake_at);
   if(!times.length){await store.deleteAlarm();return;}
   await store.setAlarm(Math.max(Date.now()+1,gate?.next_allowed_at??0,Math.min(...times)));
  });
 }
 private async finish(key:string,status:string):Promise<void> {
  if(['WAIT_OR_BLOCKED','STORAGE_ERROR','FETCHING'].includes(status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
  const entry=await this.ctx.storage.get<Entry>(key);if(!entry)return;
  if(entry.target.sport==='jra'){await this.ctx.storage.delete(key);await this.arm();return;}
  const dailyEntry:DailyEntry={...entry,target:entry.target};
  const event=`sports:${entry.target.sport}:${entry.at}:${await resourceId(entry.target)}`;
  let source:Awaited<ReturnType<typeof savedProgram>>|null=null;
  if(entry.daily_task&&this.dailyEnabled(entry.target.sport)&&supportsProgram(entry.target)&&status==='RAW_STORED'){
   try{source=await savedProgram(this.env,event);}
   catch(e){if(!(e instanceof Error)||!['PROGRAM_UNAVAILABLE','PROGRAM_STALE'].includes(e.message))throw e;status=e.message;}
  }
  const closing=!!entry.daily_task?.startsWith('final_odds:')&&status==='RAW_STORED'&&await closedOddsSaved(this.env,event);
  let published=false;
  if(entry.daily_task?.startsWith('result:')&&status==='RAW_STORED'){
   const state=await this.ctx.storage.get<DailyState>('daily-state'),facts=state?.races[entry.target.race_id];
   if(facts?.clock){const plan=oddsTargets(facts.clock,entry.target.race_id,(state!.tasks[entry.daily_task]?.close_at??Date.now())+config.daily.final_odds_delay_seconds*1000,facts.runners,'CLOSED');
    const markets=entry.target.sport==='keirin'?plan.targets.map(t=>t.market!):['win','place','exacta','quinella','wide','trifecta','trio'];
    if(entry.target.sport!=='keirin'||!plan.deferred.length)published=await publishedResultSaved(this.env,event,markets);}
  }
  // Concurrent HTTP completion must merge into the current state atomically.
  await this.ctx.storage.transaction(async store=>{
   const state=entry.daily_task?await store.get<DailyState>('daily-state'):null;
   if(state&&entry.daily_day===state.day){
    if(source)await acceptProgram(state,{...source,event},Date.now());
    else if(supportsProgram(dailyEntry.target))rejectProgram(state,dailyEntry.target);
    await completeDaily(state,dailyEntry,status,Date.now(),closing,published);await store.put('daily-state',state);
   }
   await store.delete(key);
  });await this.arm();
 }
 async alarm():Promise<void> {
  const running=new Map<string,Promise<void>>(),attempted=new Set<string>(),errors:unknown[]=[];
  const end=performance.now()+config.alarm_budget_seconds*1000;
  try {while(performance.now()<end){
   await this.planDaily();
   const ordered=[...(await this.ctx.storage.list<Entry>({prefix:'plan:'})).entries()].filter(([k])=>!attempted.has(k)).sort((a,b)=>a[1].at-b[1].at||a[0].localeCompare(b[0]));
   const session=await this.ctx.storage.get<Session>('guest-session'),pending=await this.ctx.storage.get<Entry>('guest-pending');
   const exclusive=(e:Entry)=>e.target.kind==='guest'&&e.target.url===config.sources.keirin.origin+config.sources.keirin.guest_path||
    e.target.sport==='auto'&&(e.target.kind==='guest'||e.target.body&&(!session||session.expires<Date.now()));
   const sport=ordered[0]?.[1].target.sport;
   const gate=sport?await this.env.INDEX.prepare('SELECT blocked,next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(sport)).first<{blocked:number;next_allowed_at:number}>():null;
   const allowed=gate?.blocked||!gate||gate.next_allowed_at<=Date.now();
   const due=allowed?ordered.filter(([,e])=>e.at<=Date.now()):[];
   if(due.length&&(pending||exclusive(due[0][1]))){if(!running.size){attempted.add(due[0][0]);await this.runEntry(...due[0]);}break;}
   for(const [key,entry] of due){if(running.size>=config.maximum_parallel_requests||exclusive(entry))break;
    attempted.add(key);const job=this.runEntry(key,entry,true).catch(e=>{errors.push(e);}).finally(()=>{running.delete(key);});running.set(key,job);
   }
   if(!running.size)break;
   // A freed slot may advance another race's next page while a slow request runs.
   const future=ordered.filter(([k,e])=>!attempted.has(k)&&e.at>Date.now()).map(([,e])=>e.at);
   let delay=end-performance.now();
   if(gate&&!gate.blocked&&gate.next_allowed_at>Date.now())delay=Math.min(delay,gate.next_allowed_at-Date.now());
   else if(future.length)delay=Math.min(delay,Math.min(...future)-Date.now());
   let timer:ReturnType<typeof setTimeout>|undefined;
   try{await Promise.race([...running.values(),new Promise<void>(resolve=>{timer=setTimeout(resolve,Math.max(1,delay));})]);}
   finally{if(timer!==undefined)clearTimeout(timer);}
  }}finally{await Promise.allSettled(running.values());await this.arm();}
  if(errors.length)throw errors[0];
 }
 private async runEntry(key:string,entry:Entry,parallel=false):Promise<void> {
  const t=entry.target;let deadline=t.deadline_at;
  if(entry.daily_task){const state=await this.ctx.storage.transaction(async store=>{const value=await store.get<DailyState>('daily-state');if(value&&migrateDailyCloses(value))await store.put('daily-state',value);return value;}),action=state?.actions?.[entry.daily_task]??(state?.action?.task===entry.daily_task?state.action:null),task=state?.tasks[entry.daily_task];
   if(task?.kind==='odds')deadline=task.close_at??deadline;
   if(action&&task&&action.close_at!==task.close_at){
    const event=`sports:${t.sport}:${entry.at}:${await resourceId(t)}`,prior=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(event).first();
    await this.finish(key,prior?(await collect(entry.at,this.env,t)).status:'CLOSE_CHANGED');return;}
  }
  // Turning off daily discovery stops unsent automatic requests, while finite
  // reservations and publication of already fetched raw bodies remain usable.
  if(entry.daily_task&&!this.dailyEnabled(t.sport)){
   const pending=await this.ctx.storage.get<Entry>('guest-pending');
   if(pending){const event=`sports:${pending.target.sport}:${pending.at}:${await resourceId(pending.target)}`;
    const prior=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(event).first();
    if(prior){const result=await collect(pending.at,this.env,pending.target);
     if(['STORAGE_ERROR','FETCHING','WAIT_OR_BLOCKED'].includes(result.status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}}
    await this.ctx.storage.delete('guest-pending');}
   const event=`sports:${t.sport}:${entry.at}:${await resourceId(t)}`;
   const prior=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(event).first();
   await this.finish(key,prior?(await collect(entry.at,this.env,t)).status:'DAILY_DISABLED');return;
  }
  const gate=await this.env.INDEX.prepare('SELECT blocked,next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(t.sport)).first<{blocked:number;next_allowed_at:number}>();
  if(gate?.blocked){const event=`sports:${t.sport}:${entry.at}:${await resourceId(t)}`;
   const prior=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(event).first();
   await this.finish(key,prior?(await collect(entry.at,this.env,t)).status:'SOURCE_BLOCKED');return;}
  if(gate&&Date.now()<gate.next_allowed_at){await this.ctx.storage.setAlarm(Math.max(Date.now()+1,gate.next_allowed_at));return;}
  const pending=await this.ctx.storage.get<Entry>('guest-pending');
  if(pending){const repaired=await collect(pending.at,this.env,pending.target);
   if(['STORAGE_ERROR','FETCHING','WAIT_OR_BLOCKED'].includes(repaired.status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
   await this.ctx.storage.delete('guest-pending');if(repaired.status!=='RAW_STORED'){await this.finish(key,repaired.status);return;}}
  if(Date.now()-entry.at>config.capture_window_seconds*1000){const r=await collect(entry.at,this.env,t);await this.rememberKeirinGuest(t,r);await this.finish(key,r.status);return;}
  const session=await this.ctx.storage.get<Session>('guest-session');
  if(t.sport==='auto'&&t.body&&(!session||session.expires<Date.now())) {
   const place=JSON.parse(t.body!).placeCode;
   const page=config.sources.auto.guest_pages.find(p=>p.endsWith(['','','kawaguchi','isesaki','hamamatsu','iizuka','sanyou'][place]));
   if(!page)throw new Error('GUEST_PLACE');
   const guest={sport:'auto' as const,race_id:t.race_id,kind:'guest' as const,url:config.sources.auto.origin+page};
   await this.ctx.storage.put('guest-pending',{at:entry.at,target:guest});
   const result=await collect(entry.at,this.env,guest,async(body,responseHeaders)=>{
    const token=new TextDecoder().decode(body).match(/<meta name="csrf-token" content="([^"]+)"/)?.[1];
    const cookie=responseHeaders.getSetCookie().map(v=>v.split(';')[0]).join('; ');
    if(!token||!cookie)throw new Error('GUEST_FORMAT');
    await this.ctx.storage.put('guest-session',{cookie,token,expires:Date.now()+config.guest_session_seconds*1000});
   });
   if(!['STORAGE_ERROR','FETCHING','WAIT_OR_BLOCKED'].includes(result.status))await this.ctx.storage.delete('guest-pending');
   if(!result.body)await this.finish(key,result.status);
   await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;
  }
  const headers:Record<string,string>={};
  if(t.sport==='auto'&&t.body){if(!session)throw new Error('GUEST_SESSION_REQUIRED');Object.assign(headers,{'Cookie':session.cookie,'X-CSRF-TOKEN':session.token!,'X-Requested-With':'XMLHttpRequest'});}
  if(t.sport==='keirin'&&session&&session.expires>Date.now()&&session.cookie)headers.Cookie=session.cookie;
  if(t.form)headers['Content-Type']='application/x-www-form-urlencoded';
  const result=await collect(entry.at,this.env,{...t,headers},undefined,parallel,deadline);
  // Successful anonymous initialization does not require a Set-Cookie header.
  // Redelivery retains the original receipt time, so it cannot refresh readiness.
  await this.rememberKeirinGuest(t,result);
  if(t.sport==='keirin'&&result.response_headers){const cookie=result.response_headers.getSetCookie().map(v=>v.split(';')[0]).join('; ');
   if(cookie)await this.ctx.storage.put('guest-session',{cookie,expires:Date.now()+config.guest_session_seconds*1000});}
  if(result.status==='WAIT_OR_BLOCKED'){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
  await this.finish(key,result.status);
 }
}
export class SportsControl extends WorkerEntrypoint<SportsEnv> {
 async ensureDaily(sport:CaptureSport):Promise<string> {return this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).ensureDaily(sport);}
 async dailyState(sport:CaptureSport):Promise<string> {return JSON.stringify(await this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).dailyState());}
 async discoveryPlan(event:string,at:number):Promise<string> {
  const now=planTime(event,at);
  const source=await savedProgram(this.env,event,now),plan=discoveryTargets(source.value,event);
  const entries=planEntries(plan.targets,at,now);
  return JSON.stringify({parent_observation:event,available_at:source.available_at,received_at:source.received_at,entries,deferred:plan.deferred});
 }
 async oddsPlan(event:string,raceId:string,at:number,runnersEvent?:string):Promise<string> {
  const now=planTime(event,at);if(runnersEvent)planTime(runnersEvent,at);
  const source=await savedProgram(this.env,event,now),runners=runnersEvent?await savedProgram(this.env,runnersEvent,now):undefined;
  await validateContext(source.target,this.env);if(runners)await validateContext(runners.target,this.env);
  const plan=oddsTargets(source,raceId,at,runners);
  return JSON.stringify({parent_observation:event,available_at:source.available_at,received_at:source.received_at,
   runners_observation:runnersEvent??null,runners_available_at:runners?.available_at??null,runners_received_at:runners?.received_at??null,
   entries:planEntries(plan.targets,at,now),deferred:plan.deferred,not_offered:plan.not_offered});
 }
 async planState(sport:CaptureSport):Promise<string> {
  if(sport!=='jra'&&!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
  return JSON.stringify(await this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).planState());
 }
 async schedule(payload:string):Promise<string> {
  try {if(payload.length>config.maximum_raw_bytes)throw new Error('INPUT_LIMIT');
   const entries:Entry[]=JSON.parse(payload);if(!Array.isArray(entries)||entries.length<1||entries.length>config.maximum_plan_entries)throw new Error('PLAN_COUNT');
   for(const e of entries)validateTarget(e.target);
   // One source per call avoids partial registrations across provider objects.
   const sport=entries[0].target.sport;if(entries.some(e=>e.target.sport!==sport))throw new Error('ONE_SOURCE');
   return await this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).schedule(entries);
  }catch{return 'INPUT_OR_CAPACITY_ERROR';}
 }
 async history(sport:CaptureSport,race:string,cutoff:string,limit:number,after=''):Promise<string> {
  return JSON.stringify(await history(this.env,sport,race,cutoff,limit,after));
 }
 async programHistory(sport:Sport,race:string,cutoff:string,limit:number,after=''):Promise<string> {
  return JSON.stringify(await history(this.env,sport,race,cutoff,limit,after,'program'));
 }
 async resultHistory(sport:Sport,race:string,cutoff:string,limit:number,after=''):Promise<string> {
  return JSON.stringify(await history(this.env,sport,race,cutoff,limit,after,'result'));
 }
 async reparse(event:string,version:string):Promise<string> {
  if(!/^sports:[a-z]+:\d+:[0-9a-f]{64}$/.test(event)||!/^sports-(?:odds|program|result)-v\d+$/.test(version))throw new Error('PARSER_ID');
  const object=await this.env.RAW.get(`manifests/${event}.json`);if(!object)throw new Error('MANIFEST_MISSING');
  const m=await object.json<CaptureManifest & {target:CaptureTarget}>();return normalize(this.env,event,m.target,version);
 }
}
export default {
 async fetch(){return new Response('Not found',{status:404});},
 async scheduled(_controller:ScheduledController,env:SportsEnv):Promise<void> {
  if(env.SPORTS_DAILY_ENABLED!=='true'||env.SPORTS_ENABLED!=='true')return;
  for(const sport of JSON.parse(env.SPORTS_PROVIDERS_JSON) as CaptureSport[]){
   if(sport==='jra')continue;
   if(!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
   await env.SPORTS.get(env.SPORTS.idFromName(sport)).ensureDaily(sport);
  }
 }
} satisfies ExportedHandler<SportsEnv>;
