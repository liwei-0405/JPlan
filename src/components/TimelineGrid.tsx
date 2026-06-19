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
import { formatActivityDuration } from "../utils/durationUtils";

const PIXELS_PER_MINUTE = 1.8; 
const MIN_BLOCK_HEIGHT = 65;   
const MIN_CLASH_CARD_HEIGHT = 86;
const MIN_CELL_WIDTH = 120;   // Minimum width for each event card in a clash grid

type RouteWarningGroup = {
  activities: ActivityBlock[];
  conflicts: ActivityBlock[];
  start: number;
  activityKeys: Set<string>;
  conflictKeys: Set<string>;
};

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
  /** Allow editable buffer support blocks to use the activity click handler. */
  editableBuffers?: boolean;
};

export function TimelineGrid({
  activities,
  interactive = false,
  onActivityClick,
  showEditIcon = false,
  compact = false,
  editableBuffers = false,
}: TimelineGridProps) {
  const visibleActivities = useMemo(
    () => dedupeSupportBlocks(activities).filter((activity) => getTimelineBlockKind(activity) !== "free_time"),
    [activities],
  );

  const startRouteRows = useMemo(
    () => visibleActivities.filter(isDisplayOnlyStartRouteBlock),
    [visibleActivities],
  );
  const routeConflictRows = useMemo(
    () => visibleActivities.filter(isDisplayOnlyRouteConflictBlock),
    [visibleActivities],
  );
  const scheduledActivities = useMemo(
    () => visibleActivities.filter((activity) => !isDisplayOnlyStartRouteBlock(activity) && !isDisplayOnlyRouteConflictBlock(activity)),
    [visibleActivities],
  );
  const routeWarningMeta = useMemo(
    () => buildRouteWarningGroups(scheduledActivities, routeConflictRows),
    [scheduledActivities, routeConflictRows],
  );
  const collisionActivities = useMemo(
    () => scheduledActivities.filter((activity) => !routeWarningMeta.activityKeys.has(activityIdentity(activity))),
    [scheduledActivities, routeWarningMeta],
  );
  const standaloneRouteConflictRows = useMemo(
    () => routeConflictRows.filter((activity) => (
      !routeWarningMeta.conflictKeys.has(activityIdentity(activity))
      && !routeWarningMeta.conflictKeys.has(routeConflictPairKey(activity))
    )),
    [routeConflictRows, routeWarningMeta],
  );
  const groups = useMemo(() => buildCollisionGroups(collisionActivities), [collisionActivities]);
  const collidingIds = useMemo(() => getCollidingIds(collisionActivities), [collisionActivities]);
  const timelineRows = useMemo(
    () => [
      ...groups.map((group) => ({ kind: "group" as const, start: group.groupStart, group })),
      ...routeWarningMeta.groups.map((group) => ({ kind: "route_warning" as const, start: group.start, group })),
      ...startRouteRows.map((activity) => ({
        kind: "start_route" as const,
        start: timeToMinutes(activity.startTime || activity.start || "00:00"),
        activity,
      })),
      ...standaloneRouteConflictRows.map((activity) => ({
        kind: "route_conflict" as const,
        start: timeToMinutes(activity.startTime || activity.start || "00:00"),
        activity,
      })),
    ].sort((a, b) => a.start - b.start),
    [groups, routeWarningMeta.groups, startRouteRows, standaloneRouteConflictRows],
  );

  if (visibleActivities.length === 0) return null;

  return (
    <div className="space-y-2">
      {timelineRows.map((row, index) => (
        row.kind === "route_warning" ? (
          <RouteWarningGroupRow
            key={`route-warning-${index}`}
            group={row.group}
            interactive={interactive}
            onActivityClick={onActivityClick}
            showEditIcon={showEditIcon}
            compact={compact}
          />
        ) : row.kind === "start_route" || row.kind === "route_conflict" ? (
          <SupportTimelineBlock key={row.activity.id || `${row.kind}-${index}`} activity={row.activity} kind={row.kind} />
        ) : (
          <CollisionGroupRow
            key={index}
            group={row.group}
            collidingIds={collidingIds}
            interactive={interactive}
            onActivityClick={onActivityClick}
            showEditIcon={showEditIcon}
            compact={compact}
            editableBuffers={editableBuffers}
          />
        )
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
  editableBuffers,
}: {
  group: CollisionGroup;
  collidingIds: Set<string>;
  interactive: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
  showEditIcon: boolean;
  compact: boolean;
  editableBuffers: boolean;
}) {
  const isCollision = group.activities.length > 1 || group.activities.some((activity) => activity.isConflict);

  if (!isCollision) {
    const act = group.activities[0];
    const blockKind = getTimelineBlockKind(act);
    
    if (blockKind === "free_time") {
      return null;
    }
    if (blockKind !== "activity") {
      return (
        <SupportTimelineBlock
          activity={act}
          kind={blockKind}
          interactive={editableBuffers && blockKind === "buffer" && interactive}
          onClick={onActivityClick}
        />
      );
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
  const mainActivityCount = group.activities.filter((act) => getTimelineBlockKind(act) === "activity").length;
  const containerHeight = Math.max(
    MIN_BLOCK_HEIGHT,
    groupDuration * PIXELS_PER_MINUTE,
    mainActivityCount > 1 ? MIN_CLASH_CARD_HEIGHT : MIN_BLOCK_HEIGHT
  );
  const conflictLabel = mainActivityCount >= 2
    ? `Time Clash — ${mainActivityCount} overlapping events`
    : mainActivityCount === 0
      ? "Route timing conflict — overlapping travel/buffer blocks"
      : "Route timing conflict — travel/buffer overlaps an activity";
  const shouldAlignActivityClashCards = mainActivityCount >= 2;

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

      {shouldAlignActivityClashCards ? (
        <div
          className="grid gap-3 overflow-x-auto pb-2 -mx-2 px-2 no-scrollbar"
          style={{
            gridTemplateColumns: `repeat(${group.activities.length}, minmax(190px, 1fr))`,
          }}
        >
          {group.activities.map((act) => {
            const blockKind = getTimelineBlockKind(act);

            if (blockKind === "free_time") {
              return null;
            }
            if (blockKind !== "activity") {
              return (
                <SupportTimelineBlock
                  key={act.id || act.title}
                  activity={act}
                  kind={blockKind}
                  interactive={editableBuffers && blockKind === "buffer" && interactive}
                  onClick={onActivityClick}
                />
              );
            }

            return (
              <ActivityCard
                key={act.id || act.title}
                activity={act}
                isClashing={true}
                interactive={interactive}
                onActivityClick={onActivityClick}
                showEditIcon={showEditIcon}
                compact={compact}
                heightPx={undefined}
              />
            );
          })}
        </div>
      ) : (
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
            const blockKind = getTimelineBlockKind(act);
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

            if (blockKind === "free_time") {
              return null;
            }
            if (blockKind !== "activity") {
              return (
                <div key={act.id || act.title} style={{ position: "relative", height: `${containerHeight}px` }}>
                  <div style={{ position: "absolute", top: `${topOffset}px`, width: "100%" }}>
                    <SupportTimelineBlock
                      activity={act}
                      kind={blockKind}
                      interactive={editableBuffers && blockKind === "buffer" && interactive}
                      onClick={onActivityClick}
                    />
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
      )}
    </div>
  );
}

function RouteWarningGroupRow({
  group,
  interactive,
  onActivityClick,
  showEditIcon,
  compact,
}: {
  group: RouteWarningGroup;
  interactive: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
  showEditIcon: boolean;
  compact: boolean;
}) {
  return (
    <div className="rounded-2xl border border-red-200 bg-red-50/55 p-2 shadow-sm">
      <div className="mb-2 flex flex-col gap-1 rounded-xl border border-red-200 bg-white/75 px-3 py-2 text-xs font-semibold text-red-800">
        <span className="flex items-center gap-2">
          <MapPin className="h-3.5 w-3.5 text-red-500" />
          Route warning
        </span>
        {group.conflicts.map((conflict) => (
          <span key={activityIdentity(conflict)} className="font-medium">
            {routeConflictLabel(conflict)}
          </span>
        ))}
      </div>
      <div
        className="grid gap-3"
        style={{
          gridTemplateColumns: `repeat(${group.activities.length}, minmax(${MIN_CELL_WIDTH}px, 1fr))`,
        }}
      >
        {group.activities.map((activity) => (
          <ActivityCard
            key={activityIdentity(activity)}
            activity={activity}
            isClashing={false}
            interactive={interactive}
            onActivityClick={onActivityClick}
            showEditIcon={showEditIcon}
            compact={compact}
            heightPx={undefined}
          />
        ))}
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
      className={`w-full text-left group ${
        isClashing
          ? "bg-white border border-red-200 rounded-2xl shadow-sm hover:shadow-red-100/80"
          : "bg-white border border-slate-100 rounded-2xl shadow-sm hover:shadow-slate-200/80"
      } ${interactive ? "hover:shadow-md cursor-pointer" : "cursor-default"} transition-shadow duration-200`}
      style={{
        padding: "12px 14px",
        height: heightPx ? `${heightPx}px` : "auto",
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
        gap: "8px",
      }}
    >
      {/* Top row: title + badges */}
      <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "8px" }}>
        <h4
          title={activity.title}
          style={{
            fontSize: "13.5px",
            fontWeight: 600,
            lineHeight: 1.3,
            color: isClashing ? "#b91c1c" : "#111827",
            margin: 0,
            display: "-webkit-box",
            WebkitLineClamp: 2,
            WebkitBoxOrient: "vertical",
            overflow: "hidden",
          }}
        >
          {activity.title}
        </h4>
        <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: "4px", flexShrink: 0 }}>
          {isClashing && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "3px",
                background: "#fee2e2",
                color: "#dc2626",
                fontSize: "10px",
                fontWeight: 700,
                padding: "2px 6px",
                borderRadius: "4px",
                letterSpacing: "0.04em",
                whiteSpace: "nowrap",
              }}
            >
              ⚡ Clash
            </span>
          )}
          {(activity.duration || activity.duration_minutes) && (
            <span
              style={{
                background: isClashing ? "#fee2e2" : "#f1f5f9",
                color: isClashing ? "#b91c1c" : "#475569",
                fontSize: "11px",
                fontWeight: 500,
                padding: "2px 7px",
                borderRadius: "4px",
                whiteSpace: "nowrap",
              }}
            >
              {formatActivityDuration(activity)}
            </span>
          )}
          {showEditIcon && (
            <div className="opacity-0 group-hover:opacity-100 transition-opacity">
              <Edit2 size={12} style={{ color: "var(--primary)" }} />
            </div>
          )}
        </div>
      </div>

      {/* Bottom row: time + location */}
      <div style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
        <div style={{ display: "flex", alignItems: "center", gap: "5px" }}>
          <Clock size={12} style={{ flexShrink: 0, color: isClashing ? "#ef4444" : "#94a3b8" }} />
          <span style={{ fontSize: "11.5px", color: isClashing ? "#dc2626" : "#64748b", whiteSpace: "nowrap", fontVariantNumeric: "tabular-nums" }}>
            {st} – {et}
          </span>
        </div>
        {activity.location && (
          <div style={{ display: "flex", alignItems: "flex-start", gap: "5px" }}>
            <MapPin size={12} style={{ flexShrink: 0, marginTop: "2px", color: "#94a3b8" }} />
            <span
              title={activity.location}
              style={{
                fontSize: "11.5px",
                color: "#64748b",
                lineHeight: 1.4,
                display: "-webkit-box",
                WebkitLineClamp: 2,
                WebkitBoxOrient: "vertical",
                overflow: "hidden",
              }}
            >
              {activity.location}
            </span>
          </div>
        )}
      </div>
    </Tag>
  );
}

type TimelineBlockKind = "activity" | "travel" | "buffer" | "free_time" | "start_route" | "route_conflict";

function normalizedBlockText(value: unknown): string {
  return String(value || "").trim().toLowerCase();
}

function activityIdentity(activity: ActivityBlock): string {
  return String(activity.stable_activity_id || activity.id || activity.title || "").trim().toLowerCase();
}

function routeConflictEndpoint(conflict: ActivityBlock, side: "from" | "to"): string {
  const raw = conflict as ActivityBlock & {
    from_activity?: string;
    to_activity?: string;
    from?: string;
    to?: string;
  };
  const direct = side === "from"
    ? raw.from_activity || raw.from
    : raw.to_activity || raw.to;
  if (direct) return String(direct);

  const text = String(conflict.title || conflict.display_label || "").replace(/^route conflict:\s*/i, "");
  const [fromPart, toPart] = text.split("->").map((part) => part.trim());
  if (side === "from") return fromPart || "";
  return (toPart || "").split(":")[0].trim();
}

function routeConflictLabel(conflict: ActivityBlock): string {
  const fromTitle = routeConflictEndpoint(conflict, "from") || "Previous fixed event";
  const toTitle = routeConflictEndpoint(conflict, "to") || "Next fixed event";
  const required = Number(conflict.route_duration_minutes || conflict.duration_minutes || 0);
  const available = Number((conflict as ActivityBlock & { available_gap_minutes?: number }).available_gap_minutes || 0);
  return `${fromTitle} -> ${toTitle}: needs ${required} min, only ${available} min.`;
}

function routeConflictPairKey(conflict: ActivityBlock): string {
  return `${normalizedBlockText(routeConflictEndpoint(conflict, "from"))}->${normalizedBlockText(routeConflictEndpoint(conflict, "to"))}`;
}

function buildRouteWarningGroups(
  activities: ActivityBlock[],
  conflicts: ActivityBlock[],
): { groups: RouteWarningGroup[]; activityKeys: Set<string>; conflictKeys: Set<string> } {
  type MutableGroup = {
    activities: Map<string, ActivityBlock>;
    conflicts: Map<string, ActivityBlock>;
  };

  const titleToActivity = new Map<string, ActivityBlock>();
  for (const activity of activities) {
    titleToActivity.set(normalizedBlockText(activity.title), activity);
  }

  const mutableGroups: MutableGroup[] = [];
  for (const conflict of conflicts) {
    const fromActivity = titleToActivity.get(normalizedBlockText(routeConflictEndpoint(conflict, "from")));
    const toActivity = titleToActivity.get(normalizedBlockText(routeConflictEndpoint(conflict, "to")));
    if (!fromActivity || !toActivity) continue;

    const fromKey = activityIdentity(fromActivity);
    const toKey = activityIdentity(toActivity);
    const matchedIndexes = mutableGroups
      .map((group, index) => group.activities.has(fromKey) || group.activities.has(toKey) ? index : -1)
      .filter((index) => index >= 0);
    const conflictKey = routeConflictPairKey(conflict) || activityIdentity(conflict);

    if (matchedIndexes.length === 0) {
      mutableGroups.push({
        activities: new Map([[fromKey, fromActivity], [toKey, toActivity]]),
        conflicts: new Map([[conflictKey, conflict]]),
      });
      continue;
    }

    const target = mutableGroups[matchedIndexes[0]];
    target.activities.set(fromKey, fromActivity);
    target.activities.set(toKey, toActivity);
    if (!target.conflicts.has(conflictKey)) {
      target.conflicts.set(conflictKey, conflict);
    }

    for (const index of matchedIndexes.slice(1).sort((a, b) => b - a)) {
      const other = mutableGroups[index];
      for (const [key, activity] of other.activities) target.activities.set(key, activity);
      for (const [key, routeConflict] of other.conflicts) target.conflicts.set(key, routeConflict);
      mutableGroups.splice(index, 1);
    }
  }

  const activityKeys = new Set<string>();
  const conflictKeys = new Set<string>();
  const groups = mutableGroups.map((group) => {
    const groupedActivities = [...group.activities.values()]
      .sort((a, b) => timeToMinutes(a.startTime || a.start || "00:00") - timeToMinutes(b.startTime || b.start || "00:00"));
    const groupedConflicts = [...group.conflicts.values()]
      .sort((a, b) => timeToMinutes(a.startTime || a.start || "00:00") - timeToMinutes(b.startTime || b.start || "00:00"));
    for (const activity of groupedActivities) activityKeys.add(activityIdentity(activity));
    for (const conflict of groupedConflicts) {
      conflictKeys.add(activityIdentity(conflict));
      conflictKeys.add(routeConflictPairKey(conflict));
    }
    return {
      activities: groupedActivities,
      conflicts: groupedConflicts,
      start: Math.min(...groupedActivities.map((activity) => timeToMinutes(activity.startTime || activity.start || "00:00"))),
      activityKeys: new Set(groupedActivities.map(activityIdentity)),
      conflictKeys: new Set(groupedConflicts.map(activityIdentity)),
    };
  });

  return { groups, activityKeys, conflictKeys };
}

function getTimelineBlockKind(activity: ActivityBlock): TimelineBlockKind {
  if (isDisplayOnlyRouteConflictBlock(activity)) {
    return "route_conflict";
  }
  if (isDisplayOnlyStartRouteBlock(activity)) {
    return "start_route";
  }

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
  if (kind === "start_route") {
    return `start_route:${activity.id || activity.title || activity.startTime || activity.start}`;
  }
  if (kind === "route_conflict") {
    return `route_conflict:${activity.id || activity.title || activity.startTime || activity.start}`;
  }
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

function isDisplayOnlyStartRouteBlock(activity: ActivityBlock): boolean {
  return Boolean(activity.is_start_route || activity.type === "start_route" || activity.block_type === "start_route");
}

function isDisplayOnlyRouteConflictBlock(activity: ActivityBlock): boolean {
  return Boolean(
    activity.is_route_conflict ||
    activity.reason_code === "fixed_to_fixed_infeasible" ||
    activity.type === "route_conflict" ||
    activity.block_type === "route_conflict",
  );
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

function SupportTimelineBlock({
  activity,
  kind,
  interactive = false,
  onClick,
}: {
  activity: ActivityBlock;
  kind: Exclude<TimelineBlockKind, "activity" | "free_time">;
  interactive?: boolean;
  onClick?: (activity: ActivityBlock) => void;
}) {
  const st = activity.startTime || activity.start;
  const et = activity.endTime || activity.end;
  const duration = activity.duration_minutes || getDurationMinutes(activity);
  const destination = activity.title.replace(/^travel to\s+/i, "").trim();
  const label = activity.display_label || (kind === "start_route"
    ? activity.title
    : kind === "route_conflict"
    ? activity.title
    : kind === "travel"
    ? `${duration} min travel${destination ? ` to ${destination}` : ""}`
    : `${duration} min buffer`);

  const colorClass = kind === "start_route"
    ? "border-amber-200 bg-amber-50/70 text-amber-900"
    : kind === "route_conflict"
    ? "border-red-200 bg-red-50/80 text-red-900"
    : kind === "travel"
    ? "border-indigo-200 bg-indigo-50/70 text-indigo-800"
    : "border-emerald-200 bg-emerald-50/60 text-emerald-800";
  const iconClass = kind === "start_route"
    ? "text-amber-500"
    : kind === "route_conflict"
    ? "text-red-500"
    : kind === "travel"
    ? "text-indigo-500"
    : "text-emerald-500";

  const isTravelOrRoute = kind === "travel" || kind === "start_route" || kind === "route_conflict";

  const bgStyle: React.CSSProperties = kind === "travel" || kind === "start_route"
    ? { background: "linear-gradient(90deg, #eef2ff 0%, #f5f3ff 100%)", borderColor: "#c7d2fe" }
    : kind === "route_conflict"
    ? { background: "#fff1f2", borderColor: "#fecaca" }
    : { background: "linear-gradient(90deg, #f0fdf4 0%, #ecfdf5 100%)", borderColor: "#bbf7d0" };

  const accentColor = kind === "travel" || kind === "start_route"
    ? "#6366f1"
    : kind === "route_conflict"
    ? "#ef4444"
    : "#10b981";

  const Icon = kind === "buffer" ? Clock : MapPin;

  return (
    <div
      role={interactive ? "button" : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={interactive && onClick ? () => onClick(activity) : undefined}
      onKeyDown={interactive && onClick ? (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick(activity);
        }
      } : undefined}
      style={{
        borderRadius: "10px",
        border: "1px solid",
        ...bgStyle,
        padding: "8px 12px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: "12px",
        boxShadow: "0 1px 3px rgba(0,0,0,0.05)",
        cursor: interactive ? "pointer" : "default",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "7px", minWidth: 0, flex: 1 }}>
        <Icon size={13} style={{ flexShrink: 0, color: accentColor }} />
        <span
          style={{
            fontSize: "12.5px",
            fontWeight: 500,
            color: kind === "route_conflict" ? "#b91c1c" : "#374151",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={label}
        >
          {label}
        </span>
      </div>
      <span
        style={{
          fontSize: "11.5px",
          color: "#6b7280",
          whiteSpace: "nowrap",
          flexShrink: 0,
          fontVariantNumeric: "tabular-nums",
        }}
      >
        {st} – {et}
      </span>
    </div>
  );
}
