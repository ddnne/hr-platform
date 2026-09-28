/** Capture only. Market availability remains null until a separately versioned parser publishes it.
 * No data API, model dependency, credentials for wagering, or automatic deployment.
 */
const SOURCE = "nar-daily-odds";
const URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload/OddsDataDownload?type=daily";
const MAX_BYTES = 16 * 1024 * 1024;
const INTERVAL = 120_000;
interface Manifest {
  event_id: string; scheduled_capture_at: string; fetch_started_at: string;
  headers_received_at: string; collector_received_at: string; raw_saved_at: string | null; raw_sha256: string; raw_bytes: number;
  http_status: number; etag: string | null; validator_sent: string | null;
  validator_raw_sha256: string | null; file_name: string | null; file_timestamp: string | null;
  duration_ms: number;
}
const iso = (n: number) => new Date(n).toISOString();
async function discard(response: Response): Promise<void> {
  // Cleanup failure must never bypass refusal or Retry-After controls.
  try { await response.body?.cancel(); } catch { /* already errored/closed */ }
}

export function retryAfter(value: string | null, now: number): number {
  if (value === null) return now + INTERVAL;
  const seconds = /^\d+$/.test(value.trim()) ? Number(value) : NaN;
  const at = Number.isFinite(seconds) ? now + seconds * 1000 : Date.parse(value);
  return Number.isFinite(at) ? Math.max(now + INTERVAL, at) : now + INTERVAL;
}

export async function boundedBody(response: Response): Promise<Uint8Array> {
  const length = Number(response.headers.get("content-length"));
  if (length > MAX_BYTES) {
    await discard(response);
    throw new Error("BODY_LIMIT");
  }
  if (!response.body) throw new Error("BODY_EMPTY");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_BYTES) throw new Error("BODY_LIMIT");
      chunks.push(next.value);
    }
  } finally {
    await reader.cancel();
  }
  const all = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { all.set(chunk, offset); offset += chunk.byteLength; }
  return all;
}

async function publish(env: Env, m: Manifest): Promise<void> {
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
}

export async function capture(scheduledTime: number, env: Env): Promise<void> {
  if (env.COLLECTION_ENABLED !== "true" || env.SOURCE_APPROVED !== "true") return;
  const eventId = `${SOURCE}:${scheduledTime}`;
  const existing = await env.INDEX.prepare("SELECT status FROM captures WHERE event_id=?")
    .bind(eventId).first<{status: string}>();
  if (existing) {
    if (existing.status === "PENDING" || existing.status === "STORAGE_ERROR") {
      const stored = await env.RAW.get(`manifests/${eventId}.json`);
      if (stored) await publish(env, await stored.json<Manifest>());
    }
    return; // no re-fetch on event redelivery; a later slot is a new attempt
  }
  const now = Date.now();
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
  let stage: "NETWORK" | "STORAGE" = "NETWORK";
  try {
    const response = await fetch(URL, {redirect: "manual", signal: controller.signal,
      headers: prior?.etag ? {"If-None-Match": prior.etag} : {}});
    httpStatus = response.status;
    const received = Date.now();
    headersAt = iso(received);
    if (response.status === 403 || response.status === 401 || response.headers.get("cf-mitigated") === "challenge"
        || (response.status !== 200 && response.status !== 304 && response.headers.get("content-type")?.includes("text/html"))) {
      await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
      await discard(response);
      throw new Error("SOURCE_DENIED");
    }
    if (response.status === 429) {
      const next = retryAfter(response.headers.get("retry-after"), received);
      await env.INDEX.batch([
        env.INDEX.prepare("UPDATE source_control SET next_allowed_at=? WHERE source=?").bind(next, SOURCE),
        env.INDEX.prepare("UPDATE captures SET retry_after_at=? WHERE event_id=?").bind(iso(next), eventId)
      ]);
      await discard(response);
      throw new Error("RATE_LIMITED");
    }
    if (response.status !== 200 && response.status !== 304) {
      if (response.body) {
        const errorBody = await boundedBody(response);
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
    await publish(env, manifest);
  } catch (error) {
    const allowed = new Set(["SOURCE_DENIED", "RATE_LIMITED", "HTTP_ERROR", "UNBOUND_304", "BODY_LIMIT", "BODY_EMPTY", "NON_ZIP_OR_CHALLENGE"]);
    const code = error instanceof Error && allowed.has(error.message) ? error.message : stage === "STORAGE" ? "STORAGE_ERROR" : controller.signal.aborted ? "FETCH_TIMEOUT" : "NETWORK_ERROR";
    await env.INDEX.prepare("UPDATE captures SET status=?,http_status=?,duration_ms=?,error_code=?,headers_received_at=?,collector_received_at=? WHERE event_id=?")
      .bind(code === "STORAGE_ERROR" ? "STORAGE_ERROR" : "FAILED", httpStatus, Date.now() - now, code, headersAt, receivedAt, eventId).run();
    // No response payload, raw odds, URLs, or credentials in public/runtime logs.
    console.log(JSON.stringify({component: "collector", status: code}));
  } finally { clearTimeout(timer); }
}

export default {
  async fetch(): Promise<Response> { return new Response("Not found", {status: 404}); },
  async scheduled(controller: ScheduledController, env: Env): Promise<void> {
    await capture(controller.scheduledTime, env);
  }
} satisfies ExportedHandler<Env>;
