/** Capture only. Market availability remains null until a separately versioned parser publishes it.
 * No data API, model dependency, credentials for wagering, or automatic deployment.
 */
import {boundedBody, discard, fetchPublic, retryAfter, USER_AGENT} from "../http";
export {boundedBody, retryAfter} from "../http";

const SOURCE = "nar-daily-odds";
const URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=daily";
const INTERVAL = 120_000;
const REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/zip"};
interface Manifest {
  event_id: string; scheduled_capture_at: string; fetch_started_at: string;
  headers_received_at: string; collector_received_at: string; raw_saved_at: string | null; raw_sha256: string; raw_bytes: number;
  http_status: number; etag: string | null; validator_sent: string | null;
  validator_raw_sha256: string | null; file_name: string | null; file_timestamp: string | null;
  duration_ms: number;
}
const iso = (n: number) => new Date(n).toISOString();
async function publish(env: Env, m: Manifest, processingStarted: number): Promise<void> {
  // R2 object and manifest must both exist before publishing an observation index.
  const raw = await env.RAW.head(`raw/${m.raw_sha256}`);
  if (!raw) throw new Error("RAW_MISSING");
  if (m.raw_saved_at === null) {
    // Recover a successful body write whose completion-manifest write was interrupted.
    m.raw_saved_at = raw.uploaded.toISOString();
    await env.RAW.put(`manifests/${m.event_id}.json`, JSON.stringify(m));
  }
  await env.INDEX.batch([
    env.INDEX.prepare(`UPDATE captures SET status='RAW_STORED',http_status=?,headers_received_at=?,collector_received_at=?,
      raw_saved_at=?,raw_sha256=?,raw_bytes=?,etag=?,validator_sent=?,validator_raw_sha256=?,
      file_name=?,file_timestamp=?,duration_ms=?,error_code=NULL WHERE event_id=?`).bind(
      m.http_status, m.headers_received_at, m.collector_received_at, m.raw_saved_at, m.raw_sha256, m.raw_bytes, m.etag,
      m.validator_sent, m.validator_raw_sha256, m.file_name, m.file_timestamp, m.duration_ms, m.event_id),
    env.INDEX.prepare(`INSERT OR IGNORE INTO raw_observations VALUES(?,?,?,?,?,'DAILY_SNAPSHOT',NULL,NULL)`)
      .bind(m.event_id, m.raw_sha256, m.collector_received_at, m.raw_saved_at,
        m.http_status === 304 ? "validator_304" : "body_200")
  ]);
  // Include raw writes and index publication; the final metric write itself is excluded.
  try {
    await env.INDEX.prepare("UPDATE captures SET processing_ms=? WHERE event_id=?")
      .bind(Date.now() - processingStarted, m.event_id).run();
  } catch {
    // Metrics cannot invalidate an already published observation or its ETag.
    console.log(JSON.stringify({component: "collector", status: "METRIC_WRITE_FAILED"}));
  }
}

export async function capture(scheduledTime: number, env: Env): Promise<void> {
  if (env.COLLECTION_ENABLED !== "true" || env.SOURCE_APPROVED !== "true") return;
  const processingStarted = Date.now();
  const eventId = `${SOURCE}:${scheduledTime}`;
  const existing = await env.INDEX.prepare("SELECT status FROM captures WHERE event_id=?")
    .bind(eventId).first<{status: string}>();
  if (existing) {
    if (existing.status === "PENDING" || existing.status === "STORAGE_ERROR") {
      const stored = await env.RAW.get(`manifests/${eventId}.json`);
      if (stored) await publish(env, await stored.json<Manifest>(), processingStarted);
    }
    return; // no re-fetch on event redelivery; a later slot is a new attempt
  }
  const now = Date.now();
  if (env.CAPTURE_SLOTS_JSON !== undefined && !sampleSlotAllowed(env.CAPTURE_SLOTS_JSON, scheduledTime, now)) return;
  const claim = await env.INDEX.prepare(`UPDATE source_control SET next_allowed_at=?,owner_event_id=?
    WHERE source=? AND blocked=0 AND next_allowed_at<=?`).bind(now + INTERVAL, eventId, SOURCE, now).run();
  if (!claim.meta.changes) {
    const owner = await env.INDEX.prepare("SELECT owner_event_id FROM source_control WHERE source=?")
      .bind(SOURCE).first<{owner_event_id: string | null}>();
    if (owner?.owner_event_id === eventId) return;
    await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'WAIT_OR_BLOCKED')")
      .bind(eventId, iso(scheduledTime)).run();
    return;
  }
  const inserted = await env.INDEX.prepare(`INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,
    fetch_started_at,status) VALUES(?,?,?,'PENDING')`).bind(eventId, iso(scheduledTime), iso(now)).run();
  if (!inserted.meta.changes) return;
  const prior = await env.INDEX.prepare(`SELECT etag,raw_sha256,file_name,file_timestamp FROM captures
    WHERE status='RAW_STORED' AND http_status=200 ORDER BY collector_received_at DESC LIMIT 1`)
    .first<{etag: string | null; raw_sha256: string; file_name: string | null; file_timestamp: string | null}>();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20_000);
  let httpStatus: number | null = null;
  let receivedAt: string | null = null;
  let headersAt: string | null = null;
  let contentType: string | null = null;
  let cfMitigated: string | null = null;
  let denialBasis: string | null = null;
  let errorBody: Uint8Array | null = null;
  let stage: "NETWORK" | "STORAGE" = "NETWORK";
  try {
    if (env.CAPTURE_SLOTS_JSON !== undefined
        && !sampleSlotAllowed(env.CAPTURE_SLOTS_JSON, scheduledTime, Date.now())) throw new Error("SAMPLE_WINDOW_EXPIRED");
    const response = await fetchPublic(URL, "application/zip", controller.signal,
      prior?.etag ? {"If-None-Match": prior.etag} : {});
    httpStatus = response.status;
    contentType = response.headers.get("content-type");
    cfMitigated = response.headers.get("cf-mitigated");
    const received = Date.now();
    headersAt = iso(received);
    denialBasis = response.status === 403 || response.status === 401 ? "HTTP_AUTH_OR_FORBIDDEN"
      : cfMitigated === "challenge" ? "CHALLENGE_HEADER"
      : response.status !== 200 && response.status !== 304 && response.status !== 429
        && contentType?.includes("text/html") ? "NON_SUCCESS_HTML" : null;
    if (denialBasis !== null) {
      await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
      // The stop is durable before reading diagnostic bytes. Broken/oversized bodies cannot undo it.
      try { errorBody = await boundedBody(response); receivedAt = iso(Date.now()); }
      catch { await discard(response); }
      throw new Error("SOURCE_DENIED");
    }
    if (response.status === 429) {
      const next = retryAfter(response.headers.get("retry-after"), received);
      await env.INDEX.batch([
        env.INDEX.prepare("UPDATE source_control SET next_allowed_at=? WHERE source=?").bind(next, SOURCE),
        env.INDEX.prepare("UPDATE captures SET retry_after_at=? WHERE event_id=?").bind(iso(next), eventId)
      ]);
      // Save the wait before inspecting the body: a broken stream must not undo Retry-After.
      if (response.body) {
        const rateBody = await boundedBody(response);
        errorBody = rateBody;
        receivedAt = iso(Date.now());
        if (/captcha|challenge/i.test(new TextDecoder().decode(rateBody.subarray(0, 8192)))) {
          await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
          throw new Error("NON_ZIP_OR_CHALLENGE");
        }
      }
      throw new Error("RATE_LIMITED");
    }
    if (response.status !== 200 && response.status !== 304) {
      if (response.body) {
        errorBody = await boundedBody(response);
        receivedAt = iso(Date.now());
        if (/captcha|<html|<!doctype html/i.test(new TextDecoder().decode(errorBody.subarray(0, 8192)))) {
          await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
          throw new Error("NON_ZIP_OR_CHALLENGE");
        }
      }
      throw new Error("HTTP_ERROR"); // includes redirects; never follow to another host or evade denial
    }
    let hash: string, size: number;
    let content: Uint8Array | null = null;
    let rawSavedAt: string | null = null;
    const disposition = response.headers.get("content-disposition");
    const filename = disposition?.match(/filename="?([^";]+)"?/)?.[1] ?? null;
    const unix = filename?.match(/_(\d{10})_odds\.zip$/)?.[1];
    if (response.status === 304) {
      await discard(response);
      receivedAt = iso(Date.now());
      stage = "STORAGE";
      if (!prior?.etag || (response.headers.get("etag") !== null && response.headers.get("etag") !== prior.etag)) throw new Error("UNBOUND_304");
      const raw = await env.RAW.head(`raw/${prior.raw_sha256}`);
      if (!raw) throw new Error("UNBOUND_304");
      hash = prior.raw_sha256;
      size = raw.size;
      rawSavedAt = iso(Date.now());
    } else {
      const body = await boundedBody(response);
      receivedAt = iso(Date.now());
      if (body[0] !== 0x50 || body[1] !== 0x4b) {
        errorBody = body;
        // Any HTML/interstitial on the ZIP path stops the source for manual investigation.
        await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
        throw new Error("NON_ZIP_OR_CHALLENGE");
      }
      const digest = await crypto.subtle.digest("SHA-256", body);
      hash = [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
      size = body.byteLength;
      stage = "STORAGE";
      if (await env.RAW.head(`raw/${hash}`)) rawSavedAt = iso(Date.now());
      else content = body;
    }
    const manifest: Manifest = {event_id: eventId, scheduled_capture_at: iso(scheduledTime),
      fetch_started_at: iso(now), headers_received_at: headersAt, collector_received_at: receivedAt!, raw_saved_at: rawSavedAt,
      raw_sha256: hash, raw_bytes: size, http_status: response.status,
      etag: response.headers.get("etag") ?? (response.status === 304 ? prior?.etag ?? null : null),
      validator_sent: prior?.etag ?? null, validator_raw_sha256: prior?.raw_sha256 ?? null,
      file_name: filename ?? (response.status === 304 ? prior?.file_name ?? null : null),
      file_timestamp: unix ? iso(Number(unix) * 1000) : response.status === 304 ? prior?.file_timestamp ?? null : null,
      duration_ms: Date.now() - now};
    // Store the event-to-hash intent first. It is not a successful observation.
    await env.RAW.put(`manifests/${eventId}.json`, JSON.stringify(manifest));
    if (content !== null) {
      const saved = await env.RAW.put(`raw/${hash}`, content);
      if (!saved) throw new Error("RAW_PUT_FAILED");
      manifest.raw_saved_at = saved.uploaded.toISOString();
      await env.RAW.put(`manifests/${eventId}.json`, JSON.stringify(manifest));
    }
    await publish(env, manifest, processingStarted);
  } catch (error) {
    const allowed = new Set(["SAMPLE_WINDOW_EXPIRED", "SOURCE_DENIED", "RATE_LIMITED", "HTTP_ERROR", "UNBOUND_304", "BODY_LIMIT", "BODY_EMPTY", "NON_ZIP_OR_CHALLENGE"]);
    const code = error instanceof Error && allowed.has(error.message) ? error.message : stage === "STORAGE" ? "STORAGE_ERROR" : controller.signal.aborted ? "FETCH_TIMEOUT" : "NETWORK_ERROR";
    await env.INDEX.prepare("UPDATE captures SET status=?,http_status=?,duration_ms=?,error_code=?,headers_received_at=?,collector_received_at=? WHERE event_id=?")
      .bind(code === "STORAGE_ERROR" ? "STORAGE_ERROR" : "FAILED", httpStatus, Date.now() - now, code, headersAt, receivedAt, eventId).run();
    // Private diagnostic metadata only. Never turn an error response into an odds observation.
    // Keep the original stop/wait outcome even if this optional evidence write fails.
    try {
      await env.RAW.put(`failure-metadata/${eventId}.json`, JSON.stringify({
        schema: "collector-failure-metadata-v1", event_id: eventId,
        request_profile_headers: REQUEST_HEADERS, http_status: httpStatus,
        fetch_started_at: iso(now), headers_received_at: headersAt,
        content_type: contentType?.slice(0, 512) ?? null,
        cf_mitigated: cfMitigated?.slice(0, 128) ?? null,
        header_value_truncated: (contentType?.length ?? 0) > 512 || (cfMitigated?.length ?? 0) > 128,
        denial_basis: denialBasis, error_code: code, recorded_at: iso(Date.now()),
        body_bytes: errorBody?.byteLength ?? null,
        body_prefix_base64: errorBody === null ? null : btoa(String.fromCharCode(...errorBody.subarray(0, 8192))),
        body_prefix_truncated: errorBody === null ? null : errorBody.byteLength > 8192
      }));
    } catch {
      console.log(JSON.stringify({component: "collector", status: "FAILURE_METADATA_WRITE_FAILED"}));
    }
    // No response payload, raw odds, URLs, or credentials in public/runtime logs.
    console.log(JSON.stringify({component: "collector", status: code}));
  } finally { clearTimeout(timer); }
}

/** Finite sample plan: at most two minute-aligned UTC slots, >=5 minutes apart. */
export function sampleSlotAllowed(plan: string, scheduled: number, now: number): boolean {
  let slots: unknown;
  try { slots = JSON.parse(plan); } catch { return false; }
  if (!Array.isArray(slots) || slots.length < 1 || slots.length > 2
      || !slots.every(t => Number.isSafeInteger(t) && t > 0 && t % 60_000 === 0)
      || (slots.length === 2 && slots[1] - slots[0] < 300_000)) return false;
  return Number.isFinite(now) && slots.includes(scheduled) && now >= scheduled && now - scheduled <= 120_000;
}

export default {
  async fetch(): Promise<Response> { return new Response("Not found", {status: 404}); },
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    // Cloudflare may schedule a Cron invocation partway through its minute.
    // Use the configured minute as the stable slot/event ID; receipt clocks stay actual.
    const slot = Math.floor(controller.scheduledTime / 60_000) * 60_000;
    if (!Number.isSafeInteger(controller.scheduledTime)
        || !sampleSlotAllowed(env.CAPTURE_SLOTS_JSON, slot, controller.scheduledTime)
        || Date.now() < controller.scheduledTime) return;
    await capture(slot, env);
  }
} satisfies ExportedHandler<Env>;
