// The NUC. The hostname resolves on the LAN and over WireGuard, nowhere else.
export const API_BASE = 'https://ev-smart-charge.home.dkms.se';

// Times arrive as ISO 8601 with no offset: Stockholm wall-clock time, exactly
// as the database stores it. See format.ts for reading them.

export type Run = {
  at: string;
  decision: string;
  decision_label: string;
  decision_icon: string;
  charging_state: string | null;
  battery_percent: number | null;
  target_percent: number | null;
  error: string | null;
};

export type Settings = {
  departure_hour: number;
  charger_speed_kw: number;
  battery_capacity_kwh: number;
  charge_limit_percent: number;
  smart_charging_enabled: boolean;
};

export type Session = {
  started_at: string;
  ended_at: string;
  start_percent: number | null;
  end_percent: number | null;
  energy_kwh: number;
  // null when spot prices are missing for some slot: unknown, not zero.
  actual_cost: number | null;
  baseline_cost: number | null;
  savings: number | null;
  error_count: number;
};

export type Totals = {
  sessions: number;
  energy_kwh: number;
  actual_cost: number;
  baseline_cost: number;
  savings: number;
  unpriced: number;
};

export type Overview = {
  days: number;
  latest: Run | null;
  reading: Run | null;
  settings: Settings;
  totals: Totals;
  daily_savings: { day: string; savings: number }[];
  sessions: Session[];
};

export type StripSlot = { slot: string; price: number | null; charged: boolean };

export type RecentRow = {
  started_at: string;
  ended_at: string;
  count: number;
  decision: string;
  decision_label: string;
  decision_icon: string;
  charging_state: string | null;
  error: string | null;
  start_percent: number | null;
  end_percent: number | null;
};

export type History = {
  days: number;
  sessions: (Session & { strip: StripSlot[] | null })[];
  recent: RecentRow[];
};

/**
 * The settings form as typed. The numbers stay text: the server parses them,
 * decimal commas included, and answers with the same messages the web form
 * shows, so the rules live in one place.
 */
export type SettingsDraft = {
  [K in Exclude<keyof Settings, 'smart_charging_enabled'>]: string;
} & { smart_charging_enabled: boolean };

export type SaveResult =
  | { ok: true; settings: Settings }
  | { ok: false; problems: Record<string, string> };

async function get<T>(path: string): Promise<T> {
  const response = await fetch(API_BASE + path);
  if (!response.ok) {
    throw new Error(`The server answered ${response.status}.`);
  }
  return response.json();
}

export const getOverview = (days: number) => get<Overview>(`/api/overview?days=${days}`);
export const getHistory = (days: number) => get<History>(`/api/history?days=${days}`);
export const getSettings = () => get<Settings>('/api/settings');

export async function saveSettings(draft: SettingsDraft): Promise<SaveResult> {
  const response = await fetch(`${API_BASE}/api/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(draft),
  });
  if (response.status === 422) {
    return { ok: false, problems: (await response.json()).problems };
  }
  if (!response.ok) {
    throw new Error(`The server answered ${response.status}.`);
  }
  return { ok: true, settings: await response.json() };
}
