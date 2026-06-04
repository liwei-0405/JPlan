import { Button } from "./ui/button";
import { TopNav } from "./TopNav";
import { CalendarWidget } from "./CalendarWidget";
import { Clock, MapPin, Sparkles, Calendar as CalendarIcon } from "lucide-react";
import type { DailySchedule } from "../App";
import { useState } from "react";
import { TimelineGrid } from "./TimelineGrid";
import { getBlocksForView } from "../utils/scheduleDisplayUtils";

type EntryPageProps = {
  onStartPlanning: (date: Date) => void;
  onViewSchedule: (schedule: DailySchedule) => void;
  onReplanToday: () => void;
  onSettingsClick: () => void;
  todaySchedule: DailySchedule | null;
  scheduleHistory: DailySchedule[];
  onSyncComplete?: () => void;
};

export function EntryPage({
  onStartPlanning,
  onViewSchedule,
  onReplanToday,
  onSettingsClick,
  todaySchedule,
  scheduleHistory,
  onSyncComplete
}: EntryPageProps) {
  const [selectedDate, setSelectedDate] = useState<Date>(new Date());

  const scheduleDates = scheduleHistory.map(s => s.date);

  const handleDateSelect = (date: Date) => {
    setSelectedDate(date);
  };

  const hasTodayPlan = todaySchedule !== null;

  // Get the schedule for the selected date
  const formatDateToISO = (date: Date) => {
    const yyyy = date.getFullYear();
    const mm = String(date.getMonth() + 1).padStart(2, '0');
    const dd = String(date.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
  };

  const selectedDateStr = formatDateToISO(selectedDate);
  const selectedSchedule = scheduleHistory.find(s => s.date === selectedDateStr);
  const isToday = selectedDateStr === formatDateToISO(new Date());
  const hasSelectedPlan = selectedSchedule !== undefined;

  // Check if selected date is in the past
  const isPastDate = selectedDate < new Date(new Date().setHours(0, 0, 0, 0));

  return (
    <div className="min-h-screen bg-background">
      <TopNav 
        onSettingsClick={onSettingsClick} 
        onSyncComplete={onSyncComplete} 
        syncDate={selectedDateStr}
      />

      <div className="max-w-6xl mx-auto px-6 py-8">
        {/* Header */}
        <div className="mb-8">
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="h-6 w-6 text-primary" />
            <h1>Welcome to JPlan</h1>
          </div>
          <p className="text-muted-foreground">
            Your feasibility-aware daily planning assistant
          </p>
        </div>

        <div className="grid lg:grid-cols-2 gap-6" >
          {/* Calendar */}
          <div>
            <CalendarWidget
              scheduleDates={scheduleDates}
              onDateSelect={handleDateSelect}
              selectedDate={selectedDate}
            />
          </div>

          {/* Selected Date's Plan Preview */}
          <div className="h-[500px]" style={{ height: "40.85vw", maxHeight:"475px" }}>
            <div
              onClick={
                hasSelectedPlan 
                  ? () => selectedSchedule && onViewSchedule(selectedSchedule) 
                  : (!isPastDate ? () => onStartPlanning(selectedDate) : undefined)
              }
              role={hasSelectedPlan || !isPastDate ? "button" : undefined}
              tabIndex={hasSelectedPlan || !isPastDate ? 0 : undefined}
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && (hasSelectedPlan || !isPastDate)) {
                  e.preventDefault();
                  hasSelectedPlan ? selectedSchedule && onViewSchedule(selectedSchedule) : onStartPlanning(selectedDate);
                }
              }}
              className={`bg-card rounded-2xl border border-border p-6 shadow-sm h-full flex flex-col w-full text-left transition-all ${
                hasSelectedPlan || !isPastDate 
                  ? "hover:shadow-md hover:border-primary/30 cursor-pointer group" 
                  : "cursor-default"
              }`}
            >
              <div className="flex items-center gap-2 mb-4 shrink-0">
                <CalendarIcon className="h-5 w-5 text-primary" />
                <h3 className="font-semibold">{isToday ? "Today's Plan" : `Plan for ${selectedDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`}</h3>
                <div className={`ml-auto opacity-0 ${hasSelectedPlan || !isPastDate ? "group-hover:opacity-100" : ""} transition-opacity text-xs text-muted-foreground`}>
                  {hasSelectedPlan ? 'Click to view' : (!isPastDate ? 'Click to plan' : '')}
                </div>
              </div>

              {hasSelectedPlan ? (
                <div className="flex-1 overflow-y-auto pr-2 mb-4 -mr-2 custom-scrollbar">
                  <TimelineGrid
                    activities={getBlocksForView(selectedSchedule, "jplan")}
                    compact={true}
                  />
                </div>
              ) : (
                <div className="flex-1 flex items-center justify-center">
                  <div className="text-center py-8">
                    <div className="w-16 h-16 rounded-full bg-muted/50 flex items-center justify-center mx-auto mb-3">
                      <CalendarIcon className="h-8 w-8 text-muted-foreground" />
                    </div>
                    <p className="text-muted-foreground mb-2">
                      No schedule for {isToday ? 'today' : 'this date'}.
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {isPastDate ? "This date is in the past." : "Start planning to organize your day!"}
                    </p>
                  </div>
                </div>
              )}

              {/* Action Buttons */}
              <div className="space-y-2.5" onClick={(e) => e.stopPropagation()}>
                {!hasSelectedPlan ? (
                  !isPastDate ? (
                    <Button
                      onClick={() => onStartPlanning(selectedDate)}
                      className="w-full rounded-xl shadow-sm"
                      size="lg"
                    >
                      <Sparkles className="mr-2 h-4 w-4" />
                      Start Planning
                    </Button>
                  ) : null
                ) : (
                  <>
                    <Button
                      onClick={() => selectedSchedule && onViewSchedule(selectedSchedule)}
                      className="w-full rounded-xl shadow-sm"
                      size="lg"
                    >
                      View Full Schedule
                    </Button>
                    {(!isPastDate) && (
                      <Button
                        onClick={() => onStartPlanning(selectedDate)}
                        variant="outline"
                        className="w-full rounded-xl gap-2"
                        size="lg"
                      >
                        <Sparkles className="h-4 w-4" />
                        Replan with Assistant
                      </Button>
                    )}
                  </>
                )}
              </div>
            </div>
          </div>
        </div>

        {/* Quick Stats */}
        {scheduleHistory.length > 0 && (
          <div className="mt-6 grid grid-cols-3 gap-4">
            <div className="bg-gradient-to-br from-[#ffd4e5]/20 to-[#ffd4e5]/5 rounded-xl p-4 border border-[#ffd4e5]/30">
              <p className="text-sm text-muted-foreground mb-1">Total Plans</p>
              <p className="text-2xl">{scheduleHistory.length}</p>
            </div>
            <div className="bg-gradient-to-br from-[#d4e5ff]/20 to-[#d4e5ff]/5 rounded-xl p-4 border border-[#d4e5ff]/30">
              <p className="text-sm text-muted-foreground mb-1">This Week</p>
              <p className="text-2xl">{Math.min(scheduleHistory.length, 5)}</p>
            </div>
            <div className="bg-gradient-to-br from-[#fff9d4]/20 to-[#fff9d4]/5 rounded-xl p-4 border border-[#fff9d4]/30">
              <p className="text-sm text-muted-foreground mb-1">Success Rate</p>
              <p className="text-2xl">98%</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
