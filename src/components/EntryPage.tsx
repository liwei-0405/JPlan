import { Button } from "./ui/button";
import { TopNav } from "./TopNav";
import { CalendarWidget } from "./CalendarWidget";
import { Sparkles, Calendar as CalendarIcon } from "lucide-react";
import type { ActivityBlock, DailySchedule } from "../App";
import { useState } from "react";
import { TimelineGrid } from "./TimelineGrid";
import { getBlocksForView } from "../utils/scheduleDisplayUtils";
import { jplanLogoUrl } from "../brand";

type EntryPageProps = {
  onStartPlanning: (date: Date) => void;
  onViewSchedule: (schedule: DailySchedule) => void;
  onReplanToday: () => void;
  onSettingsClick: () => void;
  todaySchedule: DailySchedule | null;
  scheduleHistory: DailySchedule[];
  onSyncComplete?: () => void;
};

type TravelMetric = {
  minutes: number;
  source: "timeline" | "route_total" | "none";
};

function parseISODateParts(date: string): { year: number; month: number } | null {
  const [year, month] = String(date || "").split("-").map(Number);
  if (!year || !month) return null;
  return { year, month: month - 1 };
}

function countPlansInSelectedMonth(schedules: DailySchedule[], selectedDate: Date): number {
  return schedules.filter((schedule) => {
    const parts = parseISODateParts(schedule.date);
    return parts?.year === selectedDate.getFullYear() && parts.month === selectedDate.getMonth();
  }).length;
}

function normalizedMetricText(value: unknown): string {
  return String(value || "").trim().toLowerCase();
}

function blockTypeText(block: ActivityBlock): string {
  return normalizedMetricText(block.block_type || block.type);
}

function blockCategoryText(block: ActivityBlock): string {
  return normalizedMetricText((block as ActivityBlock & { block_category?: string }).block_category || block.category);
}

function isTravelMetricBlock(block: ActivityBlock): boolean {
  const title = normalizedMetricText(block.title || block.display_label);
  const type = blockTypeText(block);
  const category = blockCategoryText(block);
  return Boolean(
    title.startsWith("travel to")
    || type === "travel"
    || type === "transition"
    || type === "start_route"
    || category === "travel"
    || category === "transition"
    || block.is_start_route
  );
}

function isSupportMetricBlock(block: ActivityBlock): boolean {
  const title = normalizedMetricText(block.title || block.display_label);
  const type = blockTypeText(block);
  const category = blockCategoryText(block);
  return Boolean(
    block.maybe_support_block
    || block.display_only
    || block.is_start_route
    || block.is_route_conflict
    || title === "free time"
    || title.startsWith("travel to")
    || title.includes("prep / buffer")
    || title.includes("buffer")
    || title.includes("route conflict")
    || ["travel", "transition", "buffer", "idle", "start_route", "route_conflict", "prep_buffer", "free_time"].includes(type)
    || ["travel", "transition", "buffer", "idle", "route_conflict", "prep", "free_time"].includes(category)
  );
}

function countRealEventBlocks(blocks: ActivityBlock[]): number {
  return blocks.filter((block) => !isSupportMetricBlock(block)).length;
}

function minutesBetweenBlockTimes(block: ActivityBlock): number {
  const start = block.startTime || block.start;
  const end = block.endTime || block.end;
  if (!start || !end) return 0;

  const toMinutes = (value: string): number => {
    const text = value.trim().toUpperCase();
    if (!text.includes(" ")) {
      const [hour, minute] = text.split(":").map(Number);
      return (hour || 0) * 60 + (minute || 0);
    }
    const [time, period] = text.split(" ");
    let [hour, minute] = time.split(":").map(Number);
    if (period === "PM" && hour !== 12) hour += 12;
    if (period === "AM" && hour === 12) hour = 0;
    return (hour || 0) * 60 + (minute || 0);
  };

  const startMinutes = toMinutes(start);
  let endMinutes = toMinutes(end);
  if (endMinutes <= startMinutes && end !== "00:00") endMinutes += 24 * 60;
  return Math.max(0, endMinutes - startMinutes);
}

function getTravelMetric(schedule: DailySchedule | undefined, blocks: ActivityBlock[]): TravelMetric {
  const timelineMinutes = blocks
    .filter(isTravelMetricBlock)
    .reduce((total, block) => total + Number(
      block.route_duration_minutes
      || block.duration_minutes
      || minutesBetweenBlockTimes(block)
      || 0
    ), 0);

  if (timelineMinutes > 0) {
    return { minutes: timelineMinutes, source: "timeline" };
  }

  const routeTotal = Number(schedule?.route_total_after ?? 0);
  if (routeTotal > 0) {
    return { minutes: routeTotal, source: "route_total" };
  }

  return { minutes: 0, source: "none" };
}

function formatMinutes(minutes: number): string {
  if (minutes <= 0) return "0 min";
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  if (!hours) return `${mins} min`;
  if (!mins) return `${hours} hr`;
  return `${hours} hr ${mins} min`;
}

function derivePlanStatus(schedule: DailySchedule | undefined, realEventCount: number): string {
  if (!schedule || realEventCount === 0) return "Empty";

  const statusText = normalizedMetricText([
    schedule.travel_validation_status,
    schedule.preview_status,
    schedule.schedule_status,
    schedule.status,
  ].filter(Boolean).join(" "));

  if (schedule.travel_validation_status === "pending_locations" || schedule.needs_travel_validation) {
    return "Needs locations";
  }
  if (schedule.needs_reschedule || schedule.draft_dirty) {
    return "Not re-optimized";
  }
  if ((schedule.route_conflicts || []).length > 0 || statusText.includes("route_conflict") || statusText.includes("route conflict")) {
    return "Route warning";
  }
  if (
    (schedule.unfit_activities || []).length > 0
    || (schedule.blocked_activities || []).length > 0
    || (schedule.optional_skipped || []).length > 0
    || statusText.includes("partial")
    || statusText.includes("unfit")
  ) {
    return "Partially fit";
  }

  return "Optimized";
}

export function EntryPage({
  onStartPlanning,
  onViewSchedule,
  onReplanToday,
  onSettingsClick,
  todaySchedule,
  scheduleHistory,
  onSyncComplete
}: EntryPageProps) {
  const [selectedDate, setSelectedDate] = useState<Date>(new Date());

  const scheduleDates = scheduleHistory.map(s => s.date);

  const handleDateSelect = (date: Date) => {
    setSelectedDate(date);
  };

  const hasTodayPlan = todaySchedule !== null;

  // Get the schedule for the selected date
  const formatDateToISO = (date: Date) => {
    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  };

  const selectedDateStr = formatDateToISO(selectedDate);
  const selectedSchedule = scheduleHistory.find(s => s.date === selectedDateStr);
  const isToday = selectedDateStr === formatDateToISO(new Date());
  const hasSelectedPlan = selectedSchedule !== undefined;
  const selectedBlocks = getBlocksForView(selectedSchedule, "jplan");
  const plansThisMonth = countPlansInSelectedMonth(scheduleHistory, selectedDate);
  const selectedEventCount = countRealEventBlocks(selectedBlocks);
  const travelMetric = getTravelMetric(selectedSchedule, selectedBlocks);
  const statusLabel = derivePlanStatus(selectedSchedule, selectedEventCount);
  const googleEventCount = selectedSchedule?.external_calendar_events?.length || 0;

  // Check if selected date is in the past
  const isPastDate = selectedDate < new Date(new Date().setHours(0, 0, 0, 0));

  return (
    <div className="jplan-entry-page min-h-screen bg-background">
      <TopNav 
        onSettingsClick={onSettingsClick} 
        onSyncComplete={onSyncComplete} 
        syncDate={selectedDateStr}
      />

      <div className="entry-content">
        {/* Header */}
        <div className="entry-header">
          <div className="flex items-center gap-2 mb-2">
            <img src={jplanLogoUrl} alt="JPlan logo" className="brand-logo-entry rounded-lg object-cover shadow-sm" />
            <h1>Welcome to JPlan</h1>
          </div>
          <p className="text-muted-foreground">
            Your feasibility-aware daily planning assistant
          </p>
        </div>

        <div className="entry-hero-grid">
          {/* Calendar */}
          <div className="min-w-0">
            <CalendarWidget
              scheduleDates={scheduleDates}
              onDateSelect={handleDateSelect}
              selectedDate={selectedDate}
            />
          </div>

          {/* Selected Date's Plan Preview */}
          <div className="entry-preview-shell">
            <div
              onClick={
                hasSelectedPlan 
                  ? () => selectedSchedule && onViewSchedule(selectedSchedule) 
                  : (!isPastDate ? () => onStartPlanning(selectedDate) : undefined)
              }
              role={hasSelectedPlan || !isPastDate ? "button" : undefined}
              tabIndex={hasSelectedPlan || !isPastDate ? 0 : undefined}
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && (hasSelectedPlan || !isPastDate)) {
                  e.preventDefault();
                  hasSelectedPlan ? selectedSchedule && onViewSchedule(selectedSchedule) : onStartPlanning(selectedDate);
                }
              }}
              className={`bg-card rounded-2xl border border-border p-4 shadow-sm h-full flex flex-col w-full text-left transition-all sm:p-6 ${
                hasSelectedPlan || !isPastDate 
                  ? "hover:shadow-md hover:border-primary/30 cursor-pointer group" 
                  : "cursor-default"
              }`}
            >
              <div className="flex items-center gap-2 mb-4 shrink-0">
                <CalendarIcon className="h-5 w-5 text-primary" />
                <h3 className="font-semibold">{isToday ? "Today's Plan" : `Plan for ${selectedDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`}</h3>
                <div className={`ml-auto opacity-0 ${hasSelectedPlan || !isPastDate ? "group-hover:opacity-100" : ""} transition-opacity text-xs text-muted-foreground`}>
                  {hasSelectedPlan ? 'Click to view' : (!isPastDate ? 'Click to plan' : '')}
                </div>
              </div>

              {hasSelectedPlan ? (
                <div className="flex-1 overflow-y-auto pr-2 mb-4 -mr-2 custom-scrollbar">
                  <TimelineGrid
                    activities={selectedBlocks}
                    compact={true}
                  />
                </div>
              ) : (
                <div className="flex-1 flex items-center justify-center">
                  <div className="text-center py-8">
                    <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center mx-auto mb-3">
                      <CalendarIcon className="h-8 w-8 text-muted-foreground" />
                    </div>
                    <p className="text-muted-foreground mb-2">
                      No schedule for {isToday ? 'today' : 'this date'}.
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {isPastDate ? "This date is in the past." : "Start planning to organize your day!"}
                    </p>
                  </div>
                </div>
              )}

              {/* Action Buttons */}
              <div className="space-y-2.5" onClick={(e) => e.stopPropagation()}>
                {!hasSelectedPlan ? (
                  !isPastDate ? (
                    <Button
                      onClick={() => onStartPlanning(selectedDate)}
                      className="w-full rounded-xl shadow-sm"
                      size="lg"
                    >
                      <Sparkles className="mr-2 h-4 w-4" />
                      Start Planning
                    </Button>
                  ) : null
                ) : (
                  <>
                    <Button
                      onClick={() => selectedSchedule && onViewSchedule(selectedSchedule)}
                      className="w-full rounded-xl shadow-sm"
                      size="lg"
                    >
                      View Full Schedule
                    </Button>
                    {(!isPastDate) && (
                      <Button
                        onClick={() => onStartPlanning(selectedDate)}
                        variant="outline"
                        className="w-full rounded-xl gap-2"
                        size="lg"
                      >
                        <Sparkles className="h-4 w-4" />
                        Replan with Assistant
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Planning Metrics */}
        <div className="entry-metrics-grid">
          <div className="min-w-0 min-h-[104px] bg-gradient-to-br from-[#ffd4e5]/20 to-[#ffd4e5]/5 rounded-xl p-3 border border-[#ffd4e5]/30 flex flex-col justify-between overflow-hidden sm:min-h-[112px] sm:p-4">
            <p className="text-sm text-muted-foreground">Plans This Month</p>
            <p className="text-xl leading-tight break-words sm:text-2xl">{plansThisMonth}</p>
            <p className="text-xs leading-snug text-muted-foreground">
              saved plans in {selectedDate.toLocaleDateString("en-US", { month: "short" })}
            </p>
          </div>
          <div className="min-w-0 min-h-[104px] bg-gradient-to-br from-[#d4e5ff]/20 to-[#d4e5ff]/5 rounded-xl p-3 border border-[#d4e5ff]/30 flex flex-col justify-between overflow-hidden sm:min-h-[112px] sm:p-4">
            <p className="text-sm text-muted-foreground">Selected Day Events</p>
            <p className="text-xl leading-tight break-words sm:text-2xl">{selectedEventCount}</p>
            <p className="text-xs leading-snug text-muted-foreground">
              JPlan activities only
            </p>
          </div>
          <div className="min-w-0 min-h-[104px] bg-gradient-to-br from-[#dff7e8]/20 to-[#dff7e8]/5 rounded-xl p-3 border border-[#dff7e8]/30 flex flex-col justify-between overflow-hidden sm:min-h-[112px] sm:p-4">
            <p className="text-sm text-muted-foreground">Travel Time</p>
            <p className="text-xl leading-tight break-words sm:text-2xl">{formatMinutes(travelMetric.minutes)}</p>
            <p className="text-xs leading-snug text-muted-foreground">
              {travelMetric.source === "route_total" ? "from route total" : "from timeline travel blocks"}
            </p>
          </div>
          <div className="min-w-0 min-h-[104px] bg-gradient-to-br from-[#fff9d4]/20 to-[#fff9d4]/5 rounded-xl p-3 border border-[#fff9d4]/30 flex flex-col justify-between overflow-hidden sm:min-h-[112px] sm:p-4">
            <p className="text-sm text-muted-foreground">Plan Status</p>
            <p className="text-xl leading-tight break-words sm:text-2xl">{statusLabel}</p>
            <p className="text-xs leading-snug text-muted-foreground">
              {googleEventCount > 0 ? `${googleEventCount} Google events stored separately` : "selected date summary"}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
