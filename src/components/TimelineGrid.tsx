import { useMemo } from "react";
import { Clock, MapPin, Edit2 } from "lucide-react";
import type { ActivityBlock } from "../App";
import {
  buildCollisionGroups,
  getCollidingIds,
  timeToMinutes,
  getDurationMinutes,
  type CollisionGroup,
} from "../utils/collisionUtils";

const PIXELS_PER_MINUTE = 1.8; 
const MIN_BLOCK_HEIGHT = 65;   
const MIN_CELL_WIDTH = 120;   // Minimum width for each event card in a clash grid

type TimelineGridProps = {
  activities: ActivityBlock[];
  /** If true, clicking an activity fires onActivityClick */
  interactive?: boolean;
  /** Called when user clicks an activity card */
  onActivityClick?: (activity: ActivityBlock) => void;
  /** If true, show edit icon on hover */
  showEditIcon?: boolean;
  /** Compact mode for entry page preview */
  compact?: boolean;
};

export function TimelineGrid({
  activities,
  interactive = false,
  onActivityClick,
  showEditIcon = false,
  compact = false,
}: TimelineGridProps) {
  const visibleActivities = useMemo(
    () => dedupeSupportBlocks(activities).filter((activity) => getTimelineBlockKind(activity) !== "free_time"),
    [activities],
  );

  const groups = useMemo(() => buildCollisionGroups(visibleActivities), [visibleActivities]);
  const collidingIds = useMemo(() => getCollidingIds(visibleActivities), [visibleActivities]);

  if (visibleActivities.length === 0) return null;

  return (
    <div className="space-y-2">
      {groups.map((group, gi) => (
        <CollisionGroupRow
          key={gi}
          group={group}
          collidingIds={collidingIds}
          interactive={interactive}
          onActivityClick={onActivityClick}
          showEditIcon={showEditIcon}
          compact={compact}
        />
      ))}
    </div>
  );
}

function CollisionGroupRow({
  group,
  collidingIds,
  interactive,
  onActivityClick,
  showEditIcon,
  compact,
}: {
  group: CollisionGroup;
  collidingIds: Set<string>;
  interactive: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
  showEditIcon: boolean;
  compact: boolean;
}) {
  const isCollision = group.activities.length > 1 || group.activities.some((activity) => activity.isConflict);

  if (!isCollision) {
    const act = group.activities[0];
    const blockKind = getTimelineBlockKind(act);
    
    if (blockKind === "free_time") {
      return null;
    }
    if (blockKind !== "activity") {
      return <SupportTimelineBlock activity={act} kind={blockKind} />;
    }
    return (
      <ActivityCard
        activity={act}
        isClashing={Boolean(act.isConflict)}
        interactive={interactive}
        onActivityClick={onActivityClick}
        showEditIcon={showEditIcon}
        compact={compact}
        heightPx={undefined}
      />
    );
  }

  const groupDuration = group.groupEnd - group.groupStart;
  const containerHeight = Math.max(
    MIN_BLOCK_HEIGHT,
    groupDuration * PIXELS_PER_MINUTE
  );
  const mainActivityCount = group.activities.filter((act) => getTimelineBlockKind(act) === "activity").length;
  const conflictLabel = mainActivityCount >= 2
    ? `Time Clash — ${mainActivityCount} overlapping events`
    : mainActivityCount === 0
      ? "Route timing conflict — overlapping travel/buffer blocks"
      : "Route timing conflict — travel/buffer overlaps an activity";

  return (
    <div className="relative group/group-row">
      <div
        className="flex items-center gap-2 mb-3 px-3 py-1.5 rounded-full w-fit animate-in fade-in slide-in-from-left-2 duration-500"
        style={{
          background: "rgba(254, 242, 242, 0.8)",
          backdropFilter: "blur(8px)",
          border: "1px solid rgba(252, 165, 165, 0.5)",
          boxShadow: "0 4px 12px rgba(220, 38, 38, 0.08)",
        }}
      >
        <span className="relative flex h-2 w-2">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-red-400 opacity-75"></span>
          <span className="relative inline-flex rounded-full h-2 w-2 bg-red-600"></span>
        </span>
        <span style={{ fontSize: "12px", fontWeight: 700, color: "#dc2626" }}>
          ⚡ {conflictLabel}
        </span>
      </div>

      <div
        className="overflow-x-auto pb-2 -mx-2 px-2 no-scrollbar"
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${group.activities.length}, minmax(${MIN_CELL_WIDTH}px, 1fr))`,
          gap: "12px",
          minHeight: `${containerHeight}px`,
        }}
      >
        {group.activities.map((act) => {
          const dur = getDurationMinutes(act);
          const blockHeight = Math.max(
            MIN_BLOCK_HEIGHT,
            (dur / groupDuration) * containerHeight
          );

          const stStr = act.startTime || act.start || "00:00";
          const actStart = timeToMinutes(stStr);
          let normalizedStart = actStart - group.groupStart;
          if (normalizedStart < 0) normalizedStart += 24 * 60;
          const topOffset = (normalizedStart / groupDuration) * containerHeight;

          const blockKind = getTimelineBlockKind(act);

          if (blockKind === "free_time") {
            return null;
          }
          if (blockKind !== "activity") {
            return (
              <div key={act.id || act.title} style={{ position: "relative", height: `${containerHeight}px` }}>
                <div style={{ position: "absolute", top: `${topOffset}px`, width: "100%" }}>
                  <SupportTimelineBlock activity={act} kind={blockKind} />
                </div>
              </div>
            );
          }

          return (
            <div key={act.id || act.title} style={{ position: "relative", height: `${containerHeight}px` }}>
              <div style={{ position: "absolute", top: `${topOffset}px`, width: "100%", height: `${blockHeight}px` }}>
                <ActivityCard
                  activity={act}
                  isClashing={true}
                  interactive={interactive}
                  onActivityClick={onActivityClick}
                  showEditIcon={showEditIcon}
                  compact={compact}
                  heightPx={blockHeight}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function ActivityCard({
  activity,
  isClashing,
  interactive,
  onActivityClick,
  showEditIcon,
  compact,
  heightPx,
}: {
  activity: ActivityBlock;
  isClashing: boolean;
  interactive: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
  showEditIcon: boolean;
  compact: boolean;
  heightPx: number | undefined;
}) {
  const Tag = interactive ? "button" : "div";
  const st = activity.startTime || activity.start;
  const et = activity.endTime || activity.end;

  if (compact) {
    return (
      <Tag
        onClick={interactive && onActivityClick ? () => onActivityClick(activity) : undefined}
        style={{
          width: "100%",
          textAlign: "left",
          height: heightPx ? `${heightPx}px` : "auto",
          overflow: "hidden",
          borderRadius: "12px",
          padding: "10px 12px",
          border: isClashing ? "2px solid #f87171" : "1px solid var(--border, #e5e7eb)",
          backgroundColor: isClashing ? "#fef2f2" : "hsl(var(--secondary) / 0.3)",
          cursor: interactive ? "pointer" : "default",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          transition: "all 0.15s ease",
        }}
      >
        <h4 style={{ fontSize: "13px", fontWeight: 600, marginBottom: "4px", lineHeight: "1.2" }}>
          {activity.title}
        </h4>
        <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "11px", color: "#6b7280" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "3px" }}>
            <Clock size={11} />
            <span>{st}</span>
          </div>
          {activity.location && (
            <div style={{ display: "flex", alignItems: "center", gap: "3px" }}>
              <MapPin size={11} />
              <span>{activity.location}</span>
            </div>
          )}
        </div>
      </Tag>
    );
  }

  return (
    <Tag
      onClick={interactive && onActivityClick ? () => onActivityClick(activity) : undefined}
      className={`w-full bg-card rounded-2xl shadow-sm transition-all text-left group ${
        isClashing
          ? "border-2 border-red-400 hover:border-red-500 shadow-red-100"
          : "border border-border hover:border-primary/30"
      } ${interactive ? "hover:shadow-md cursor-pointer" : "cursor-default"}`}
      style={{
        padding: compact ? "12px" : "20px",
        height: heightPx ? `${heightPx}px` : "auto",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        justifyContent: "center",
      }}
    >
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <h4 style={{ fontSize: "15px" }}>{activity.title}</h4>
          {isClashing && (
            <span
              style={{
                fontSize: "10px",
                padding: "2px 7px",
                borderRadius: "9999px",
                backgroundColor: "#fef2f2",
                color: "#dc2626",
                fontWeight: 700,
                border: "1px solid #fca5a5",
                whiteSpace: "nowrap",
              }}
            >
              ⚡ Clash
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {activity.duration && (
            <span
              className={`text-sm px-3 py-1 rounded-full ${
                isClashing ? "bg-red-50 text-red-600" : "bg-secondary text-secondary-foreground"
              }`}
            >
              {activity.duration}
            </span>
          )}
          {showEditIcon && (
            <div className="opacity-0 group-hover:opacity-100 transition-opacity">
              <Edit2 className="h-4 w-4 text-primary" />
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3 text-sm text-muted-foreground flex-wrap">
        <div
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-full ${
            isClashing ? "bg-red-50 text-red-700" : "bg-muted/50"
          }`}
        >
          <Clock className="h-4 w-4" />
          <span>
            {st} – {et}
          </span>
        </div>

        {activity.location && (
          <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-muted/50">
            <MapPin className="h-4 w-4" />
            <span>{activity.location}</span>
          </div>
        )}
      </div>
    </Tag>
  );
}

type TimelineBlockKind = "activity" | "travel" | "buffer" | "free_time";

function normalizedBlockText(value: unknown): string {
  return String(value || "").trim().toLowerCase();
}

function getTimelineBlockKind(activity: ActivityBlock): TimelineBlockKind {
  const title = normalizedBlockText(activity.title);
  const blockType = normalizedBlockText(activity.block_type || activity.type);
  const category = normalizedBlockText((activity as ActivityBlock & { category?: string; block_category?: string }).category || (activity as ActivityBlock & { category?: string; block_category?: string }).block_category);

  if (
    title === "free time" ||
    blockType === "idle" ||
    blockType === "free_time" ||
    category === "idle" ||
    category === "free_time"
  ) {
    return "free_time";
  }

  if (
    title.startsWith("travel to") ||
    blockType === "travel" ||
    blockType === "transition" ||
    category === "travel" ||
    category === "transition"
  ) {
    return "travel";
  }

  if (
    title.includes("prep / buffer") ||
    title.includes("buffer") ||
    blockType === "buffer" ||
    blockType === "prep" ||
    category === "buffer" ||
    category === "prep"
  ) {
    return "buffer";
  }

  return "activity";
}

function dedupeSupportBlocks(activities: ActivityBlock[]): ActivityBlock[] {
  const result: ActivityBlock[] = [];
  const supportIndexByKey = new Map<string, number>();

  for (const activity of activities || []) {
    const kind = getTimelineBlockKind(activity);
    const key = getSupportDedupeKey(activity, kind);
    if (!key) {
      result.push(activity);
      continue;
    }

    const existingIndex = supportIndexByKey.get(key);
    if (existingIndex === undefined) {
      supportIndexByKey.set(key, result.length);
      result.push(activity);
      continue;
    }

    if (shouldPreferSupportBlock(activity, result[existingIndex])) {
      result[existingIndex] = activity;
    }
  }

  return result;
}

function getSupportDedupeKey(activity: ActivityBlock, kind: TimelineBlockKind): string | null {
  if (kind === "travel") {
    const destination = normalizedTravelDestination(activity);
    const end = normalizedBlockText(activity.endTime || activity.end);
    if (!destination || !end) return null;
    return `travel:${destination}:${end}`;
  }
  if (kind === "buffer") {
    const start = normalizedBlockText(activity.startTime || activity.start);
    const end = normalizedBlockText(activity.endTime || activity.end);
    if (!start || !end) return null;
    return `buffer:${start}:${end}`;
  }
  return null;
}

function normalizedTravelDestination(activity: ActivityBlock): string {
  return normalizedBlockText(
    activity.location ||
    activity.title.replace(/^travel to\s+/i, ""),
  );
}

function shouldPreferSupportBlock(candidate: ActivityBlock, existing: ActivityBlock): boolean {
  const score = (activity: ActivityBlock) => {
    const source = normalizedBlockText(activity.travel_estimate_source);
    const status = normalizedBlockText(activity.travel_validation_status);
    const start = timeToMinutes(activity.startTime || activity.start || "00:00");
    const elapsed = getDurationMinutes(activity);
    const declared = Number(activity.duration_minutes || activity.route_duration_minutes || elapsed);
    let value = 0;
    if (source === "routing_service") value += 100;
    if (status === "validated") value += 50;
    if (declared === elapsed) value += 25;
    value += Math.max(0, elapsed);
    value -= start / 10000;
    return value;
  };

  return score(candidate) > score(existing);
}

function SupportTimelineBlock({ activity, kind }: { activity: ActivityBlock; kind: Exclude<TimelineBlockKind, "activity" | "free_time"> }) {
  const st = activity.startTime || activity.start;
  const et = activity.endTime || activity.end;
  const duration = activity.duration_minutes || getDurationMinutes(activity);
  const destination = activity.title.replace(/^travel to\s+/i, "").trim();
  const label = activity.display_label || (kind === "travel"
    ? `${duration} min travel${destination ? ` to ${destination}` : ""}`
    : `${duration} min buffer`);

  const colorClass = kind === "travel"
    ? "border-indigo-200 bg-indigo-50/70 text-indigo-800"
    : "border-emerald-200 bg-emerald-50/60 text-emerald-800";
  const iconClass = kind === "travel" ? "text-indigo-500" : "text-emerald-500";

  return (
    <div className={`rounded-xl border px-3 py-2 shadow-sm ${colorClass}`}>
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-xs font-semibold">
          {kind === "travel" ? <MapPin className={`h-3.5 w-3.5 ${iconClass}`} /> : <Clock className={`h-3.5 w-3.5 ${iconClass}`} />}
          {label}
        </span>
        <span className="text-xs font-medium opacity-75 whitespace-nowrap">
          {st} – {et}
        </span>
      </div>
    </div>
  );
}
