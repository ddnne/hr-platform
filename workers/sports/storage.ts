import {digest,iso,type CaptureStorage} from '../capture-storage';
import config from '../../configs/sports-collection.json';
import {parseOdds} from './parsers';
import type {Sport,Target} from './types';
export async function normalize(env:CaptureStorage,event:string,target:Target,version=config.parser_version):Promise<string> {
 const manifest=await env.RAW.get(`manifests/${event}.json`);if(!manifest)throw new Error('MANIFEST_REQUIRED');
 const saved=await manifest.json<{target:Target}>();if(!saved.target)throw new Error('PARSE_RECIPE_REQUIRED');
 target=saved.target;
 const existing=await env.INDEX.prepare('SELECT status FROM sports_parses WHERE observation_id=? AND parser_version=?').bind(event,version).first<{status:string}>();
 if(existing)return existing.status;
 const observation=await env.INDEX.prepare('SELECT raw_sha256 FROM raw_observations WHERE observation_id=? AND dataset_kind=?')
  .bind(event,`SPORT_${target.sport.toUpperCase()}_ODDS`).first<{raw_sha256:string}>();
 if(!observation)throw new Error('OBSERVATION_REQUIRED');
 const raw=await env.RAW.get(`raw/${observation.raw_sha256}`);if(!raw)throw new Error('RAW_MISSING');
 let key:string|null=null,error:string|null=null,status='PARSE_ERROR';
 const text=await raw.text();let result;
 try {result=parseOdds(text,target);}catch {error='PROVIDER_FORMAT';}
 if(result){const body=new TextEncoder().encode(JSON.stringify(result));
  key=`sports/normalized/${await digest(body)}.json`;const saved=await env.RAW.put(key,body);if(!saved)throw new Error('NORMALIZED_PUT_FAILED');
  status=result.markets.every(m=>m.complete)?'COMPLETE':'INCOMPLETE';
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
export async function history(env:CaptureStorage,sport:Sport,race:string,cutoff:string,limit=config.maximum_history_rows,after='') {
 if(!Number.isFinite(Date.parse(cutoff)) || !Number.isInteger(limit)||limit<1||limit>config.maximum_history_rows)throw new Error('HISTORY_QUERY');
 const canonical=iso(Date.parse(cutoff));
 const rows=await env.INDEX.prepare(`SELECT p.*,o.received_at,o.raw_saved_at,o.raw_sha256 FROM sports_parses p
 JOIN raw_observations o ON o.observation_id=p.observation_id
 WHERE p.sport=? AND p.race_id=? AND p.available_at<=? AND o.received_at<=?
 AND (o.received_at||'|'||p.observation_id||'|'||p.parser_version)>?
 ORDER BY o.received_at,p.observation_id,p.parser_version LIMIT ?`).bind(sport,race,canonical,canonical,after,limit).all();
 return rows.results;
}
