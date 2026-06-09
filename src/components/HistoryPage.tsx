import { Button } from "./ui/button";
import { ArrowLeft, Calendar, Clock, Sparkles } from "lucide-react";
import type { DailySchedule } from "../App";

type HistoryPageProps = {
  schedules: DailySchedule[];
  onSelectSchedule: (schedule: DailySchedule) => void;
  onBack: () => void;
};

export function HistoryPage({ schedules, onSelectSchedule, onBack }: HistoryPageProps) {
  // Sort schedules by date (most recent first)
  const sortedSchedules = [...schedules].reverse();

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-4xl mx-auto px-4 py-5 sm:px-6 sm:py-8">
        <Button 
          variant="ghost" 
          onClick={onBack}
          className="mb-5 rounded-xl sm:mb-8"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Dashboard
        </Button>

        <div className="mb-5 sm:mb-8">
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="h-6 w-6 text-primary" />
            <h2>Schedule History</h2>
          </div>
          <p className="text-muted-foreground">
            View and retrieve your previous schedules
          </p>
        </div>

        {sortedSchedules.length > 0 ? (
          <div className="space-y-3">
            {sortedSchedules.map((schedule, index) => (
              <button
                key={index}
                onClick={() => onSelectSchedule(schedule)}
                className="w-full border border-border rounded-2xl p-4 bg-card hover:bg-secondary/30 hover:shadow-md transition-all text-left group sm:p-5"
              >
                <div className="flex items-start justify-between mb-4">
                  <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center group-hover:bg-primary/20 transition-colors sm:h-12 sm:w-12">
                      <Calendar className="h-6 w-6 text-primary" />
                    </div>
                    <div>
                      <h4>{schedule.date}</h4>
                      <p className="text-sm text-muted-foreground">
                        {schedule.activities.filter(a => a.type === "activity").length} activities
                      </p>
                    </div>
                  </div>
                </div>

                <div className="space-y-2 sm:pl-15">
                  {schedule.activities
                    .filter(a => a.type === "activity")
                    .slice(0, 3)
                    .map((activity) => (
                      <div 
                        key={activity.id} 
                        className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground bg-muted/30 rounded-lg px-3 py-2 sm:gap-3"
                      >
                        <Clock className="h-3.5 w-3.5" />
                        <span className="shrink-0">{activity.startTime}</span>
                        <span>•</span>
                        <span className="min-w-0 truncate">{activity.title}</span>
                      </div>
                    ))}
                  
                  {schedule.activities.filter(a => a.type === "activity").length > 3 && (
                    <p className="text-sm text-muted-foreground pl-3 pt-1">
                      + {schedule.activities.filter(a => a.type === "activity").length - 3} more activities
                    </p>
                  )}
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="text-center py-12 sm:py-16 border border-dashed border-border rounded-2xl bg-card">
            <div className="w-20 h-20 rounded-full bg-muted/50 flex items-center justify-center mx-auto mb-4">
              <Calendar className="h-10 w-10 text-muted-foreground" />
            </div>
            <p className="text-muted-foreground mb-2">
              No schedule history available yet.
            </p>
            <p className="text-sm text-muted-foreground">
              Your created schedules will appear here.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
