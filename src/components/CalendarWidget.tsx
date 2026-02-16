import { ChevronLeft, ChevronRight } from "lucide-react";
import { Button } from "./ui/button";
import { useState } from "react";

type CalendarWidgetProps = {
  scheduleDates: string[]; // Array of dates that have schedules
  onDateSelect: (date: Date) => void;
  selectedDate?: Date;
};

export function CalendarWidget({ scheduleDates, onDateSelect, selectedDate }: CalendarWidgetProps) {
  const [currentMonth, setCurrentMonth] = useState(new Date());
  
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

  const hasScheduleOnDate = (day: number): boolean => {
    const date = new Date(year, month, day);
    const dateStr = date.toLocaleDateString('en-US', { 
      weekday: 'long', 
      day: 'numeric', 
      month: 'long', 
      year: 'numeric' 
    });
    return scheduleDates.includes(dateStr);
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

  const handlePrevMonth = () => {
    setCurrentMonth(new Date(year, month - 1, 1));
  };

  const handleNextMonth = () => {
    setCurrentMonth(new Date(year, month + 1, 1));
  };

  const handleDayClick = (day: number) => {
    const date = new Date(year, month, day);
    onDateSelect(date);
  };

  return (
    <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
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
      <div className="grid grid-cols-7 gap-2 mb-2">
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
      <div className="grid grid-cols-7 gap-2">
        {calendarDays.map((day, index) => (
          <div key={index} className="aspect-square">
            {day !== null ? (
              <button
                onClick={() => handleDayClick(day)}
                className={`
                  w-full h-full rounded-xl flex flex-col items-center justify-center
                  transition-all duration-200 relative
                  ${isToday(day) 
                    ? 'bg-primary text-primary-foreground shadow-md ring-2 ring-primary/20' 
                    : isSelected(day)
                    ? 'bg-secondary text-secondary-foreground'
                    : 'hover:bg-muted'
                  }
                `}
              >
                <span className="text-sm">{day}</span>
                {hasScheduleOnDate(day) && (
                  <div className={`
                    w-1.5 h-1.5 rounded-full mt-0.5
                    ${isToday(day) ? 'bg-white' : 'bg-primary'}
                  `} />
                )}
              </button>
            ) : (
              <div />
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
