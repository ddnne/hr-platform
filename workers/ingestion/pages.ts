/** Small prospective evidence plans. Transport and provider controls stay in capture.ts. */
import policy from "../../configs/cloud-collection.json";
import collection from "../../configs/collection.json";
import schedule from "../../configs/cloud-paper-schedule.json";

export type PageTarget = {kind: "state" | "payout"; url: string; race_id: string};
export type EvidenceTarget = {kind: "state" | "payout" | "race"; url: string; race_id: string};
export type PagePlan = EvidenceTarget & {at: number; event_id: string; registered_at: string};

export function validatePage(target: EvidenceTarget): void {
  if (!target || !["state", "payout", "race"].includes(target.kind)
      || typeof target.race_id !== "string" || typeof target.url !== "string") throw new Error("PAGE_TARGET");
  const match = /^(\d{4})(\d{2})(\d{2}):[^:]+:([1-9]|1[0-2])$/.exec(target.race_id);
  if (!match) throw new Error("PAGE_RACE");
  const [, year, month, day, race] = match;
  const date = `${year}-${month}-${day}`;
  const parsed = Date.parse(date);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 10) !== date) throw new Error("PAGE_DATE");
  if (target.kind === "race") {
    if (target.url !== policy.urls.race) throw new Error("PAGE_URL");
    return;
  }
  const url = new URL(target.url);
  if (url.origin !== "https://www.keiba.go.jp" || url.username || url.password || url.hash
      || url.pathname !== policy.page_paths[target.kind]
      || [...url.searchParams.keys()].sort().join() !== "k_babaCode,k_raceDate,k_raceNo"
      || url.searchParams.get("k_raceDate") !== `${year}/${month}/${day}`
      || url.searchParams.get("k_raceNo") !== race
      || !/^\d{2}$/.test(url.searchParams.get("k_babaCode") ?? "")) throw new Error("PAGE_URL");
}

export async function registerPage(env: Env, at: number, target: PageTarget): Promise<string> {
  return (await registerEvidenceBatch(env, [{at, ...target}]))[0];
}

/** Reserve a complete input packet atomically, through the same provider queue. */
export async function registerEvidenceBatch(env: Env, entries: (EvidenceTarget & {at: number})[], revision?: string): Promise<string[]> {
  if (env.COLLECTION_ENABLED !== "true" || env.SOURCE_APPROVED !== "true"
      || env.DAILY_COLLECTION_ENABLED !== "true") return ["DISABLED"];
  if (!Array.isArray(entries) || !entries.length || entries.length > policy.page_max_pending) throw new Error("PAGE_PLAN_LIMIT");
  const now = Date.now();
  const rows = entries.map(e => ({...e, event_id: `nar-daily-${e.kind}:${e.at}`}));
  if (new Set(rows.map(e => e.event_id)).size !== rows.length) throw new Error("PAGE_PLAN_CONFLICT");
  const automatic = revision !== undefined;
  if (automatic && (typeof revision !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00\|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00\|[0-9a-f]{64}$/.test(revision))) throw new Error("PAGE_PACKET_REVISION");
  const owner = automatic ? schedule.version : null;
  const race = rows[0].race_id;
  if (automatic && (rows.length !== 3 || rows.map(e => e.kind).sort().join() !== "payout,race,state"
      || rows.some(e => e.race_id !== race))) throw new Error("PAGE_PACKET_REQUIRED");
  for (const entry of rows) {
    validatePage(entry);
    const old = await env.INDEX.prepare("SELECT * FROM page_capture_plans WHERE event_id=?").bind(entry.event_id).first<PagePlan>();
    if (old) {
      if (old.url !== entry.url || old.race_id !== entry.race_id) throw new Error("PAGE_PLAN_CONFLICT");
    } else if (!Number.isSafeInteger(entry.at) || entry.at < now + policy.page_min_lead_seconds * 1000
      || entry.at > now + policy.page_horizon_seconds * 1000) throw new Error("PAGE_PLAN_TIME");
  }
  const body = JSON.stringify(rows);
  // Both writes run in one D1 transaction. Recheck conflicts inside SQL so a
  // concurrent registration cannot leave a partly accepted packet or cancel it.
  const obsolete = `p.packet_owner IS ? AND p.race_id=? AND p.at>? AND NOT EXISTS
    (SELECT 1 FROM json_each(?) j WHERE p.event_id=json_extract(j.value,'$.event_id'))`;
  const guard = `NOT EXISTS (SELECT 1 FROM page_capture_plans p
      WHERE p.packet_owner IS ? AND p.race_id=? AND p.packet_revision>?)
    AND NOT EXISTS (SELECT 1 FROM page_capture_plans p JOIN json_each(?) j
      ON p.event_id=json_extract(j.value,'$.event_id') LEFT JOIN captures c USING(event_id)
      WHERE p.url<>json_extract(j.value,'$.url') OR p.race_id<>json_extract(j.value,'$.race_id')
        OR c.status='SUPERSEDED_PLAN')
    AND (SELECT count(*) FROM page_capture_plans p LEFT JOIN captures c USING(event_id)
      WHERE c.event_id IS NULL AND NOT (${obsolete}))
      + (SELECT count(*) FROM json_each(?) j WHERE NOT EXISTS
        (SELECT 1 FROM page_capture_plans p WHERE p.event_id=json_extract(j.value,'$.event_id'))) <= ?`;
  const guardArgs = [owner ?? "", race, revision ?? "", body, owner ?? "", race, now, body, body, policy.page_max_pending];
  const insert = env.INDEX.prepare(`INSERT OR IGNORE INTO page_capture_plans
    SELECT json_extract(value,'$.event_id'),json_extract(value,'$.at'),json_extract(value,'$.kind'),
      json_extract(value,'$.url'),json_extract(value,'$.race_id'),strftime('%Y-%m-%dT%H:%M:%f000+00:00','now'),?,?
    FROM json_each(?) WHERE ${guard}`).bind(owner, revision ?? null, body, ...guardArgs);
  if (automatic) {
    await env.INDEX.batch([
      env.INDEX.prepare(`INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status)
        SELECT p.event_id,strftime('%Y-%m-%dT%H:%M:%f000+00:00',p.at/1000.0,'unixepoch'),'SUPERSEDED_PLAN'
        FROM page_capture_plans p LEFT JOIN captures c USING(event_id)
        WHERE c.event_id IS NULL AND ${obsolete} AND ${guard}`)
        .bind(owner, race, now, body, ...guardArgs),
      insert,
      env.INDEX.prepare(`UPDATE page_capture_plans SET packet_revision=?
        WHERE packet_owner IS ? AND packet_revision<? AND event_id IN
          (SELECT json_extract(value,'$.event_id') FROM json_each(?)) AND ${guard}`)
        .bind(revision, owner, revision, body, ...guardArgs),
    ]);
  } else await insert.run();
  for (const entry of rows) {
    const saved = await env.INDEX.prepare(`SELECT p.url,p.race_id,c.status FROM page_capture_plans p
      LEFT JOIN captures c USING(event_id) WHERE p.event_id=?`)
      .bind(entry.event_id).first<{url: string; race_id: string; status: string | null}>();
    if (!saved) throw new Error("PAGE_PLAN_LIMIT");
    if (saved.url !== entry.url || saved.race_id !== entry.race_id || saved.status === "SUPERSEDED_PLAN") throw new Error("PAGE_PLAN_CONFLICT");
  }
  return rows.map(e => e.event_id);
}

export async function nextPage(env: Env, nextRegularAt: number): Promise<PagePlan | null> {
  // A page consumes a regular provider slot. It never adds a parallel request.
  // Keep an expired plan's original time, so capture records a gap without fetching.
  return env.INDEX.prepare(`SELECT p.* FROM page_capture_plans p LEFT JOIN captures c USING(event_id)
    WHERE c.event_id IS NULL AND p.at<=? ORDER BY p.at,p.event_id LIMIT 1`)
    .bind(nextRegularAt + collection.interval_seconds * 1000).first<PagePlan>();
}
