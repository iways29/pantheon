/** Times are shown in the owner's time zone. */
export const OWNER_TIME_ZONE = 'America/New_York';

const clock = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  timeZone: OWNER_TIME_ZONE,
});
const dayMonth = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  timeZone: OWNER_TIME_ZONE,
});
const ymd = new Intl.DateTimeFormat('en-CA', { timeZone: OWNER_TIME_ZONE });

/** "14:29" today, "12 Sep" before. */
export function when(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '';
  const at = new Date(iso);
  return ymd.format(at) === ymd.format(now) ? clock.format(at) : dayMonth.format(at);
}

export function time(iso: string | null | undefined): string {
  return iso ? clock.format(new Date(iso)) : '';
}

/** Dollars: cents for amounts a person reads, more digits for tiny ones. */
export function money(usd: number | null | undefined): string {
  const value = usd ?? 0;
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

export function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}

/** "research-lead" → "Research lead". */
export function agentName(name: string | null | undefined): string {
  if (!name) return '';
  const words = name.replace(/[-_]+/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** "executive" → "Executive". */
export function departmentName(name: string | null | undefined): string {
  return agentName(name);
}
