/** One provider coordinator. Alarms persist on Cloudflare; no Mac or model scheduler. */
import {DurableObject} from "cloudflare:workers";
import {capture, eventIdFor, lastMonthlyAttempt, SOURCE, type CaptureKind} from "./capture";
import collection from "../../configs/collection.json";
import policy from "../../configs/cloud-collection.json";
import {nextPage, type PageTarget, type PagePlan} from "./pages";

type Job = {at: number; kind: CaptureKind; date: string; page?: PageTarget; planned?: boolean};
type Control = {blocked: number; next_allowed_at: number};
const DAY = 86_400_000;
const enabled = (env: Env) => env.DAILY_COLLECTION_ENABLED === "true"
  && env.COLLECTION_ENABLED === "true" && env.SOURCE_APPROVED === "true";
const minute = (s: string) => {const [h, m] = s.split(":").map(Number); return h * 60 + m;};
const pageJob = (page: PagePlan): Job => ({at: page.at, kind: page.kind,
  date: page.race_id.split(":")[0], planned: true,
  ...(["state", "payout"].includes(page.kind) ? {page: page as PageTarget} : {})});

/** Public source windows are JST; actual receipt clocks are never shifted. */
export function nextJob(after: number, lastRaceAt: number | null): Job {
  const offset = policy.timezone_offset_minutes * 60_000;
  const midnight = Math.floor((after + offset) / DAY) * DAY - offset;
  const start = midnight + minute(policy.day_start) * 60_000;
  const end = midnight + minute(policy.day_end) * 60_000;
  const at = after < start ? start : after >= end ? start + DAY : after;
  const date = new Date(at + offset).toISOString().slice(0, 10).replaceAll("-", "");
  const oldDate = lastRaceAt === null ? null
    : new Date(lastRaceAt + offset).toISOString().slice(0, 10).replaceAll("-", "");
  const kind = oldDate !== date || lastRaceAt === null
    || at - lastRaceAt >= policy.race_refresh_seconds * 1000 ? "race" : "odds";
  return {at, kind, date};
}

/** One finalized archive per rolling day, using the existing provider coordinator. */
export function nextMonthly(after: number, previous: number | null): Job {
  const earliest = Math.max(after, previous === null ? 0 : previous + policy.monthly_min_interval_seconds * 1000);
  const offset = policy.timezone_offset_minutes * 60_000;
  const midnight = Math.floor((earliest + offset) / DAY) * DAY - offset;
  const at = Math.max(earliest, midnight + minute(policy.monthly_capture_time) * 60_000);
  return {at, kind: "monthly", date: new Date(at + offset).toISOString().slice(0, 10).replaceAll("-", "")};
}

export class NarCollector extends DurableObject<Env> {
  private async next(after: number, previous: number | null, allowMonthly = true): Promise<Job> {
    let regular = nextJob(after, previous);
    // The first odds after state/race evidence is needed for the fixed Paper
    // input. Let that ordinary slot run before inserting an archive request.
    if (allowMonthly && this.env.MONTHLY_COLLECTION_ENABLED === "true") {
      const monthly = nextMonthly(after, await lastMonthlyAttempt(this.env));
      if (monthly.at <= regular.at) regular = monthly;
    }
    // Previously reserved state/payout slots take precedence over the archive.
    const page = await nextPage(this.env, regular.at);
    return page ? pageJob(page) : regular;
  }
  private async control(): Promise<Control | null> {
    return this.env.INDEX.prepare("SELECT blocked,next_allowed_at FROM source_control WHERE source=?")
      .bind(SOURCE).first<Control>();
  }
  async ensure(): Promise<void> {
    if (!enabled(this.env)) return;
    const control = await this.control();
    if (!control || control.blocked) return;
    await this.ctx.blockConcurrencyWhile(async () => {
      const alarm = await this.ctx.storage.getAlarm();
      let job = await this.ctx.storage.get<Job>("job");
      if (alarm !== null) {
        // A newly reserved page may precede the existing overnight alarm.
        // Keep due/in-flight work; any replacement obeys the provider wait/window.
        if (job && job.at > Date.now() && alarm > Date.now()) {
          const window = collection.capture_window_seconds * 1000;
          const page = await nextPage(this.env, job.at, Math.max(Date.now() + 1, control.next_allowed_at) - window);
          const executionAt = page ? Math.max(Date.now() + 1, page.at, control.next_allowed_at) : Infinity;
          if (page && executionAt <= page.at + window
              && (executionAt < job.at || executionAt === job.at && !job.planned && !job.page)) {
            job = pageJob(page);
            await this.ctx.storage.put("job", job);
            await this.ctx.storage.setAlarm(executionAt);
          }
        }
        return;
      }
      if (job) {
        const previous = await this.env.INDEX.prepare("SELECT status,error_code FROM captures WHERE event_id=?")
          .bind(eventIdFor(job.kind, job.at)).first<{status: string; error_code: string | null}>();
        // After an investigated stop is explicitly cleared, schedule a new
        // observation. Redelivering the failed event would re-latch its stop.
        // This path cannot clear the source control or alter the old evidence.
        if (previous && ["FAILED", "INCOMPLETE_FETCH"].includes(previous.status)
            && ["SOURCE_DENIED", "NON_ZIP_OR_CHALLENGE", "INCOMPLETE_FETCH"].includes(previous.error_code ?? "")) {
          job = undefined;
        }
      }
      if (!job) {
        const previous = await this.ctx.storage.get<number>("lastRaceAt") ?? null;
        job = await this.next(Math.max(Date.now(), control.next_allowed_at), previous);
        await this.ctx.storage.put("job", job);
      }
      // A missed alarm retains its old job. It is recorded as missed, never backfilled.
      await this.ctx.storage.setAlarm(Math.max(Date.now() + 1, job.at, control.next_allowed_at));
    });
  }
  async alarm(): Promise<void> {
    if (!enabled(this.env)) return;
    const job = await this.ctx.storage.get<Job>("job");
    if (!job) return;
    if (Date.now() < job.at) { await this.ctx.storage.setAlarm(job.at); return; }
    const control = await this.control();
    if (!control || control.blocked) return;
    if ((job.planned || job.page) && Date.now() < control.next_allowed_at
        && Date.now() <= job.at + collection.capture_window_seconds * 1000) {
      // Preserve the planned ID/window while waiting for the previous response.
      // Do not consume a page as WAIT_OR_BLOCKED just because that receipt was late.
      await this.ctx.storage.setAlarm(Math.max(job.at, control.next_allowed_at));
      return;
    }
    const eventId = eventIdFor(job.kind, job.at);
    await capture(job.at, this.env, job.kind, true, job.page);
    const stored = await this.env.INDEX.prepare("SELECT status,collector_received_at FROM captures WHERE event_id=?")
      .bind(eventId).first<{status: string; collector_received_at: string | null}>();
    if (!stored || ["PENDING", "RESERVED", "FETCHING"].includes(stored.status)) throw new Error("CAPTURE_NOT_RECORDED");
    if (stored.status === "RAW_STORED" && job.kind === "race" && stored.collector_received_at) {
      const originalReceipt = Date.parse(stored.collector_received_at);
      const previous = await this.ctx.storage.get<number>("lastRaceAt") ?? 0;
      await this.ctx.storage.put("lastRaceAt", Math.max(previous, originalReceipt));
    }
    const nextControl = await this.control();
    if (!nextControl || nextControl.blocked) return;
    const receipt = stored?.collector_received_at === null || !stored?.collector_received_at
      ? Date.now() : Date.parse(stored.collector_received_at);
    const previous = await this.ctx.storage.get<number>("lastRaceAt") ?? null;
    const next = await this.next(Math.max(Date.now(), stored.status === "SUPERSEDED_PLAN" ? 0 : receipt + collection.interval_seconds * 1000,
      nextControl.next_allowed_at), previous, job.kind === "odds");
    // Parsing runs in another Worker Cron. No parser/model call can hold this alarm open.
    await this.ctx.storage.put("job", next);
    await this.ctx.storage.setAlarm(Math.max(next.at, nextControl.next_allowed_at));
    if (stored.status === "RAW_STORED" && this.env.RESEARCH) {
      const normalizer = this.env.RESEARCH as unknown as {normalize_saved(): Promise<string>};
      // The next capture is durable before waking the existing normalizer.
      // Its failure cannot delay collection; the minute Cron remains a fallback.
      this.ctx.waitUntil((async () => {
        try { await normalizer.normalize_saved(); }
        catch { /* The raw observation remains pending for the existing Cron. */ }
      })());
    }
  }
}

export async function ensureDaily(env: Env): Promise<void> {
  if (enabled(env)) await env.COLLECTOR.getByName(SOURCE).ensure();
}
