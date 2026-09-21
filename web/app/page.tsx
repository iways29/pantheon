import { redirect } from 'next/navigation';

import { apiBaseUrl } from '@/lib/env';
import { createClient } from '@/lib/supabase/server';

export const dynamic = 'force-dynamic';

type ProbeResult = { ok: true; status: number; body: unknown } | { ok: false; reason: string };

/**
 * Step 0 acceptance check: call the API's owner-only health route with the
 * signed-in owner's Supabase token and show what came back.
 */
async function probeApi(accessToken: string): Promise<ProbeResult> {
  try {
    const response = await fetch(`${apiBaseUrl()}/health/authed`, {
      headers: { Authorization: `Bearer ${accessToken}` },
      cache: 'no-store',
    });
    return { ok: true, status: response.status, body: await response.json() };
  } catch (error) {
    return { ok: false, reason: error instanceof Error ? error.message : String(error) };
  }
}

export default async function HomePage() {
  const supabase = await createClient();

  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user) {
    redirect('/login');
  }

  const {
    data: { session },
  } = await supabase.auth.getSession();

  const probe = session ? await probeApi(session.access_token) : null;

  return (
    <main>
      <h1>Pantheon</h1>
      <p>
        Signed in as <strong>{user.email}</strong>
      </p>

      <h2>API health (behind auth)</h2>
      {probe === null ? <p>No active session token.</p> : null}
      {probe?.ok === true ? (
        <pre>
          {probe.status} {JSON.stringify(probe.body, null, 2)}
        </pre>
      ) : null}
      {probe?.ok === false ? <p role="alert">Could not reach the API: {probe.reason}</p> : null}

      <form action="/auth/signout" method="post">
        <button type="submit">Sign out</button>
      </form>
    </main>
  );
}
