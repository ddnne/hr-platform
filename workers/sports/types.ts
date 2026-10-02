import type {CaptureStorage} from '../capture-storage';
export type Sport = 'boat' | 'keirin' | 'auto';
export type Quote = {combination: number[]; display: string; lower: number | null; upper: number | null; status: 'NUMERIC' | 'ZERO_DISPLAY' | 'UNAVAILABLE'};
export type Market = {market: string; quotes: Quote[]; expected: number; complete: boolean; source_time_label: string | null};
export type Snapshot = {schema: 'sports-odds-v1'; sport: Sport; race_id: string; phase: 'INTERMEDIATE' | 'CLOSE_ONLY' | 'FINAL_ONLY' | 'UNKNOWN';
 source_updated_at: null; source_published_at: string | null; time_semantics: string; markets: Market[]};
export type Target = {sport: Sport; race_id: string; url: string; body?: string; form?: boolean;
 // Public navigation/guest headers only. Never persisted in capture manifests or logs.
 headers?: Record<string,string>; kind: 'odds' | 'schedule' | 'result' | 'guest'; market?: string; entrants?: number[]; frames?: Record<string,number>; context_event?: string};
export interface SportsEnv extends CaptureStorage {
 SPORTS_ENABLED: string; SPORTS_PROVIDERS_JSON: string;
 SPORTS: DurableObjectNamespace<import('./index').SportsCollector>;
}
