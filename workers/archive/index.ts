/** Closed-day public archives. Collection only; never publishes live Paper inputs. */
import {WorkerEntrypoint} from "cloudflare:workers";
import {boundedBody, discard, fetchPublic, retryAfter} from "../http";

const SOURCE = "keibaodds-history";
const MINUTE = 60_000, DAY = 24 * 60 * MINUTE, JST_OFFSET = 9 * 60 * MINUTE;
function settings(env: ArchiveEnv) {
  const positive = (key: keyof ArchiveEnv) => {
    const value = Number(env[key]);
    if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`ARCHIVE_CONFIG:${key}`);
    return value;
  };
  const config = {
    days: positive("BACKFILL_DAYS"), interval: positive("ARCHIVE_MIN_INTERVAL_MS"),
    window: positive("ARCHIVE_SLOT_WINDOW_MS"), timeout: positive("ARCHIVE_REQUEST_TIMEOUT_MS"),
    lease: positive("ARCHIVE_REQUEST_LEASE_MS"), retryWait: positive("ARCHIVE_RETRY_WAIT_MS"),
    maximumBytes: positive("ARCHIVE_MAX_BODY_BYTES"), displayTimes: positive("ARCHIVE_DISPLAY_TIMES"),
  };
  if (config.lease < config.window + config.timeout) throw new Error("ARCHIVE_CONFIG:lease");
  return config;
}
const iso = (n: number) => new Date(n).toISOString();
const bets = ["tanpuku", "huku", "wakuhuku", "wakuren", "wide", "umaren", "umatan", "trio", "tierce"];
type Job = {day: string; page: number};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("ARCHIVE_SHAPE");
  return value as Record<string, unknown>;
}

export function archiveUrl(job: Job, displayTimes: number): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(job.day) || iso(Date.parse(job.day)).slice(0,10) !== job.day
      || !Number.isInteger(job.page) || job.page < 1 || job.page > 100) throw new Error("ARCHIVE_JOB");
  return "https://keibaodds.com/odds?" + new URLSearchParams({page: String(job.page), display_times: String(displayTimes),
    race_kind: "nar", race_date: job.day, date_limit: "4", time: "00:00"});
}

export function inspectPage(value: unknown, job: Job, displayTimes: number) {
  const data = object(value), info = object(data.nar_info), total = data.total_count;
  // A date with no listed races uses null instead of the usual race array.
  const empty = job.page === 1 && data.race_type === null && total === 0 && info.race_info === null
    && [info.races, info.date_info, info.track_info].every(v => Array.isArray(v) && v.length === 0);
  const raceValues = empty ? [] : info.race_info;
  if (data.race_kind !== "nar" || (!empty && data.race_type !== "NAR_TODAY") || data.race_date !== job.day
      || info.race_date !== job.day || typeof total !== "number" || !Number.isInteger(total)
      || total < 0 || total > 200 || !Array.isArray(raceValues)
      || raceValues.length !== Math.min(2, Math.max(0, total - (job.page - 1) * 2))) {
    throw new Error("ARCHIVE_SCOPE_OR_PAGINATION");
  }
  const races = raceValues.map((value: unknown) => {
    const race = object(value), identity = object(race.race_info), odds = object(race.odds_info);
    if (race.race_type !== "NAR" || identity.race_date !== job.day || typeof identity.track !== "string"
        || typeof identity.race !== "number" || !Number.isInteger(identity.race)
        || identity.race < 1 || identity.race > 12 || typeof identity.course !== "string") throw new Error("ARCHIVE_RACE");
    const clocks = object(odds.time_odds_times), pops = object(odds.time_pops);
    const markets = Object.fromEntries(bets.filter(bet => bet in clocks).map(bet => {
      const labels = clocks[bet];
      if (!Array.isArray(labels) || labels.length > displayTimes || !labels.every(t => typeof t === "string")) {
        throw new Error("ARCHIVE_LABELS");
      }
      const rows = pops[bet];
      return [bet, labels.map((label: string, i: number) => ({label,
        kind: /^(?:最終|最終オッズ)$/.test(label) ? "FINAL_ONLY"
          : /^(?:[01]?\d|2[0-3]):[0-5]\d$/.test(label) ? "CLOCK_ONLY" : "UNKNOWN",
        combinations: Array.isArray(rows) && rows[i] && typeof rows[i] === "object"
          ? Object.keys(rows[i]).length : null}))];
    }));
    return {race_id: `${job.day}:${identity.track}:${identity.race}`,
      discipline: !identity.track.includes("帯広") && /^(?:ダート|芝)\s*[\d０-９]/.test(identity.course) ? "FLAT" : "EXCLUDED_OR_UNKNOWN",
      runners: Object.keys(object(race.horse_info)).length, markets};
  });
  if (new Set(races.map(r => r.race_id)).size !== races.length) throw new Error("ARCHIVE_DUPLICATE_RACE");
  return {total_count: total, pages: Math.ceil(total / 2), races,
    coverage_status: total === 0 ? "NO_RACES_RETURNED" : "RACES_RETURNED",
    dataset_kind: "HISTORICAL_ARCHIVE", paper_eligible: false,
    source_updated_at: null, historical_available_at: null,
    timing_uncertainty: ["CLOCK_ONLY_LABELS", "PUBLICATION_DELAY_UNKNOWN"]};
}

async function seed(env: ArchiveEnv, now: number, count: number) {
  const today = Date.parse(iso(now + JST_OFFSET).slice(0,10));
  const oldest = iso(today - count * DAY).slice(0,10);
  const newest = iso(today - DAY).slice(0,10);
  // The seeder inserts the entire range atomically; completed dates are retained.
  const existing = await env.INDEX.prepare("SELECT min(day) oldest,max(day) newest FROM archive_jobs WHERE page=1")
    .first<{oldest: string | null; newest: string | null}>();
  if (existing?.oldest && existing.newest && existing.oldest <= oldest && existing.newest >= newest) return;
  await env.INDEX.batch([env.INDEX.prepare(`WITH RECURSIVE days(offset) AS (
    SELECT 1 UNION ALL SELECT offset+1 FROM days WHERE offset<?)
    INSERT OR IGNORE INTO archive_jobs(day,page)
    SELECT date(?, '-'||offset||' days'),1 FROM days`)
      .bind(count, iso(today).slice(0,10))]);
}

export async function collectArchive(slot: number, env: ArchiveEnv, requestedDay?: string) {
  const now = Date.now();
  if (env.ARCHIVE_ENABLED !== "true") return {status: "DISABLED"};
  const config = settings(env);
  if (!Number.isSafeInteger(slot) || slot % MINUTE || now < slot || now - slot > config.window) return {status: "EXPIRED"};
  const event = `${SOURCE}:${slot}`;
  if (await env.INDEX.prepare("SELECT event_id FROM archive_attempts WHERE event_id=?").bind(event).first()) return {status: "REPLAY"};
  // Claim across manual calls and Cron; only one request can start per interval.
  const leaseUntil = now + config.lease;
  const claim = await env.INDEX.prepare(`UPDATE source_control SET next_allowed_at=?,owner_event_id=?
    WHERE source=? AND blocked=0 AND next_allowed_at<=? AND NOT EXISTS (
      SELECT 1 FROM archive_attempts WHERE event_id=source_control.owner_event_id AND status='PENDING')`)
    .bind(leaseUntil, event, SOURCE, now).run();
  if (!claim.meta.changes) return {status: "WAIT_OR_STOPPED"};
  await seed(env, now, config.days);
  // Internal range checks use the same queue, source control, receipts and transport.
  const job = requestedDay === undefined
    ? await env.INDEX.prepare("SELECT day,page FROM archive_jobs WHERE status='PENDING' ORDER BY day DESC,page LIMIT 1").first<Job>()
    : await env.INDEX.prepare("SELECT day,page FROM archive_jobs WHERE status='PENDING' AND day=? ORDER BY page LIMIT 1")
      .bind(requestedDay).first<Job>();
  if (!job) return {status: "IDLE"};
  const url = archiveUrl(job, config.displayTimes);
  await env.INDEX.prepare(`INSERT INTO archive_attempts(event_id,day,page,status,scheduled_at,reserved_at)
    VALUES(?,?,?,'PENDING',?,?)`).bind(event, job.day, job.page, iso(slot), iso(now)).run();
  let stage = "NETWORK", retryAt = 0;
  // Use the database clock at terminal publication, including slow object writes.
  const release = () => env.INDEX.prepare(`UPDATE source_control SET next_allowed_at=max(
    CASE WHEN next_allowed_at=? THEN 0 ELSE next_allowed_at END,
    CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)+?,?)
    WHERE source=? AND owner_event_id=?`)
    .bind(leaseUntil, config.interval, retryAt, SOURCE, event);
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), config.timeout);
  const receipt: Record<string, unknown> = {schema: "archive-page-v2", event_id: event, url, ...job,
    reserved_at: iso(now), fetch_started_at: null, http_status: null, received_at: null, raw_saved_at: null,
    source_updated_at: null, historical_available_at: null, paper_eligible: false};
  try {
    if (Date.now() - slot > config.window) throw new Error("WINDOW_EXPIRED");
    receipt.fetch_started_at = iso(Date.now());
    const response = await fetchPublic(url, "application/json", controller.signal);
    receipt.http_status = response.status;
    receipt.headers_received_at = iso(Date.now());
    if ([401,403,404].includes(response.status) || response.headers.get("cf-mitigated") === "challenge"
        || response.status >= 300 && response.status < 400) {
      await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
      await discard(response);
      throw new Error("SOURCE_DENIED");
    }
    if (response.status === 429) {
      retryAt = retryAfter(response.headers.get("retry-after"), Date.now(), config.retryWait);
      await env.INDEX.prepare("UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?")
        .bind(retryAt, SOURCE).run();
      try {
        const body = new TextDecoder().decode(await boundedBody(response, config.maximumBytes));
        receipt.received_at = iso(Date.now());
        if (/captcha|cf-chl-/i.test(body)) {
          await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
        }
      } catch {await discard(response);}
      throw new Error("RATE_LIMITED");
    }
    const raw = await boundedBody(response, config.maximumBytes);
    receipt.received_at = iso(Date.now());
    receipt.fetch_duration_ms = Date.now() - Date.parse(String(receipt.fetch_started_at));
    receipt.raw_bytes = raw.byteLength;
    const body = new TextDecoder().decode(raw);
    if (!response.headers.get("content-type")?.includes("application/json") || /captcha|cf-chl-/i.test(body)) {
      await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
      throw new Error("NON_JSON_OR_CHALLENGE");
    }
    if (response.status !== 200) throw new Error("HTTP_ERROR");
    stage = "STORAGE";
    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", raw))].map(x => x.toString(16).padStart(2,"0")).join("");
    receipt.raw_sha256 = hash;
    if (!await env.RAW.head(`archive/raw/${hash}`)) await env.RAW.put(`archive/raw/${hash}`, raw);
    receipt.raw_saved_at = iso(Date.now());
    // Preserve the original before parsing. Malformed/incomplete data remain re-readable.
    await env.RAW.put(`archive/receipts/${event}.json`, JSON.stringify(receipt));
    stage = "PARSE";
    const inventory = inspectPage(JSON.parse(body), job, config.displayTimes);
    receipt.parsed_at = iso(Date.now());
    const manifest = {...receipt, ...inventory};
    await env.RAW.put(`archive/manifests/${event}.json`, JSON.stringify(manifest));
    const statements = [env.INDEX.prepare(`UPDATE archive_attempts SET status='STORED',http_status=200,fetch_started_at=?,
      headers_received_at=?,received_at=?,raw_saved_at=?,raw_sha256=?,raw_bytes=?,parsed_at=?,
      available_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),duration_ms=? WHERE event_id=?`)
      .bind(receipt.fetch_started_at,receipt.headers_received_at,receipt.received_at,receipt.raw_saved_at,hash,raw.length,receipt.parsed_at,Date.now()-now,event),
      env.INDEX.prepare("UPDATE archive_jobs SET status='DONE' WHERE day=? AND page=?").bind(job.day,job.page)];
    if (job.page < inventory.pages) statements.push(env.INDEX.prepare("INSERT OR IGNORE INTO archive_jobs(day,page) VALUES(?,?)").bind(job.day,job.page+1));
    await env.INDEX.batch([...statements, release()]);
    return {status: "STORED", races: inventory.races.length, bytes: raw.length};
  } catch (error) {
    // Provider refusals and unexpected formats require inspection. Ordinary network
    // failures retain their attempt; a later Cron may retry the closed-day resource.
    if (stage !== "NETWORK") await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
    const code = stage === "NETWORK" && error instanceof Error && ["SOURCE_DENIED","RATE_LIMITED","NON_JSON_OR_CHALLENGE","HTTP_ERROR","BODY_LIMIT","WINDOW_EXPIRED"].includes(error.message)
      ? error.message : `${stage}_ERROR`;
    await env.RAW.put(`archive/receipts/${event}.json`, JSON.stringify({...receipt, error_code: code}));
    await env.INDEX.batch([env.INDEX.prepare(`UPDATE archive_attempts SET status='FAILED',fetch_started_at=?,http_status=?,headers_received_at=?,received_at=?,
      raw_saved_at=?,raw_sha256=?,raw_bytes=?,error_code=?,duration_ms=? WHERE event_id=?`)
      .bind(receipt.fetch_started_at,receipt.http_status,receipt.headers_received_at ?? null,receipt.received_at,receipt.raw_saved_at,
        receipt.raw_sha256 ?? null,receipt.raw_bytes ?? null,code,Date.now()-now,event), release()]);
    return {status: code};
  } finally {
    clearTimeout(timer);
    // Terminal status and the wait above are atomic; no unprotected release here.
  }
}

export default class ArchiveWorker extends WorkerEntrypoint<ArchiveEnv> {
  async fetch() {return new Response("Not found", {status: 404});}
  async collect(slot: number, requestedDay?: string) {return collectArchive(slot, this.env, requestedDay);}
  async scheduled(controller: ScheduledController) {
    const result = await this.collect(Math.floor(controller.scheduledTime / MINUTE) * MINUTE);
    console.log(JSON.stringify({component: "archive", ...result}));
  }
}
