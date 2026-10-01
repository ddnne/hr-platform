/** Private collection only; the HTTP endpoint exposes no data. */
import {capture, sampleSlotAllowed} from "./capture";
import {ensureDaily} from "./daily";
export {capture, retryAfter, boundedBody, sampleSlotAllowed} from "./capture";
export {NarCollector} from "./daily";

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
