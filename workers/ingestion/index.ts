/** Private collection only; the HTTP endpoint exposes no data. */
import {WorkerEntrypoint} from "cloudflare:workers";
import {capture, sampleSlotAllowed} from "./capture";
import {ensureDaily} from "./daily";
import {registerPage, registerEvidenceBatch, type PageTarget} from "./pages";
export {capture, retryAfter, boundedBody, sampleSlotAllowed} from "./capture";
export {NarCollector} from "./daily";
export {PaperClock} from "./paper-clock";

/** Private startup/diagnostic binding; all collection still uses the shared gates. */
export class CollectionControl extends WorkerEntrypoint<Env> {
  async scheduleEvidenceBatch(payload: string): Promise<string> {
    try {
      if (typeof payload !== "string" || payload.length > 8192) throw new Error("INPUT_LIMIT");
      const {entries, revision} = JSON.parse(payload);
      if (typeof revision !== "string") throw new Error("PAGE_PACKET_REVISION");
      const events = await registerEvidenceBatch(this.env, entries, revision);
      await ensureDaily(this.env);
      return JSON.stringify({status: events[0] === "DISABLED" ? "DISABLED" : "REGISTERED", events});
    } catch {
      return JSON.stringify({status: "INPUT_OR_CAPACITY_ERROR"});
    }
  }
  async schedulePage(at: number, target: PageTarget): Promise<string> {
    const event = await registerPage(this.env, at, target);
    await ensureDaily(this.env);
    return event;
  }
  async collectSample(slot: number): Promise<void> {
    if (this.env.DAILY_COLLECTION_ENABLED === "true"
        || !sampleSlotAllowed(this.env.CAPTURE_SLOTS_JSON, slot, Date.now())) return;
    await capture(slot, this.env);
  }
  async ensureDaily(): Promise<void> { await ensureDaily(this.env); }
}

export default {
  async fetch(): Promise<Response> { return new Response("Not found", {status: 404}); },
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    if (env.DAILY_COLLECTION_ENABLED === "true") { await ensureDaily(env); return; }
    // Cloudflare may schedule a Cron invocation partway through its minute.
    // Use the configured minute as the stable slot/event ID; receipt clocks stay actual.
    const slot = Math.floor(controller.scheduledTime / 60_000) * 60_000;
    if (!Number.isSafeInteger(controller.scheduledTime)
        || !sampleSlotAllowed(env.CAPTURE_SLOTS_JSON, slot, controller.scheduledTime)
        || Date.now() < controller.scheduledTime) return;
    await capture(slot, env);
  }
} satisfies ExportedHandler<Env>;
