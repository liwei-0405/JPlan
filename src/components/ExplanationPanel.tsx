import { AlertTriangle, ArrowLeft, Clock, Info, Lock, Route, SlidersHorizontal, Sparkles } from "lucide-react";
import type { ActivityBlock, DailySchedule } from "../App";
import { Button } from "./ui/button";

type ExplanationPanelProps = {
  schedule: DailySchedule;
  onBack: () => void;
};

type ExplanationSection = {
  id: string;
  title: string;
  description?: string;
  icon: "summary" | "fixed" | "travel" | "adjust" | "warning" | "time";
  items: string[];
};

type ExplanationModel = {
  sections: ExplanationSection[];
  technicalTrace: string[];
};

export function ExplanationPanel({ schedule, onBack }: ExplanationPanelProps) {
  const model = buildExplanationModel(schedule);

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="mx-auto max-w-3xl px-4 py-5 sm:px-6 sm:py-8">
        <Button
          variant="ghost"
          onClick={onBack}
          className="mb-5 rounded-xl sm:mb-8"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Schedule
        </Button>

        <div className="mb-5 sm:mb-8">
          <div className="mb-2 flex items-center gap-2">
            <Sparkles className="h-6 w-6 text-primary" />
            <h2>Schedule Explanation</h2>
          </div>
          <p className="text-muted-foreground">
            A readable summary of why the planner kept, moved, or left out activities.
          </p>
        </div>

        <div className="space-y-4">
          {model.sections.map((section) => (
            <section
              key={section.id}
              className="rounded-xl border border-border bg-card p-4 shadow-sm sm:p-5"
            >
              <div className="mb-4 flex gap-3">
                <div className="mt-0.5 flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-xl bg-primary/10 sm:h-10 sm:w-10">
                  {renderSectionIcon(section.icon)}
                </div>
                <div>
                  <h3 className="font-semibold">{section.title}</h3>
                  {section.description && (
                    <p className="mt-1 text-sm text-muted-foreground">{section.description}</p>
                  )}
                </div>
              </div>

              <ul className="space-y-2">
                {section.items.map((item, index) => (
                  <li key={`${section.id}-${index}`} className="text-sm leading-relaxed text-foreground">
                    {item}
                  </li>
                ))}
              </ul>
            </section>
          ))}

          {model.sections.length === 0 && (
            <div className="rounded-xl border border-dashed border-border bg-card py-12 text-center">
              <p className="text-muted-foreground">
                No explanation metadata is available for this schedule yet.
              </p>
            </div>
          )}
        </div>

        {model.technicalTrace.length > 0 && (
          <details className="mt-6 rounded-xl border border-border bg-card p-4 text-sm sm:p-5">
            <summary className="cursor-pointer font-medium">
              Technical trace
            </summary>
            <div className="mt-4 space-y-2 text-muted-foreground">
              {model.technicalTrace.map((trace, index) => (
                <p key={index}>{trace}</p>
              ))}
            </div>
          </details>
        )}

        <div className="mt-6 rounded-xl border border-[#fff9d4]/30 bg-gradient-to-br from-[#fff9d4]/20 to-[#fff9d4]/5 p-4 sm:mt-8 sm:p-5">
          <p className="text-sm text-muted-foreground">
            These explanations are generated from stored planner metadata: constraints, route validation, repair actions, and activity traces.
          </p>
        </div>
      </div>
    </div>
  );
}

function renderSectionIcon(icon: ExplanationSection["icon"]) {
  const className = "h-5 w-5 text-primary";
  if (icon === "fixed") return <Lock className={className} />;
  if (icon === "travel") return <Route className={className} />;
  if (icon === "adjust") return <SlidersHorizontal className={className} />;
  if (icon === "warning") return <AlertTriangle className={className} />;
  if (icon === "time") return <Clock className={className} />;
  return <Info className={className} />;
}

function buildExplanationModel(schedule: DailySchedule): ExplanationModel {
  const activities = scheduledActivities(schedule);
  const sections: ExplanationSection[] = [];

  addSection(sections, {
    id: "summary",
    title: "How this plan was built",
    icon: "summary",
    items: buildSummaryItems(schedule, activities),
  });

  addSection(sections, {
    id: "fixed",
    title: "Fixed commitments",
    description: "Events that the planner treated as locked or protected.",
    icon: "fixed",
    items: buildFixedItems(activities),
  });

  addSection(sections, {
    id: "timing",
    title: "Timing and sequence decisions",
    description: "Why some activities were placed near anchors or preferred windows.",
    icon: "time",
    items: buildTimingItems(activities),
  });

  addSection(sections, {
    id: "travel",
    title: "Travel realism",
    description: "How route validation affected the final timeline.",
    icon: "travel",
    items: buildTravelItems(schedule),
  });

  addSection(sections, {
    id: "adjustments",
    title: "What changed during optimization",
    description: "Flexible activities may move when that improves travel, idle time, or preference fit.",
    icon: "adjust",
    items: buildAdjustmentItems(schedule, activities),
  });

  addSection(sections, {
    id: "unfit",
    title: "Could not fit",
    description: "Activities that were not placed in the final schedule.",
    icon: "warning",
    items: buildUnfitItems(schedule),
  });

  addSection(sections, {
    id: "activity-details",
    title: "Activity details",
    description: "Short per-activity rationale based on available planner metadata.",
    icon: "summary",
    items: buildActivityDetailItems(activities),
  });

  return {
    sections,
    technicalTrace: buildTechnicalTrace(schedule),
  };
}

function addSection(sections: ExplanationSection[], section: ExplanationSection) {
  const items = unique(section.items).filter(Boolean);
  if (items.length === 0) return;
  sections.push({ ...section, items });
}

function buildSummaryItems(schedule: DailySchedule, activities: ActivityBlock[]): string[] {
  const items: string[] = [];
  const fixedCount = activities.filter(isFixedOrProtected).length;
  const flexCount = activities.filter((activity) => !isFixedOrProtected(activity)).length;
  const status = String(schedule.travel_validation_status || schedule.schedule_status || schedule.status || "");

  if (fixedCount) {
    items.push(`${fixedCount} fixed or protected ${plural(fixedCount, "event was", "events were")} placed first and kept stable.`);
  }
  if (flexCount) {
    items.push(`${flexCount} flexible ${plural(flexCount, "activity was", "activities were")} fitted around those commitments.`);
  }
  if (status === "validated" || status === "repaired_validated") {
    items.push("Accurate travel time was validated, and travel blocks were updated from route durations.");
  } else if (status === "partial_feasible_with_fixed_route_conflicts") {
    items.push("The schedule is usable, but some fixed events still have route warnings because their times cannot be moved automatically.");
  } else if (status === "partial_feasible_with_unfit") {
    items.push("The planner found a usable partial schedule and listed activities that could not fit.");
  } else if (status === "pending_locations") {
    items.push("Travel validation is waiting for exact locations before route durations can be finalized.");
  } else {
    items.push("The planner used fixed times, relative anchors, preferences, and available free slots to build the timeline.");
  }
  return items;
}

function buildFixedItems(activities: ActivityBlock[]): string[] {
  return activities
    .filter(isFixedOrProtected)
    .slice(0, 8)
    .map((activity) => {
      const time = timeRange(activity);
      const reason = activity.repair_protection === "protected_social"
        ? "it appears to be an important protected commitment"
        : "it has a fixed or user-confirmed time";
      return `${activity.title}${time ? ` (${time})` : ""} was kept in place because ${reason}.`;
    });
}

function buildTimingItems(activities: ActivityBlock[]): string[] {
  const items: string[] = [];

  for (const activity of activities) {
    const relation = activity.anchor_relation as Record<string, unknown> | undefined;
    if (relation && typeof relation === "object") {
      const anchorTitle = text(relation.anchor_title || relation.target_title || relation.anchor_id);
      const relationType = text(relation.type || relation.relation || activity.semantic_constraint_type);
      items.push(`${activity.title} was placed ${relationType || "relative to"} ${anchorTitle || "its anchor"} because the request linked those activities.`);
      continue;
    }

    if (activity.semantic_constraint_type === "dropoff" || activity.service_kind === "dropoff") {
      items.push(`${activity.title} was treated as a deadline-style drop-off, so the service needed to finish by the requested time.`);
    } else if (activity.semantic_constraint_type === "pickup" || activity.service_kind === "pickup") {
      items.push(`${activity.title} was treated as a pickup commitment at the requested time.`);
    } else if (activity.preferred_time_window || activity.preferred_window_start || activity.preferred_window_end) {
      const window = preferredWindowLabel(activity);
      items.push(`${activity.title} was scored against its preferred time window${window ? ` (${window})` : ""}.`);
    } else if (hasTrace(activity, "Placed near anchor")) {
      const anchor = traceAnchor(activity);
      items.push(`${activity.title} was placed near ${anchor || "its related activity"} to respect the requested sequence.`);
    }
  }

  return items.slice(0, 8);
}

function buildTravelItems(schedule: DailySchedule): string[] {
  const items: string[] = [];
  const status = String(schedule.travel_validation_status || "");
  const startRoute = schedule.start_route_summary || {};

  if (startRoute && typeof startRoute === "object" && startRoute.leave_by) {
    const leaveBy = text(startRoute.leave_by);
    const startLocation = text(startRoute.start_location);
    const destination = text(startRoute.first_physical_event_location || startRoute.destination_location || startRoute.first_physical_event);
    const duration = text(startRoute.travel_duration_minutes);
    items.push(`Start route: leave ${startLocation || "the starting point"} by ${leaveBy} for ${destination || "the first physical event"}${duration ? ` (${duration} min travel)` : ""}.`);
  }

  if (status === "validated" || status === "repaired_validated") {
    items.push("Travel blocks were recalculated using the routing service where coordinates were available.");
  } else if (status === "fallback_used") {
    items.push("Some travel estimates used the fallback estimator because live route validation was unavailable.");
  }

  for (const conflict of schedule.route_conflicts || []) {
    const reasonCode = text(conflict.reason_code);
    if (reasonCode === "fixed_to_fixed_infeasible") {
      const from = text(conflict.from_activity || conflict.from);
      const to = text(conflict.to_activity || conflict.to);
      const required = text(conflict.required_minutes || conflict.required_travel_minutes || conflict.travel_minutes);
      const available = text(conflict.available_minutes);
      items.push(`${from || "One fixed event"} to ${to || "the next fixed event"} still has a route warning${required ? `: needs about ${required} min` : ""}${available ? `, with only ${available} min available` : ""}. Fixed times were kept unchanged.`);
    } else if (conflict.reason) {
      items.push(text(conflict.reason));
    }
  }

  return items;
}

function buildAdjustmentItems(schedule: DailySchedule, activities: ActivityBlock[]): string[] {
  const items: string[] = [];

  for (const action of schedule.route_repair_actions || []) {
    const title = text(action.title || action.activity_title);
    const from = text(action.from || action.old_time || action.old_start || action.previous_start);
    const to = text(action.to || action.new_time || action.new_start || action.updated_start);
    const reason = text(action.reason || action.explanation);
    if (!title) continue;
    items.push(`${title}${from || to ? ` moved${from ? ` from ${from}` : ""}${to ? ` to ${to}` : ""}` : " was adjusted"}${reason ? ` because ${lowerFirst(reason)}` : " to keep the route feasible"}.`);
  }

  for (const activity of activities) {
    if (hasTrace(activity, "Adjusted by Module D refinement")) {
      items.push(`${activity.title} was moved during local optimization to reduce idle time or travel while keeping constraints valid.`);
    }
    if (hasTrace(activity, "Backfilled into an earlier route-safe free slot")) {
      items.push(`${activity.title} was moved into an earlier route-safe free slot after travel repair created space.`);
    }
    if (hasTrace(activity, "Moved before a lower-priority same-location blocker")) {
      items.push(`${activity.title} was placed before a lower-priority same-location activity to protect its preferred time.`);
    }
  }

  return items.slice(0, 10);
}

function buildUnfitItems(schedule: DailySchedule): string[] {
  const items: string[] = [];

  for (const item of schedule.unfit_activities || []) {
    const title = text(item.title) || "One activity";
    const duration = text(item.duration_minutes);
    const reason = text(item.reason || item.unscheduled_reason) || "there was no feasible slot";
    const blocker = text(item.blocking_constraint);
    items.push(`${title}${duration ? ` (${duration} min)` : ""} could not fit because ${lowerFirst(reason)}${blocker ? ` Blocking constraint: ${blocker}.` : ""}`);
  }

  for (const item of schedule.unscheduled_activities || []) {
    const title = item.title || "One activity";
    const reason = item.reason || "there was no feasible slot";
    items.push(`${title} was left out because ${lowerFirst(reason)}.`);
  }

  for (const item of schedule.optional_skipped || []) {
    const title = text(item.title) || "One optional item";
    const reason = text(item.reason || item.unscheduled_reason) || "it was optional and the day was tight";
    items.push(`${title} was skipped because ${lowerFirst(reason)}.`);
  }

  return items;
}

function buildActivityDetailItems(activities: ActivityBlock[]): string[] {
  return activities
    .map((activity) => activityDetail(activity))
    .filter(Boolean)
    .slice(0, 10) as string[];
}

function activityDetail(activity: ActivityBlock): string | null {
  const reasons: string[] = [];
  if (isFixedOrProtected(activity)) {
    reasons.push("kept at its fixed time");
  }
  if (activity.location || activity.location_label || activity.resolved_location?.display_name) {
    reasons.push("scheduled with its confirmed location");
  }
  if (activity.travel_required) {
    reasons.push("included in travel checks");
  }
  if (activity.timing_mode === "relative" || activity.anchor_relation) {
    reasons.push("placed relative to another activity");
  }
  if (activity.activity_role === "recovery") {
    reasons.push("treated as a recovery activity");
  }
  if (!reasons.length) return null;
  return `${activity.title}: ${reasons.join(", ")}.`;
}

function buildTechnicalTrace(schedule: DailySchedule): string[] {
  const traces: string[] = [];

  for (const explanation of schedule.explanations || []) {
    if (explanation?.trim()) traces.push(explanation.trim());
  }

  for (const activity of schedule.activities || []) {
    if (activity.explanation?.trim()) traces.push(`${activity.title}: ${activity.explanation.trim()}`);
    for (const trace of activity.trace || []) {
      if (trace?.trim()) traces.push(`${activity.title}: ${trace.trim()}`);
    }
  }

  return unique(traces);
}

function scheduledActivities(schedule: DailySchedule): ActivityBlock[] {
  const source = schedule.activities || [];
  return source.filter((activity) => {
    const kind = String(activity.block_type || activity.type || "activity").toLowerCase();
    if (activity.display_only || activity.is_route_conflict || activity.is_start_route) return false;
    return kind === "activity" || (!kind && Boolean(activity.title));
  });
}

function isFixedOrProtected(activity: ActivityBlock): boolean {
  const mode = String(activity.timing_mode || activity.original_timing_mode || "").toLowerCase();
  const protection = String(activity.repair_protection || "").toLowerCase();
  return Boolean(
    mode === "fixed" ||
    activity.is_user_fixed ||
    activity.fixed_start != null ||
    activity.fixed_end != null ||
    protection === "fixed" ||
    protection.includes("protected"),
  );
}

function timeRange(activity: ActivityBlock): string {
  const start = activity.startTime || activity.start;
  const end = activity.endTime || activity.end;
  return start && end ? `${start} - ${end}` : "";
}

function preferredWindowLabel(activity: ActivityBlock): string {
  const start = text((activity as ActivityBlock & { preferred_window_start?: unknown }).preferred_window_start);
  const end = text((activity as ActivityBlock & { preferred_window_end?: unknown }).preferred_window_end);
  const named = text((activity as ActivityBlock & { preferred_time_window?: unknown }).preferred_time_window);
  if (start && end) return `${start} - ${end}`;
  return named;
}

function hasTrace(activity: ActivityBlock, marker: string): boolean {
  return (activity.trace || []).some((trace) => trace.includes(marker));
}

function traceAnchor(activity: ActivityBlock): string {
  const trace = (activity.trace || []).find((item) => item.includes("Placed near anchor"));
  const match = trace?.match(/'([^']+)'/);
  return match?.[1] || "";
}

function unique(items: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const item of items) {
    const clean = item.trim();
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    result.push(clean);
  }
  return result;
}

function text(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function lowerFirst(value: string): string {
  if (!value) return value;
  return value.charAt(0).toLowerCase() + value.slice(1);
}

function plural(count: number, singular: string, pluralText: string): string {
  return count === 1 ? singular : pluralText;
}
