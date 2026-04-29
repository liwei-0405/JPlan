import type { ActivityBlock } from "../App";

/**
 * Convert time string to total minutes since midnight.
 * Supports "HH:mm" (24H) and "hh:mm AM/PM" (12H).
 */
export function timeToMinutes(t: string): number {
  if (!t) return 0;
  const upper = t.toUpperCase().trim();
  
  if (!upper.includes(" ")) {
    // 24H format "HH:mm"
    const [h, m] = upper.split(":").map(Number);
    return (h || 0) * 60 + (m || 0);
  }
  
  // 12H format "hh:mm AM"
  const [time, period] = upper.split(" ");
  let [h, m] = time.split(":").map(Number);
  if (period === "PM" && h !== 12) h += 12;
  if (period === "AM" && h === 12) h = 0;
  return (h || 0) * 60 + (m || 0);
}

/**
 * Get the duration in minutes for an activity.
 */
export function getDurationMinutes(activity: ActivityBlock): number {
  const st = activity.startTime || activity.start;
  const et = activity.endTime || activity.end;
  
  if (!st || !et) return activity.duration_minutes || 60;
  
  let start = timeToMinutes(st);
  let end = timeToMinutes(et);
  if (end <= start && et !== "00:00") end += 24 * 60; // overnight
  return end - start;
}

export type CollisionGroup = {
  activities: ActivityBlock[];
  groupStart: number;
  groupEnd: number;
};

/**
 * Groups activities that overlap in time into collision groups.
 */
export function buildCollisionGroups(activities: ActivityBlock[]): CollisionGroup[] {
  if (!activities || activities.length === 0) return [];

  const items = activities.map((act) => {
    const st = act.startTime || act.start || "00:00";
    const et = act.endTime || act.end || "00:00";
    let s = timeToMinutes(st);
    let e = timeToMinutes(et);
    if (e <= s && et !== "00:00") e += 24 * 60;
    return { act, s, e };
  });

  // Sort by start time
  items.sort((a, b) => a.s - b.s);

  const groups: CollisionGroup[] = [];
  if (items.length === 0) return [];

  let currentGroupItems: typeof items = [items[0]];
  let maxEnd = items[0].e;

  for (let i = 1; i < items.length; i++) {
    const item = items[i];
    if (item.s < maxEnd) {
      // Overlaps with current group
      currentGroupItems.push(item);
      maxEnd = Math.max(maxEnd, item.e);
    } else {
      // Flush current group
      groups.push({
        activities: currentGroupItems.map((c) => c.act),
        groupStart: currentGroupItems[0].s,
        groupEnd: maxEnd,
      });
      currentGroupItems = [item];
      maxEnd = item.e;
    }
  }
  
  // Flush the last group
  groups.push({
    activities: currentGroupItems.map((c) => c.act),
    groupStart: currentGroupItems[0].s,
    groupEnd: maxEnd,
  });

  return groups.sort((a, b) => a.groupStart - b.groupStart);
}

/**
 * Returns a Set of IDs involved in any collision.
 */
export function getCollidingIds(activities: ActivityBlock[]): Set<string> {
  const groups = buildCollisionGroups(activities);
  const ids = new Set<string>();
  for (const g of groups) {
    if (g.activities.length > 1) {
      for (const a of g.activities) {
        ids.add(a.id || a.title);
      }
    }
  }
  return ids;
}

/**
 * Standardize time string for display.
 */
export function formatTo12Hour(timeStr: string): string {
  if (!timeStr) return "";
  if (timeStr.includes("AM") || timeStr.includes("PM")) return timeStr;
  
  const [hStr, mStr] = timeStr.split(":");
  let h = parseInt(hStr, 10);
  const m = parseInt(mStr, 10) || 0;
  const ampm = h >= 12 ? "PM" : "AM";
  h = h % 12;
  h = h ? h : 12; 
  const mm = m < 10 ? `0${m}` : m;
  return `${h}:${mm} ${ampm}`;
}
