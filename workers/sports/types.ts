import type {CaptureStorage} from '../capture-storage';
import type {JraPage} from '../jra';
export type Sport = 'boat' | 'keirin' | 'auto';
export type CaptureSport = Sport | 'jra';
export type Quote = {combination: number[]; display: string; lower: number | null; upper: number | null; status: 'NUMERIC' | 'ZERO_DISPLAY' | 'UNAVAILABLE'};
export type Market = {market: string; quotes: Quote[]; expected: number; complete: boolean; source_time_label: string | null};
export type Snapshot = {schema: 'sports-odds-v1'; sport: Sport; race_id: string; phase: 'INTERMEDIATE' | 'CLOSE_ONLY' | 'FINAL_ONLY' | 'UNKNOWN';
 source_updated_at: null; source_published_at: string | null; time_semantics: string; markets: Market[]};
export type ResultSnapshot = {schema:'sports-result-v1';sport:CaptureSport;race_id:string;phase:'RESULT_ONLY';publication:'PUBLISHED'|'PENDING';
 source_updated_at:null;source_published_at:null;source_time_label:string|null;
 identity_status:'REQUEST_BOUND'|'CONTEXT_VERIFIED'|'DOCUMENT_VERIFIED';identity_evidence:string|null;
 payout_unit_yen:number|null;settlement_qualified:false;
 placings:{entrant:number;rank_label:string;rank:number|null;state_label:string|null;frame?:number|null}[];
 payouts:{market:string;combination:number[]|null;combination_label:string;display:string;amount_yen:number|null;status:'NUMERIC'|'DISPLAY_ONLY'}[];
 refund_evidence:{display:string|null;source_flags:Record<string,string|boolean|null>}};
export type Target = {sport: Sport; race_id: string; url: string; body?: string; form?: boolean;
 // Public navigation/guest headers only. Never persisted in capture manifests or logs.
 headers?: Record<string,string>; kind: 'odds' | 'schedule' | 'result' | 'guest'; market?: string; entrants?: number[]; frames?: Record<string,number>; context_event?: string;
 // Venue navigation may discover an unknown race; a race detail must not fan out again.
 discovery_stage?: 'venue' | 'race';deadline_at?:number};
export type JraTarget = Omit<Target,'sport'|'kind'|'market'|'discovery_stage'> & {
 sport:'jra';kind:'odds';page:JraPage;body:string;form:true;discovery_stage?:never;context_phase?:'INTERMEDIATE'|'FINAL_ONLY'};
export type JraScheduleTarget = Omit<Target,'sport'|'kind'|'market'|'discovery_stage'> & {
 sport:'jra';kind:'schedule';discovery_stage:'catalog'|'venue';body:string;form:true;program_kind?:'results'};
export type JraResultTarget = Omit<Target,'sport'|'kind'|'market'|'discovery_stage'|'context_event'|'deadline_at'> & {
 sport:'jra';kind:'result';body:string;form:true;market?:never;discovery_stage?:never;context_event?:never;deadline_at?:never};
export type CaptureTarget = Target | JraTarget | JraScheduleTarget | JraResultTarget;
export interface SportsEnv extends CaptureStorage {
 SPORTS_ENABLED: string; SPORTS_PROVIDERS_JSON: string;
 SPORTS_DAILY_ENABLED?: string;
 SPORTS: DurableObjectNamespace<import('./index').SportsCollector>;
}
