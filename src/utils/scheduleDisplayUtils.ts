import type { ActivityBlock, DailySchedule } from "../App";

export type ScheduleViewMode = "jplan" | "google_calendar";

export type BlocksForViewOptions = {
  preferDraft?: boolean;
};

function normalizeBlock(block: ActivityBlock): ActivityBlock {
  return {
    ...block,
    start: block.start || block.startTime,
    end: block.end || block.endTime,
    startTime: block.startTime || block.start || "",
    endTime: block.endTime || block.end || "",
  };
}

function materializeActivities(schedule: DailySchedule): ActivityBlock[] {
  return (schedule.activities || []).map((activity) => normalizeBlock({
    ...activity,
    block_type: activity.block_type || "activity",
    type: activity.type || "activity",
  }));
}

function hasDraftTimeline(schedule: DailySchedule): boolean {
  return Boolean(
    (schedule.has_unsaved_draft || schedule.draft_dirty)
    && (schedule.schedule_blocks?.length || 0) > 0
  );
}

function timeToMinutes(timeStr: string | undefined): number {
  if (!timeStr) return Number.POSITIVE_INFINITY;
  const match = timeStr.trim().match(/^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?$/i);
  if (!match) return Number.POSITIVE_INFINITY;
  let hours = Number(match[1]);
  const minutes = Number(match[2] || 0);
  const period = match[3]?.toUpperCase();
  if (period === "PM" && hours !== 12) hours += 12;
  if (period === "AM" && hours === 12) hours = 0;
  return hours * 60 + minutes;
}

function minutesTo12Hour(totalMinutes: number): string {
  const normalized = ((Math.round(totalMinutes) % (24 * 60)) + (24 * 60)) % (24 * 60);
  const hours24 = Math.floor(normalized / 60);
  const minutes = normalized % 60;
  const period = hours24 >= 12 ? "PM" : "AM";
  const hours12 = hours24 % 12 || 12;
  return `${String(hours12).padStart(2, "0")}:${String(minutes).padStart(2, "0")} ${period}`;
}

function normalizedTitle(value: unknown): string {
  return String(value || "").trim().toLowerCase();
}

function isExistingStartRouteBlock(block: ActivityBlock): boolean {
  return Boolean(block.is_start_route || block.block_type === "start_route" || block.type === "start_route");
}

function hasUnresolvedStartRouteConflict(schedule: DailySchedule): boolean {
  return (schedule.route_conflicts || []).some((conflict) => {
    const reasonCode = String(conflict.reason_code || "");
    const hasStartRouteMarker = Boolean(
      conflict.leave_by
      || conflict.first_physical_event
      || conflict.blocker_activity_id
      || conflict.blocker_activity_title
    );
    return reasonCode === "start_route_blocker" || (reasonCode === "fixed_to_fixed_infeasible" && hasStartRouteMarker);
  });
}

function isInvalidStartRouteTarget(schedule: DailySchedule, firstEvent: string): boolean {
  if (!firstEvent) return false;
  const invalidTitles = [
    ...(schedule.unfit_activities || []),
    ...(schedule.unscheduled_activities || []),
  ]
    .map((item) => normalizedTitle(item.title || item.activity_title || item.name))
    .filter(Boolean);
  return invalidTitles.includes(normalizedTitle(firstEvent));
}

function addDisplayOnlyStartRouteRow(schedule: DailySchedule, blocks: ActivityBlock[]): ActivityBlock[] {
  if (blocks.some(isExistingStartRouteBlock) || hasUnresolvedStartRouteConflict(schedule)) {
    return blocks;
  }

  const summary = (schedule.start_route_summary || {}) as Record<string, unknown>;
  const startLocation = String(summary.start_location || "").trim();
  const firstEvent = String(summary.first_physical_event || "").trim();
  const destinationLocation = String(
    summary.first_physical_event_location
    || summary.destination_location
    || summary.to_location
    || firstEvent
  ).trim();
  const leaveBy = String(summary.leave_by || "").trim();
  const duration = Number(summary.travel_duration_minutes || 0);
  if (!startLocation || !destinationLocation || !leaveBy || !duration || isInvalidStartRouteTarget(schedule, firstEvent)) {
    return blocks;
  }

  const explicitEnd = String(summary.first_physical_event_start || "").trim();
  const endTime = explicitEnd || minutesTo12Hour(timeToMinutes(leaveBy) + duration);
  const startRouteBlock: ActivityBlock = normalizeBlock({
    id: "__start_route__",
    type: "start_route",
    block_type: "start_route",
    title: `Leave ${startLocation} by ${leaveBy}`,
    startTime: leaveBy,
    endTime,
    start: leaveBy,
    end: endTime,
    duration_minutes: duration,
    display_label: `Leave ${startLocation} by ${leaveBy} - ${duration} min travel to ${destinationLocation}`,
    location: destinationLocation,
    is_start_route: true,
    display_only: true,
    read_only: true,
    maybe_support_block: true,
  });

  const targetIndex = blocks.findIndex((block) => {
    const title = normalizedTitle(block.title);
    const blockType = String(block.block_type || block.type || "").toLowerCase();
    return blockType === "activity" && firstEvent && title === normalizedTitle(firstEvent);
  });
  if (targetIndex >= 0) {
    return [
      ...blocks.slice(0, targetIndex),
      startRouteBlock,
      ...blocks.slice(targetIndex),
    ];
  }

  const insertAt = blocks.findIndex((block) => timeToMinutes(block.startTime || block.start) >= timeToMinutes(endTime));
  if (insertAt >= 0) {
    return [
      ...blocks.slice(0, insertAt),
      startRouteBlock,
      ...blocks.slice(insertAt),
    ];
  }
  return [...blocks, startRouteBlock];
}

export function getBlocksForView(
  schedule: DailySchedule | null | undefined,
  activeView: ScheduleViewMode | string | undefined = "jplan",
  options: BlocksForViewOptions = {},
): ActivityBlock[] {
  if (!schedule) return [];

  if (activeView === "google_calendar") {
    return (schedule.external_calendar_events || []).map(normalizeBlock);
  }

  let blocks: ActivityBlock[];
  if ((options.preferDraft || hasDraftTimeline(schedule)) && schedule.schedule_blocks?.length) {
    blocks = schedule.schedule_blocks.map(normalizeBlock);
  } else if (schedule.committed_schedule_blocks?.length) {
    blocks = schedule.committed_schedule_blocks.map(normalizeBlock);
  } else if (schedule.schedule_blocks?.length) {
    blocks = schedule.schedule_blocks.map(normalizeBlock);
  } else {
    blocks = materializeActivities(schedule);
  }

  return addDisplayOnlyStartRouteRow(schedule, blocks);
}

export function hasGoogleCalendarLayer(schedule: DailySchedule | null | undefined): boolean {
  return Boolean((schedule?.external_calendar_events?.length || 0) > 0);
}

export function calendarEventKey(event: ActivityBlock): string {
  return String(
    event.original_google_event_id
    || event.google_event_id
    || event.calendar_event_id
    || event.id
  );
}
