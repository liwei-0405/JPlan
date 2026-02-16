import { useState, useEffect } from "react";
import { EntryPage } from "./components/EntryPage";
import { PlanningInputPage } from "./components/PlanningInputPage";
import { ScheduleViewPage } from "./components/ScheduleViewPage";
import { ExplanationPanel } from "./components/ExplanationPanel";
import { HistoryPage } from "./components/HistoryPage";
import { PreferencesPage } from "./components/PreferencesPage";
import { getAllPlans, savePlan as savePlanToDb } from "./services/planService";

export type Page =
  | "entry"
  | "planning"
  | "processing"
  | "schedule"
  | "explanation"
  | "modification"
  | "history"
  | "preferences";

export type ActivityBlock = {
  id: string;
  type: "activity" | "travel" | "buffer";
  title: string;
  startTime: string;
  endTime: string;
  location?: string;
  duration?: string;
};

export type DailySchedule = {
  date: string;
  activities: ActivityBlock[];
};

const formatToISODate = (date: Date) => {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
};

export default function App() {
  const [currentPage, setCurrentPage] = useState<Page>("entry");
  const [previousPage, setPreviousPage] = useState<Page>("entry");
  const [currentSchedule, setCurrentSchedule] = useState<DailySchedule | null>(null);
  const [planningSchedule, setPlanningSchedule] = useState<DailySchedule | null>(null);
  const [scheduleHistory, setScheduleHistory] = useState<DailySchedule[]>([]);
  const [planningDate, setPlanningDate] = useState<Date>(new Date());
  const [isLoadingPlans, setIsLoadingPlans] = useState(true);

  const navigateTo = (page: Page) => {
    setCurrentPage(page);
  };

  // Load all plans from Supabase on mount
  useEffect(() => {
    const loadPlans = async () => {
      setIsLoadingPlans(true);
      try {
        const plans = await getAllPlans();
        setScheduleHistory(plans);
      } catch (error) {
        console.error('Failed to load plans:', error);
      } finally {
        setIsLoadingPlans(false);
      }
    };

    loadPlans();
  }, []);

  const saveSchedule = async (schedule: DailySchedule) => {
    setCurrentSchedule(schedule);

    // Save to Supabase
    const result = await savePlanToDb(schedule);
    if (!result.success) {
      console.error('Failed to save plan to database:', result.error);
      // Still update local state even if database save fails
    }

    // Add to history if not already there
    const existingIndex = scheduleHistory.findIndex(s => s.date === schedule.date);
    if (existingIndex >= 0) {
      const updated = [...scheduleHistory];
      updated[existingIndex] = schedule;
      setScheduleHistory(updated);
    } else {
      setScheduleHistory([...scheduleHistory, schedule]);
    }
  };

  // Get today's schedule
  const getTodaySchedule = (): DailySchedule | null => {
    const todayStr = formatToISODate(new Date());
    return scheduleHistory.find(s => s.date === todayStr) || null;
  };

  const todaySchedule = getTodaySchedule();

  return (
    <div className="min-h-screen bg-background">
      {currentPage === "entry" && (
        <EntryPage
          onStartPlanning={(date: Date) => {
            setPlanningDate(date);
            setPlanningSchedule(null); // Reset planning state
            setPreviousPage("entry");
            navigateTo("planning");
          }}
          onViewSchedule={(schedule: DailySchedule) => {
            setCurrentSchedule(schedule);
            navigateTo("schedule");
          }}
          onReplanToday={() => {
            if (todaySchedule) {
              setPlanningSchedule(todaySchedule);
              setPlanningDate(new Date());
              setPreviousPage("entry");
              navigateTo("planning");
            }
          }}
          onSettingsClick={() => navigateTo("preferences")}
          todaySchedule={todaySchedule}
          scheduleHistory={scheduleHistory}
        />
      )}

      {currentPage === "planning" && (
        <PlanningInputPage
          // add selected date to planning input page
          selectedDate={planningDate}
          initialSchedule={planningSchedule}
          onUpdateSchedule={setPlanningSchedule}
          onScheduleGenerated={(schedule) => {
            saveSchedule(schedule);
            navigateTo("entry");
          }}
          onBack={() => navigateTo(previousPage)}
          onViewExplanation={(schedule) => {
            setCurrentSchedule(schedule);
            setPreviousPage("planning");
            navigateTo("explanation");
          }}
        />
      )}

      {currentPage === "schedule" && currentSchedule && (
        <ScheduleViewPage
          schedule={currentSchedule}
          onModify={() => {
            setPlanningSchedule(currentSchedule);
            setPlanningDate(new Date(currentSchedule.date));
            setPreviousPage("schedule");
            navigateTo("planning");
          }}
          onViewExplanation={() => {
            setPreviousPage("schedule");
            navigateTo("explanation");
          }}
          onSave={() => {
            saveSchedule(currentSchedule);
            navigateTo("entry");
          }}
          onBack={() => navigateTo("entry")}
          onViewHistory={() => navigateTo("history")}
          onViewPreferences={() => navigateTo("preferences")}
          onUpdateSchedule={(updatedSchedule) => {
            setCurrentSchedule(updatedSchedule);
            saveSchedule(updatedSchedule);
          }}
        />
      )}

      {currentPage === "explanation" && currentSchedule && (
        <ExplanationPanel
          schedule={currentSchedule}
          onBack={() => navigateTo(previousPage)}
        />
      )}

      {currentPage === "history" && (
        <HistoryPage
          schedules={scheduleHistory}
          onSelectSchedule={(schedule) => {
            setCurrentSchedule(schedule);
            navigateTo("schedule");
          }}
          onBack={() => navigateTo("entry")}
        />
      )}

      {currentPage === "preferences" && (
        <PreferencesPage
          onBack={() => {
            // Go back to entry page if coming from entry, otherwise to schedule
            if (currentSchedule) {
              navigateTo("schedule");
            } else {
              navigateTo("entry");
            }
          }}
        />
      )}
    </div>
  );
}