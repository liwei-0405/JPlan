import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { X, AlertCircle } from "lucide-react";
import type { ActivityBlock } from "../App";

type EventEditModalProps = {
  event: ActivityBlock;
  onSave: (updatedEvent: ActivityBlock) => void;
  onCancel: () => void;
  onDelete?: () => void;
  allActivities: ActivityBlock[];
};

export function EventEditModal({ event, onSave, onCancel, onDelete, allActivities }: EventEditModalProps) {
  const [title, setTitle] = useState(event.title);
  const [startTime, setStartTime] = useState(event.startTime);
  const [endTime, setEndTime] = useState(event.endTime);
  const [location, setLocation] = useState(event.location || "");
  const [duration, setDuration] = useState(event.duration || "");
  const [showConflictWarning, setShowConflictWarning] = useState(false);
  const [formData, setFormData] = useState({ ...event });
  const [isConflict, setIsConflict] = useState(false);

  useEffect(() => {
    const newStartMins = timeToMinutes(formData.startTime);
    const newEndMins = timeToMinutes(formData.endTime);
    const effectiveEnd = newEndMins < newStartMins ? newEndMins + 1440 : newEndMins;

    const hasCollision = allActivities.some(act => {
      if (act.id === event.id) return false; // 排除当前正在编辑的活动
      const s = timeToMinutes(act.startTime);
      let e = timeToMinutes(act.endTime);
      if (e < s) e += 1440;
      return (newStartMins < e) && (effectiveEnd > s);
    });

    setIsConflict(hasCollision);
  }, [formData.startTime, formData.endTime, allActivities]);

  const handleSave = () => {
    // Simple validation: check if times make sense
    const hasConflict = checkForPotentialConflict();

    if (hasConflict) {
      setShowConflictWarning(true);
      return;
    }

    const updatedEvent: ActivityBlock = {
      ...event,
      title,
      startTime,
      endTime,
      location: location || undefined,
      duration: duration || undefined,
    };

    onSave(updatedEvent);
  };

  const checkForPotentialConflict = (): boolean => {
    // Basic time validation
    if (!startTime || !endTime) return false;

    // You could add more sophisticated conflict detection here
    // For now, just a simple placeholder
    return false;
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

  const handleSaveAnyway = () => {
    const updatedEvent: ActivityBlock = {
      ...event,
      title,
      startTime,
      endTime,
      location: location || undefined,
      duration: duration || undefined,
    };
    onSave(updatedEvent);
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
              value={title}
              onChange={(e) => setTitle(e.target.value)}
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
            <Label htmlFor="location">Location (optional)</Label>
            <Input
              id="location"
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              placeholder="e.g., Downtown Office"
              className="mt-1.5 rounded-xl"
            />
          </div>

          {/* Conflict Warning */}
          {showConflictWarning && (
            <div className="bg-yellow-50 border border-yellow-200 rounded-xl p-4 flex gap-3">
              <AlertCircle className="h-5 w-5 text-yellow-600 flex-shrink-0 mt-0.5" />
              <div className="flex-1">
                <p className="text-sm mb-2">
                  This change may affect schedule feasibility.
                </p>
                <p className="text-sm text-muted-foreground mb-3">
                  You can adjust it manually or replan using the assistant.
                </p>
                <div className="flex gap-2">
                  <Button
                    onClick={handleSaveAnyway}
                    variant="outline"
                    size="sm"
                    className="rounded-lg"
                  >
                    Save Anyway
                  </Button>
                  <Button
                    onClick={() => setShowConflictWarning(false)}
                    variant="ghost"
                    size="sm"
                    className="rounded-lg"
                  >
                    Cancel
                  </Button>
                </div>
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
            onClick={() => onSave(formData)}
            className={`rounded-xl ${isConflict ? "bg-destructive hover:bg-destructive/90" : ""}`}
            disabled={!title.trim() || !formData.startTime || !formData.endTime || isConflict}
          >
            {isConflict ? "Conflict Detected" : "Save Changes"}
          </Button>
        </div>
      </div>
    </div>
  );
}

// Helper functions to convert between 12-hour and 24-hour formats
function convertTo24Hour(time12h: string): string {
  if (!time12h) return "";

  const [time, modifier] = time12h.split(" ");
  let [hours, minutes] = time.split(":");

  if (hours === "12") {
    hours = "00";
  }

  if (modifier === "PM") {
    hours = String(parseInt(hours, 10) + 12);
  }

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