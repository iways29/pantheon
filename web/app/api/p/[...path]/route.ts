import { NextResponse, type NextRequest } from 'next/server';

import { apiBaseUrl } from '@/lib/env';
import { createClient } from '@/lib/supabase/server';

/**
 * The browser's only way to the Pantheon API: `/api/p/<path>` is forwarded to
 * `<API>/<path>` with the signed-in owner's Supabase token. Same origin, so no
 * cross-site setup; the API still checks the token and applies RLS, so this
 * relay grants nothing by itself.
 */
async function relay(request: NextRequest, path: string[]): Promise<NextResponse> {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    return NextResponse.json({ detail: 'Not signed in' }, { status: 401 });
  }
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) {
    return NextResponse.json({ detail: 'Not signed in' }, { status: 401 });
  }

  const target = `${apiBaseUrl()}/${path.map(encodeURIComponent).join('/')}${request.nextUrl.search}`;
  const init: RequestInit = {
    method: request.method,
    headers: {
      Authorization: `Bearer ${session.access_token}`,
      'Content-Type': request.headers.get('content-type') ?? 'application/json',
    },
    cache: 'no-store',
  };
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    init.body = await request.text();
  }

  try {
    const upstream = await fetch(target, init);
    const type = upstream.headers.get('content-type') ?? '';
    if (type.startsWith('text/event-stream') && upstream.body) {
      // A streamed reply (the chat, ADR 037): passed through as it arrives.
      return new NextResponse(upstream.body, {
        status: upstream.status,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache, no-transform',
          'X-Accel-Buffering': 'no',
        },
      });
    }
    return new NextResponse(await upstream.text(), {
      status: upstream.status,
      headers: { 'Content-Type': upstream.headers.get('content-type') ?? 'application/json' },
    });
  } catch {
    return NextResponse.json({ detail: 'The API could not be reached' }, { status: 502 });
  }
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: Context) {
  return relay(request, (await context.params).path);
}

export async function POST(request: NextRequest, context: Context) {
  return relay(request, (await context.params).path);
}

export async function PUT(request: NextRequest, context: Context) {
  return relay(request, (await context.params).path);
}
