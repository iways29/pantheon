import { createServerClient } from '@supabase/ssr';
import { NextResponse, type NextRequest } from 'next/server';

import { supabaseAnonKey, supabaseUrl } from '@/lib/env';

/**
 * Refreshes the Supabase session on every navigation so Server Components see
 * a valid token. Without this, an expired access token would only be noticed
 * on the client. `getClaims` checks the token against the project's published
 * keys (cached) instead of asking Supabase Auth on every page.
 */
export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });

  const supabase = createServerClient(supabaseUrl(), supabaseAnonKey(), {
    cookies: {
      getAll() {
        return request.cookies.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value } of cookiesToSet) {
          request.cookies.set(name, value);
        }
        response = NextResponse.next({ request });
        for (const { name, value, options } of cookiesToSet) {
          response.cookies.set(name, value, options);
        }
      },
    },
  });

  await supabase.auth.getClaims();

  return response;
}

export const config = {
  // The local preview (development only) runs without a session. The API
  // relay (/api/p) reads and refreshes the session itself: running this as
  // well cost every API call an extra trip to Supabase Auth.
  matcher: ['/((?!_next/static|_next/image|favicon.ico|preview|api/p/).*)'],
};
