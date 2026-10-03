/** Separate private Worker. No horse, research, Paper or wagering bindings. */
import {DurableObject,WorkerEntrypoint} from 'cloudflare:workers';
import config from '../../configs/sports-collection.json';
import {collect,validateTarget,validateContext,sourceFor} from './capture';
import {history,resourceId,normalize,savedProgram} from './storage';
import {discoveryTargets} from './discovery-plan';
import {oddsTargets} from './odds-plan';
import {businessDay,initialDaily,nextDaily,acceptProgram,rejectProgram,completeDaily,type DailyState,type DailyEntry} from './daily-plan';
import {supportsProgram} from './discovery';
import type {Sport,SportsEnv,Target} from './types';
import type {CaptureManifest} from '../capture-storage';
type Entry=DailyEntry;
type Session={cookie:string;token?:string;expires:number};
function planTime(event:string,at:number):number {
 const now=Date.now();if(!/^sports:(?:auto|boat|keirin):\d+:[0-9a-f]{64}$/.test(event)||!Number.isSafeInteger(at)||at<now||at>now+config.plan_horizon_seconds*1000)throw new Error('PLAN_WINDOW');return now;
}
function planEntries(targets:Target[],at:number,now:number):Entry[] {
 const entries=targets.map((target,i)=>({at:at+i*config.request_spacing_seconds*1000,target}));
 if(entries.length>config.maximum_plan_entries||entries.some(e=>e.at>now+config.plan_horizon_seconds*1000))throw new Error('PLAN_WINDOW');return entries;
}
export class SportsCollector extends DurableObject<SportsEnv> {
 private dailyEnabled(sport:Sport):boolean {
  return this.env.SPORTS_DAILY_ENABLED==='true'&&this.env.SPORTS_ENABLED==='true'&&JSON.parse(this.env.SPORTS_PROVIDERS_JSON).includes(sport);
 }
 async ensureDaily(sport:Sport):Promise<string> {
  if(!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
  if(!this.dailyEnabled(sport))return 'DISABLED';
  const saved=await this.ctx.storage.get<Sport>('daily-sport');if(saved&&saved!==sport)throw new Error('ONE_SOURCE');
  await this.ctx.storage.put('daily-sport',sport);await this.daily();await this.arm();return 'ARMED';
 }
 private async daily():Promise<DailyState|null> {
  const sport=await this.ctx.storage.get<Sport>('daily-sport');if(!sport||!this.dailyEnabled(sport))return null;
  let state=await this.ctx.storage.get<DailyState>('daily-state');
  if(!state||state.day!==businessDay(Date.now()).day&&!state.action){state=await initialDaily(sport,Date.now());await this.ctx.storage.put('daily-state',state);}
  return state;
 }
 async dailyState():Promise<DailyState|null> {return this.env.SPORTS_DAILY_ENABLED==='true'?await this.ctx.storage.get<DailyState>('daily-state')??null:null;}
 private async planDaily():Promise<void> {
  const state=await this.daily();if(!state||Date.now()<state.wake_at)return;
  const queued=[...(await this.ctx.storage.list<Entry>({prefix:'plan:'})).values()];
  if(queued.some(e=>e.daily_task||e.at<=Date.now()))return;
  const gate=await this.env.INDEX.prepare('SELECT blocked,next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(state.sport)).first<{blocked:number;next_allowed_at:number}>();
  if(gate?.blocked||gate&&gate.next_allowed_at>Date.now()){
   state.wake_at=gate.blocked?Date.now()+config.daily.blocked_interval_seconds*1000:gate.next_allowed_at;
   state.report={at:Date.now(),status:gate.blocked?'SOURCE_BLOCKED':'SOURCE_WAIT',planned:0,deferred:0};
   await this.ctx.storage.put('daily-state',state);return;
  }
  const guest=await this.ctx.storage.get<Session>('guest-session'),received=await this.ctx.storage.get<string>('keirin-guest-received-at'),
   age=received?Date.now()-Date.parse(received):Infinity,recovering=!!state.action,
   entries=nextDaily(state,Date.now(),!!guest&&guest.expires>Date.now()||age>=0&&age<config.guest_session_seconds*1000);
  if(queued.length+entries.length>config.maximum_pending_requests){
   if(!recovering)state.action=null;
   state.wake_at=Math.min(...queued.map(e=>e.at));
   state.report={at:Date.now(),status:'CAPACITY_WAIT',planned:0,deferred:entries.length};
   await this.ctx.storage.put('daily-state',state);return;
  }
  // Persist the exact slots before queue registration. Recovery reuses the same events.
  await this.ctx.storage.put('daily-state',state);if(entries.length)await this.enqueue(entries,true);
 }
 async schedule(entries:Entry[]):Promise<string> {
  return this.enqueue(entries);
 }
 private async enqueue(entries:Entry[],recovery=false):Promise<string> {
  if(this.env.SPORTS_ENABLED!=='true')return 'DISABLED';
  const items:Record<string,Entry>={};
  for(const entry of entries){validateTarget(entry.target);
   if(!recovery)await validateContext(entry.target,this.env);
   if(entry.target.headers||!Number.isSafeInteger(entry.at)||!recovery&&entry.at<Date.now()-config.capture_window_seconds*1000||entry.at>Date.now()+config.plan_horizon_seconds*1000)throw new Error('PLAN_WINDOW');
   const resource=await resourceId(entry.target),key=`plan:${entry.at}:${resource}`;
   const recorded=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(`sports:${entry.target.sport}:${entry.at}:${resource}`).first();
   if(recorded){const saved=await this.env.RAW.get(`manifests/sports:${entry.target.sport}:${entry.at}:${resource}.json`);if(saved){const m=await saved.json<{target:Target}>();if(JSON.stringify(m.target)!==JSON.stringify(entry.target))throw new Error('PLAN_CONFLICT');}}
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
 private async rememberKeirinGuest(t:Target,result:Awaited<ReturnType<typeof collect>>):Promise<void> {
  if(t.sport!=='keirin'||t.kind!=='guest'||t.url!==config.sources.keirin.origin+config.sources.keirin.guest_path||result.status!=='RAW_STORED'||!result.received_at)return;
  const received=Date.parse(result.received_at),prior=await this.ctx.storage.get<string>('keirin-guest-received-at');
  if(Number.isFinite(received)&&received<=Date.now()&&(!prior||received>Date.parse(prior)))
   await this.ctx.storage.put('keirin-guest-received-at',result.received_at);
 }
 private async arm() {
  const daily=await this.daily();
  await this.ctx.storage.transaction(async store=>{
   const queue=[...(await store.list<Entry>({prefix:'plan:'})).values()],times=queue.map(e=>e.at);
   if(daily&&!queue.some(e=>e.daily_task))times.push(daily.wake_at);
   if(!times.length){await store.deleteAlarm();return;}
   await store.setAlarm(Math.max(Date.now()+1,Math.min(...times)));
  });
 }
 private async finish(key:string,status:string):Promise<void> {
  if(['WAIT_OR_BLOCKED','STORAGE_ERROR','FETCHING'].includes(status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
  const entry=await this.ctx.storage.get<Entry>(key),state=entry?.daily_task?await this.ctx.storage.get<DailyState>('daily-state'):null;
  if(entry&&state&&entry.daily_day===state.day){
   if(this.dailyEnabled(entry.target.sport)&&supportsProgram(entry.target)){
    if(status==='RAW_STORED'){
     const event=`sports:${entry.target.sport}:${entry.at}:${await resourceId(entry.target)}`;
     try{await acceptProgram(state,{...await savedProgram(this.env,event),event},Date.now());}
     catch(e){if(!(e instanceof Error)||!['PROGRAM_UNAVAILABLE','PROGRAM_STALE'].includes(e.message))throw e;rejectProgram(state,entry.target);status=e.message;}
    }else rejectProgram(state,entry.target);
   }
   await completeDaily(state,entry,status,Date.now());await this.ctx.storage.put('daily-state',state);
  }
  await this.ctx.storage.delete(key);await this.arm();
 }
 async alarm():Promise<void> {
  await this.planDaily();
  const queue=await this.ctx.storage.list<Entry>({prefix:'plan:'});
  const ordered=[...queue.entries()].sort((a,b)=>a[1].at-b[1].at||a[0].localeCompare(b[0]));
  const next=ordered[0];if(!next){await this.arm();return;}
  const [key,entry]=next,t=entry.target;
  if(Date.now()<entry.at){await this.arm();return;}
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
  if(gate?.blocked){await this.finish(key,'SOURCE_BLOCKED');return;}
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
  const result=await collect(entry.at,this.env,{...t,headers});
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
 async ensureDaily(sport:Sport):Promise<string> {return this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).ensureDaily(sport);}
 async dailyState(sport:Sport):Promise<string> {return JSON.stringify(await this.env.SPORTS.get(this.env.SPORTS.idFromName(sport)).dailyState());}
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
 async planState(sport:Sport):Promise<string> {
  if(!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
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
 async history(sport:Sport,race:string,cutoff:string,limit:number,after=''):Promise<string> {
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
  const m=await object.json<CaptureManifest & {target:Target}>();return normalize(this.env,event,m.target,version);
 }
}
export default {
 async fetch(){return new Response('Not found',{status:404});},
 async scheduled(_controller:ScheduledController,env:SportsEnv):Promise<void> {
  if(env.SPORTS_DAILY_ENABLED!=='true'||env.SPORTS_ENABLED!=='true')return;
  for(const sport of JSON.parse(env.SPORTS_PROVIDERS_JSON) as Sport[]){
   if(!Object.hasOwn(config.sources,sport))throw new Error('SPORT');
   await env.SPORTS.get(env.SPORTS.idFromName(sport)).ensureDaily(sport);
  }
 }
} satisfies ExportedHandler<SportsEnv>;
