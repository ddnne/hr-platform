/** Shared race identity evidence for odds, result collection and saved-raw parsing. */
import type {CaptureManifest,CaptureStorage} from '../capture-storage';
import type {CaptureTarget,JraTarget} from './types';
import config from '../../configs/jra-source.json';
import type {JraRunners,JraSnapshot} from '../jra';
export class RaceContextError extends Error {}
function contextJson(raw:string):any {try{return JSON.parse(raw);}catch{throw new RaceContextError('RACE_CONTEXT_FORMAT');}}
export function requiresContext(t:CaptureTarget):boolean {
 if(t.sport==='jra')return t.page!=='win_place';
 if(t.sport!=='keirin')return false;const u=new URL(t.url);
 return t.kind==='guest'&&t.discovery_stage==='race'||['odds','result'].includes(t.kind)||t.kind==='schedule'&&u.pathname==='/pc/json'&&u.searchParams.get('type')==='JST010';
}
export const contextNavigation=(t:CaptureTarget)=>t.form?new URLSearchParams(t.body).get('encp'):new URL(t.url).searchParams.get('encp');
export async function jraRunners(t:JraTarget,env:CaptureStorage,at:number):Promise<JraRunners|undefined> {
 if(t.page==='win_place')return undefined;
 if(!t.context_event)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
 const cutoff=new Date(at).toISOString().replace('Z','000+00:00');
 const row=await env.INDEX.prepare(`SELECT p.status,p.normalized_key,o.received_at FROM sports_parses p
 JOIN raw_observations o ON p.observation_id=o.observation_id WHERE p.observation_id=?
 AND p.sport='jra' AND p.race_id=? AND p.parser_version LIKE 'sports-odds-v%'
 AND p.available_at<=? AND o.received_at<=? AND o.dataset_kind='SPORT_JRA_ODDS'
 ORDER BY p.available_at DESC,CAST(substr(p.parser_version,length('sports-odds-v')+1) AS INTEGER) DESC LIMIT 1`)
 .bind(t.context_event,t.race_id,cutoff,cutoff).first<{status:string;normalized_key:string|null;received_at:string}>();
 if(!row||row.status!=='COMPLETE'||!row.normalized_key||at-Date.parse(row.received_at)>config.maximum_context_age_seconds*1000)
  throw new RaceContextError('RACE_CONTEXT_REQUIRED');
 const meta=await env.RAW.get(`manifests/${t.context_event}.json`);
 if(!meta)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
 const manifest=await meta.json<{target:CaptureTarget}>(),recipe=manifest.target;
 if(recipe?.sport!=='jra'||recipe.kind!=='odds'||recipe.page!=='win_place'||recipe.race_id!==t.race_id)
  throw new RaceContextError('RACE_CONTEXT_IDENTITY');
 const object=await env.RAW.get(row.normalized_key);if(!object)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
 const snapshot=await object.json<JraSnapshot>();
 if(snapshot.sport!=='jra'||snapshot.race_id!==t.race_id||!snapshot.runners||snapshot.markets.length!==2||
  !snapshot.markets.every(m=>['win','place'].includes(m.market)&&m.complete))throw new RaceContextError('RACE_CONTEXT_IDENTITY');
 return snapshot.runners;
}
export async function validateContext(t:CaptureTarget,env:CaptureStorage,at=Date.now()):Promise<void> {
 if(t.sport==='jra'){await jraRunners(t,env,at);return;}
 if(requiresContext(t)){
  if(!t.context_event)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
  const evidence=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=? AND dataset_kind=\'SPORT_KEIRIN_SCHEDULE\'').bind(t.context_event).first<{raw_sha256:string}>();
  const meta=await env.RAW.get(`manifests/${t.context_event}.json`);if(!evidence||!meta)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
  const manifest:CaptureManifest=contextJson(await meta.text());let request:URL;
  try{request=new URL(manifest.url!);}catch{throw new RaceContextError('RACE_CONTEXT_IDENTITY');}
  if(request.searchParams.get('type')!=='JST015'||request.searchParams.get('encp')!==contextNavigation(t))throw new RaceContextError('RACE_CONTEXT_IDENTITY');
  const object=await env.RAW.get(`raw/${evidence.raw_sha256}`);if(!object)throw new RaceContextError('RACE_CONTEXT_REQUIRED');const response=contextJson(await object.text());const d=response?.data;
  if(response?.resultCd!==0||!d||t.race_id!==`keirin:${d.kaisaiDate}:${Number(d.keirinJyoCd)}:${Number(d.raceNo)}`)throw new RaceContextError('RACE_CONTEXT_IDENTITY');
 }
}
