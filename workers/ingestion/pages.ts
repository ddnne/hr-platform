/** Small prospective evidence plans. Transport and provider controls stay in capture.ts. */
import policy from "../../configs/cloud-collection.json";
import collection from "../../configs/collection.json";

export type PageTarget = {kind: "state" | "payout"; url: string; race_id: string};
export type PagePlan = PageTarget & {at: number; event_id: string; registered_at: string};

export function validatePage(target: PageTarget): void {
  if (!target || !["state", "payout"].includes(target.kind)
      || typeof target.race_id !== "string" || typeof target.url !== "string") throw new Error("PAGE_TARGET");
  const match = /^(\d{4})(\d{2})(\d{2}):[^:]+:([1-9]|1[0-2])$/.exec(target.race_id);
  if (!match) throw new Error("PAGE_RACE");
  const [, year, month, day, race] = match;
  const date = `${year}-${month}-${day}`;
  const parsed = Date.parse(date);
  if (!Number.isFinite(parsed) || new Date(parsed).toISOString().slice(0, 10) !== date) throw new Error("PAGE_DATE");
  const url = new URL(target.url);
  if (url.origin !== "https://www.keiba.go.jp" || url.username || url.password || url.hash
      || url.pathname !== policy.page_paths[target.kind]
      || [...url.searchParams.keys()].sort().join() !== "k_babaCode,k_raceDate,k_raceNo"
      || url.searchParams.get("k_raceDate") !== `${year}/${month}/${day}`
      || url.searchParams.get("k_raceNo") !== race
      || !/^\d{2}$/.test(url.searchParams.get("k_babaCode") ?? "")) throw new Error("PAGE_URL");
}

export async function registerPage(env: Env, at: number, target: PageTarget): Promise<string> {
  if (env.COLLECTION_ENABLED !== "true" || env.SOURCE_APPROVED !== "true"
      || env.DAILY_COLLECTION_ENABLED !== "true") return "DISABLED";
  validatePage(target);
  const event = `nar-daily-${target.kind}:${at}`;
  const old = await env.INDEX.prepare("SELECT * FROM page_capture_plans WHERE event_id=?").bind(event).first<PagePlan>();
  if (old) {
    if (old.url !== target.url || old.race_id !== target.race_id) throw new Error("PAGE_PLAN_CONFLICT");
    return event;
  }
  const now = Date.now();
  if (!Number.isSafeInteger(at) || at < now + policy.page_min_lead_seconds * 1000
      || at > now + policy.page_horizon_seconds * 1000) throw new Error("PAGE_PLAN_TIME");
  // One statement bounds pending work even if private callers register concurrently.
  await env.INDEX.prepare(`INSERT OR IGNORE INTO page_capture_plans
    SELECT ?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%f000+00:00','now')
    WHERE (SELECT count(*) FROM page_capture_plans p LEFT JOIN captures c USING(event_id)
      WHERE c.event_id IS NULL)< ?`).bind(event, at, target.kind, target.url, target.race_id, policy.page_max_pending).run();
  const saved = await env.INDEX.prepare("SELECT url,race_id FROM page_capture_plans WHERE event_id=?")
    .bind(event).first<{url: string; race_id: string}>();
  if (!saved) throw new Error("PAGE_PLAN_LIMIT");
  if (saved.url !== target.url || saved.race_id !== target.race_id) throw new Error("PAGE_PLAN_CONFLICT");
  return event;
}

export async function nextPage(env: Env, nextRegularAt: number): Promise<PagePlan | null> {
  // A page consumes a regular provider slot. It never adds a parallel request.
  // Keep an expired plan's original time, so capture records a gap without fetching.
  return env.INDEX.prepare(`SELECT p.* FROM page_capture_plans p LEFT JOIN captures c USING(event_id)
    WHERE c.event_id IS NULL AND p.at<=? ORDER BY p.at,p.event_id LIMIT 1`)
    .bind(nextRegularAt + collection.interval_seconds * 1000).first<PagePlan>();
}
