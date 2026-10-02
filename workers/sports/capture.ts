import {boundedBody,discard,fetchPublic,retryAfter} from '../http';
import {digest,iso,publishCapture,saveCapture,type CaptureManifest} from '../capture-storage';
import config from '../../configs/sports-collection.json';
import {normalize,resourceId} from './storage';
import {supportsProgram} from './discovery';
import type {SportsEnv,Target,Sport} from './types';
export const sourceFor=(sport:Sport)=>`sports-${sport}`;
export function validateTarget(t:Target):void {
 const p=config.sources[t.sport],u=new URL(t.url);
 if(!p || u.origin!==p.origin || !(p.paths as string[]).includes(u.pathname) && !(t.sport==='auto' && config.sources.auto.guest_pages.includes(u.pathname)))throw new Error('TARGET_ORIGIN');
 if(u.username||u.password||u.hash || !['odds','guest','schedule','result'].includes(t.kind))throw new Error('TARGET_KIND');
 if(!new RegExp(`^${t.sport}:\\d{8}:\\d{1,2}:\\d{1,2}$`).test(t.race_id))throw new Error('RACE_ID');
 if(t.sport==='boat'&&['odds','result'].includes(t.kind)){const race=`boat:${u.searchParams.get('hd')}:${Number(u.searchParams.get('jcd'))}:${Number(u.searchParams.get('rno'))}`;if(t.race_id!==race)throw new Error('RACE_ID');}
 if(t.sport==='keirin'&&t.kind==='odds'&&(!t.entrants||!t.market||!t.context_event||!u.searchParams.get('encp')))throw new Error('RACE_CONTEXT_REQUIRED');
 if(t.sport==='keirin'&&u.pathname==='/pc/json'&&!config.sources.keirin.read_json_types.includes(u.searchParams.get('type')??''))throw new Error('READ_API_REQUIRED');
 if(t.form){if(t.sport!=='keirin'||u.pathname!=='/pc/racelive'||t.kind!=='guest'||new URLSearchParams(t.body).size!==1||!new URLSearchParams(t.body).get('encp'))throw new Error('READ_FORM_REQUIRED');}
 else if(t.body!==undefined){if(t.sport!=='auto'||!['/race_info/Odds','/race_info/OtherRaceInfo'].includes(u.pathname))throw new Error('READ_POST_REQUIRED');
  const b=JSON.parse(t.body);if(Object.keys(b).sort().join(',')!=='placeCode,raceDate,raceNo'||!Number.isInteger(b.placeCode)||!Number.isInteger(b.raceNo)||!/^\d{4}-\d{2}-\d{2}$/.test(b.raceDate))throw new Error('READ_POST_BODY');
  if(t.race_id!==`auto:${b.raceDate.replaceAll('-','')}:${b.placeCode}:${b.raceNo}`)throw new Error('RACE_ID');}
}
export async function validateContext(t:Target,env:SportsEnv):Promise<void> {
 if(t.sport==='keirin'&&t.kind==='odds'){
  const evidence=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=? AND dataset_kind=\'SPORT_KEIRIN_SCHEDULE\'').bind(t.context_event).first<{raw_sha256:string}>();
  const meta=await env.RAW.get(`manifests/${t.context_event}.json`);if(!evidence||!meta)throw new Error('RACE_CONTEXT_REQUIRED');
  const manifest=await meta.json<CaptureManifest>();const request=new URL(manifest.url!);
  if(request.searchParams.get('type')!=='JST015'||request.searchParams.get('encp')!==new URL(t.url).searchParams.get('encp'))throw new Error('RACE_CONTEXT_IDENTITY');
  const object=await env.RAW.get(`raw/${evidence.raw_sha256}`);if(!object)throw new Error('RACE_CONTEXT_REQUIRED');const response=await object.json<any>();const d=response.data;
  if(response.resultCd!==0||t.race_id!==`keirin:${d.kaisaiDate}:${Number(d.keirinJyoCd)}:${Number(d.raceNo)}`)throw new Error('RACE_CONTEXT_IDENTITY');
 }
}
export async function collect(at:number,env:SportsEnv,t:Target,onResponse?:(body:Uint8Array,headers:Headers)=>Promise<void>):Promise<{status:string;event_id:string;body?:Uint8Array;response_headers?:Headers}> {
 validateTarget(t);
 const resource=await resourceId(t),event=`sports:${t.sport}:${at}:${resource}`,source=sourceFor(t.sport),started=Date.now();
 const active=env.SPORTS_ENABLED==='true'&&JSON.parse(env.SPORTS_PROVIDERS_JSON).includes(t.sport);
 if(!active)return {status:'DISABLED',event_id:event};
 try{await validateContext(t,env);}catch{await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status,error_code) VALUES(?,?,'FAILED','INVALID_CONTEXT')").bind(event,iso(at)).run();return {status:'INVALID_CONTEXT',event_id:event};}
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
  const saved=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=?').bind(event).first<{raw_sha256:string}>();
  if(saved&&(t.kind==='odds'||supportsProgram(t)))await normalize(env,event,t);
  return {status:saved?'RAW_STORED':prior.status,event_id:event};
 }
 if(!Number.isSafeInteger(at)||Date.now()<at||Date.now()-at>config.capture_window_seconds*1000){
  await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'MISSED_WINDOW')").bind(event,iso(at)).run();return {status:'MISSED_WINDOW',event_id:event};}
 await env.INDEX.prepare('INSERT OR IGNORE INTO source_control(source) VALUES(?)').bind(source).run();
 const claim=await env.INDEX.prepare('UPDATE source_control SET owner_event_id=?,next_allowed_at=? WHERE source=? AND blocked=0 AND next_allowed_at<=?')
  .bind(event,Date.now()+(config.request_timeout_seconds+config.request_spacing_seconds)*1000,source,Date.now()).run();
 if(!claim.meta.changes)return {status:'WAIT_OR_BLOCKED',event_id:event};
 const insertion=await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,fetch_started_at,status) VALUES(?,?,?,'FETCHING')").bind(event,iso(at),iso(Date.now())).run();
 if(!insertion.meta.changes)return {status:'DUPLICATE',event_id:event};
 let http:number|null=null,headersAt:string|null=null,received:string|null=null,stop=false,stage='HTTP';
 const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),config.request_timeout_seconds*1000);
 try {
  if(Date.now()-at>config.capture_window_seconds*1000)throw new Error('MISSED_WINDOW');
  const response=await fetchPublic(t.url,t.sport==='boat'||t.kind==='guest'?'text/html':'application/json',controller.signal,t.headers,t.body);
  http=response.status;headersAt=iso(Date.now());
  if([401,403,419].includes(http)||response.headers.get('cf-mitigated')==='challenge'){stop=true;await discard(response);throw new Error('SOURCE_DENIED');}
  if(http===429){await env.INDEX.prepare('UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?').bind(retryAfter(response.headers.get('retry-after'),Date.now(),config.interval_seconds*1000),source).run();const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}throw new Error('RATE_LIMITED');}
  if(http!==200){const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}throw new Error('HTTP_ERROR');}
  const body=await boundedBody(response,config.maximum_raw_bytes);received=iso(Date.now());
  if(/captcha|cf-chl-|<title>[^<]*challenge/i.test(new TextDecoder().decode(body))){stop=true;throw new Error('CHALLENGE');}
  stage='STORAGE';if(onResponse)await onResponse(body,response.headers);const hash=await digest(body),present=await env.RAW.head(`raw/${hash}`);
  const {headers,...safeTarget}=t;
  const m:CaptureManifest & {target:Omit<Target,'headers'>}={event_id:event,scheduled_capture_at:iso(at),fetch_started_at:iso(started),headers_received_at:headersAt,
   collector_received_at:received,raw_saved_at:present?iso(Date.now()):null,raw_sha256:hash,raw_bytes:body.length,http_status:http,
   etag:null,validator_sent:null,validator_raw_sha256:null,file_name:null,file_timestamp:null,duration_ms:Date.now()-started,
   dataset_kind:`SPORT_${t.sport.toUpperCase()}_${t.kind.toUpperCase()}`,url:t.url,race_id:t.race_id,target:safeTarget};
  await saveCapture(env,m,present?null:body,started);
  if(t.kind==='odds'||supportsProgram(t))await normalize(env,event,t);
  return {status:'RAW_STORED',event_id:event,body,response_headers:response.headers};
 }catch(e){const reason=e instanceof Error?e.message:'';
  if(reason==='GUEST_FORMAT')stop=true;
  const code=['GUEST_FORMAT','SOURCE_DENIED','RATE_LIMITED','HTTP_ERROR','MISSED_WINDOW','CHALLENGE','BODY_LIMIT','BODY_EMPTY'].includes(reason)?reason:stage==='STORAGE'?'STORAGE_ERROR':controller.signal.aborted?'FETCH_TIMEOUT':'NETWORK_ERROR';
  await env.INDEX.prepare('UPDATE captures SET status=?,error_code=?,http_status=?,headers_received_at=?,collector_received_at=?,duration_ms=? WHERE event_id=?')
   .bind(code==='STORAGE_ERROR'?'STORAGE_ERROR':'FAILED',code,http,headersAt,received,Date.now()-started,event).run();
  return {status:code,event_id:event};
 }finally{clearTimeout(timer);await env.INDEX.prepare('UPDATE source_control SET blocked=max(blocked,?),next_allowed_at=max(next_allowed_at,?) WHERE source=?')
   .bind(Number(stop),Date.now()+config.request_spacing_seconds*1000,source).run();
  // Release only this request's lease. A 429 keeps the durable Retry-After floor.
  if(http!==429)await env.INDEX.prepare('UPDATE source_control SET next_allowed_at=? WHERE source=? AND owner_event_id=?')
   .bind(Date.now()+config.request_spacing_seconds*1000,source,event).run();}
}
