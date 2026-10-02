import {digest,iso,type CaptureStorage} from '../capture-storage';
import config from '../../configs/sports-collection.json';
import {parseOdds} from './parsers';
import {parseProgram,supportsProgram,type Program} from './discovery';
import {parseResult,supportsResult} from './results';
import {validateContext,requiresContext,RaceContextError} from './context';
import type {Sport,Target} from './types';
export function normalizationKind(t:Target):'odds'|'program'|'result'|null {
 return supportsProgram(t)?'program':supportsResult(t)?'result':t.kind==='odds'?'odds':null;
}
export async function normalize(env:CaptureStorage,event:string,target:Target,version?:string):Promise<string> {
 const manifest=await env.RAW.get(`manifests/${event}.json`);if(!manifest)throw new Error('MANIFEST_REQUIRED');
 const saved=await manifest.json<{target:Target}>();if(!saved.target)throw new Error('PARSE_RECIPE_REQUIRED');
 target=saved.target;
 const kind=normalizationKind(target);if(!kind)throw new Error('PARSE_RESOURCE');
 version??=kind==='program'?config.program_parser_version:kind==='result'?config.result_parser_version:config.parser_version;
 if(!new RegExp(`^sports-${kind}-v\\d+$`).test(version))throw new Error('PARSER_KIND');
 const existing=await env.INDEX.prepare('SELECT status FROM sports_parses WHERE observation_id=? AND parser_version=?').bind(event,version).first<{status:string}>();
 if(existing)return existing.status;
 const observation=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=? AND dataset_kind=?')
  .bind(event,`SPORT_${target.sport.toUpperCase()}_${target.kind.toUpperCase()}`).first<{raw_sha256:string}>();
 if(!observation)throw new Error('OBSERVATION_REQUIRED');
 const raw=await env.RAW.get(`raw/${observation.raw_sha256}`);if(!raw)throw new Error('RAW_MISSING');
 let key:string|null=null,error:string|null=null,status='PARSE_ERROR';
 const text=await raw.text();let result;
 if(requiresContext(target)){
  try{await validateContext(target,env);}catch(e){if(!(e instanceof RaceContextError))throw e;error='INVALID_CONTEXT';}
 }
 try {
  if(error)throw new Error(error);
  result=kind==='program'?parseProgram(text,target):kind==='result'?parseResult(text,target):parseOdds(text,target);
  if(result.schema==='sports-result-v1'&&target.sport==='keirin'){result.identity_status='CONTEXT_VERIFIED';result.identity_evidence=target.context_event!;}
 }catch {error??='PROVIDER_FORMAT';}
 if(result){const body=new TextEncoder().encode(JSON.stringify(result));
  key=`sports/normalized/${await digest(body)}.json`;const saved=await env.RAW.put(key,body);if(!saved)throw new Error('NORMALIZED_PUT_FAILED');
  status=result.schema==='sports-odds-v1'?result.markets.every(m=>m.complete)?'COMPLETE':'INCOMPLETE':
   result.schema==='sports-result-v1'?result.publication==='PUBLISHED'?'RESULT_PARSED':'RESULT_PENDING':'PROGRAM_PARSED';
 }
 const resource=await resourceId(target);
 // Publication time is assigned by D1 AFTER the immutable normalized body is stored.
 await env.INDEX.prepare(`INSERT OR IGNORE INTO sports_parses VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%f','now')||'000+00:00',?,?,?)`)
  .bind(event,version,target.sport,target.race_id,resource,iso(Date.now()),status,key,error).run();
 return status;
}
export async function resourceId(t:Target):Promise<string> {
 return digest(new TextEncoder().encode(JSON.stringify([t.sport,t.race_id,t.kind,t.url,t.body??null])));
}
export async function savedProgram(env:CaptureStorage,event:string,at=Date.now()) {
 const cutoff=iso(at);
 const row=await env.INDEX.prepare(`SELECT p.status,p.normalized_key,p.available_at,o.received_at FROM sports_parses p
 JOIN raw_observations o ON o.observation_id=p.observation_id
 WHERE p.observation_id=? AND p.parser_version LIKE 'sports-program-v%'
 AND p.available_at<=? AND o.received_at<=?
 ORDER BY p.available_at DESC,CAST(substr(p.parser_version,length('sports-program-v')+1) AS INTEGER) DESC LIMIT 1`)
 .bind(event,cutoff,cutoff).first<{status:string;normalized_key:string|null;available_at:string;received_at:string}>();
 if(!row||row.status!=='PROGRAM_PARSED'||!row.normalized_key)throw new Error('PROGRAM_UNAVAILABLE');
 if(at-Date.parse(row.received_at)>config.discovery.maximum_program_age_seconds*1000)throw new Error('PROGRAM_STALE');
 const object=await env.RAW.get(row.normalized_key);if(!object)throw new Error('NORMALIZED_MISSING');
 const manifest=await env.RAW.get(`manifests/${event}.json`);if(!manifest)throw new Error('MANIFEST_REQUIRED');
 const {target}=await manifest.json<{target:Target}>();if(!target)throw new Error('PARSE_RECIPE_REQUIRED');
 return {value:await object.json<Program>(),target,available_at:row.available_at,received_at:row.received_at};
}
export async function history(env:CaptureStorage,sport:Sport,race:string,cutoff:string,limit=config.maximum_history_rows,after='',kind:'odds'|'program'|'result'='odds') {
 if(!Number.isFinite(Date.parse(cutoff)) || !Number.isInteger(limit)||limit<1||limit>config.maximum_history_rows)throw new Error('HISTORY_QUERY');
 const canonical=iso(Date.parse(cutoff));
 const rows=await env.INDEX.prepare(`SELECT p.*,o.received_at,o.raw_saved_at,o.raw_sha256 FROM sports_parses p
 JOIN raw_observations o ON o.observation_id=p.observation_id
 WHERE p.sport=? AND p.race_id=? AND p.available_at<=? AND o.received_at<=? AND p.parser_version LIKE ?
 AND (o.received_at||'|'||p.observation_id||'|'||p.parser_version)>?
 ORDER BY o.received_at,p.observation_id,p.parser_version LIMIT ?`).bind(sport,race,canonical,canonical,`sports-${kind}-v%`,after,limit).all();
 return rows.results;
}
