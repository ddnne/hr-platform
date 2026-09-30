/** Common public provider transport: no credentials, redirects or automatic retries. */
const INTERVAL = 120_000;
export const USER_AGENT = "hr-platform-personal-research/0.1";
export function fetchPublic(url: string, accept: string, signal: AbortSignal, extra: Record<string, string> = {}): Promise<Response> {
  return fetch(url, {redirect: "manual", signal, headers: {"User-Agent": USER_AGENT, "Accept": accept, ...extra}});
}

export async function discard(response: Response): Promise<void> {
  // Cleanup failure must never bypass refusal or Retry-After controls.
  try { await response.body?.cancel(); } catch { /* already errored/closed */ }
}

export function retryAfter(value: string | null, now: number, minimumWait = INTERVAL): number {
  if (value === null) return now + minimumWait;
  const seconds = /^\d+$/.test(value.trim()) ? Number(value) : NaN;
  const at = Number.isFinite(seconds) ? now + seconds * 1000 : Date.parse(value);
  return Number.isFinite(at) ? Math.max(now + minimumWait, at) : now + minimumWait;
}

export async function boundedBody(response: Response, maximum = 16 * 1024 * 1024): Promise<Uint8Array> {
  const length = Number(response.headers.get("content-length"));
  if (length > maximum) {
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
      if (size > maximum) throw new Error("BODY_LIMIT");
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
