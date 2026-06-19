import { Button } from "./ui/button";
import { Clock, MapPin, CheckCircle, Bot } from "lucide-react";
import type { DailySchedule } from "../App";
import { formatActivityDuration } from "../utils/durationUtils";

type SchedulePreviewProps = {
  schedule: DailySchedule;
  onAccept: () => void;
  onReject: () => void;
};

export function SchedulePreview({ schedule, onAccept, onReject }: SchedulePreviewProps) {
  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4 overflow-y-auto">
      <div className="bg-card rounded-2xl border border-border shadow-xl max-w-2xl w-full my-8">
        {/* Header */}
        <div className="p-6 border-b border-border">
          <div className="flex items-center gap-3 mb-2">
            <div className="w-12 h-12 rounded-full bg-primary/10 flex items-center justify-center">
              <Bot className="h-6 w-6 text-primary" />
            </div>
            <div>
              <h3>Assistant Suggestion</h3>
              <p className="text-sm text-muted-foreground">
                Review your generated schedule
              </p>
            </div>
          </div>
        </div>

        {/* Schedule Content */}
        <div className="p-6 max-h-[60vh] overflow-y-auto">
          <div className="mb-4">
            <h4 className="mb-2">{schedule.date}</h4>
            <p className="text-sm text-muted-foreground">
              {schedule.activities.filter(a => a.type === "activity").length} activities scheduled
            </p>
          </div>

          <div className="space-y-3">
            {schedule.activities.map((activity) => (
              <div key={activity.id}>
                {activity.type === "activity" ? (
                  <div className="bg-secondary/30 border border-secondary rounded-xl p-4">
                    <div className="flex items-start justify-between mb-2">
                      <h4 className="text-sm">{activity.title}</h4>
                      {(activity.duration || activity.duration_minutes) && (
                        <span className="text-xs px-2 py-1 rounded-full bg-background">
                          {formatActivityDuration(activity)}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-3 text-xs text-muted-foreground">
                      <div className="flex items-center gap-1">
                        <Clock className="h-3.5 w-3.5" />
                        <span>{activity.startTime} – {activity.endTime}</span>
                      </div>
                      {activity.location && (
                        <div className="flex items-center gap-1">
                          <MapPin className="h-3.5 w-3.5" />
                          <span>{activity.location}</span>
                        </div>
                      )}
                    </div>
                  </div>
                ) : (
                  <div className="border border-border/50 rounded-lg p-2 bg-muted/20">
                    <div className="flex items-center justify-between text-xs text-muted-foreground">
                      <span className="flex items-center gap-2">
                        <div className="w-1.5 h-1.5 rounded-full bg-muted-foreground/40" />
                        {activity.title}
                      </span>
                      <span>{activity.startTime} – {activity.endTime}</span>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="p-6 border-t border-border bg-gradient-to-br from-[#d4e5ff]/10 to-transparent">
          <p className="text-sm text-muted-foreground mb-4">
            The assistant has created this schedule based on your input. Review it and choose to accept or make changes.
          </p>
          <div className="flex gap-3">
            <Button 
              onClick={onReject}
              variant="outline"
              className="flex-1 rounded-xl"
            >
              Make Changes
            </Button>
            <Button 
              onClick={onAccept}
              className="flex-1 rounded-xl gap-2"
            >
              <CheckCircle className="h-4 w-4" />
              Accept Schedule
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
