import { redirect } from 'next/navigation';
import type { ReactNode } from 'react';

import { Auras } from '@/components/Auras';
import { CompanySplash } from '@/components/Splash';
import { CompanyProvider } from '@/lib/company';
import { createClient } from '@/lib/supabase/server';

import '../shell.css';

export const dynamic = 'force-dynamic';

/** Everything behind sign-in: the Brain and the Control Center. */
export default async function SignedInLayout({ children }: { children: ReactNode }) {
  const supabase = await createClient();
  // Checked against the project's signing keys, not a call to Supabase Auth.
  const { data } = await supabase.auth.getClaims();
  if (!data?.claims) redirect('/login');

  return (
    <CompanyProvider>
      <Auras />
      {children}
      <CompanySplash />
    </CompanyProvider>
  );
}
