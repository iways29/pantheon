/**
 * Browser-visible configuration.
 *
 * Only `NEXT_PUBLIC_*` values belong here. Service-role keys, provider keys and
 * the Supabase JWT secret are server-side only and must never reach the bundle.
 */

function required(name: string, value: string | undefined): string {
  if (!value) {
    throw new Error(`Missing environment variable: ${name}. See .env.example.`);
  }
  return value;
}

export function supabaseUrl(): string {
  return required('NEXT_PUBLIC_SUPABASE_URL', process.env.NEXT_PUBLIC_SUPABASE_URL);
}

export function supabaseAnonKey(): string {
  return required('NEXT_PUBLIC_SUPABASE_ANON_KEY', process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY);
}

export function apiBaseUrl(): string {
  return required('NEXT_PUBLIC_API_BASE_URL', process.env.NEXT_PUBLIC_API_BASE_URL).replace(
    /\/$/,
    '',
  );
}
