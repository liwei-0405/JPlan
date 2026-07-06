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
import { PrivacyPage, TermsPage } from "./components/LegalPage";
import { useAuth } from "./context/AuthContext";
import { getAllPlans, savePlan as savePlanToDb } from "./services/planService";
import { apiUrl } from "./services/apiConfig";
import { jplanLogoUrl } from "./brand";

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
  activity_id?: string;
  type?: "activity" | "travel" | "buffer" | "transition" | "idle" | "start_route" | "route_conflict";
  block_type?: "activity" | "transition" | "travel" | "buffer" | "idle" | "start_route" | "route_conflict" | "prep_buffer" | "free_time";
  block_id?: string;
  related_activity_id?: string;
  calendar_event_id?: string;
  google_event_id?: string;
  original_google_event_id?: string;
  source_system?: string;
  maybe_support_block?: boolean;
  read_only?: boolean;
  category?: string;
  date?: string;
  title: string;
  startTime: string;
  endTime: string;
  start?: string; // Backend compat
  end?: string;   // Backend compat
  scheduled_start?: number;
  scheduled_end?: number;
  location?: string;
  location_label?: string;
  location_category?: string;
  location_kind?: string;
  location_status?: string;
  location_resolution_status?: string;
  location_source?: string;
  location_policy?: "no_location_required" | "location_flexible" | "exact_location_required" | "category_location_required" | string;
  location_confidence?: number;
  location_warning?: string;
  location_flexible?: boolean;
  can_be_done_at_current_location?: boolean;
  quiet_place_required?: boolean;
  activity_role?: string;
  travel_context_required?: boolean;
  semantic_constraint_type?: string;
  service_kind?: string;
  arrive_by?: number | null;
  saved_location_label?: string;
  resolved_location?: {
    label?: string;
    display_name?: string;
    address?: string;
    category?: string;
    latitude: number;
    longitude: number;
    source?: string;
    confirmed_by_user?: boolean;
    resolved_for_activity_id?: string;
    saved_location_label?: string;
  };
  travel_estimate_source?: "heuristic" | "routing_service" | "fallback" | string;
  travel_validation_status?: string;
  travel_required?: boolean;
  route_duration_minutes?: number;
  display_label?: string;
  is_start_route?: boolean;
  is_route_conflict?: boolean;
  reason_code?: string;
  display_only?: boolean;
  source_activity_id?: string;
  destination_activity_id?: string;
  related_activity_ids?: string[];
  from_activity?: string;
  to_activity?: string;
  from_location?: string;
  to_location?: string;
  from_coordinate?: { latitude: number; longitude: number };
  to_coordinate?: { latitude: number; longitude: number };
  duration?: string;
  duration_minutes?: number;
  prep_buffer?: number | null;
  priority?: "low" | "medium" | "high";
  isMandatory?: boolean;
  is_mandatory?: boolean;
  timing_mode?: string;
  original_timing_mode?: string;
  is_user_fixed?: boolean;
  is_system_scheduled?: boolean;
  user_fixed_start?: number | null;
  fixed_start?: number | null;
  fixed_end?: number | null;
  preferred_start?: number | null;
  can_move_for_repair?: boolean;
  repair_protection?: "fixed" | "protected_social" | "flexible" | "optional" | string;
  notes?: string;
  description?: string;
  explanation?: string | null;
  trace?: string[];
  source?: string;
  jplan_metadata?: Record<string, unknown>;
  isConflict?: boolean;
  is_conflicting?: boolean;
  conflict_ids?: string[];
  conflictWith?: string[];
  conflictReason?: string;
  conflictPriority?: "low" | "medium" | "high" | "critical" | string;
  conflictSeverity?: "low" | "medium" | "high" | "critical" | string;
};

export type DailySchedule = {
  scheduleId?: string;
  date: string;
  version?: number;
  schema_version?: number;
  status?: "ok" | "partial" | "conflict" | "infeasible" | string;
  schedule_status?: "ok" | "warning" | "partial" | "conflict" | "location_pending" | "route_conflict" | "infeasible" | string;
  travel_validation_status?: "not_requested" | "pending_locations" | "validated" | "fallback_used" | "route_conflict" | string;
  planning_mode?: "feasibility_first" | "clash_allowed" | string;
  allow_clash?: boolean;
  accurate_travel_time?: boolean;
  travel_intent?: boolean;
  preferences?: Record<string, unknown>;
  schedule_constraints?: Record<string, unknown>;
  activities: ActivityBlock[];
  schedule_blocks?: ActivityBlock[];
  committed_schedule_blocks?: ActivityBlock[];
  external_calendar_events?: ActivityBlock[];
  sync_links?: Array<Record<string, unknown>>;
  active_view?: "jplan" | "google_calendar" | string;
  has_unsaved_draft?: boolean;
  draft_dirty?: boolean;
  export_warning?: string | null;
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
  warnings?: Array<Record<string, unknown>>;
  location_resolution_requests?: Array<Record<string, any>>;
  route_conflicts?: Array<Record<string, unknown>>;
  pending_repair_suggestions?: Array<Record<string, any>>;
  unfit_activities?: Array<Record<string, unknown>>;
  optional_skipped?: Array<Record<string, unknown>>;
  blocked_activities?: Array<Record<string, unknown>>;
  route_repair_actions?: Array<Record<string, unknown>>;
  route_efficiency?: Record<string, unknown>;
  route_total_before?: number | null;
  route_total_after?: number | null;
  route_minutes_saved?: number | null;
  location_revisits_count?: number | null;
  same_location_split_penalty_before?: number | null;
  same_location_split_penalty_after?: number | null;
  revisit_penalty_before?: number | null;
  revisit_penalty_after?: number | null;
  start_route_summary?: Record<string, unknown> | null;
  preview_id?: string | null;
  preview_base_version?: number | null;
  preview_status?: string | null;
  preview_reason?: string | null;
  preview_schedule?: Partial<DailySchedule> | null;
  failed_repair_attempt?: Record<string, unknown> | null;
  needs_reschedule?: boolean;
  reschedule_reason?: "manual_edit" | "location_changed" | "time_changed" | "event_added" | "event_deleted" | "preferences_changed" | string | null;
  needs_travel_validation?: boolean;
  last_rescheduled_at?: string | null;
  unmet_items?: Array<Record<string, unknown>>;
  validation_issues?: string[];
  performance_summary?: Record<string, unknown> | null;
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

type BackendStatus = "idle" | "checking" | "waiting" | "ready";

const BackendPendingScreen: React.FC<{ status: BackendStatus; onRetry: () => void }> = ({ status, onRetry }) => (
  <div className="flex min-h-screen items-center justify-center bg-gradient-to-b from-background to-secondary/20 px-6">
    <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 text-center shadow-lg">
      <div className="relative mx-auto mb-4 h-16 w-16">
        <img src={jplanLogoUrl} alt="JPlan logo" className="brand-logo-auth rounded-2xl object-cover shadow-md" />
        <div className="absolute -inset-1 animate-spin rounded-2xl border-2 border-primary/20 border-t-primary" />
      </div>
      <h2 className="mb-2 text-xl font-semibold">Starting JPlan</h2>
      <p className="text-sm leading-6 text-muted-foreground">
        {status === "waiting"
          ? "The backend is still waking up. JPlan will unlock automatically once it connects."
          : "Checking the backend connection before loading your plans."}
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-5 rounded-xl border border-border px-4 py-2 text-sm font-medium transition-colors hover:bg-secondary"
      >
        Retry now
      </button>
    </div>
  </div>
);

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
  const location = useLocation();
  const [currentSchedule, setCurrentSchedule] = useState<DailySchedule | null>(null);
  const [planningSchedule, setPlanningSchedule] = useState<DailySchedule | null>(null);
  const [scheduleHistory, setScheduleHistory] = useState<DailySchedule[]>([]);
  const [planningDate, setPlanningDate] = useState<Date>(new Date());
  const [isLoadingPlans, setIsLoadingPlans] = useState(true);
  const [backendStatus, setBackendStatus] = useState<BackendStatus>("idle");
  const [backendAttempt, setBackendAttempt] = useState(0);

  useEffect(() => {
    if (loading) return;
    if (!user) {
      setBackendStatus("idle");
      return;
    }

    let cancelled = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const timeoutTimer = setTimeout(() => controller.abort(), 15000);

    setBackendStatus((current) => current === "ready" ? "ready" : "checking");
    fetch(apiUrl("/health"), { signal: controller.signal, cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error("Backend health check failed");
        if (!cancelled) setBackendStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        setBackendStatus("waiting");
        retryTimer = setTimeout(() => {
          setBackendAttempt((attempt) => attempt + 1);
        }, 3000);
      })
      .finally(() => clearTimeout(timeoutTimer));

    return () => {
      cancelled = true;
      controller.abort();
      clearTimeout(timeoutTimer);
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [user?.id, loading, backendAttempt]);

  // Load all plans from Supabase on mount/user change
  useEffect(() => {
    if (!user || backendStatus !== "ready") return;

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
  }, [user, backendStatus]);

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

  const isPublicRoute = ["/login", "/signup", "/admin/signup", "/privacy", "/terms"].includes(location.pathname);

  if (user && backendStatus !== "ready" && !isPublicRoute) {
    return (
      <BackendPendingScreen
        status={backendStatus}
        onRetry={() => setBackendAttempt((attempt) => attempt + 1)}
      />
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/signup" element={<SignupPage />} />
        <Route path="/admin/signup" element={<AdminSignupPage />} />
        <Route path="/privacy" element={<PrivacyPage />} />
        <Route path="/terms" element={<TermsPage />} />

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
                  const dateStr = formatToISODate(new Date());
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
        <Route path="/planning" element={<Navigate to={`/planning/${formatToISODate(new Date())}`} replace />} />

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
                    navigate(`/planning/${currentSchedule.date}`);
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
                    setPlanningSchedule(updatedSchedule);
                    setScheduleHistory(prev => {
                      const index = prev.findIndex(item => item.date === updatedSchedule.date);
                      if (index < 0) return [...prev, updatedSchedule];
                      const next = [...prev];
                      next[index] = updatedSchedule;
                      return next;
                    });
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
