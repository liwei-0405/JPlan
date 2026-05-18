import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { X, AlertCircle, MapPin } from "lucide-react";
import type { ActivityBlock } from "../App";
import { LocationPickerDialog, candidateToMapPoint } from "./LocationPickerDialog";
import type { GeocodeCandidate, SavedLocation } from "../services/planService";

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

  useEffect(() => {
    setFormData(normalizeEventForEdit(event));
  }, [event]);

  useEffect(() => {
    const newStartMins = timeToMinutes(formData.startTime);
    const newEndMins = timeToMinutes(formData.endTime);
    // Handle overnight normalization for conflict check
    const effectiveEnd = newEndMins < newStartMins ? newEndMins + 1440 : newEndMins;

    const hasCollision = allActivities.some(act => {
      if (act.id === event.id) return false; // Skip the current event being edited
      const s = timeToMinutes(act.startTime || act.start || "");
      let e = timeToMinutes(act.endTime || act.end || "");
      if (e < s) e += 1440;
      return (newStartMins < e) && (effectiveEnd > s);
    });

    setIsConflict(hasCollision);
  }, [formData.startTime, formData.endTime, allActivities, event.id]);

  const handleSave = () => {
    const confirmedLocationName = formData.resolved_location?.display_name || formData.resolved_location?.address;
    onSave({
      ...formData,
      title: formData.title.trim(),
      location: confirmedLocationName || formData.location?.trim() || undefined,
      location_label: confirmedLocationName || formData.location_label || formData.location,
      location_status: formData.resolved_location ? "resolved" : formData.location_status,
      start: formData.startTime,
      end: formData.endTime,
    });
  };

  const handleEndTimeChange = (newEndTime12h: string) => {
    const startMins = timeToMinutes(formData.startTime);
    const endMins = timeToMinutes(newEndTime12h);
    let diff = endMins - startMins;
    if (diff < 0) diff += 1440;

    const h = Math.floor(diff / 60);
    const m = diff % 60;
    const durStr = `${h > 0 ? h + 'h ' : ''}${m > 0 ? m + 'm' : ''}` || '0m';

    setFormData({ ...formData, endTime: newEndTime12h, duration: durStr });
  };

  const handleStartTimeChange = (newStartTime12h: string) => {
    const startMins = timeToMinutes(newStartTime12h);
    const endMins = timeToMinutes(formData.endTime);
    let diff = endMins - startMins;
    if (diff < 0) diff += 1440;

    const h = Math.floor(diff / 60);
    const m = diff % 60;
    const durStr = `${h > 0 ? h + 'h ' : ''}${m > 0 ? m + 'm' : ''}` || '0m';

    setFormData({ ...formData, startTime: newStartTime12h, duration: durStr });
  };

  const handleDurationChange = (durStr: string) => {
    let totalMins = 0;
    const hourMatch = durStr.match(/(\d+)h/);
    const minMatch = durStr.match(/(\d+)m/);
    if (hourMatch) totalMins += parseInt(hourMatch[1]) * 60;
    if (minMatch) totalMins += parseInt(minMatch[1]);
    if (!hourMatch && !minMatch) totalMins = parseFloat(durStr) * 60 || 0;

    const startMins = timeToMinutes(formData.startTime);
    const newEndMins = (startMins + totalMins) % 1440;

    const formattedEnd = minutesTo12Hour(newEndMins);
    setFormData({ ...formData, duration: durStr, endTime: formattedEnd });
  };

  const existingLocationCandidate = resolvedLocationToCandidate(formData);
  const existingLocationPoint = candidateToMapPoint(existingLocationCandidate);

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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
      <div className="bg-card rounded-2xl border border-border shadow-xl max-w-md w-full max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between p-5 border-b border-border">
          <div>
            <h3>Manual Edit</h3>
            <p className="text-sm text-muted-foreground mt-1">
              Edit activity details directly
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
        <div className="p-5 space-y-4">
          {/* Activity Title */}
          <div>
            <Label htmlFor="title">Activity Title</Label>
            <Input
              id="title"
              value={formData.title}
              onChange={(e) => setFormData({ ...formData, title: e.target.value })}
              placeholder="Activity name"
              className="mt-1.5 rounded-xl"
            />
          </div>

          {/* Time Fields */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <Label htmlFor="start-time">Start Time</Label>
              <Input
                id="start-time"
                type="time"
                value={convertTo24Hour(formData.startTime)}
                onChange={(e) => handleStartTimeChange(convertTo12Hour(e.target.value))}
                className="mt-1.5 rounded-xl"
              />
            </div>
            <div>
              <Label htmlFor="end-time">End Time</Label>
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
            <Label htmlFor="duration">Duration (optional)</Label>
            <Input
              id="duration"
              value={formData.duration}
              onChange={(e) => handleDurationChange(e.target.value)}
              placeholder="e.g., 2 hours"
              className="mt-1.5 rounded-xl"
            />
          </div>

          {/* Location */}
          <div>
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
            <div className="mt-2 flex gap-2">
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
          </div>

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
        <div className="p-5 border-t border-border flex gap-3">
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
            className={`rounded-xl px-8 ${isConflict ? "bg-destructive hover:bg-destructive/90" : ""}`}
            disabled={!formData.title.trim() || !formData.startTime || !formData.endTime}
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

  return {
    ...event,
    startTime,
    endTime,
    start: startTime,
    end: endTime,
    duration: event.duration || deriveDuration(startTime, endTime),
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

function deriveDuration(startTime: string, endTime: string): string {
  if (!startTime || !endTime) return "";

  const startMins = timeToMinutes(startTime);
  const endMins = timeToMinutes(endTime);
  let diff = endMins - startMins;
  if (diff < 0) diff += 1440;

  const hours = Math.floor(diff / 60);
  const minutes = diff % 60;

  if (hours === 0 && minutes === 0) return "0m";
  if (hours === 0) return `${minutes}m`;
  if (minutes === 0) return `${hours}h`;
  return `${hours}h ${minutes}m`;
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

function minutesTo12Hour(totalMinutes: number): string {
  let hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  const modifier = hours >= 12 ? "PM" : "AM";
  hours = hours % 12 || 12;
  return `${hours}:${minutes.toString().padStart(2, '0')} ${modifier}`;
}
