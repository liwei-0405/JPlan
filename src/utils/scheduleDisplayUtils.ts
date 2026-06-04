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

export function getBlocksForView(
  schedule: DailySchedule | null | undefined,
  activeView: ScheduleViewMode | string | undefined = "jplan",
  options: BlocksForViewOptions = {},
): ActivityBlock[] {
  if (!schedule) return [];

  if (activeView === "google_calendar") {
    return (schedule.external_calendar_events || []).map(normalizeBlock);
  }

  if ((options.preferDraft || hasDraftTimeline(schedule)) && schedule.schedule_blocks?.length) {
    return schedule.schedule_blocks.map(normalizeBlock);
  }

  if (schedule.committed_schedule_blocks?.length) {
    return schedule.committed_schedule_blocks.map(normalizeBlock);
  }

  if (schedule.schedule_blocks?.length) {
    return schedule.schedule_blocks.map(normalizeBlock);
  }

  return materializeActivities(schedule);
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
