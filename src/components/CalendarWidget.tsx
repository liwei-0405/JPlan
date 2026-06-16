import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "./ui/button";
import { useState } from "react";

export type CalendarPlanStatus =
  | "optimized"
  | "needs_locations"
  | "not_reoptimized"
  | "route_warning"
  | "partial"
  | "google_only"
  | "saved";

type CalendarWidgetProps = {
  scheduleDates: string[]; // ISO dates that have schedules
  scheduleStatusByDate?: Record<string, CalendarPlanStatus>;
  onDateSelect: (date: Date) => void;
  selectedDate?: Date;
};

export const calendarPlanStatusMeta: Record<CalendarPlanStatus, { label: string; className: string }> = {
  optimized: { label: "Optimized", className: "calendar-plan-optimized" },
  needs_locations: { label: "Needs locations", className: "calendar-plan-needs-locations" },
  not_reoptimized: { label: "Not re-optimized", className: "calendar-plan-not-reoptimized" },
  route_warning: { label: "Route warning", className: "calendar-plan-route-warning" },
  partial: { label: "Partially fit", className: "calendar-plan-partial" },
  google_only: { label: "Google only", className: "calendar-plan-google-only" },
  saved: { label: "Saved", className: "calendar-plan-saved" },
};

function formatDateToISO(date: Date): string {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

export function CalendarWidget({ scheduleDates, scheduleStatusByDate = {}, onDateSelect, selectedDate }: CalendarWidgetProps) {
  const [currentMonth, setCurrentMonth] = useState(new Date());
  const scheduleDateSet = new Set(scheduleDates);
  
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  // Get days in month
  const year = currentMonth.getFullYear();
  const month = currentMonth.getMonth();
  const firstDay = new Date(year, month, 1);
  const lastDay = new Date(year, month + 1, 0);
  const daysInMonth = lastDay.getDate();
  const startingDayOfWeek = firstDay.getDay();

  const monthNames = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];

  const dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  // Generate calendar days
  const calendarDays: (number | null)[] = [];
  
  // Add empty cells for days before month starts
  for (let i = 0; i < startingDayOfWeek; i++) {
    calendarDays.push(null);
  }
  
  // Add days of month
  for (let day = 1; day <= daysInMonth; day++) {
    calendarDays.push(day);
  }
  while (calendarDays.length < 42) {
    calendarDays.push(null);
  }

  const dateKeyForDay = (day: number): string => {
    return formatDateToISO(new Date(year, month, day));
  };

  const planStatusForDate = (day: number): CalendarPlanStatus | null => {
    const dateKey = dateKeyForDay(day);
    if (scheduleStatusByDate[dateKey]) return scheduleStatusByDate[dateKey];
    return scheduleDateSet.has(dateKey) ? "saved" : null;
  };

  const isToday = (day: number): boolean => {
    const date = new Date(year, month, day);
    return date.getTime() === today.getTime();
  };

  const isSelected = (day: number): boolean => {
    if (!selectedDate) return false;
    const date = new Date(year, month, day);
    return date.getTime() === selectedDate.getTime();
  };

  const moveToMonth = (targetMonth: Date) => {
    const targetYear = targetMonth.getFullYear();
    const targetMonthIndex = targetMonth.getMonth();
    const firstOfMonth = new Date(targetYear, targetMonthIndex, 1);
    const selectedDay = targetYear === today.getFullYear() && targetMonthIndex === today.getMonth()
      ? new Date(today)
      : firstOfMonth;
    setCurrentMonth(firstOfMonth);
    onDateSelect(selectedDay);
  };

  const handlePrevMonth = () => {
    moveToMonth(new Date(year, month - 1, 1));
  };

  const handleNextMonth = () => {
    moveToMonth(new Date(year, month + 1, 1));
  };

  const handleDayClick = (day: number) => {
    const date = new Date(year, month, day);
    onDateSelect(date);
  };

  return (
    <div className="jplan-calendar-card bg-card rounded-2xl border border-border p-4 shadow-sm sm:p-5">
      {/* Month Header */}
      <div className="flex items-center justify-between mb-4">
        <h3>{monthNames[month]} {year}</h3>
        <div className="flex gap-1">
          <Button 
            variant="ghost" 
            size="icon"
            className="h-8 w-8"
            onClick={handlePrevMonth}
          >
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <Button 
            variant="ghost" 
            size="icon"
            className="h-8 w-8"
            onClick={handleNextMonth}
          >
            <ChevronRight className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Day Names */}
      <div className="grid grid-cols-7 gap-1.5 mb-2 sm:gap-2">
        {dayNames.map(day => (
          <div 
            key={day} 
            className="text-center text-xs text-muted-foreground py-1"
          >
            {day}
          </div>
        ))}
      </div>

      {/* Calendar Grid */}
      <div className="jplan-calendar-grid grid grid-cols-7 gap-1.5 sm:gap-2">
        {calendarDays.map((day, index) => {
          const planStatus = day !== null ? planStatusForDate(day) : null;
          const meta = planStatus ? calendarPlanStatusMeta[planStatus] : null;
          const selected = day !== null && isSelected(day);
          const todayDate = day !== null && isToday(day);
          return (
            <div key={index} className="jplan-calendar-day-cell">
              {day !== null ? (
              <button
                onClick={() => handleDayClick(day)}
                title={meta ? `${dateKeyForDay(day)} · ${meta.label}` : dateKeyForDay(day)}
                aria-label={meta ? `${dateKeyForDay(day)}, ${meta.label}` : dateKeyForDay(day)}
                className={`
                  w-full h-full rounded-lg sm:rounded-xl flex flex-col items-center justify-center
                  transition-all duration-200 relative calendar-day-button
                  ${meta && !selected && !todayDate ? meta.className : ""}
                  ${todayDate 
                    ? 'bg-primary text-primary-foreground shadow-md ring-2 ring-primary/20' 
                    : selected
                    ? 'bg-secondary text-secondary-foreground'
                    : 'hover:bg-muted'
                  }
                `}
              >
                <span className="text-xs sm:text-sm">{day}</span>
                {meta && (
                  <span
                    className={`calendar-status-dot mt-0.5 ${meta.className} ${todayDate ? "calendar-status-dot-on-primary" : ""}`}
                    aria-hidden="true"
                  />
                )}
              </button>
              ) : (
              <div />
              )}
            </div>
          );
        })}
      </div>

    </div>
  );
}
