/** Capture only. Market availability remains null until a separately versioned parser publishes it.
 * Private R2/D1 only. No wagering credentials or public data API.
 */
import {boundedBody, discard, fetchPublic, retryAfter, USER_AGENT} from "../http";
export {boundedBody, retryAfter} from "../http";

import collection from "../../configs/collection.json";
import policy from "../../configs/cloud-collection.json";
import {validatePage, type PageTarget} from "./pages";
export type CaptureKind = "odds" | "race" | "monthly" | PageTarget["kind"];
// Keep the existing source row: both ZIP endpoints share the provider's stop/wait state.
export const SOURCE = "nar-daily-odds";
const INTERVAL = collection.interval_seconds * 1000;
export const eventIdFor = (kind: CaptureKind, at: number) => `nar-daily-${kind}:${at}`;
const REQUEST_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/zip"};
interface Manifest {
  event_id: string; scheduled_capture_at: string; fetch_started_at: string;
  headers_received_at: string; collector_received_at: string; raw_saved_at: string | null; raw_sha256: string; raw_bytes: number;
  http_status: number; etag: string | null; validator_sent: string | null;
  validator_raw_sha256: string | null; file_name: string | null; file_timestamp: string | null;
  duration_ms: number; dataset_kind?: string;
  url?: string; race_id?: string;
}
/** The first day collects the previous month containing yesterday's results. */
export function monthlyTarget(at: number): {url: string; month: string} {
  const date = new Date(at + policy.timezone_offset_minutes * 60_000);
  if (!Number.isSafeInteger(at) || !Number.isFinite(date.getTime())) throw new Error("MONTHLY_TIME");
  if (date.getUTCDate() === 1) date.setUTCDate(0);
  const year = date.getUTCFullYear(), month = date.getUTCMonth() + 1;
  return {url: `${policy.urls.monthly}&k_year=${year}&k_month=${month}`,
    month: `${year}${String(month).padStart(2, "0")}`};
}
export async function lastMonthlyAttempt(env: Env): Promise<number | null> {
  const row = await env.INDEX.prepare(`SELECT fetch_started_at,collector_received_at,duration_ms FROM captures
    WHERE event_id LIKE 'nar-daily-monthly:%' AND fetch_started_at IS NOT NULL
    ORDER BY fetch_started_at DESC LIMIT 1`)
    .first<{fetch_started_at: string; collector_received_at: string | null; duration_ms: number | null}>();
  // Count failed requests too; anchor the limit after HTTP, conservatively using
  // processing duration when a response body never finished. An unfinished
  // attempt stays held for its full bounded window rather than guessing a retry.
  return row ? Math.max(Date.parse(row.fetch_started_at) + (row.duration_ms
    ?? (collection.capture_window_seconds + policy.request_timeout_seconds) * 1000),
    row.collector_received_at ? Date.parse(row.collector_received_at) : 0) : null;
}
const iso = (n: number) => new Date(n).toISOString().replace("Z", "000+00:00");
async function publish(env: Env, m: Manifest, processingStarted: number): Promise<void> {
  // R2 object and manifest must both exist before publishing an observation index.
  const raw = await env.RAW.head(`raw/${m.raw_sha256}`);
  if (!raw) throw new Error("RAW_MISSING");
  if (m.raw_saved_at === null) {
    // Recover a successful body write whose completion-manifest write was interrupted.
    m.raw_saved_at = iso(raw.uploaded.getTime());
    await env.RAW.put(`manifests/${m.event_id}.json`, JSON.stringify(m));
  }
  await env.INDEX.batch([
    env.INDEX.prepare(`UPDATE captures SET status='RAW_STORED',http_status=?,headers_received_at=?,collector_received_at=?,
      raw_saved_at=?,raw_sha256=?,raw_bytes=?,etag=?,validator_sent=?,validator_raw_sha256=?,
      file_name=?,file_timestamp=?,duration_ms=?,error_code=NULL WHERE event_id=?`).bind(
      m.http_status, m.headers_received_at, m.collector_received_at, m.raw_saved_at, m.raw_sha256, m.raw_bytes, m.etag,
      m.validator_sent, m.validator_raw_sha256, m.file_name, m.file_timestamp, m.duration_ms, m.event_id),
    env.INDEX.prepare(`INSERT OR IGNORE INTO raw_observations VALUES(?,?,?,?,?,?,NULL,NULL)`)
      .bind(m.event_id, m.raw_sha256, m.collector_received_at, m.raw_saved_at,
        m.http_status === 304 ? "validator_304" : "body_200", m.dataset_kind ?? "DAILY_SNAPSHOT")
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

export async function capture(scheduledTime: number, env: Env, kind: CaptureKind = "odds", daily = false, page?: PageTarget): Promise<void> {
  if (env.COLLECTION_ENABLED !== "true" || env.SOURCE_APPROVED !== "true") return;
  if (daily && env.DAILY_COLLECTION_ENABLED !== "true") return;
  const isMonthly = kind === "monthly";
  if (isMonthly && !daily) throw new Error("CAPTURE_KIND");
  const isPage = kind === "state" || kind === "payout";
  if (isPage) {
    if (!daily || !page || page.kind !== kind) throw new Error("PAGE_TARGET_REQUIRED");
    validatePage(page);
    const plan = await env.INDEX.prepare("SELECT url,race_id FROM page_capture_plans WHERE event_id=?")
      .bind(eventIdFor(kind, scheduledTime)).first<{url: string; race_id: string}>();
    if (!plan || plan.url !== page.url || plan.race_id !== page.race_id) throw new Error("PAGE_PLAN_REQUIRED");
  } else if (page || !["odds", "race", "monthly"].includes(kind)) throw new Error("CAPTURE_KIND");
  const monthly = isMonthly ? monthlyTarget(scheduledTime) : null;
  const url = page?.url ?? monthly?.url ?? policy.urls[kind as "odds" | "race"];
  const accept = isPage ? "text/html" : "application/zip";
  const allowed = (now: number) => daily
    ? Number.isSafeInteger(scheduledTime) && now >= scheduledTime
      && now - scheduledTime <= collection.capture_window_seconds * 1000
    : env.CAPTURE_SLOTS_JSON === undefined || sampleSlotAllowed(env.CAPTURE_SLOTS_JSON, scheduledTime, now);
  const processingStarted = Date.now();
  const eventId = eventIdFor(kind, scheduledTime);
  const existing = await env.INDEX.prepare("SELECT status,error_code FROM captures WHERE event_id=?")
    .bind(eventId).first<{status: string; error_code: string | null}>();
  if (existing) {
    if (["SOURCE_DENIED", "NON_ZIP_OR_CHALLENGE", "STOP_WRITE_FAILED", "INCOMPLETE_FETCH"].includes(existing.error_code ?? "")) {
      // Repair a refused request's stop before considering any future event.
      await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
    }
    if (["PENDING", "FETCHING", "STORAGE_ERROR"].includes(existing.status)) {
      const stored = await env.RAW.get(`manifests/${eventId}.json`);
      if (stored) {
        const manifest = await stored.json<Manifest>();
        if (await env.RAW.head(`raw/${manifest.raw_sha256}`)) await publish(env, manifest, processingStarted);
        else if (daily && Date.now() > scheduledTime
            + (collection.capture_window_seconds + policy.request_timeout_seconds) * 1000) {
          // The intent proves a validated ZIP response, but not a saved body.
          // Preserve its receipt and missing observation, then allow later slots.
          await env.INDEX.prepare(`UPDATE captures SET status='STORAGE_ERROR',error_code='RAW_MISSING',
            http_status=?,headers_received_at=?,collector_received_at=?
            WHERE event_id=? AND status IN ('PENDING','FETCHING','STORAGE_ERROR')`)
            .bind(manifest.http_status, manifest.headers_received_at, manifest.collector_received_at, eventId).run();
        }
      }
      else if (daily && existing.status !== "STORAGE_ERROR"
          && Date.now() > scheduledTime + (collection.capture_window_seconds + policy.request_timeout_seconds) * 1000) {
        // The old PENDING phase or FETCHING may have sent HTTP. Do not guess
        // whether a refusal was received before an interruption: record a stop.
        await env.INDEX.batch([
          env.INDEX.prepare("UPDATE captures SET status='INCOMPLETE_FETCH',error_code='INCOMPLETE_FETCH' WHERE event_id=? AND status IN ('PENDING','FETCHING')")
            .bind(eventId),
          env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=? AND EXISTS (SELECT 1 FROM captures WHERE event_id=? AND error_code='INCOMPLETE_FETCH')")
            .bind(SOURCE, eventId)
        ]);
      }
    } else if (daily && existing.status === "RESERVED" && !allowed(Date.now())) {
      // This phase is durable BEFORE the pre-flight query. HTTP starts only
      // after FETCHING is committed, so an expired reservation is a known gap.
      await env.INDEX.prepare("UPDATE captures SET status='MISSED_WINDOW' WHERE event_id=? AND status='RESERVED'")
        .bind(eventId).run();
    }
    if (isMonthly && env.MONTHLY_COLLECTION_ENABLED !== "true" && existing.status === "RESERVED") {
      await env.INDEX.prepare("UPDATE captures SET status='SKIPPED_DISABLED' WHERE event_id=? AND status='RESERVED'")
        .bind(eventId).run();
    }
    return; // no re-fetch on event redelivery; a later slot is a new attempt
  }
  if (isMonthly && env.MONTHLY_COLLECTION_ENABLED !== "true") {
    await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'SKIPPED_DISABLED')")
      .bind(eventId, iso(scheduledTime)).run();
    return;
  }
  const now = Date.now();
  if (!allowed(now)) {
    if (daily) await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'MISSED_WINDOW')")
      .bind(eventId, iso(scheduledTime)).run();
    return;
  }
  if (isMonthly) {
    const previous = await lastMonthlyAttempt(env);
    if (previous !== null && now < previous + policy.monthly_min_interval_seconds * 1000) {
      await env.INDEX.prepare("INSERT OR IGNORE INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,'WAIT_OR_BLOCKED')")
        .bind(eventId, iso(scheduledTime)).run();
      return;
    }
  }
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
    fetch_started_at,status) VALUES(?,?,?,'RESERVED')`).bind(eventId, iso(scheduledTime), iso(now)).run();
  if (!inserted.meta.changes) return;
  // Monthly responses vary by month; do not reuse another month's validator.
  const prior = isPage || isMonthly ? null : await env.INDEX.prepare(`SELECT etag,raw_sha256,file_name,file_timestamp FROM captures
    WHERE status='RAW_STORED' AND http_status=200 AND event_id LIKE ? ORDER BY collector_received_at DESC LIMIT 1`)
    .bind(`nar-daily-${kind}:%`).first<{etag: string | null; raw_sha256: string; file_name: string | null; file_timestamp: string | null}>();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), policy.request_timeout_seconds * 1000);
  let httpStatus: number | null = null;
  let receivedAt: string | null = null;
  let headersAt: string | null = null;
  let contentType: string | null = null;
  let cfMitigated: string | null = null;
  let denialBasis: string | null = null;
  let errorBody: Uint8Array | null = null;
  let stage: "NETWORK" | "STORAGE" = "NETWORK";
  let stopRequired = false;
  const stopSource = async () => {
    stopRequired = true;
    await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?").bind(SOURCE).run();
  };
  try {
    if (!allowed(Date.now())) throw new Error("SAMPLE_WINDOW_EXPIRED");
    await env.INDEX.prepare("UPDATE captures SET status='FETCHING' WHERE event_id=? AND status='RESERVED'")
      .bind(eventId).run();
    if (!allowed(Date.now())) throw new Error("SAMPLE_WINDOW_EXPIRED");
    const response = await fetchPublic(url, accept, controller.signal,
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
      await stopSource();
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
          await stopSource();
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
          await stopSource();
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
    const unix = filename?.match(/_(\d{10})_(?:odds|race)\.zip$/)?.[1];
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
      const body = await boundedBody(response, isPage ? policy.page_max_bytes : policy.max_raw_bytes);
      receivedAt = iso(Date.now());
      const validBody = isPage
        ? !!contentType?.includes("text/html") && /<(?:!doctype\s+html|html)\b/i.test(new TextDecoder().decode(body.subarray(0, 8192)))
          && !/captcha|challenge/i.test(new TextDecoder().decode(body))
        : body[0] === 0x50 && body[1] === 0x4b;
      if (!validBody) {
        errorBody = body;
        // Any HTML/interstitial on the ZIP path stops the source for manual investigation.
        await stopSource();
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
      duration_ms: Date.now() - now,
      dataset_kind: isMonthly ? "FINAL_ONLY" : isPage ? `NAR_PAGE_${kind.toUpperCase()}` : kind === "odds" ? "DAILY_SNAPSHOT" : "NAR_RACE_BUNDLE",
      ...(monthly ? {url: monthly.url} : {}),
      ...(page ? {url: page.url, race_id: page.race_id} : {})};
    // Store the event-to-hash intent first. It is not a successful observation.
    await env.RAW.put(`manifests/${eventId}.json`, JSON.stringify(manifest));
    if (content !== null) {
      const saved = await env.RAW.put(`raw/${hash}`, content);
      if (!saved) throw new Error("RAW_PUT_FAILED");
      manifest.raw_saved_at = iso(saved.uploaded.getTime());
      await env.RAW.put(`manifests/${eventId}.json`, JSON.stringify(manifest));
    }
    await publish(env, manifest, processingStarted);
  } catch (error) {
    const allowed = new Set(["SAMPLE_WINDOW_EXPIRED", "SOURCE_DENIED", "RATE_LIMITED", "HTTP_ERROR", "UNBOUND_304", "BODY_LIMIT", "BODY_EMPTY", "NON_ZIP_OR_CHALLENGE"]);
    const code = error instanceof Error && allowed.has(error.message) ? error.message : stopRequired ? "STOP_WRITE_FAILED" : stage === "STORAGE" ? "STORAGE_ERROR" : controller.signal.aborted ? "FETCH_TIMEOUT" : "NETWORK_ERROR";
    await env.INDEX.prepare("UPDATE captures SET status=?,http_status=?,duration_ms=?,error_code=?,headers_received_at=?,collector_received_at=? WHERE event_id=?")
      .bind(code === "STORAGE_ERROR" ? "STORAGE_ERROR" : "FAILED", httpStatus, Date.now() - now, code, headersAt, receivedAt, eventId).run();
    // Private diagnostic metadata only. Never turn an error response into an odds observation.
    // Keep the original stop/wait outcome even if this optional evidence write fails.
    try {
      await env.RAW.put(`failure-metadata/${eventId}.json`, JSON.stringify({
        schema: "collector-failure-metadata-v1", event_id: eventId,
        request_profile_headers: {...REQUEST_HEADERS, Accept: accept}, http_status: httpStatus,
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
  } finally {
    clearTimeout(timer);
    // A transient failure writing the stop must not turn a refusal into a retry.
    if (stopRequired) await env.INDEX.prepare("UPDATE source_control SET blocked=1 WHERE source=?")
      .bind(SOURCE).run();
    // Both ZIP endpoints and later invocations share the wait after this response.
    const finished = receivedAt === null ? Date.now() : Date.parse(receivedAt);
    await env.INDEX.prepare("UPDATE source_control SET next_allowed_at=max(next_allowed_at,?) WHERE source=?")
      .bind(finished + INTERVAL, SOURCE).run();
  }
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
