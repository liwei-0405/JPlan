import { Button } from "./ui/button";
import { ArrowLeft, Clock, MapPin, History, Settings, Sparkles, Edit2 } from "lucide-react";
import type { DailySchedule, ActivityBlock } from "../App";

type ScheduleViewPageProps = {
  schedule: DailySchedule;
  onModify: () => void;
  onViewExplanation: () => void;
  onSave: () => void;
  onBack: () => void;
  onViewHistory: () => void;
  onViewPreferences: () => void;
  onUpdateSchedule: (updatedSchedule: DailySchedule) => void;
};

export function ScheduleViewPage({
  schedule,
  onModify,
  onViewExplanation,
  onSave,
  onBack,
  onViewHistory,
  onViewPreferences,
  onUpdateSchedule
}: ScheduleViewPageProps) {

  // Check if the schedule date is in the past
  const scheduleDate = new Date(schedule.date);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const isPastDate = scheduleDate < today;

  const handleEventClick = (event: ActivityBlock) => {
    if (event.type === "activity") {
      onModify();
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-4xl mx-auto px-6 py-8">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <Button
            variant="ghost"
            onClick={onBack}
            className="rounded-xl"
          >
            <ArrowLeft className="mr-2 h-4 w-4" />
            Back to Dashboard
          </Button>

          <div className="flex gap-2">
            <Button
              variant="ghost"
              size="icon"
              onClick={onViewHistory}
              title="View history"
              className="rounded-xl"
            >
              <History className="h-5 w-5" />
            </Button>
            <Button
              variant="ghost"
              size="icon"
              onClick={onViewPreferences}
              title="Preferences"
              className="rounded-xl"
            >
              <Settings className="h-5 w-5" />
            </Button>
          </div>
        </div>

        {/* Date Header */}
        <div className="mb-8 bg-card rounded-2xl border border-border p-6 shadow-sm">
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="h-5 w-5 text-primary" />
            <h2>Your Daily Schedule</h2>
          </div>
          <p className="text-muted-foreground">{schedule.date}</p>
        </div>

        {/* Action Buttons */}
        <div className="flex gap-3 mb-6">
          {!isPastDate && (
            <Button onClick={onModify} variant="outline" className="rounded-xl gap-2">
              <Sparkles className="h-4 w-4" />
              Replan with Assistant
            </Button>
          )}
          <Button onClick={onViewExplanation} variant="outline" className="rounded-xl">
            View Explanation
          </Button>
        </div>

        {/* Timeline */}
        <div className="space-y-3">
          {schedule.activities.map((activity, index) => (
            <div key={activity.id}>
              {activity.type === "activity" ? (
                <button
                  onClick={() => !isPastDate && handleEventClick(activity)}
                  className={`w-full bg-card border border-border rounded-2xl p-5 shadow-sm transition-all text-left group ${!isPastDate ? 'hover:shadow-md hover:border-primary/30 cursor-pointer' : 'cursor-default'
                    }`}
                >
                  <div className="flex items-start justify-between mb-3">
                    <h4>{activity.title}</h4>
                    <div className="flex items-center gap-2">
                      {activity.duration && (
                        <span className="text-sm px-3 py-1 rounded-full bg-secondary text-secondary-foreground">
                          {activity.duration}
                        </span>
                      )}
                      {!isPastDate && (
                        <div className="opacity-0 group-hover:opacity-100 transition-opacity">
                          <Edit2 className="h-4 w-4 text-primary" />
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="flex items-center gap-4 text-sm text-muted-foreground">
                    <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-muted/50">
                      <Clock className="h-4 w-4" />
                      <span>{activity.startTime} – {activity.endTime}</span>
                    </div>

                    {activity.location && (
                      <div className="flex items-center gap-1.5 px-3 py-1.5 rounded-full bg-muted/50">
                        <MapPin className="h-4 w-4" />
                        <span>{activity.location}</span>
                      </div>
                    )}
                  </div>
                </button>
              ) : (
                <div className={`border-l-4 rounded-xl p-4 shadow-sm bg-gradient-to-r ${activity.type === "travel"
                  ? "from-indigo-50 to-white border-indigo-500"
                  : "from-emerald-50 to-white border-emerald-500"
                  }`}>
                  <div className="flex items-center justify-between">
                    <span className={`flex items-center gap-2 font-bold text-base ${activity.type === "travel" ? "text-indigo-800" : "text-emerald-800"
                      }`}>
                      <div className={`w-2.5 h-2.5 rounded-full ${activity.type === "travel" ? "bg-indigo-500" : "bg-emerald-500"
                        }`} />
                      {activity.title}
                    </span>
                    <span className={`text-sm font-semibold ${activity.type === "travel" ? "text-indigo-600" : "text-emerald-600"
                      }`}>
                      {activity.startTime} – {activity.endTime}
                    </span>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>

        {schedule.activities.length === 0 && (
          <div className="text-center py-12 bg-card rounded-2xl border border-dashed border-border">
            <p className="text-muted-foreground">
              No schedule has been created for this day.
            </p>
          </div>
        )}

        {/* Hint */}
        {!isPastDate && (
          <div className="mt-6 bg-gradient-to-br from-[#d4e5ff]/20 to-[#d4e5ff]/5 rounded-2xl p-4 border border-[#d4e5ff]/30">
            <p className="text-sm text-muted-foreground">
              💡 Click any activity to edit it manually, or use "Replan with Assistant" for AI-powered changes
            </p>
          </div>
        )}
      </div>
    </div>
  );
}