/** Private capture persistence shared by every sport. No provider requests. */
export type CaptureStorage = {RAW: R2Bucket; INDEX: D1Database};
export const iso = (n: number) => new Date(n).toISOString().replace("Z", "000+00:00");
export interface CaptureManifest {
  event_id: string; scheduled_capture_at: string; fetch_started_at: string;
  headers_received_at: string; collector_received_at: string; raw_saved_at: string | null; raw_sha256: string; raw_bytes: number;
  http_status: number; etag: string | null; validator_sent: string | null;
  validator_raw_sha256: string | null; file_name: string | null; file_timestamp: string | null;
  duration_ms: number; dataset_kind: string;
  url?: string; race_id?: string;
}
export async function publishCapture(env: CaptureStorage, m: CaptureManifest, processingStarted: number): Promise<void> {
  if (!m.dataset_kind) throw new Error("DATASET_KIND_REQUIRED");
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
        m.http_status === 304 ? "validator_304" : "body_200", m.dataset_kind)
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

export async function saveCapture(env: CaptureStorage, m: CaptureManifest, content: Uint8Array | null, started: number): Promise<void> {
  await env.RAW.put(`manifests/${m.event_id}.json`, JSON.stringify(m));
  if (content !== null) {
    const saved = await env.RAW.put(`raw/${m.raw_sha256}`, content);
    if (!saved) throw new Error("RAW_PUT_FAILED");
    m.raw_saved_at = iso(saved.uploaded.getTime());
    await env.RAW.put(`manifests/${m.event_id}.json`, JSON.stringify(m));
  }
  await publishCapture(env, m, started);
}
export async function digest(body: Uint8Array): Promise<string> {
  const hash = await crypto.subtle.digest("SHA-256", body);
  return [...new Uint8Array(hash)].map(x => x.toString(16).padStart(2, "0")).join("");
}
