/** Separate private Worker. No horse, research, Paper or wagering bindings. */
import {DurableObject,WorkerEntrypoint} from 'cloudflare:workers';
import config from '../../configs/sports-collection.json';
import {collect,validateTarget,validateContext,sourceFor} from './capture';
import {history,resourceId,normalize} from './storage';
import type {Sport,SportsEnv,Target} from './types';
import type {CaptureManifest} from '../capture-storage';
type Entry={at:number;target:Target};
type Session={cookie:string;token?:string;expires:number};
export class SportsCollector extends DurableObject<SportsEnv> {
 async schedule(entries:Entry[]):Promise<string> {
  if(this.env.SPORTS_ENABLED!=='true')return 'DISABLED';
  const items:Record<string,Entry>={};
  for(const entry of entries){validateTarget(entry.target);await validateContext(entry.target,this.env);
   if(entry.target.headers||!Number.isSafeInteger(entry.at)||entry.at<Date.now()-config.capture_window_seconds*1000||entry.at>Date.now()+config.plan_horizon_seconds*1000)throw new Error('PLAN_WINDOW');
   const resource=await resourceId(entry.target),key=`plan:${entry.at}:${resource}`;
   const recorded=await this.env.INDEX.prepare('SELECT event_id FROM captures WHERE event_id=?').bind(`sports:${entry.target.sport}:${entry.at}:${resource}`).first();
   if(recorded){const saved=await this.env.RAW.get(`manifests/sports:${entry.target.sport}:${entry.at}:${resource}.json`);if(saved){const m=await saved.json<{target:Target}>();if(JSON.stringify(m.target)!==JSON.stringify(entry.target))throw new Error('PLAN_CONFLICT');}}
   else {if(items[key]&&JSON.stringify(items[key])!==JSON.stringify(entry))throw new Error('PLAN_CONFLICT');items[key]=entry;}
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
 private async arm() {
  await this.ctx.storage.transaction(async store=>{
   const times=[...(await store.list<Entry>({prefix:'plan:'})).values()].map(e=>e.at);
   if(!times.length){await store.deleteAlarm();return;}
   await store.setAlarm(Math.max(Date.now()+1,Math.min(...times)));
  });
 }
 private async finish(key:string,status:string):Promise<void> {
  if(['WAIT_OR_BLOCKED','STORAGE_ERROR','FETCHING'].includes(status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
  await this.ctx.storage.delete(key);await this.arm();
 }
 async alarm():Promise<void> {
  const queue=await this.ctx.storage.list<Entry>({prefix:'plan:'});
  const ordered=[...queue.entries()].sort((a,b)=>a[1].at-b[1].at||a[0].localeCompare(b[0]));
  const next=ordered[0];if(!next){await this.ctx.storage.deleteAlarm();return;}
  const [key,entry]=next,t=entry.target;
  if(Date.now()<entry.at){await this.arm();return;}
  const gate=await this.env.INDEX.prepare('SELECT blocked,next_allowed_at FROM source_control WHERE source=?').bind(sourceFor(t.sport)).first<{blocked:number;next_allowed_at:number}>();
  if(gate?.blocked){await this.finish(key,'SOURCE_BLOCKED');return;}
  if(gate&&Date.now()<gate.next_allowed_at){await this.ctx.storage.setAlarm(Math.max(Date.now()+1,gate.next_allowed_at));return;}
  const pending=await this.ctx.storage.get<Entry>('guest-pending');
  if(pending){const repaired=await collect(pending.at,this.env,pending.target);
   if(['STORAGE_ERROR','FETCHING','WAIT_OR_BLOCKED'].includes(repaired.status)){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
   await this.ctx.storage.delete('guest-pending');if(repaired.status!=='RAW_STORED'){await this.finish(key,repaired.status);return;}}
  if(Date.now()-entry.at>config.capture_window_seconds*1000){const r=await collect(entry.at,this.env,t);await this.finish(key,r.status);return;}
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
  if(t.sport==='keirin'&&session)headers.Cookie=session.cookie;
  if(t.form)headers['Content-Type']='application/x-www-form-urlencoded';
  const result=await collect(entry.at,this.env,{...t,headers});
  if(t.sport==='keirin'&&result.response_headers){const cookie=result.response_headers.getSetCookie().map(v=>v.split(';')[0]).join('; ');
   if(cookie)await this.ctx.storage.put('guest-session',{cookie,expires:Date.now()+config.guest_session_seconds*1000});}
  if(result.status==='WAIT_OR_BLOCKED'){await this.ctx.storage.setAlarm(Date.now()+config.request_spacing_seconds*1000);return;}
  await this.finish(key,result.status);
 }
}
export class SportsControl extends WorkerEntrypoint<SportsEnv> {
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
 async reparse(event:string,version:string):Promise<string> {
  if(!/^sports:[a-z]+:\d+:[0-9a-f]{64}$/.test(event)||!/^sports-(?:odds|program)-v\d+$/.test(version))throw new Error('PARSER_ID');
  const object=await this.env.RAW.get(`manifests/${event}.json`);if(!object)throw new Error('MANIFEST_MISSING');
  const m=await object.json<CaptureManifest & {target:Target}>();return normalize(this.env,event,m.target,version);
 }
}
export default {async fetch(){return new Response('Not found',{status:404});}} satisfies ExportedHandler<SportsEnv>;
