import type { ActivityBlock } from "../App";
import { getDurationMinutes } from "./collisionUtils";

export function formatDurationMinutes(totalMinutes: number): string {
  const total = Math.max(1, Math.round(Number(totalMinutes) || 0));
  const hours = Math.floor(total / 60);
  const minutes = total % 60;

  if (hours && minutes) return `${hours}h ${minutes}m`;
  if (hours) return `${hours}h`;
  return `${minutes}m`;
}

export function parseDurationMinutes(value: unknown): number | null {
  const text = String(value || "").trim().toLowerCase();
  if (!text) return null;

  const hourMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b/);
  const minuteMatch = text.match(/(\d+(?:\.\d+)?)\s*(?:m|min|mins|minute|minutes)\b/);
  if (hourMatch || minuteMatch) {
    return Math.max(1, Math.round(
      Number(hourMatch?.[1] || 0) * 60 + Number(minuteMatch?.[1] || 0),
    ));
  }

  const numeric = Number(text);
  return Number.isFinite(numeric) && numeric > 0
    ? Math.max(1, Math.round(numeric * 60))
    : null;
}

export function getCanonicalDurationMinutes(activity: ActivityBlock): number {
  const explicit = Number(activity.duration_minutes);
  if (Number.isFinite(explicit) && explicit > 0) return Math.round(explicit);

  const elapsed = getDurationMinutes(activity);
  if (Number.isFinite(elapsed) && elapsed > 0) return Math.round(elapsed);

  return parseDurationMinutes(activity.duration) || 60;
}

export function formatActivityDuration(activity: ActivityBlock): string {
  return formatDurationMinutes(getCanonicalDurationMinutes(activity));
}
