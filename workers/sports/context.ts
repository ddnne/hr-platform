/** Shared race identity evidence for odds, result collection and saved-raw parsing. */
import type {CaptureManifest,CaptureStorage} from '../capture-storage';
import type {Target} from './types';
export class RaceContextError extends Error {}
function contextJson(raw:string):any {try{return JSON.parse(raw);}catch{throw new RaceContextError('RACE_CONTEXT_FORMAT');}}
export async function validateContext(t:Target,env:CaptureStorage):Promise<void> {
 if(t.sport==='keirin'&&['odds','result'].includes(t.kind)){
  if(!t.context_event)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
  const evidence=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=? AND dataset_kind=\'SPORT_KEIRIN_SCHEDULE\'').bind(t.context_event).first<{raw_sha256:string}>();
  const meta=await env.RAW.get(`manifests/${t.context_event}.json`);if(!evidence||!meta)throw new RaceContextError('RACE_CONTEXT_REQUIRED');
  const manifest:CaptureManifest=contextJson(await meta.text());let request:URL;
  try{request=new URL(manifest.url!);}catch{throw new RaceContextError('RACE_CONTEXT_IDENTITY');}
  if(request.searchParams.get('type')!=='JST015'||request.searchParams.get('encp')!==new URL(t.url).searchParams.get('encp'))throw new RaceContextError('RACE_CONTEXT_IDENTITY');
  const object=await env.RAW.get(`raw/${evidence.raw_sha256}`);if(!object)throw new RaceContextError('RACE_CONTEXT_REQUIRED');const response=contextJson(await object.text());const d=response?.data;
  if(response.resultCd!==0||!d||t.race_id!==`keirin:${d.kaisaiDate}:${Number(d.keirinJyoCd)}:${Number(d.raceNo)}`)throw new RaceContextError('RACE_CONTEXT_IDENTITY');
 }
}
