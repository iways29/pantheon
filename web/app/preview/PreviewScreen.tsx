'use client';

import ControlPage from '@/app/(app)/control/page';
import { Auras } from '@/components/Auras';
import { BrainScreen } from '@/components/BrainScreen';
import { CompanyProvider } from '@/lib/company';
import type { PreviewState } from '@/lib/fixtures';

import '../shell.css';

export function PreviewScreen({ state, tab }: { state: PreviewState; tab: 'brain' | 'control' }) {
  return (
    <CompanyProvider key={state} preview={state}>
      <Auras />
      {tab === 'control' ? <ControlPage /> : <BrainScreen />}
    </CompanyProvider>
  );
}
