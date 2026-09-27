import { notFound } from 'next/navigation';

import { PreviewScreen } from '@/app/preview/PreviewScreen';
import type { PreviewState } from '@/lib/fixtures';

const STATES: PreviewState[] = ['rest', 'busy', 'needs', 'paused', 'killed'];

/**
 * The screens with sample data, for comparing with the designs on a
 * developer's machine: /preview?state=needs. Never served in production.
 */
export default async function PreviewPage({
  searchParams,
}: {
  searchParams: Promise<{ state?: string; tab?: string }>;
}) {
  if (process.env.NODE_ENV !== 'development') notFound();
  const { state, tab } = await searchParams;
  const chosen = STATES.includes(state as PreviewState) ? (state as PreviewState) : 'rest';
  return <PreviewScreen state={chosen} tab={tab === 'control' ? 'control' : 'brain'} />;
}
