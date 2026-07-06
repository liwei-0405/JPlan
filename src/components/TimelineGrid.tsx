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

type ActivitySupportBundle = {
  activity: ActivityBlock;
  before: ActivityBlock[];
  after: ActivityBlock[];
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
    () => {
      const routeWarningFiltered = scheduledActivities.filter((activity) => !routeWarningMeta.activityKeys.has(activityIdentity(activity)));
      return routeWarningFiltered.filter((activity) => {
        const kind = getTimelineBlockKind(activity);
        return kind === "activity" || !isBoundarySupportChainBlock(activity, scheduledActivities);
      });
    },
    [scheduledActivities, routeWarningMeta],
  );
  const boundarySupportRows = useMemo(
    () => scheduledActivities.filter((activity) => (
      !routeWarningMeta.activityKeys.has(activityIdentity(activity))
      && getTimelineBlockKind(activity) !== "activity"
      && isBoundarySupportChainBlock(activity, scheduledActivities)
    )),
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
      ...boundarySupportRows.map((activity) => ({
        kind: "support" as const,
        start: timeToMinutes(activity.startTime || activity.start || "00:00"),
        activity,
      })),
    ].sort((a, b) => a.start - b.start),
    [groups, routeWarningMeta.groups, startRouteRows, standaloneRouteConflictRows, boundarySupportRows],
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
        ) : row.kind === "start_route" || row.kind === "route_conflict" || row.kind === "support" ? (
          <SupportTimelineBlock
            key={row.activity.id || `${row.kind}-${index}`}
            activity={row.activity}
            kind={row.kind === "support" ? supportRenderKind(row.activity) : row.kind}
          />
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
  const visibleGroupBlocks = group.activities.filter((activity) => getTimelineBlockKind(activity) !== "free_time");
  const activityBlocks = visibleGroupBlocks
    .filter((activity) => getTimelineBlockKind(activity) === "activity")
    .sort((a, b) => timeToMinutes(a.startTime || a.start || "00:00") - timeToMinutes(b.startTime || b.start || "00:00"));
  const supportBlocks = visibleGroupBlocks
    .filter((activity) => getTimelineBlockKind(activity) !== "activity")
    .sort((a, b) => timeToMinutes(a.startTime || a.start || "00:00") - timeToMinutes(b.startTime || b.start || "00:00"));
  const supportBundles = useMemo(
    () => buildSupportBundles(activityBlocks, supportBlocks),
    [activityBlocks, supportBlocks],
  );
  const isCollision = group.activities.length > 1 || (supportBlocks.length > 0 && group.activities.some((activity) => activity.isConflict));

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
        isClashing={false}
        interactive={interactive}
        onActivityClick={onActivityClick}
        showEditIcon={showEditIcon}
        compact={compact}
        heightPx={undefined}
      />
    );
  }

  const groupDuration = group.groupEnd - group.groupStart;
  const mainActivityCount = activityBlocks.length;
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

      {activityBlocks.length > 0 ? (
        <>
          <div
            className="grid gap-3 overflow-x-auto pb-2 -mx-2 px-2 no-scrollbar"
            style={{
              gridTemplateColumns: shouldAlignActivityClashCards
                ? `repeat(${activityBlocks.length}, minmax(190px, 1fr))`
                : `repeat(${activityBlocks.length}, minmax(${MIN_CELL_WIDTH}px, 1fr))`,
              minHeight: shouldAlignActivityClashCards ? undefined : `${containerHeight}px`,
            }}
          >
            {supportBundles.bundles.map((bundle) => (
              <ActivityBundleCard
                key={bundle.activity.id || bundle.activity.title}
                bundle={bundle}
                isClashing={true}
                interactive={interactive}
                onActivityClick={onActivityClick}
                showEditIcon={showEditIcon}
                compact={compact}
                editableBuffers={editableBuffers}
              />
            ))}
          </div>
          {supportBundles.unassigned.length > 0 && (
            <SupportStack
              blocks={supportBundles.unassigned}
              interactive={interactive}
              editableBuffers={editableBuffers}
              onActivityClick={onActivityClick}
            />
          )}
        </>
      ) : (
        <SupportStack
          blocks={supportBlocks}
          interactive={interactive}
          editableBuffers={editableBuffers}
          onActivityClick={onActivityClick}
        />
      )}
    </div>
  );
}

function ActivityBundleCard({
  bundle,
  isClashing,
  interactive,
  onActivityClick,
  showEditIcon,
  compact,
  editableBuffers,
}: {
  bundle: ActivitySupportBundle;
  isClashing: boolean;
  interactive: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
  showEditIcon: boolean;
  compact: boolean;
  editableBuffers: boolean;
}) {
  return (
    <div className="min-w-0 space-y-1.5 rounded-2xl border border-slate-200 bg-slate-50/65 p-2">
      {bundle.before.map((support) => {
        const kind = getTimelineBlockKind(support);
        if (kind === "activity" || kind === "free_time") return null;
        return (
          <SupportTimelineBlock
            key={support.id || `${support.title}-${support.startTime || support.start}`}
            activity={support}
            kind={kind}
            compact
            interactive={editableBuffers && kind === "buffer" && interactive}
            onClick={onActivityClick}
          />
        );
      })}
      <ActivityCard
        activity={bundle.activity}
        isClashing={isClashing}
        interactive={interactive}
        onActivityClick={onActivityClick}
        showEditIcon={showEditIcon}
        compact={compact}
        heightPx={undefined}
      />
      {bundle.after.map((support) => {
        const kind = getTimelineBlockKind(support);
        if (kind === "activity" || kind === "free_time") return null;
        return (
          <SupportTimelineBlock
            key={support.id || `${support.title}-${support.startTime || support.start}`}
            activity={support}
            kind={kind}
            compact
            interactive={editableBuffers && kind === "buffer" && interactive}
            onClick={onActivityClick}
          />
        );
      })}
    </div>
  );
}

function SupportStack({
  blocks,
  interactive,
  editableBuffers,
  onActivityClick,
}: {
  blocks: ActivityBlock[];
  interactive: boolean;
  editableBuffers: boolean;
  onActivityClick?: (activity: ActivityBlock) => void;
}) {
  if (!blocks.length) return null;

  return (
    <div className="mt-2 space-y-1.5 rounded-xl border border-slate-200 bg-slate-50/70 p-2">
      {blocks.map((act) => {
        const blockKind = getTimelineBlockKind(act);
        if (blockKind === "activity" || blockKind === "free_time") return null;
        return (
          <SupportTimelineBlock
            key={act.id || `${act.title}-${act.startTime || act.start}`}
            activity={act}
            kind={blockKind}
            compact
            interactive={editableBuffers && blockKind === "buffer" && interactive}
            onClick={onActivityClick}
          />
        );
      })}
    </div>
  );
}

function buildSupportBundles(
  activities: ActivityBlock[],
  supports: ActivityBlock[],
): { bundles: ActivitySupportBundle[]; unassigned: ActivityBlock[] } {
  const bundles = activities.map((activity) => ({
    activity,
    before: [] as ActivityBlock[],
    after: [] as ActivityBlock[],
  }));
  const unassigned: ActivityBlock[] = [];
  const attachWindowMinutes = 45;

  for (const support of supports) {
    const supportBounds = blockBounds(support);
    const relatedTarget = relatedSupportTarget(support, bundles);
    if (relatedTarget) {
      bundles[relatedTarget.index][relatedTarget.side].push(support);
      continue;
    }

    let best:
      | { index: number; side: "before" | "after"; score: number }
      | null = null;
    let bestOverlap:
      | { index: number; side: "before" | "after"; score: number }
      | null = null;

    bundles.forEach((bundle, index) => {
      const activityBounds = blockBounds(bundle.activity);
      if (!activityBounds || !supportBounds) return;

      const gapBefore = activityBounds.start - supportBounds.end;
      const gapAfter = supportBounds.start - activityBounds.end;
      const overlap = Math.min(activityBounds.end, supportBounds.end) - Math.max(activityBounds.start, supportBounds.start);

      if (gapBefore >= 0 && gapBefore <= attachWindowMinutes) {
        const candidate = { index, side: "before" as const, score: gapBefore };
        if (!best || candidate.score < best.score) best = candidate;
      }
      if (gapAfter >= 0 && gapAfter <= attachWindowMinutes) {
        const candidate = { index, side: "after" as const, score: gapAfter };
        if (!best || candidate.score < best.score) best = candidate;
      }
      if (overlap > 0) {
        const supportMidpoint = supportBounds.start + (supportBounds.end - supportBounds.start) / 2;
        const activityMidpoint = activityBounds.start + (activityBounds.end - activityBounds.start) / 2;
        const side = supportMidpoint <= activityMidpoint ? "before" : "after";
        const candidate = { index, side, score: overlap };
        if (!bestOverlap || candidate.score > bestOverlap.score) bestOverlap = candidate;
      }
    });

    best = best || bestOverlap;
    if (!best) {
      unassigned.push(support);
      continue;
    }
    bundles[best.index][best.side].push(support);
  }

  const sortSupports = (items: ActivityBlock[]) => items.sort(
    (a, b) => timeToMinutes(a.startTime || a.start || "00:00") - timeToMinutes(b.startTime || b.start || "00:00"),
  );
  for (const bundle of bundles) {
    bundle.before = sortSupports(bundle.before);
    bundle.after = sortSupports(bundle.after);
  }

  return { bundles, unassigned: sortSupports(unassigned) };
}

function relatedSupportTarget(
  support: ActivityBlock,
  bundles: ActivitySupportBundle[],
): { index: number; side: "before" | "after" } | null {
  const supportBounds = blockBounds(support);
  const directCandidates = supportRelationCandidates(support);
  for (const candidate of directCandidates) {
    const index = bundles.findIndex((bundle) => activityMatchesRelation(bundle.activity, candidate.key));
    if (index < 0) continue;
    return {
      index,
      side: candidate.side || sideRelativeToActivity(supportBounds, blockBounds(bundles[index].activity)),
    };
  }

  const relatedIds = Array.isArray((support as ActivityBlock & { related_activity_ids?: unknown[] }).related_activity_ids)
    ? (support as ActivityBlock & { related_activity_ids?: unknown[] }).related_activity_ids || []
    : [];
  let best:
    | { index: number; side: "before" | "after"; score: number }
    | null = null;
  for (const relatedId of relatedIds) {
    const index = bundles.findIndex((bundle) => activityMatchesRelation(bundle.activity, String(relatedId)));
    if (index < 0) continue;
    const activityBounds = blockBounds(bundles[index].activity);
    if (!supportBounds || !activityBounds) continue;
    const gapBefore = Math.abs(activityBounds.start - supportBounds.end);
    const gapAfter = Math.abs(supportBounds.start - activityBounds.end);
    const side = gapBefore <= gapAfter ? "before" : "after";
    const score = Math.min(gapBefore, gapAfter);
    if (!best || score < best.score) best = { index, side, score };
  }
  return best ? { index: best.index, side: best.side } : null;
}

function supportRelationCandidates(support: ActivityBlock): Array<{ key: string; side?: "before" | "after" }> {
  const raw = support as ActivityBlock & {
    related_activity_id?: string;
    activity_id?: string;
    from_activity?: string;
    from_title?: string;
    from?: string;
    to_activity?: string;
    to_title?: string;
    to?: string;
  };
  return [
    { key: raw.related_activity_id || raw.activity_id || "" },
    { key: raw.from_activity || raw.from_title || raw.from || "", side: "after" as const },
    { key: raw.to_activity || raw.to_title || raw.to || "", side: "before" as const },
  ].filter((candidate) => normalizedBlockText(candidate.key));
}

function activityMatchesRelation(activity: ActivityBlock, key: string): boolean {
  const normalizedKey = normalizedBlockText(key);
  if (!normalizedKey) return false;
  return [
    activity.id,
    activity.stable_activity_id,
    (activity as ActivityBlock & { activity_id?: string }).activity_id,
    activity.title,
  ].some((value) => normalizedBlockText(value) === normalizedKey);
}

function sideRelativeToActivity(
  supportBounds: { start: number; end: number } | null,
  activityBounds: { start: number; end: number } | null,
): "before" | "after" {
  if (!supportBounds || !activityBounds) return "after";
  const gapBefore = Math.abs(activityBounds.start - supportBounds.end);
  const gapAfter = Math.abs(supportBounds.start - activityBounds.end);
  return gapBefore <= gapAfter ? "before" : "after";
}

function blockBounds(block: ActivityBlock): { start: number; end: number } | null {
  const rawStart = block.startTime || block.start;
  const rawEnd = block.endTime || block.end;
  if (!rawStart || !rawEnd) return null;
  const start = timeToMinutes(rawStart);
  let end = timeToMinutes(rawEnd);
  if (end <= start && rawEnd !== "00:00") end += 24 * 60;
  return { start, end };
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

function supportRenderKind(activity: ActivityBlock): Exclude<TimelineBlockKind, "activity" | "free_time"> {
  const kind = getTimelineBlockKind(activity);
  if (kind === "activity" || kind === "free_time") return "buffer";
  return kind;
}

function isBoundarySupportChainBlock(block: ActivityBlock, timeline: ActivityBlock[]): boolean {
  const kind = getTimelineBlockKind(block);
  if (kind === "activity" || kind === "free_time" || kind === "start_route" || kind === "route_conflict") {
    return false;
  }

  const supports = timeline.filter((item) => {
    const itemKind = getTimelineBlockKind(item);
    return itemKind !== "activity" && itemKind !== "free_time" && itemKind !== "start_route" && itemKind !== "route_conflict";
  });
  const activities = timeline.filter((item) => getTimelineBlockKind(item) === "activity");
  const supportKeysConnectedToActivity = new Set<string>();

  const supportKey = (item: ActivityBlock) => activityIdentity(item) || `${item.title}-${item.startTime || item.start}-${item.endTime || item.end}`;
  const boundsTouch = (left: ActivityBlock, right: ActivityBlock): boolean => {
    const leftBounds = blockBounds(left);
    const rightBounds = blockBounds(right);
    if (!leftBounds || !rightBounds) return false;
    return Math.abs(leftBounds.end - rightBounds.start) <= 1 || Math.abs(leftBounds.start - rightBounds.end) <= 1;
  };

  for (const support of supports) {
    if (activities.some((activity) => boundsTouch(support, activity))) {
      supportKeysConnectedToActivity.add(supportKey(support));
    }
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (const support of supports) {
      const key = supportKey(support);
      if (supportKeysConnectedToActivity.has(key)) continue;
      const touchesConnectedSupport = supports.some((other) => (
        supportKeysConnectedToActivity.has(supportKey(other)) && boundsTouch(support, other)
      ));
      if (touchesConnectedSupport) {
        supportKeysConnectedToActivity.add(key);
        changed = true;
      }
    }
  }

  return supportKeysConnectedToActivity.has(supportKey(block));
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
    const start = canonicalTimeKey(activity.startTime || activity.start);
    const end = canonicalTimeKey(activity.endTime || activity.end);
    const duration = getDurationMinutes(activity);
    if (!destination || !start || !end) return null;
    return `travel:${destination}:${start}:${end}:${duration}`;
  }
  if (kind === "buffer") {
    const start = canonicalTimeKey(activity.startTime || activity.start);
    const end = canonicalTimeKey(activity.endTime || activity.end);
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
    activity.title.replace(/^travel to\s+/i, "").replace(/\s+\(tight\)$/i, "")
    || activity.location,
  );
}

function canonicalTimeKey(value: unknown): string {
  const text = normalizedBlockText(value);
  if (!text) return "";
  return String(timeToMinutes(text));
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
  compact = false,
  interactive = false,
  onClick,
}: {
  activity: ActivityBlock;
  kind: Exclude<TimelineBlockKind, "activity" | "free_time">;
  compact?: boolean;
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
        borderRadius: compact ? "8px" : "10px",
        border: "1px solid",
        ...bgStyle,
        padding: compact ? "6px 9px" : "8px 12px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        gap: compact ? "8px" : "12px",
        boxShadow: "0 1px 3px rgba(0,0,0,0.05)",
        cursor: interactive ? "pointer" : "default",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "7px", minWidth: 0, flex: 1 }}>
        <Icon size={13} style={{ flexShrink: 0, color: accentColor }} />
        <span
          style={{
            fontSize: compact ? "11.5px" : "12.5px",
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
          fontSize: compact ? "11px" : "11.5px",
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
