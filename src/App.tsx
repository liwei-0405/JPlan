import { useState, useEffect } from "react";
import { Routes, Route, Navigate, useNavigate, useLocation } from "react-router-dom";
import { EntryPage } from "./components/EntryPage";
import { PlanningInputPage } from "./components/PlanningInputPage";
import { ScheduleViewPage } from "./components/ScheduleViewPage";
import { ExplanationPanel } from "./components/ExplanationPanel";
import { HistoryPage } from "./components/HistoryPage";
import { PreferencesPage } from "./components/PreferencesPage";
import { LoginPage } from "./components/auth/LoginPage";
import { SignupPage } from "./components/auth/SignupPage";
import { AdminSignupPage } from "./components/auth/AdminSignupPage";
import { useAuth } from "./context/AuthContext";
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
  stable_activity_id?: string;
  type?: "activity" | "travel" | "buffer" | "transition" | "idle";
  block_type?: "activity" | "transition" | "buffer" | "idle";
  title: string;
  startTime: string;
  endTime: string;
  start?: string; // Backend compat
  end?: string;   // Backend compat
  location?: string;
  duration?: string;
  duration_minutes?: number;
  priority?: "low" | "medium" | "high";
  isMandatory?: boolean;
  notes?: string;
  explanation?: string | null;
  trace?: string[];
  source?: string;
  isConflict?: boolean;
  is_conflicting?: boolean;
  conflict_ids?: string[];
  conflictWith?: string[];
  conflictReason?: string;
  conflictPriority?: "low" | "medium" | "high" | "critical" | string;
  conflictSeverity?: "low" | "medium" | "high" | "critical" | string;
};

export type DailySchedule = {
  scheduleId: string;
  date: string;
  version: number;
  schema_version?: number;
  status?: "ok" | "partial" | "conflict" | "infeasible" | string;
  planning_mode?: "feasibility_first" | "clash_allowed" | string;
  allow_clash?: boolean;
  activities: ActivityBlock[];
  schedule_blocks?: ActivityBlock[];
  explanations?: string[];
  conflicts?: Array<{
    conflict_id: string;
    type: string;
    activities: string[];
    activity_ids: string[];
    start: string;
    end: string;
    severity: "low" | "medium" | "high" | "critical" | string;
    priority_label: string;
    user_forced: boolean;
    explanation: string;
    suggested_resolution?: string[];
  }>;
  conflict?: Record<string, unknown> | null;
  unmet_items?: Array<Record<string, unknown>>;
  validation_issues?: string[];
  unscheduled_activities?: Array<{
    title: string;
    reason: string;
    priority?: "low" | "medium" | "high";
    isMandatory?: boolean;
  }>;
};

const formatToISODate = (date: Date) => {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, '0');
  const dd = String(date.getDate()).padStart(2, '0');
  return `${yyyy}-${mm}-${dd}`;
};

const ProtectedRoute: React.FC<{ children: React.ReactNode; adminOnly?: boolean }> = ({ children, adminOnly = false }) => {
  const { user, loading, isAdmin } = useAuth();
  const location = useLocation();

  if (loading) {
    return <div className="flex h-screen items-center justify-center">Loading...</div>;
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (adminOnly && !isAdmin) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
};

export default function App() {
  const { user, loading } = useAuth();
  const navigate = useNavigate();
  const [currentSchedule, setCurrentSchedule] = useState<DailySchedule | null>(null);
  const [planningSchedule, setPlanningSchedule] = useState<DailySchedule | null>(null);
  const [scheduleHistory, setScheduleHistory] = useState<DailySchedule[]>([]);
  const [planningDate, setPlanningDate] = useState<Date>(new Date());
  const [isLoadingPlans, setIsLoadingPlans] = useState(true);

  // Load all plans from Supabase on mount/user change
  useEffect(() => {
    if (!user) return;

    const loadPlans = async () => {
      setIsLoadingPlans(true);
      try {
        const plans = await getAllPlans(user.id);
        setScheduleHistory(plans);
      } catch (error) {
        console.error('Failed to load plans:', error);
      } finally {
        setIsLoadingPlans(false);
      }
    };

    loadPlans();
  }, [user]);

  // Catch OAuth errors in URL
  useEffect(() => {
    const hash = window.location.hash;
    if (hash && hash.includes('error=')) {
      const params = new URLSearchParams(hash.substring(1));
      const error = params.get('error');
      const errorDescription = params.get('error_description');
      if (errorDescription) {
        setTimeout(() => {
           console.error("OAuth Error:", errorDescription);
           import('sonner').then(({ toast }) => {
               toast.error(`Auth Error: ${errorDescription.replace(/\+/g, ' ')}`, { duration: 10000 });
           });
        }, 500);
      }
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  }, []);


  const saveSchedule = async (schedule: DailySchedule) => {
    if (!user) return;
    setCurrentSchedule(schedule);

    // Save to Supabase and prefer the backend-normalized payload shape.
    const result = await savePlanToDb(schedule, user.id);
    if (!result.success) {
      console.error('Failed to save plan to database:', result.error);
      return;
    }

    const persistedSchedule = result.savedPlan || schedule;
    setCurrentSchedule(persistedSchedule);

    // Add to history if not already there
    const existingIndex = scheduleHistory.findIndex(s => s.date === persistedSchedule.date);
    if (existingIndex >= 0) {
      const updated = [...scheduleHistory];
      updated[existingIndex] = persistedSchedule;
      setScheduleHistory(updated);
    } else {
      setScheduleHistory([...scheduleHistory, persistedSchedule]);
    }
  };

  const getTodaySchedule = (): DailySchedule | null => {
    const todayStr = formatToISODate(new Date());
    return scheduleHistory.find(s => s.date === todayStr) || null;
  };

  const todaySchedule = getTodaySchedule();

  if (loading) {
    return <div className="flex h-screen items-center justify-center">Loading session...</div>;
  }

  return (
    <div className="min-h-screen bg-background">
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/admin/signup" element={<AdminSignupPage />} />

        <Route
          path="/"
          element={
            <ProtectedRoute>
              <EntryPage
                onSyncComplete={async () => {
                   if (!user) return;
                   setIsLoadingPlans(true);
                   try {
                     const plans = await getAllPlans(user.id);
                     setScheduleHistory(plans);
                   } catch (error) {
                     console.error('Failed to load plans after sync:', error);
                   } finally {
                     setIsLoadingPlans(false);
                   }
                }}
                onStartPlanning={(date: Date) => {
                  const dateStr = date.getFullYear() + "-" +
                    String(date.getMonth() + 1).padStart(2, '0') + "-" +
                    String(date.getDate()).padStart(2, '0');
                  const existingPlan = scheduleHistory.find(p => p.date === dateStr);
                  setPlanningDate(date);
                  setPlanningSchedule(existingPlan || null);
                  navigate(`/planning/${dateStr}`);
                }}
                onViewSchedule={(schedule: DailySchedule) => {
                  setCurrentSchedule(schedule);
                  navigate("/schedule");
                }}
                onReplanToday={() => {
                  const dateStr = new Date().toISOString().split('T')[0];
                  if (todaySchedule) {
                    setPlanningSchedule(todaySchedule);
                    setPlanningDate(new Date());
                  }
                  navigate(`/planning/${dateStr}`);
                }}
                onSettingsClick={() => navigate("/preferences")}
                todaySchedule={todaySchedule}
                scheduleHistory={scheduleHistory}
              />
            </ProtectedRoute>
          }
        />

        <Route
          path="/planning/:date"
          element={
            <ProtectedRoute>
              <PlanningInputPage
                selectedDate={planningDate}
                initialSchedule={planningSchedule}
                onUpdateSchedule={setPlanningSchedule}
                onScheduleGenerated={(schedule) => {
                  saveSchedule(schedule);
                  navigate("/");
                }}
                onBack={() => navigate(-1)}
                onViewExplanation={(schedule) => {
                  setCurrentSchedule(schedule);
                  navigate("/explanation");
                }}
              />
            </ProtectedRoute>
          }
        />
        {/* Fallback for planning without date */}
        <Route path="/planning" element={<Navigate to={`/planning/${new Date().toISOString().split('T')[0]}`} replace />} />

        <Route
          path="/schedule"
          element={
            <ProtectedRoute>
              {currentSchedule ? (
                <ScheduleViewPage
                  schedule={currentSchedule}
                  onModify={() => {
                    setPlanningSchedule(currentSchedule);
                    setPlanningDate(new Date(currentSchedule.date));
                    navigate("/planning");
                  }}
                  onViewExplanation={() => {
                    navigate("/explanation");
                  }}
                  onSave={() => {
                    saveSchedule(currentSchedule);
                    navigate("/");
                  }}
                  onBack={() => navigate("/")}
                  onViewHistory={() => navigate("/history")}
                  onViewPreferences={() => navigate("/preferences")}
                  onUpdateSchedule={(updatedSchedule) => {
                    setCurrentSchedule(updatedSchedule);
                  }}
                />
              ) : (
                <Navigate to="/" replace />
              )}
            </ProtectedRoute>
          }
        />

        <Route
          path="/explanation"
          element={
            <ProtectedRoute>
              {currentSchedule ? (
                <ExplanationPanel
                  schedule={currentSchedule}
                  onBack={() => navigate(-1)}
                />
              ) : (
                <Navigate to="/" replace />
              )}
            </ProtectedRoute>
          }
        />

        <Route
          path="/history"
          element={
            <ProtectedRoute>
              <HistoryPage
                schedules={scheduleHistory}
                onSelectSchedule={(schedule) => {
                  setCurrentSchedule(schedule);
                  navigate("/schedule");
                }}
                onBack={() => navigate("/")}
              />
            </ProtectedRoute>
          }
        />

        <Route
          path="/preferences"
          element={
            <ProtectedRoute>
              <PreferencesPage onBack={() => navigate(-1)} />
            </ProtectedRoute>
          }
        />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}
