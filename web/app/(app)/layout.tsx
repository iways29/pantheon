import { redirect } from 'next/navigation';
import type { ReactNode } from 'react';

import { Auras } from '@/components/Auras';
import { CompanyProvider } from '@/lib/company';
import { createClient } from '@/lib/supabase/server';

import '../shell.css';

export const dynamic = 'force-dynamic';

/** Everything behind sign-in: the Brain and the Control Center. */
export default async function SignedInLayout({ children }: { children: ReactNode }) {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) redirect('/login');

  return (
    <CompanyProvider>
      <Auras />
      {children}
    </CompanyProvider>
  );
}
