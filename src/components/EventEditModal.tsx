import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { X, AlertCircle, MapPin } from "lucide-react";
import type { ActivityBlock } from "../App";
import { LocationPickerDialog, candidateToMapPoint } from "./LocationPickerDialog";
import type { GeocodeCandidate, SavedLocation } from "../services/planService";
import {
  formatDurationMinutes,
  getCanonicalDurationMinutes,
} from "../utils/durationUtils";

type EventEditModalProps = {
  event: ActivityBlock;
  onSave: (updatedEvent: ActivityBlock) => void;
  onCancel: () => void;
  onDelete?: () => void;
  allActivities: ActivityBlock[];
  savedLocations?: SavedLocation[];
  recentLocations?: Array<Partial<SavedLocation>>;
  onLocationConfirmed?: (candidate: GeocodeCandidate) => void;
};

export function EventEditModal({
  event,
  onSave,
  onCancel,
  onDelete,
  allActivities,
  savedLocations = [],
  recentLocations = [],
  onLocationConfirmed,
}: EventEditModalProps) {
  const [formData, setFormData] = useState(() => normalizeEventForEdit(event));
  const [isConflict, setIsConflict] = useState(false);
  const [isLocationPickerOpen, setIsLocationPickerOpen] = useState(false);
  const isBuffer = isBufferBlock(event);

  useEffect(() => {
    setFormData(normalizeEventForEdit(event));
  }, [event]);

  useEffect(() => {
    if (!formData.startTime || !formData.endTime) {
      setIsConflict(false);
      return;
    }
    const newStartMins = timeToMinutes(formData.startTime);
    const newEndMins = timeToMinutes(formData.endTime);
    // Handle overnight normalization for conflict check
    const effectiveEnd = newEndMins < newStartMins ? newEndMins + 1440 : newEndMins;

    const hasCollision = allActivities.some(act => {
      if (act.id === event.id) return false; // Skip the current event being edited
      const actStartTime = act.startTime || act.start || "";
      const actEndTime = act.endTime || act.end || "";
      if (!actStartTime || !actEndTime) return false;
      const s = timeToMinutes(actStartTime);
      let e = timeToMinutes(actEndTime);
      if (e < s) e += 1440;
      return (newStartMins < e) && (effectiveEnd > s);
    });

    setIsConflict(hasCollision);
  }, [formData.startTime, formData.endTime, allActivities, event.id]);

  const handleSave = () => {
    const confirmedLocationName = formData.resolved_location?.display_name || formData.resolved_location?.address;
    const mode = isBuffer || String(formData.timing_mode || "").toLowerCase() === "fixed" ? "fixed" : "preferred";
    const hasStart = Boolean(formData.startTime);
    const startMinutes = hasStart ? timeToMinutes(formData.startTime) : null;
    const endMinutes = hasStart && formData.endTime ? normalizeEndMinutes(startMinutes ?? 0, timeToMinutes(formData.endTime)) : null;
    const durationMinutes = startMinutes != null && endMinutes != null
      ? Math.max(1, endMinutes - startMinutes)
      : getCanonicalDurationMinutes(formData);
    onSave({
      ...formData,
      title: isBuffer ? "Buffer" : formData.title.trim(),
      duration: formatDurationMinutes(durationMinutes),
      duration_minutes: durationMinutes,
      location: isBuffer ? undefined : (confirmedLocationName || formData.location?.trim() || undefined),
      location_label: isBuffer ? undefined : (confirmedLocationName || formData.location_label || formData.location),
      location_status: isBuffer ? undefined : (formData.resolved_location ? "resolved" : formData.location_status),
      start: formData.startTime,
      end: formData.endTime,
      timing_mode: mode,
      original_timing_mode: mode,
      fixed_start: mode === "fixed" ? startMinutes : null,
      fixed_end: mode === "fixed" ? endMinutes : null,
      user_fixed_start: mode === "fixed" ? startMinutes : null,
      is_user_fixed: mode === "fixed",
      preferred_start: mode === "fixed" ? null : startMinutes,
      scheduled_start: mode === "fixed" ? startMinutes : formData.scheduled_start,
      scheduled_end: mode === "fixed" ? endMinutes : formData.scheduled_end,
      can_move_for_repair: mode !== "fixed",
      repair_protection: mode === "fixed" ? "fixed" : "flexible",
    });
  };

  const handleEndTimeChange = (newEndTime12h: string) => {
    if (!newEndTime12h || !formData.startTime) {
      setFormData({ ...formData, endTime: newEndTime12h });
      return;
    }
    const startMins = timeToMinutes(formData.startTime);
    const endMins = timeToMinutes(newEndTime12h);
    let diff = endMins - startMins;
    if (diff < 0) diff += 1440;

    setFormData({
      ...formData,
      endTime: newEndTime12h,
      duration: formatDurationMinutes(diff),
      duration_minutes: diff,
    });
  };

  const handleStartTimeChange = (newStartTime12h: string) => {
    if (!newStartTime12h) {
      setFormData({ ...formData, startTime: "", endTime: "" });
      return;
    }
    if (!formData.endTime) {
      setFormData({ ...formData, startTime: newStartTime12h });
      return;
    }
    const startMins = timeToMinutes(newStartTime12h);
    const endMins = timeToMinutes(formData.endTime);
    let diff = endMins - startMins;
    if (diff < 0) diff += 1440;

    setFormData({
      ...formData,
      startTime: newStartTime12h,
      duration: formatDurationMinutes(diff),
      duration_minutes: diff,
    });
  };

  const handleDurationPartsChange = (hoursValue: string, minutesValue: string) => {
    const hours = clampDurationPart(hoursValue, 23);
    const minutes = clampDurationPart(minutesValue, 59);
    const totalMins = Math.max(1, Number(hours) * 60 + Number(minutes));
    const durStr = formatDurationMinutes(totalMins);
    if (!formData.startTime) {
      setFormData({ ...formData, duration: durStr, duration_minutes: totalMins });
      return;
    }
    const startMins = timeToMinutes(formData.startTime);
    const newEndMins = (startMins + totalMins) % 1440;

    const formattedEnd = minutesTo12Hour(newEndMins);
    setFormData({ ...formData, duration: durStr, duration_minutes: totalMins, endTime: formattedEnd });
  };

  const existingLocationCandidate = resolvedLocationToCandidate(formData);
  const existingLocationPoint = candidateToMapPoint(existingLocationCandidate);
  const isFixedTime = isBuffer || String(formData.timing_mode || "").toLowerCase() === "fixed";
  const durationParts = durationMinutesToParts(getCanonicalDurationMinutes(formData));

  const handleConfirmPickedLocation = async (candidate: GeocodeCandidate) => {
    const displayName = candidate.display_name || candidate.address || formData.location || formData.title;
    const address = candidate.address || candidate.display_name || displayName;
    setFormData({
      ...formData,
      location: displayName,
      location_label: displayName,
      location_status: "resolved",
      location_source: candidate.source || "event_manual_location",
      location_warning: undefined,
      saved_location_label: candidate.label || formData.saved_location_label,
      resolved_location: {
        label: candidate.label || formData.title,
        display_name: displayName,
        address,
        category: formData.location_category,
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: candidate.source || "event_manual_location",
        confirmed_by_user: candidate.confirmed_by_user ?? true,
        resolved_for_activity_id: formData.stable_activity_id || formData.id,
        saved_location_label: candidate.label || formData.saved_location_label,
      },
    });
    onLocationConfirmed?.(candidate);
    setIsLocationPickerOpen(false);
  };

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-3 sm:p-4">
      <div className="bg-card rounded-2xl border border-border shadow-xl max-w-md w-full max-h-[calc(100vh-1.5rem)] overflow-y-auto sm:max-h-[90vh]">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-border sm:p-5">
          <div>
            <h3>{isBuffer ? "Edit Buffer" : "Manual Edit"}</h3>
            <p className="text-sm text-muted-foreground mt-1">
              {isBuffer ? "Adjust or remove this buffer block" : "Edit activity details directly"}
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon"
            onClick={onCancel}
            className="rounded-xl"
          >
            <X className="h-5 w-5" />
          </Button>
        </div>

        {/* Content */}
        <div className="p-4 space-y-4 sm:p-5">
          {/* Activity Title */}
          {!isBuffer && <div>
            <Label htmlFor="title">Activity Title</Label>
            <Input
              id="title"
              value={formData.title}
              onChange={(e) => setFormData({ ...formData, title: e.target.value })}
              placeholder="Activity name"
              className="mt-1.5 rounded-xl"
            />
          </div>}

          {/* Time Fields */}
          {!isBuffer && <div className="space-y-1.5">
            <Label>Time behavior</Label>
            <div className="grid grid-cols-2 gap-2 rounded-xl border border-border bg-secondary/20 p-1">
              <Button
                type="button"
                size="sm"
                variant={isFixedTime ? "default" : "ghost"}
                className="rounded-lg text-xs"
                onClick={() => setFormData({ ...formData, timing_mode: "fixed", original_timing_mode: "fixed" })}
              >
                Fixed time
              </Button>
              <Button
                type="button"
                size="sm"
                variant={!isFixedTime ? "default" : "ghost"}
                className="rounded-lg text-xs"
                onClick={() => setFormData({
                  ...formData,
                  timing_mode: formData.startTime ? "preferred" : "unspecified",
                  original_timing_mode: formData.startTime ? "preferred" : "unspecified",
                  fixed_start: null,
                  fixed_end: null,
                  user_fixed_start: null,
                  is_user_fixed: false,
                  can_move_for_repair: true,
                  repair_protection: "flexible",
                })}
              >
                Flexible
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              Flexible events can move. A start time becomes a preference, not a lock.
            </p>
          </div>}
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <Label htmlFor="start-time">{isFixedTime ? "Start Time" : "Preferred Start (optional)"}</Label>
              <Input
                id="start-time"
                type="time"
                value={convertTo24Hour(formData.startTime)}
                onChange={(e) => handleStartTimeChange(convertTo12Hour(e.target.value))}
                className="mt-1.5 rounded-xl"
              />
            </div>
            <div>
              <Label htmlFor="end-time">{isFixedTime ? "End Time" : "Preferred End (optional)"}</Label>
              <Input
                id="end-time"
                type="time"
                value={convertTo24Hour(formData.endTime)}
                onChange={(e) => handleEndTimeChange(convertTo12Hour(e.target.value))}
                className={`mt-1.5 rounded-xl ${isConflict ? "border-destructive text-destructive" : ""}`}
              />
            </div>
          </div>

          {/* Duration */}
          <div>
            <Label htmlFor="duration">Duration</Label>
            <div className="mt-1.5 flex max-w-[210px] gap-2">
              <div className="relative w-[96px]">
                <Input
                  id="duration-hours"
                  type="number"
                  min={0}
                  max={23}
                  step={1}
                  value={durationParts.hours}
                  onChange={(e) => handleDurationPartsChange(e.target.value, String(durationParts.minutes))}
                  className="h-9 rounded-xl pr-7 text-sm"
                />
                <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 whitespace-nowrap text-[11px] text-muted-foreground">hr</span>
              </div>
              <div className="relative w-[104px]">
                <Input
                  id="duration-minutes"
                  type="number"
                  min={0}
                  max={59}
                  step={1}
                  value={durationParts.minutes}
                  onChange={(e) => handleDurationPartsChange(String(durationParts.hours), e.target.value)}
                  className="h-9 rounded-xl pr-9 text-sm"
                />
                <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 whitespace-nowrap text-[11px] text-muted-foreground">min</span>
              </div>
            </div>
          </div>

          {/* Location */}
          {!isBuffer && <div>
            <Label>Location (optional)</Label>
            <div className="mt-1.5 rounded-xl border border-border bg-secondary/20 p-3">
              {formData.resolved_location ? (
                <div>
                  <p className="text-sm font-medium">
                    {formData.resolved_location.display_name || formData.resolved_location.address || formData.location}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    Confirmed map point attached to this event.
                  </p>
                </div>
              ) : formData.location ? (
                <div>
                  <p className="text-sm font-medium">{formData.location}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    This location text is not confirmed with coordinates yet.
                  </p>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">No exact location selected.</p>
              )}
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button
                type="button"
                variant="outline"
                className="rounded-xl"
                onClick={() => setIsLocationPickerOpen(true)}
              >
                <MapPin className="mr-2 h-4 w-4" />
                {formData.resolved_location || formData.location ? "Change Location" : "Pick Location"}
              </Button>
              {(formData.resolved_location || formData.location) && (
                <Button
                  type="button"
                  variant="ghost"
                  className="rounded-xl"
                  onClick={() => setFormData({
                    ...formData,
                    location: undefined,
                    location_label: undefined,
                    location_status: undefined,
                    location_source: undefined,
                    location_warning: undefined,
                    saved_location_label: undefined,
                    resolved_location: undefined,
                  })}
                >
                  Clear
                </Button>
              )}
            </div>
          </div>}

          {/* Conflict Warning */}
          {isConflict && (
            <div className="bg-destructive/5 border border-destructive/20 rounded-xl p-4 flex gap-3">
              <AlertCircle className="h-5 w-5 text-destructive flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm font-semibold text-destructive mb-1">
                  Time Conflict Detected
                </p>
                <p className="text-xs text-muted-foreground">
                  This activity overlaps with another scheduled event. You can still save it and adjust later.
                </p>
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="p-4 border-t border-border flex flex-wrap gap-3 sm:p-5">
          {onDelete && (
            <Button
              onClick={onDelete}
              variant="ghost"
              className="rounded-xl text-destructive hover:text-destructive hover:bg-destructive/10"
            >
              Delete
            </Button>
          )}
          <div className="flex-1" />
          <Button
            onClick={onCancel}
            variant="outline"
            className="rounded-xl"
          >
            Cancel
          </Button>
          <Button
            onClick={handleSave}
            className={`min-w-[136px] rounded-xl px-4 whitespace-nowrap ${isConflict ? "bg-destructive hover:bg-destructive/90" : ""}`}
            disabled={(!isBuffer && !formData.title.trim()) || (isFixedTime && (!formData.startTime || !formData.endTime))}
          >
            {isConflict ? "Save Anyway" : "Save Changes"}
          </Button>
        </div>
      </div>
      <LocationPickerDialog
        open={isLocationPickerOpen}
        onOpenChange={setIsLocationPickerOpen}
        title={`Pick location for ${formData.title || "this event"}`}
        description={`Search, choose a saved place, or click the exact point for ${formData.title || "this event"}.`}
        label={formData.title || "this event"}
        initialCenter={existingLocationPoint}
        initialPin={existingLocationPoint}
        candidates={existingLocationCandidate ? [existingLocationCandidate] : []}
        savedLocations={savedLocations}
        recentLocations={recentLocations}
        initialSearchQuery={formData.location || formData.title || ""}
        searchCategory={formData.location_category}
        confirmLabel="Use this point"
        onConfirm={handleConfirmPickedLocation}
      />
    </div>
  );
}

// Helper functions
function normalizeEventForEdit(event: ActivityBlock): ActivityBlock {
  const startTime = event.startTime || event.start || "";
  const endTime = event.endTime || event.end || "";
  const fixed = Boolean(
    event.is_user_fixed
    || event.user_fixed_start != null
    || event.fixed_start != null
    || String(event.timing_mode || event.original_timing_mode || "").toLowerCase() === "fixed"
  );

  return {
    ...event,
    startTime,
    endTime,
    start: startTime,
    end: endTime,
    timing_mode: fixed ? "fixed" : (event.timing_mode || (startTime ? "preferred" : "unspecified")),
    original_timing_mode: fixed ? "fixed" : (event.original_timing_mode || event.timing_mode || (startTime ? "preferred" : "unspecified")),
    duration: formatDurationMinutes(getCanonicalDurationMinutes({
      ...event,
      startTime,
      endTime,
    })),
    duration_minutes: getCanonicalDurationMinutes({
      ...event,
      startTime,
      endTime,
    }),
  };
}

function resolvedLocationToCandidate(event: ActivityBlock): GeocodeCandidate | null {
  const resolved = event.resolved_location;
  if (!resolved) return null;
  return {
    label: resolved.saved_location_label || resolved.label,
    display_name: resolved.display_name || event.location_label || event.location,
    address: resolved.address || resolved.display_name || event.location,
    latitude: resolved.latitude,
    longitude: resolved.longitude,
    source: resolved.source || event.location_source,
    confirmed_by_user: resolved.confirmed_by_user,
  };
}

function durationMinutesToParts(totalMinutes: number): { hours: number; minutes: number } {
  const total = Math.max(1, Math.round(totalMinutes || 60));
  return { hours: Math.floor(total / 60), minutes: total % 60 };
}

function clampDurationPart(value: string, max: number): string {
  const numeric = Math.max(0, Math.min(max, Math.floor(Number(value || 0))));
  return String(Number.isFinite(numeric) ? numeric : 0);
}

function convertTo24Hour(time12h: string): string {
  if (!time12h) return "";
  if (!time12h.includes("AM") && !time12h.includes("PM")) return time12h;
  const [time, modifier] = time12h.split(" ");
  let [hours, minutes] = time.split(":");
  if (hours === "12") hours = "00";
  if (modifier === "PM") hours = String(parseInt(hours, 10) + 12);
  return `${hours.padStart(2, '0')}:${minutes}`;
}

function convertTo12Hour(time24h: string): string {
  if (!time24h) return "";
  let [hours, minutes] = time24h.split(":");
  const hoursNum = parseInt(hours, 10);
  const modifier = hoursNum >= 12 ? "PM" : "AM";
  const hours12 = hoursNum % 12 || 12;
  return `${hours12}:${minutes} ${modifier}`;
}

function timeToMinutes(timeStr: string): number {
  if (!timeStr) return 0;
  let hours = 0;
  let minutes = 0;
  if (timeStr.includes("AM") || timeStr.includes("PM")) {
    const [time, period] = timeStr.split(" ");
    [hours, minutes] = time.split(":").map(Number);
    if (period === "PM" && hours !== 12) hours += 12;
    if (period === "AM" && hours === 12) hours = 0;
  } else {
    [hours, minutes] = timeStr.split(":").map(Number);
  }
  return hours * 60 + minutes;
}

function normalizeEndMinutes(startMinutes: number, endMinutes: number): number {
  return endMinutes < startMinutes ? endMinutes + 1440 : endMinutes;
}

function isBufferBlock(event: ActivityBlock): boolean {
  const blockType = String(event.block_type || event.type || "").toLowerCase();
  const title = String(event.title || "").trim().toLowerCase();
  return blockType === "buffer" || blockType === "prep_buffer" || title === "buffer" || title.includes("prep / buffer");
}

function minutesTo12Hour(totalMinutes: number): string {
  let hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  const modifier = hours >= 12 ? "PM" : "AM";
  hours = hours % 12 || 12;
  return `${hours}:${minutes.toString().padStart(2, '0')} ${modifier}`;
}
