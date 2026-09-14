// The same formatting web.py applies, so the app and the web UI say the same
// thing in the same words.

export const DASH = '—';
const NBSP = ' ';

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const pad = (n: number) => String(n).padStart(2, '0');

/**
 * A Stockholm wall-clock time from the API, as a local Date.
 *
 * Built from its parts rather than handed to the Date parser: the strings can
 * carry microseconds, which not every JS engine accepts. "ago" assumes the
 * phone is on Swedish time; the absolute times are right regardless.
 */
export function parseLocal(value: string): Date {
  const [, y, mo, d, h = '0', mi = '0'] =
    value.match(/^(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?/) ?? [];
  return new Date(+y, +mo - 1, +d, +h, +mi);
}

/** Thousands separated by a space, as Swedish writes them. */
function grouped(value: number, digits: number) {
  return value.toFixed(digits).replace(/\B(?=(\d{3})+(?!\d))/g, NBSP);
}

export const kr = (value: number | null) =>
  value == null ? DASH : `${grouped(value, 2)}${NBSP}kr`;

export const kwh = (value: number | null) =>
  value == null ? DASH : `${grouped(value, 1)}${NBSP}kWh`;

export const pct = (value: number | null) => (value == null ? DASH : `${value}%`);

export function hhmm(value: string) {
  const d = parseLocal(value);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** "Mon" */
export const weekday = (value: string) => DAYS[parseLocal(value).getDay()];

/** "Mon 31 Aug" */
export function dayLabel(value: string) {
  const d = parseLocal(value);
  return `${DAYS[d.getDay()]} ${pad(d.getDate())} ${MONTHS[d.getMonth()]}`;
}

/** "31 Aug" */
export function shortDay(value: string) {
  const d = parseLocal(value);
  return `${pad(d.getDate())} ${MONTHS[d.getMonth()]}`;
}

/** Absolute and to the minute: 23:45 or 00:15 is the whole point. */
export const when = (value: string | null) =>
  value == null ? DASH : `${dayLabel(value)}, ${hhmm(value)}`;

/** Relative, for the one place it is the question being asked: liveness. */
export function ago(value: string | null) {
  if (value == null) {
    return 'never';
  }
  const seconds = (Date.now() - parseLocal(value).getTime()) / 1000;
  if (seconds < 90) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${shortDay(value)} ${hhmm(value)}`;
}
