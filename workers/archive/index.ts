/** Closed-day public archives. Collection only; never publishes live Paper inputs. */
import {WorkerEntrypoint} from "cloudflare:workers";
import {boundedBody, discard, fetchPublic, retryAfter} from "../http";

const SOURCE = "keibaodds-history";
const INTERVAL = 300_000;
const iso = (n: number) => new Date(n).toISOString();
const bets = ["tanpuku", "huku", "wakuhuku", "wakuren", "wide", "umaren", "umatan", "trio", "tierce"];
type Job = {day: string; page: number};
function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("ARCHIVE_SHAPE");
  return value as Record<string, unknown>;
}

export function archiveUrl(job: Job): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(job.day) || iso(Date.parse(job.day)).slice(0,10) !== job.day
      || !Number.isInteger(job.page) || job.page < 1 || job.page > 100) throw new Error("ARCHIVE_JOB");
  return "https://keibaodds.com/odds?" + new URLSearchParams({page: String(job.page), display_times: "24",
    race_kind: "nar", race_date: job.day, date_limit: "4"});
}

export function inspectPage(value: unknown, job: Job) {
  const data = object(value), info = object(data.nar_info), total = data.total_count;
  if (data.race_kind !== "nar" || data.race_type !== "NAR_TODAY" || data.race_date !== job.day
      || info.race_date !== job.day || typeof total !== "number" || !Number.isInteger(total)
      || total < 0 || total > 200 || !Array.isArray(info.race_info)
      || info.race_info.length !== Math.min(2, Math.max(0, total - (job.page - 1) * 2))) {
    throw new Error("ARCHIVE_SCOPE_OR_PAGINATION");
  }
  const races = info.race_info.map((value: unknown) => {
    const race = object(value), identity = object(race.race_info), odds = object(race.odds_info);
    if (race.race_type !== "NAR" || identity.race_date !== job.day || typeof identity.track !== "string"
        || typeof identity.race !== "number" || !Number.isInteger(identity.race)
        || identity.race < 1 || identity.race > 12 || typeof identity.course !== "string") throw new Error("ARCHIVE_RACE");
    const clocks = object(odds.time_odds_times), pops = object(odds.time_pops);
    const markets = Object.fromEntries(bets.filter(bet => bet in clocks).map(bet => {
      const labels = clocks[bet];
      if (!Array.isArray(labels) || labels.length > 24 || !labels.every(t => typeof t === "string")) {
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
    dataset_kind: "HISTORICAL_ARCHIVE", paper_eligible: false,
    source_updated_at: null, historical_available_at: null,
    timing_uncertainty: ["CLOCK_ONLY_LABELS", "PUBLICATION_DELAY_UNKNOWN"]};
}

async function seed(env: ArchiveEnv, now: number) {
  const count = Number(env.BACKFILL_DAYS);
  if (!Number.isInteger(count) || count < 1 || count > 366) throw new Error("ARCHIVE_DAY_LIMIT");
  const today = Date.parse(iso(now + 9 * 3600_000).slice(0,10));
  await env.INDEX.batch(Array.from({length: count}, (_, i) =>
    env.INDEX.prepare("INSERT OR IGNORE INTO archive_jobs(day,page) VALUES(?,1)")
      .bind(iso(today - (i + 1) * 86400_000).slice(0,10))));
}

export async function collectArchive(slot: number, env: ArchiveEnv) {
  const now = Date.now();
  if (env.ARCHIVE_ENABLED !== "true") return {status: "DISABLED"};
  if (!Number.isSafeInteger(slot) || slot % 60_000 || now < slot || now - slot > 120_000) return {status: "EXPIRED"};
  const event = `${SOURCE}:${slot}`;
  if (await env.INDEX.prepare("SELECT event_id FROM archive_attempts WHERE event_id=?").bind(event).first()) return {status: "REPLAY"};
  // Claim across manual calls and Cron; only one request can start per interval.
  const claim = await env.INDEX.prepare(`UPDATE source_control SET next_allowed_at=?,owner_event_id=?
    WHERE source=? AND blocked=0 AND next_allowed_at<=?`).bind(now + INTERVAL, event, SOURCE, now).run();
  if (!claim.meta.changes) return {status: "WAIT_OR_STOPPED"};
  await seed(env, now);
  const job = await env.INDEX.prepare("SELECT day,page FROM archive_jobs WHERE status='PENDING' ORDER BY day DESC,page LIMIT 1").first<Job>();
  if (!job) return {status: "IDLE"};
  const url = archiveUrl(job);
  await env.INDEX.prepare(`INSERT INTO archive_attempts(event_id,day,page,status,scheduled_at,reserved_at)
    VALUES(?,?,?,'PENDING',?,?)`).bind(event, job.day, job.page, iso(slot), iso(now)).run();
  let stage = "NETWORK";
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 30_000);
  const receipt: Record<string, unknown> = {schema: "archive-page-v1", event_id: event, url, ...job,
    reserved_at: iso(now), fetch_started_at: null, http_status: null, received_at: null, raw_saved_at: null,
    source_updated_at: null, historical_available_at: null, paper_eligible: false};
  try {
    if (Date.now() - slot > 120_000) throw new Error("WINDOW_EXPIRED");
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
      await env.INDEX.prepare("UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?")
        .bind(retryAfter(response.headers.get("retry-after"), Date.now()), SOURCE).run();
      try {
        const body = new TextDecoder().decode(await boundedBody(response));
        receipt.received_at = iso(Date.now());
        if (/captcha|cf-chl-/i.test(body)) {
          await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
        }
      } catch {await discard(response);}
      throw new Error("RATE_LIMITED");
    }
    const raw = await boundedBody(response);
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
    const inventory = inspectPage(JSON.parse(body), job);
    receipt.parsed_at = iso(Date.now());
    const manifest = {...receipt, ...inventory};
    await env.RAW.put(`archive/manifests/${event}.json`, JSON.stringify(manifest));
    const statements = [env.INDEX.prepare(`UPDATE archive_attempts SET status='STORED',http_status=200,fetch_started_at=?,
      headers_received_at=?,received_at=?,raw_saved_at=?,raw_sha256=?,raw_bytes=?,parsed_at=?,
      available_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),duration_ms=? WHERE event_id=?`)
      .bind(receipt.fetch_started_at,receipt.headers_received_at,receipt.received_at,receipt.raw_saved_at,hash,raw.length,receipt.parsed_at,Date.now()-now,event),
      env.INDEX.prepare("UPDATE archive_jobs SET status='DONE' WHERE day=? AND page=?").bind(job.day,job.page)];
    if (job.page < inventory.pages) statements.push(env.INDEX.prepare("INSERT OR IGNORE INTO archive_jobs(day,page) VALUES(?,?)").bind(job.day,job.page+1));
    await env.INDEX.batch(statements);
    return {status: "STORED", races: inventory.races.length, bytes: raw.length};
  } catch (error) {
    // Provider refusals and unexpected formats require inspection. Ordinary network
    // failures retain their attempt; a later Cron may retry the closed-day resource.
    if (stage !== "NETWORK") await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
    const code = stage === "NETWORK" && error instanceof Error && ["SOURCE_DENIED","RATE_LIMITED","NON_JSON_OR_CHALLENGE","HTTP_ERROR","BODY_LIMIT","WINDOW_EXPIRED"].includes(error.message)
      ? error.message : `${stage}_ERROR`;
    await env.RAW.put(`archive/receipts/${event}.json`, JSON.stringify({...receipt, error_code: code}));
    await env.INDEX.prepare(`UPDATE archive_attempts SET status='FAILED',fetch_started_at=?,http_status=?,headers_received_at=?,received_at=?,
      raw_saved_at=?,raw_sha256=?,raw_bytes=?,error_code=?,duration_ms=? WHERE event_id=?`)
      .bind(receipt.fetch_started_at,receipt.http_status,receipt.headers_received_at ?? null,receipt.received_at,receipt.raw_saved_at,
        receipt.raw_sha256 ?? null,receipt.raw_bytes ?? null,code,Date.now()-now,event).run();
    return {status: code};
  } finally {
    clearTimeout(timer);
    await env.INDEX.prepare("UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?")
      .bind(Date.now() + INTERVAL, SOURCE).run();
  }
}

export default class ArchiveWorker extends WorkerEntrypoint<ArchiveEnv> {
  async fetch() {return new Response("Not found", {status: 404});}
  async collect(slot: number) {return collectArchive(slot, this.env);}
  async scheduled(controller: ScheduledController) {
    const result = await this.collect(Math.floor(controller.scheduledTime / 60_000) * 60_000);
    console.log(JSON.stringify({component: "archive", ...result}));
  }
}
