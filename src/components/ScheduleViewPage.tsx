import { Button } from "./ui/button";
import { ArrowLeft, Clock, MapPin, History, Settings, Sparkles, Edit2, Calendar as CalendarIcon, Loader2, AlertTriangle } from "lucide-react";
import type { DailySchedule, ActivityBlock } from "../App";
import { useAuth } from "../context/AuthContext";
import { toast } from "sonner";
import { useState } from "react";
import {
  exportPlanToGoogle,
  importCalendarEvents,
  replacePlanFromCalendar,
  type CalendarReplacePreview,
} from "../services/planService";
import { supabase } from "../lib/supabase";
import { TimelineGrid } from "./TimelineGrid";
import {
  calendarEventKey,
  getBlocksForView,
  hasGoogleCalendarLayer,
  type ScheduleViewMode,
} from "../utils/scheduleDisplayUtils";

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
  const { user, isGoogleLinked } = useAuth();
  const [isExporting, setIsExporting] = useState(false);
  const [isCalendarActionBusy, setIsCalendarActionBusy] = useState(false);
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const [replacePreview, setReplacePreview] = useState<CalendarReplacePreview | null>(null);
  const [activeView, setActiveView] = useState<ScheduleViewMode>(
    schedule.active_view === "google_calendar" && hasGoogleCalendarLayer(schedule) ? "google_calendar" : "jplan"
  );
  const [selectedGoogleEventIds, setSelectedGoogleEventIds] = useState<string[]>(() =>
    (schedule.external_calendar_events || [])
      .filter(event => !event.maybe_support_block)
      .map(calendarEventKey)
      .filter(Boolean)
  );
  const googleEvents = schedule.external_calendar_events || [];
  const hasGoogleEvents = hasGoogleCalendarLayer(schedule);
  const timelineBlocks = getBlocksForView(schedule, activeView);

  const handleExportToGoogle = async () => {
    if (!user) return;
    setIsExporting(true);
    const exportToast = toast.loading("Exporting to Google Calendar...");
    try {
      const result = await exportPlanToGoogle(schedule.date, user.id);
      if (!result.success) {
        if (result.error === 'TOKEN_EXPIRED') {
          toast.error("Google Session Expired or Insufficient Permissions. Re-linking...", { id: exportToast, duration: 4000 });
          // Link again with new scopes
          setTimeout(async () => {
            await supabase.auth.signInWithOAuth({
              provider: 'google',
              options: {
                queryParams: {
                  access_type: 'offline',
                  prompt: 'consent',
                },
                scopes: 'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile',
                redirectTo: window.location.href,
              }
            });
          }, 1500);
          return;
        }
        throw new Error(result.error);
      }

      const count = result.exportedCount || 0;
      const activityCount = result.activityCount || 0;
      const travelCount = result.travelCount || 0;
      if (count === 0) {
        toast.info("No new activities to export. (Synced Google events are skipped)", { id: exportToast, duration: 5000 });
      } else {
        const parts = [
          `${activityCount} ${activityCount === 1 ? "activity" : "activities"}`,
          `${travelCount} travel ${travelCount === 1 ? "block" : "blocks"}`,
        ];
        toast.success(`Exported ${parts.join(" and ")} to Google Calendar.`, { id: exportToast, duration: 4000 });
      }
    } catch (err: any) {
      toast.error(err.message || "Failed to export", { id: exportToast, duration: 4000 });
    } finally {
      setIsExporting(false);
    }
  };

  // Check if the schedule date is in the past
  const scheduleDate = new Date(schedule.date);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const isPastDate = scheduleDate < today;

  const handleEventClick = (event: ActivityBlock) => {
    if (activeView === "google_calendar") return;
    const bType = event.block_type || event.type;
    if (bType === "activity") {
      onModify();
    }
  };

  const toggleGoogleEventSelection = (eventId: string) => {
    setSelectedGoogleEventIds(prev =>
      prev.includes(eventId) ? prev.filter(id => id !== eventId) : [...prev, eventId]
    );
  };

  const handleImportSelected = async () => {
    if (!user || selectedGoogleEventIds.length === 0) return;
    setIsCalendarActionBusy(true);
    try {
      const result = await importCalendarEvents(schedule.date, user.id, selectedGoogleEventIds);
      if (!result.success || !result.schedule) throw new Error(result.error || "Import failed");
      onUpdateSchedule(result.schedule);
      setActiveView("jplan");
      toast.success("Imported selected Google Calendar events into JPlan.");
    } catch (err: any) {
      toast.error(err.message || "Failed to import Google Calendar events");
    } finally {
      setIsCalendarActionBusy(false);
    }
  };

  const handleReplacePreview = async () => {
    if (!user || selectedGoogleEventIds.length === 0) return;
    setIsCalendarActionBusy(true);
    try {
      const result = await replacePlanFromCalendar(schedule.date, user.id, selectedGoogleEventIds, false);
      if (!result.success || !result.preview) throw new Error(result.error || "Preview failed");
      setReplacePreview(result.preview);
    } catch (err: any) {
      toast.error(err.message || "Failed to prepare replacement preview");
    } finally {
      setIsCalendarActionBusy(false);
    }
  };

  const handleConfirmReplace = async () => {
    if (!user || !replacePreview) return;
    setIsCalendarActionBusy(true);
    try {
      const result = await replacePlanFromCalendar(schedule.date, user.id, selectedGoogleEventIds, true);
      if (!result.success || !result.schedule) throw new Error(result.error || "Replace failed");
      onUpdateSchedule(result.schedule);
      setReplacePreview(null);
      setActiveView("jplan");
      toast.success("Replaced JPlan with selected Google Calendar events.");
    } catch (err: any) {
      toast.error(err.message || "Failed to replace JPlan");
    } finally {
      setIsCalendarActionBusy(false);
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

        {schedule.needs_reschedule && (
          <div className="mb-6 rounded-2xl border border-amber-200 bg-amber-50 p-4 text-amber-950 shadow-sm">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <div>
                <p className="text-sm font-medium">Saved, but not re-optimized.</p>
                <p className="mt-1 text-xs text-amber-800">
                  Open Modify to run the scheduler after your manual changes.
                </p>
              </div>
            </div>
          </div>
        )}

        {/* Action Buttons */}
        <div className="flex gap-3 mb-6 flex-wrap">
          {!isPastDate && (
            <Button onClick={onModify} variant="outline" className="rounded-xl gap-2">
              <Sparkles className="h-4 w-4" />
              Replan with Assistant
            </Button>
          )}
          <Button onClick={onViewExplanation} variant="outline" className="rounded-xl">
            View Explanation
          </Button>
          {isGoogleLinked && (
            <Button
              onClick={() => setShowConfirmDialog(true)}
              variant="outline"
              className="rounded-xl gap-2 border-primary/20 hover:bg-primary/5 hover:text-primary transition-colors"
              disabled={isExporting}
            >
              {isExporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <CalendarIcon className="h-4 w-4" />}
              {isExporting ? "Exporting..." : "Push to Google Calendar"}
            </Button>
          )}
        </div>

        {hasGoogleEvents && (
          <div className="mb-6 flex flex-wrap items-center gap-2">
            <Button
              variant={activeView === "jplan" ? "default" : "outline"}
              size="sm"
              onClick={() => setActiveView("jplan")}
              className="rounded-xl"
            >
              JPlan
            </Button>
            <Button
              variant={activeView === "google_calendar" ? "default" : "outline"}
              size="sm"
              onClick={() => setActiveView("google_calendar")}
              className="rounded-xl"
            >
              Google Calendar
            </Button>
          </div>
        )}

        {activeView === "google_calendar" && (
          <div className="mb-6 rounded-2xl border border-border bg-card p-4 shadow-sm">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium">Google Calendar layer</p>
                <p className="text-xs text-muted-foreground">Read-only until imported or replaced.</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleImportSelected}
                  disabled={isCalendarActionBusy || selectedGoogleEventIds.length === 0}
                  className="rounded-xl"
                >
                  Import Selected
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={handleReplacePreview}
                  disabled={isCalendarActionBusy || selectedGoogleEventIds.length === 0}
                  className="rounded-xl border-destructive/30 text-destructive hover:bg-destructive/5"
                >
                  Replace JPlan...
                </Button>
              </div>
            </div>
            <div className="grid gap-2">
              {googleEvents.map(event => {
                const key = calendarEventKey(event);
                return (
                  <label key={key} className="flex items-center gap-3 rounded-xl border border-border/60 px-3 py-2 text-sm">
                    <input
                      type="checkbox"
                      checked={selectedGoogleEventIds.includes(key)}
                      onChange={() => toggleGoogleEventSelection(key)}
                      disabled={Boolean(event.maybe_support_block)}
                    />
                    <span className="flex-1">
                      {event.title}
                      <span className="ml-2 text-xs text-muted-foreground">
                        {event.startTime || event.start} - {event.endTime || event.end}
                      </span>
                    </span>
                    {event.maybe_support_block && (
                      <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800">
                        support-like
                      </span>
                    )}
                  </label>
                );
              })}
            </div>
          </div>
        )}

        {/* Confirmation Dialog Overlay */}
        {showConfirmDialog && (
          <div style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '24px'
          }}>
            {/* Backdrop */}
            <div
              onClick={() => setShowConfirmDialog(false)}
              style={{
                position: 'absolute',
                inset: 0,
                backgroundColor: 'rgba(0,0,0,0.6)',
                backdropFilter: 'blur(10px)',
                WebkitBackdropFilter: 'blur(10px)',
              }}
            />

            {/* Modal Content */}
            <div style={{
              position: 'relative',
              backgroundColor: 'white',
              width: '100%',
              maxWidth: '520px',
              borderRadius: '28px',
              border: '1px solid rgba(0,0,0,0.05)',
              boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
              padding: '40px',
              animation: 'modalFadeIn 0.3s ease-out'
            }}>
              <style>{`
                @keyframes modalFadeIn {
                  from { opacity: 0; transform: scale(0.95) translateY(10px); }
                  to { opacity: 1; transform: scale(1) translateY(0); }
                }
              `}</style>

              <div className="flex flex-col items-center text-center">
                <div className="mb-6 p-5 bg-primary/10 rounded-[22px]">
                  <CalendarIcon size={36} className="text-primary" />
                </div>

                <h3 className="text-2xl font-bold mb-4 text-foreground">
                  Export to Google Calendar
                </h3>

                <p className="text-muted-foreground leading-relaxed mb-10 text-base px-2">
                  Ready to refresh your Google Calendar?
                  <br /><br />
                  JPlan will update linked calendar events for <span className="font-bold text-foreground" style={{ fontWeight: 'bold' }}>{schedule.date}</span> and create missing JPlan events.
                  External Google Calendar events will not be deleted.
                </p>

                <div className="flex flex-col sm:flex-row gap-4 w-full">
                  <Button
                    variant="outline"
                    onClick={() => setShowConfirmDialog(false)}
                    style={{ borderRadius: '16px' }}
                    className="flex-1 h-14 border-muted hover:bg-muted font-semibold transition-all"
                  >
                    Cancel
                  </Button>
                  <Button
                    onClick={() => {
                      setShowConfirmDialog(false);
                      handleExportToGoogle();
                    }}
                    style={{ borderRadius: '16px' }}
                    className="flex-1 h-14 bg-primary hover:bg-primary/90 text-white shadow-xl shadow-primary/20 font-bold transition-all active:scale-95"
                  >
                    Confirm & Push
                  </Button>
                </div>
              </div>
            </div>
          </div>
        )}

        {replacePreview && (
          <div className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/60 p-6">
            <div className="w-full max-w-lg rounded-2xl border border-border bg-card p-6 shadow-xl">
              <h3 className="mb-2 text-xl font-semibold">Replace JPlan with Google Calendar?</h3>
              <p className="mb-4 text-sm text-muted-foreground">
                This will import {replacePreview.import_count} selected event{replacePreview.import_count === 1 ? "" : "s"},
                replace {replacePreview.replace_count} JPlan activit{replacePreview.replace_count === 1 ? "y" : "ies"},
                and clear {replacePreview.clear_support_count} timeline block{replacePreview.clear_support_count === 1 ? "" : "s"}.
              </p>
              <div className="mb-5 max-h-48 overflow-y-auto rounded-xl border border-border/60 p-3 text-sm">
                {replacePreview.events_to_import.map(event => (
                  <div key={calendarEventKey(event)} className="py-1">
                    {event.title} <span className="text-muted-foreground">{event.startTime} - {event.endTime}</span>
                  </div>
                ))}
              </div>
              <div className="flex justify-end gap-3">
                <Button variant="outline" className="rounded-xl" onClick={() => setReplacePreview(null)}>
                  Cancel
                </Button>
                <Button
                  className="rounded-xl"
                  onClick={handleConfirmReplace}
                  disabled={isCalendarActionBusy}
                >
                  Confirm Replace
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* Timeline */}
        <TimelineGrid
          activities={timelineBlocks}
          interactive={!isPastDate && activeView === "jplan"}
          onActivityClick={(act) => handleEventClick(act)}
          showEditIcon={!isPastDate && activeView === "jplan"}
        />

        {timelineBlocks.length === 0 && (
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
