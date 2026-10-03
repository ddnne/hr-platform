import {boundedBody,discard,fetchPublic,retryAfter} from '../http';
import {digest,iso,publishCapture,saveCapture,type CaptureManifest} from '../capture-storage';
import config from '../../configs/sports-collection.json';
import jra from '../../configs/jra-source.json';
import {normalize,normalizationKind,resourceId} from './storage';
import {validateContext,requiresContext,contextNavigation,RaceContextError} from './context';
export {validateContext} from './context';
import {date} from './discovery';
import {jraNavigationIdentity} from '../jra-program';
import {supportsResult} from './results';
import type {SportsEnv,CaptureTarget,CaptureSport} from './types';
export const sourceFor=(sport:CaptureSport)=>`sports-${sport}`;
export const concurrencyFor=(sport?:CaptureSport)=>sport==='jra'?jra.finite_maximum_parallel_requests:config.maximum_parallel_requests;
export const spacingFor=(sport?:CaptureSport)=>sport==='jra'?jra.finite_request_spacing_seconds:config.request_spacing_seconds;
export function validateTarget(t:CaptureTarget):void {
 if(t.sport==='jra'){
  const u=new URL(t.url),id=t.race_id.match(/^jra:(\d{8}):(\d+):(\d+)$/),body=new URLSearchParams(t.body),name=body.get('cname');
  if(u.origin!==jra.origin||u.pathname!==jra.odds_path||u.search||u.hash||u.username||u.password)throw new Error('TARGET_ORIGIN');
  if(t.kind==='schedule'){
   if(!t.form||body.size!==1||!name||t.context_event!==undefined||t.deadline_at!==undefined)throw new Error('READ_FORM_REQUIRED');
   if(!id||Number(id[3])!==0)throw new Error('RACE_ID');date(id[1]);
   if(t.discovery_stage==='catalog'){
    if(Number(id[2])!==0||name!==jra.catalog_navigation)throw new Error('PROGRAM_NAVIGATION');
   }else if(t.discovery_stage==='venue'){
    const identity=jraNavigationIdentity(name,'venue');
    if(identity.day!==id[1]||identity.venue!==Number(id[2]))throw new Error('PROGRAM_IDENTITY');
   }else throw new Error('DISCOVERY_STAGE');
   return;
  }
  if(t.kind!=='odds'||!t.form||!Object.hasOwn(jra.tables,t.page)||body.size!==1||!name||
   !name.startsWith(jra.navigation_prefixes[t.page])||name.length>jra.maximum_navigation_length||!/^[A-Za-z0-9/+]+$/.test(name))throw new Error('READ_FORM_REQUIRED');
  if(!id||!Object.hasOwn(jra.venues,id[2])||Number(id[3])<1||Number(id[3])>jra.maximum_race_number)throw new Error('RACE_ID');
  date(id[1]);
  if(t.page==='win_place'?t.context_event!==undefined:!t.context_event)throw new Error('RACE_CONTEXT_REQUIRED');
  if(t.deadline_at!==undefined&&!Number.isSafeInteger(t.deadline_at))throw new Error('CAPTURE_DEADLINE');
  return;
 }
 const p=config.sources[t.sport],u=new URL(t.url);
 if(!p || u.origin!==p.origin || !(p.paths as string[]).includes(u.pathname) && !(t.sport==='auto' && config.sources.auto.guest_pages.includes(u.pathname)))throw new Error('TARGET_ORIGIN');
 if(u.username||u.password||u.hash || !['odds','guest','schedule','result'].includes(t.kind))throw new Error('TARGET_KIND');
 if(!new RegExp(`^${t.sport}:\\d{8}:\\d{1,2}:\\d{1,2}$`).test(t.race_id))throw new Error('RACE_ID');
 const [,day,venue,no]=t.race_id.split(':');date(day);
 if(['odds','result'].includes(t.kind)&&(Number(venue)<1||Number(venue)>config.discovery.maximum_venue_number||Number(no)<1||Number(no)>config.discovery.maximum_race_number))throw new Error('RACE_ID');
 if(t.discovery_stage!==undefined&&(!['venue','race'].includes(t.discovery_stage)||!['schedule','guest'].includes(t.kind)))throw new Error('DISCOVERY_STAGE');
 if(t.deadline_at!==undefined&&(t.kind!=='odds'||!Number.isSafeInteger(t.deadline_at)))throw new Error('CAPTURE_DEADLINE');
 if(t.sport==='boat'&&['odds','result'].includes(t.kind)){const race=`boat:${u.searchParams.get('hd')}:${Number(u.searchParams.get('jcd'))}:${Number(u.searchParams.get('rno'))}`;if(t.race_id!==race)throw new Error('RACE_ID');}
 if(t.kind==='result'&&!supportsResult(t))throw new Error('RESULT_RESOURCE');
 if(t.sport==='auto'&&u.pathname==='/race_info/RaceResult'&&t.kind!=='result')throw new Error('RESULT_RESOURCE');
 if(t.sport==='keirin'&&(requiresContext(t)&&(!t.context_event||!contextNavigation(t))||t.kind==='odds'&&(!t.entrants||!t.market)))throw new Error('RACE_CONTEXT_REQUIRED');
 if(t.sport==='keirin'&&u.searchParams.get('type')==='JST010'&&(t.kind!=='schedule'||u.searchParams.get('url.media.flg')!==config.sources.keirin.runners_media_flag))throw new Error('READ_API_PARAMS');
 if(t.sport==='keirin'&&u.pathname==='/pc/json'&&!config.sources.keirin.read_json_types.includes(u.searchParams.get('type')??''))throw new Error('READ_API_REQUIRED');
 if(t.sport==='auto'&&['/race_info/Odds','/race_info/OtherRaceInfo','/race_info/RaceResult'].includes(u.pathname)&&t.body===undefined)throw new Error('READ_POST_REQUIRED');
 if(t.form){if(t.sport!=='keirin'||u.pathname!=='/pc/racelive'||t.kind!=='guest'||new URLSearchParams(t.body).size!==1||!new URLSearchParams(t.body).get('encp'))throw new Error('READ_FORM_REQUIRED');}
 else if(t.body!==undefined){if(t.sport!=='auto'||!['/race_info/Odds','/race_info/OtherRaceInfo','/race_info/RaceResult'].includes(u.pathname))throw new Error('READ_POST_REQUIRED');
  const b=JSON.parse(t.body);if(Object.keys(b).sort().join(',')!=='placeCode,raceDate,raceNo'||!Number.isInteger(b.placeCode)||!Number.isInteger(b.raceNo)||!/^\d{4}-\d{2}-\d{2}$/.test(b.raceDate))throw new Error('READ_POST_BODY');
  if(t.race_id!==`auto:${b.raceDate.replaceAll('-','')}:${b.placeCode}:${b.raceNo}`)throw new Error('RACE_ID');}
}
export async function collect(at:number,env:SportsEnv,t:CaptureTarget,onResponse?:(body:Uint8Array,headers:Headers)=>Promise<void>,parallel=false,deadline=t.deadline_at):Promise<{status:string;event_id:string;body?:Uint8Array;response_headers?:Headers;received_at?:string}> {
 validateTarget(t);
 const spacing=spacingFor(t.sport)*1000;
 const concurrency=concurrencyFor(t.sport);
 const resource=await resourceId(t),event=`sports:${t.sport}:${at}:${resource}`,source=sourceFor(t.sport),started=Date.now();
 const active=env.SPORTS_ENABLED==='true'&&JSON.parse(env.SPORTS_PROVIDERS_JSON).includes(t.sport);
 if(!active)return {status:'DISABLED',event_id:event};
 const prior=await env.INDEX.prepare('SELECT status,error_code FROM captures WHERE event_id=?').bind(event).first<{status:string;error_code:string|null}>();
 if(prior){
  if(['SOURCE_DENIED','CHALLENGE','INCOMPLETE_FETCH'].includes(prior.error_code??''))await env.INDEX.prepare('UPDATE source_control SET blocked=1 WHERE source=?').bind(source).run();
  if(['FETCHING','STORAGE_ERROR'].includes(prior.status)) {
   const object=await env.RAW.get(`manifests/${event}.json`);
   if(object){const m=await object.json<CaptureManifest>();if(await env.RAW.head(`raw/${m.raw_sha256}`))await publishCapture(env,m,started);
    else if(Date.now()>at+(config.capture_window_seconds+config.request_timeout_seconds)*1000){await env.INDEX.prepare("UPDATE captures SET status='FAILED',error_code='RAW_MISSING' WHERE event_id=?").bind(event).run();return {status:'RAW_MISSING',event_id:event};}}
   else if(prior.status==='STORAGE_ERROR'&&Date.now()>at+(config.capture_window_seconds+config.request_timeout_seconds)*1000){await env.INDEX.prepare("UPDATE captures SET status='FAILED',error_code='RAW_MISSING' WHERE event_id=?").bind(event).run();return {status:'RAW_MISSING',event_id:event};}
   else if(Date.now()>at+(config.capture_window_seconds+config.request_timeout_seconds)*1000)await env.INDEX.batch([
    env.INDEX.prepare("UPDATE captures SET status='FAILED',error_code='INCOMPLETE_FETCH' WHERE event_id=? AND status='FETCHING'").bind(event),
    env.INDEX.prepare('UPDATE source_control SET blocked=1 WHERE source=?').bind(source)]);
  }
  const saved=await env.INDEX.prepare('SELECT raw_sha256,received_at FROM raw_observations WHERE observation_id=?').bind(event).first<{raw_sha256:string;received_at:string}>();
  if(saved&&normalizationKind(t))await normalize(env,event,t);
  return {status:saved?'RAW_STORED':prior.status,event_id:event,...saved?{received_at:saved.received_at}:{}};
 }
 try{await validateContext(t,env,started);}catch(e){if(!(e instanceof RaceContextError))throw e;await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status,error_code) VALUES(?,?,'FAILED','INVALID_CONTEXT')").bind(event,iso(at)).run();return {status:'INVALID_CONTEXT',event_id:event};}
 if(!Number.isSafeInteger(at)||Date.now()<at||Date.now()-at>config.capture_window_seconds*1000||deadline!==undefined&&Date.now()>=deadline){
  await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'MISSED_WINDOW')").bind(event,iso(at)).run();return {status:'MISSED_WINDOW',event_id:event};}
 await env.INDEX.prepare('INSERT OR IGNORE INTO source_control(source) VALUES(?)').bind(source).run();
 let insertion;
 if(parallel){
  // The single atomic insert shares the provider stop/wait and concurrency cap.
  // FETCHING remains claimed through raw publication, including crash recovery.
  insertion=await env.INDEX.prepare(`INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,fetch_started_at,status)
   SELECT ?,?,?,'FETCHING' WHERE EXISTS(SELECT 1 FROM source_control WHERE source=? AND blocked=0 AND next_allowed_at<=?)
   AND (SELECT count(*) FROM captures WHERE status='FETCHING' AND event_id GLOB ?)<?`)
   .bind(event,iso(at),iso(Date.now()),source,Date.now(),`sports:${t.sport}:*`,concurrency).run();
  if(!insertion.meta.changes)return {status:'WAIT_OR_BLOCKED',event_id:event};
 }else {
  const claim=await env.INDEX.prepare(`UPDATE source_control SET owner_event_id=?,next_allowed_at=? WHERE source=? AND blocked=0 AND next_allowed_at<=?
   AND NOT EXISTS(SELECT 1 FROM captures WHERE status='FETCHING' AND event_id GLOB ?)`)
   .bind(event,Date.now()+config.request_timeout_seconds*1000+spacing,source,Date.now(),`sports:${t.sport}:*`).run();
  if(!claim.meta.changes)return {status:'WAIT_OR_BLOCKED',event_id:event};
  insertion=await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,fetch_started_at,status) VALUES(?,?,?,'FETCHING')").bind(event,iso(at),iso(Date.now())).run();
 }
 if(!insertion.meta.changes)return {status:'DUPLICATE',event_id:event};
 let http:number|null=null,headersAt:string|null=null,received:string|null=null,stop=false,stage='HTTP';
 const remaining=(deadline??Infinity)-Date.now(),timeout=config.request_timeout_seconds*1000;
 const abortCode=remaining<=timeout?'DEADLINE_REACHED':'FETCH_TIMEOUT';
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),Math.max(1,Math.min(timeout,remaining)));
 try {
  if(Date.now()-at>config.capture_window_seconds*1000||deadline!==undefined&&Date.now()>=deadline)throw new Error('MISSED_WINDOW');
  const requestHeaders=t.sport==='jra'?{'Content-Type':'application/x-www-form-urlencoded'}:t.headers;
  const response=await fetchPublic(t.url,t.sport==='jra'||t.sport==='boat'||t.kind==='guest'?'text/html':'application/json',controller.signal,requestHeaders,t.body);
  http=response.status;headersAt=iso(Date.now());
  if([401,403,419].includes(http)||response.headers.get('cf-mitigated')==='challenge'){stop=true;await discard(response);throw new Error('SOURCE_DENIED');}
  if(http===429){await env.INDEX.prepare('UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?').bind(retryAfter(response.headers.get('retry-after'),Date.now(),config.interval_seconds*1000),source).run();const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}throw new Error('RATE_LIMITED');}
  if(http!==200){const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}throw new Error('HTTP_ERROR');}
  const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());
  if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}
  // Publish the finite JRA wait before raw publication releases the FETCHING slot.
  if(t.sport==='jra')await env.INDEX.prepare('UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?')
   .bind(Date.parse(received)+spacing,source).run();
  stage='STORAGE';if(onResponse)await onResponse(body,response.headers);const hash=await digest(body),present=await env.RAW.head(`raw/${hash}`);
  const {headers,...safeTarget}=t;
  const m:CaptureManifest & {target:Omit<CaptureTarget,'headers'>}={event_id:event,scheduled_capture_at:iso(at),fetch_started_at:iso(started),headers_received_at:headersAt,
   collector_received_at:received,raw_saved_at:present?iso(Date.now()):null,raw_sha256:hash,raw_bytes:body.length,http_status:http,
   etag:null,validator_sent:null,validator_raw_sha256:null,file_name:null,file_timestamp:null,duration_ms:Date.now()-started,
   dataset_kind:`SPORT_${t.sport.toUpperCase()}_${t.kind.toUpperCase()}`,url:t.url,race_id:t.race_id,target:safeTarget};
  await saveCapture(env,m,present?null:body,started);
  if(normalizationKind(t))await normalize(env,event,t);
  return {status:'RAW_STORED',event_id:event,body,response_headers:response.headers,received_at:received};
 }catch(e){const reason=e instanceof Error?e.message:'';
  if(reason==='GUEST_FORMAT')stop=true;
  const code=['GUEST_FORMAT','SOURCE_DENIED','RATE_LIMITED','HTTP_ERROR','MISSED_WINDOW','CHALLENGE','BODY_LIMIT','BODY_EMPTY'].includes(reason)?reason:stage==='STORAGE'?'STORAGE_ERROR':controller.signal.aborted?abortCode:'NETWORK_ERROR';
  await env.INDEX.prepare('UPDATE captures SET status=?,error_code=?,http_status=?,headers_received_at=?,collector_received_at=?,duration_ms=? WHERE event_id=?')
   .bind(code==='STORAGE_ERROR'?'STORAGE_ERROR':'FAILED',code,http,headersAt,received,Date.now()-started,event).run();
  return {status:code,event_id:event};
 }finally{clearTimeout(timer);await env.INDEX.prepare('UPDATE source_control SET blocked=max(blocked,?),next_allowed_at=max(next_allowed_at,?) WHERE source=?')
   .bind(Number(stop),Date.now()+spacing,source).run();
  // Release only this request's lease. A 429 keeps the durable Retry-After floor.
  if(!parallel&&http!==429)await env.INDEX.prepare('UPDATE source_control SET next_allowed_at=? WHERE source=? AND owner_event_id=?')
   .bind(Date.now()+spacing,source,event).run();}
}
