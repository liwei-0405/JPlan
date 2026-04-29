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
  if (activities.length === 0) return null;

  const groups = useMemo(() => buildCollisionGroups(activities), [activities]);
  const collidingIds = useMemo(() => getCollidingIds(activities), [activities]);

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
    const bType = act.block_type || act.type;
    
    if (bType && bType !== "activity") {
      return <TravelBufferBlock activity={act} />;
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
  const clashCount = group.activities.filter((act) => (act.block_type || act.type) === "activity").length;

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
          ⚡ Time Clash — {clashCount} overlapping events
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

          const bType = act.block_type || act.type;

          if (bType && bType !== "activity") {
            return (
              <div key={act.id || act.title} style={{ position: "relative", height: `${containerHeight}px` }}>
                <div style={{ position: "absolute", top: `${topOffset}px`, width: "100%" }}>
                  <TravelBufferBlock activity={act} />
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

function TravelBufferBlock({ activity }: { activity: ActivityBlock }) {
  const bType = activity.block_type || activity.type;
  const st = activity.startTime || activity.start;
  const et = activity.endTime || activity.end;

  let colorClass = "from-indigo-50 to-white border-indigo-500 text-indigo-800";
  let dotClass = "bg-indigo-500";
  let timeClass = "text-indigo-600";

  if (bType === "buffer") {
    colorClass = "from-emerald-50 to-white border-emerald-500 text-emerald-800";
    dotClass = "bg-emerald-500";
    timeClass = "text-emerald-600";
  } else if (bType === "idle") {
    colorClass = "from-slate-50 to-white border-slate-300 text-slate-500 border-dashed";
    dotClass = "bg-slate-300";
    timeClass = "text-slate-400";
  }

  return (
    <div className={`border-l-4 rounded-xl p-4 shadow-sm bg-gradient-to-r ${colorClass}`}>
      <div className="flex items-center justify-between">
        <span className={`flex items-center gap-2 font-bold text-base ${colorClass.split(" ").pop()}`}>
          <div className={`w-2.5 h-2.5 rounded-full ${dotClass}`} />
          {activity.title}
        </span>
        <span className={`text-sm font-semibold ${timeClass}`}>
          {st} – {et}
        </span>
      </div>
    </div>
  );
}
