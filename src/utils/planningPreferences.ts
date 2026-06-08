import type { DailySchedule } from "../App";
import type { GeocodeCandidate, SavedLocation } from "../services/planService";

export type PlanningLocation = {
  label?: string;
  display_name?: string;
  address?: string;
  latitude?: number | null;
  longitude?: number | null;
  category?: string;
  source?: string;
  confirmed_by_user?: boolean;
};

export type PlanningPreferences = {
  day_start_time: string;
  day_end_time: string;
  use_day_boundary_preferences?: boolean;
  default_start_location?: PlanningLocation | null;
  default_buffer_minutes?: number;
};

export type RecentLocation = PlanningLocation & {
  last_used_at: string;
  source: string;
  location_key?: string;
};

const DEFAULT_PREFERENCES: PlanningPreferences = {
  day_start_time: "08:00",
  day_end_time: "22:00",
  use_day_boundary_preferences: true,
  default_start_location: null,
  default_buffer_minutes: 5,
};

const preferenceKey = (userId?: string) => `jplan.preferences.${userId || "anonymous"}`;
const recentKey = (userId?: string) => `jplan.recentLocations.${userId || "anonymous"}`;

export function toCanonicalTime(value?: string | null): string {
  const raw = (value || "").trim();
  if (!raw) return "";
  const match24 = raw.match(/^(\d{1,2}):(\d{2})$/);
  if (match24) {
    return `${match24[1].padStart(2, "0")}:${match24[2]}`;
  }
  const match12 = raw.match(/^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)$/i);
  if (!match12) return raw;
  let hour = Number(match12[1]);
  const minute = match12[2] || "00";
  const meridiem = match12[3].toUpperCase();
  if (meridiem === "PM" && hour !== 12) hour += 12;
  if (meridiem === "AM" && hour === 12) hour = 0;
  return `${String(hour).padStart(2, "0")}:${minute}`;
}

export function toDisplayTime(value?: string | null): string {
  const canonical = toCanonicalTime(value);
  const match = canonical.match(/^(\d{2}):(\d{2})$/);
  if (!match) return value || "";
  const hour24 = Number(match[1]);
  const minute = match[2];
  const meridiem = hour24 >= 12 ? "PM" : "AM";
  const hour12 = hour24 % 12 || 12;
  return `${hour12}:${minute} ${meridiem}`;
}

export function hasLocationCoordinates(location?: PlanningLocation | null): boolean {
  if (!location) return false;
  const lat = Number(location.latitude);
  const lng = Number(location.longitude);
  return Number.isFinite(lat) && Number.isFinite(lng);
}

export function normalizeBufferMinutes(value?: unknown, fallback: number = DEFAULT_PREFERENCES.default_buffer_minutes || 5): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return fallback;
  return Math.max(0, Math.min(60, Math.round(numeric)));
}

export function savedLocationToPlanningLocation(location: SavedLocation): PlanningLocation {
  return {
    label: location.label,
    display_name: location.display_name || location.label || location.address,
    address: location.address || location.display_name || location.label,
    latitude: location.latitude,
    longitude: location.longitude,
    category: (location as SavedLocation & { category?: string }).category,
    source: location.source || "saved_location",
    confirmed_by_user: location.confirmed_by_user ?? true,
  };
}

export function candidateToPlanningLocation(candidate: GeocodeCandidate, category?: string): PlanningLocation {
  return {
    label: candidate.label || candidate.display_name || candidate.address,
    display_name: candidate.display_name || candidate.address || candidate.label,
    address: candidate.address || candidate.display_name || candidate.label,
    latitude: candidate.latitude,
    longitude: candidate.longitude,
    category,
    source: candidate.source || "event_confirmed",
    confirmed_by_user: candidate.confirmed_by_user ?? true,
  };
}

export function loadPlanningPreferences(userId?: string): PlanningPreferences {
  if (typeof window === "undefined") return DEFAULT_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(preferenceKey(userId));
    if (!raw) return DEFAULT_PREFERENCES;
    const parsed = JSON.parse(raw) as Partial<PlanningPreferences>;
    return {
      day_start_time: toCanonicalTime(parsed.day_start_time) || DEFAULT_PREFERENCES.day_start_time,
      day_end_time: toCanonicalTime(parsed.day_end_time) || DEFAULT_PREFERENCES.day_end_time,
      use_day_boundary_preferences: parsed.use_day_boundary_preferences ?? DEFAULT_PREFERENCES.use_day_boundary_preferences,
      default_start_location: parsed.default_start_location || null,
      default_buffer_minutes: normalizeBufferMinutes(parsed.default_buffer_minutes),
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

export function normalizePlanningPreferences(preferences?: Partial<PlanningPreferences> | null): PlanningPreferences {
  return {
    day_start_time: toCanonicalTime(preferences?.day_start_time) || DEFAULT_PREFERENCES.day_start_time,
    day_end_time: toCanonicalTime(preferences?.day_end_time) || DEFAULT_PREFERENCES.day_end_time,
    use_day_boundary_preferences: preferences?.use_day_boundary_preferences ?? DEFAULT_PREFERENCES.use_day_boundary_preferences,
    default_start_location: preferences?.default_start_location || null,
    default_buffer_minutes: normalizeBufferMinutes(preferences?.default_buffer_minutes),
  };
}

export function savePlanningPreferences(userId: string | undefined, preferences: PlanningPreferences): void {
  if (typeof window === "undefined") return;
  const normalized = normalizePlanningPreferences(preferences);
  window.localStorage.setItem(
    preferenceKey(userId),
    JSON.stringify({
      day_start_time: normalized.day_start_time,
      day_end_time: normalized.day_end_time,
      use_day_boundary_preferences: normalized.use_day_boundary_preferences,
      default_start_location: normalized.default_start_location,
      default_buffer_minutes: normalized.default_buffer_minutes,
    }),
  );
}

export function mergePlanningPreferences(
  schedule: DailySchedule,
  userId?: string,
  preferenceOverride?: Partial<PlanningPreferences> | null,
): DailySchedule {
  const prefs = normalizePlanningPreferences(preferenceOverride || loadPlanningPreferences(userId));
  const existing = schedule.preferences || {};
  const dayStartTime = String(existing.day_start_time || prefs.day_start_time);
  const dayEndTime = String(existing.day_end_time || prefs.day_end_time);
  const useDayBoundaries = existing.use_day_boundary_preferences ?? prefs.use_day_boundary_preferences;
  const defaultStart = (existing.default_start_location as PlanningLocation | undefined) || prefs.default_start_location || null;
  const defaultBufferMinutes = normalizeBufferMinutes(
    existing.default_buffer_minutes ?? existing.prep_buffer ?? prefs.default_buffer_minutes,
  );
  return {
    ...schedule,
    preferences: {
      ...existing,
      day_start_time: dayStartTime,
      day_end_time: dayEndTime,
      use_day_boundary_preferences: useDayBoundaries,
      ...(useDayBoundaries === false ? {} : {
        day_start: existing.day_start || dayStartTime,
        day_end: existing.day_end || dayEndTime,
      }),
      default_start_location: defaultStart,
      default_buffer_minutes: defaultBufferMinutes,
      prep_buffer: normalizeBufferMinutes(existing.prep_buffer ?? defaultBufferMinutes),
      ...(existing.day_start_location_override ? { day_start_location_override: existing.day_start_location_override } : {}),
    },
  };
}

function recentIdentity(location: PlanningLocation): string {
  if (hasLocationCoordinates(location)) {
    return `coord:${Number(location.latitude).toFixed(6)}:${Number(location.longitude).toFixed(6)}`;
  }
  return `text:${(location.label || location.display_name || location.address || "").trim().toLowerCase()}`;
}

export function loadRecentLocations(userId?: string): RecentLocation[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(recentKey(userId));
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.slice(0, 5) : [];
  } catch {
    return [];
  }
}

export function saveRecentLocations(userId: string | undefined, locations: PlanningLocation[]): RecentLocation[] {
  if (typeof window === "undefined") return [];
  const next = locations
    .filter(hasLocationCoordinates)
    .map((location) => ({
      ...location,
      source: location.source || "recent",
      last_used_at: (location as RecentLocation).last_used_at || new Date().toISOString(),
    }))
    .slice(0, 5) as RecentLocation[];
  window.localStorage.setItem(recentKey(userId), JSON.stringify(next));
  return next;
}

export function addRecentLocation(userId: string | undefined, location: PlanningLocation): RecentLocation[] {
  if (typeof window === "undefined" || !location) return [];
  const recent: RecentLocation = {
    ...location,
    source: location.source || "recent",
    last_used_at: new Date().toISOString(),
  };
  const identity = recentIdentity(recent);
  const next = [
    recent,
    ...loadRecentLocations(userId).filter(item => recentIdentity(item) !== identity),
  ].slice(0, 5);
  window.localStorage.setItem(recentKey(userId), JSON.stringify(next));
  console.debug("[JPLAN][RECENT_LOCATION] added=", recent.label || recent.display_name || recent.address);
  return next;
}
