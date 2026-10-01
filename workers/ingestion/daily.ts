/** One provider coordinator. Alarms persist on Cloudflare; no Mac or model scheduler. */
import {DurableObject} from "cloudflare:workers";
import {capture, eventIdFor, SOURCE, type CaptureKind} from "./capture";
import collection from "../../configs/collection.json";
import policy from "../../configs/cloud-collection.json";
import {nextPage, type PageTarget} from "./pages";

type Job = {at: number; kind: CaptureKind; date: string; page?: PageTarget};
type Control = {blocked: number; next_allowed_at: number};
const DAY = 86_400_000;
const enabled = (env: Env) => env.DAILY_COLLECTION_ENABLED === "true"
  && env.COLLECTION_ENABLED === "true" && env.SOURCE_APPROVED === "true";
const minute = (s: string) => {const [h, m] = s.split(":").map(Number); return h * 60 + m;};

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

export class NarCollector extends DurableObject<Env> {
  private async next(after: number, previous: number | null): Promise<Job> {
    const regular = nextJob(after, previous);
    const page = await nextPage(this.env, regular.at);
    return page ? {at: page.at, kind: page.kind, date: page.race_id.split(":")[0], page} : regular;
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
      if (await this.ctx.storage.getAlarm() !== null) return;
      let job = await this.ctx.storage.get<Job>("job");
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
    if (job.page && Date.now() < control.next_allowed_at
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
    const next = await this.next(Math.max(Date.now(), receipt + collection.interval_seconds * 1000,
      nextControl.next_allowed_at), previous);
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
