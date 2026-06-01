import { Button } from "./ui/button";
import { ArrowLeft, Clock, MapPin, History, Settings, Sparkles, Edit2, Calendar as CalendarIcon, Loader2, AlertTriangle } from "lucide-react";
import type { DailySchedule, ActivityBlock } from "../App";
import { useAuth } from "../context/AuthContext";
import { toast } from "sonner";
import { useState } from "react";
import { exportPlanToGoogle } from "../services/planService";
import { supabase } from "../lib/supabase";
import { TimelineGrid } from "./TimelineGrid";

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
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);

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
      if (count === 0) {
        toast.info("No new activities to export. (Synced Google events are skipped)", { id: exportToast, duration: 5000 });
      } else {
        toast.success(`Successfully exported ${count} activities to Google Calendar!`, { id: exportToast, duration: 4000 });
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
    const bType = event.block_type || event.type;
    if (bType === "activity") {
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
                  This will perform a <span className="font-extrabold" style={{ color: '#dc2626', fontWeight: 'bold' }}>Total Reset</span> for <span className="font-bold text-foreground" style={{ fontWeight: 'bold' }}>{schedule.date}</span>.
                  All existing events in Google Calendar for this day will be replaced by your current JPlan schedule.
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
                    Confirm & Sync
                  </Button>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Timeline */}
        <TimelineGrid
          activities={schedule.schedule_blocks || schedule.activities}
          interactive={!isPastDate}
          onActivityClick={(act) => handleEventClick(act)}
          showEditIcon={!isPastDate}
        />

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
