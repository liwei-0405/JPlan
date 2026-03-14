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
        const plans = await getAllPlans();
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
        // We use a small timeout to ensure Toaster is mounted
        setTimeout(() => {
           console.error("OAuth Error:", errorDescription);
           // Toast the error so the user can easily see it
           import('sonner').then(({ toast }) => {
               toast.error(`Auth Error: ${errorDescription.replace(/\+/g, ' ')}`, { duration: 10000 });
           });
        }, 500);
      }
      
      // Clear the hash so we don't keep showing the error on refresh
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }
  }, []);


  const saveSchedule = async (schedule: DailySchedule) => {
    setCurrentSchedule(schedule);

    // Save to Supabase
    const result = await savePlanToDb(schedule);
    if (!result.success) {
      console.error('Failed to save plan to database:', result.error);
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
                onStartPlanning={(date: Date) => {
                  setPlanningDate(date);
                  setPlanningSchedule(null);
                  navigate("/planning");
                }}
                onViewSchedule={(schedule: DailySchedule) => {
                  setCurrentSchedule(schedule);
                  navigate("/schedule");
                }}
                onReplanToday={() => {
                  if (todaySchedule) {
                    setPlanningSchedule(todaySchedule);
                    setPlanningDate(new Date());
                    navigate("/planning");
                  }
                }}
                onSettingsClick={() => navigate("/preferences")}
                todaySchedule={todaySchedule}
                scheduleHistory={scheduleHistory}
              />
            </ProtectedRoute>
          }
        />

        <Route
          path="/planning"
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
                    saveSchedule(updatedSchedule);
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

        {/* Catch-all redirect */}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </div>
  );
}