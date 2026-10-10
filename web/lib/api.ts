'use client';

/**
 * Calls to the Pantheon API from the browser, through the same-origin relay
 * (`app/api/p/[...path]/route.ts`).
 */

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/p/${path.replace(/^\//, '')}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
  });
  const text = await response.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, detailOf(data, response.statusText));
  }
  return data as T;
}

/** The API's reason, in words: FastAPI sends a string, or a list of problems. */
export function detailOf(data: unknown, fallback: string): string {
  if (!data || typeof data !== 'object' || !('detail' in data)) return fallback;
  const detail = (data as { detail: unknown }).detail;
  if (Array.isArray(detail)) {
    const first = detail[0] as { msg?: unknown; loc?: unknown[] } | undefined;
    if (first?.msg) return `${String(first.msg)}${first.loc ? ` (${first.loc.slice(1).join('.')})` : ''}`;
  }
  return typeof detail === 'string' ? detail : JSON.stringify(detail);
}

export const api = {
  get: <T>(path: string) => call<T>('GET', path),
  post: <T>(path: string, body?: unknown) => call<T>('POST', path, body ?? {}),
  put: <T>(path: string, body?: unknown) => call<T>('PUT', path, body ?? {}),
};

/** Send a file as the raw request body (a document upload). */
export async function upload<T>(path: string, file: File): Promise<T> {
  const response = await fetch(`/api/p/${path.replace(/^\//, '')}`, {
    method: 'POST',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
    cache: 'no-store',
  });
  const text = await response.text();
  const data: unknown = text ? JSON.parse(text) : null;
  if (!response.ok) {
    const detail =
      data && typeof data === 'object' && 'detail' in data
        ? String((data as { detail: unknown }).detail)
        : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

/**
 * POST and read a server-sent event stream (the chat, ADR 037): `onEvent` is
 * called with each event's name and data as it arrives.
 */
export async function stream(
  path: string,
  body: unknown,
  onEvent: (kind: string, data: unknown) => void,
): Promise<void> {
  const response = await fetch(`/api/p/${path.replace(/^\//, '')}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    cache: 'no-store',
  });
  if (!response.ok || !response.body) {
    const text = await response.text();
    let detail = response.statusText;
    try {
      const data = JSON.parse(text) as { detail?: unknown };
      if (data.detail) detail = String(data.detail);
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let kind = 'message';
      let data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) kind = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      if (data) onEvent(kind, JSON.parse(data));
    }
  }
}
