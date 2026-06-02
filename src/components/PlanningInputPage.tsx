import { useState, useEffect, useRef } from "react";
import { useParams } from "react-router-dom";
import {
  addRecentLocationRemote,
  completeTravelValidation,
  geocodeLocation,
  getPlanByDate,
  getPlanningPreferences,
  getRecentLocations,
  getSavedLocations,
  runScheduler,
  type GeocodeCandidate,
  type SavedLocation,
} from "../services/planService";
import { Button } from "./ui/button";
import { Textarea } from "./ui/textarea";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Switch } from "./ui/switch";
import {
  ArrowLeft,
  Plus,
  Send,
  Bot,
  Settings2,
  MessageSquare,
  Clock,
  MapPin,
  CheckCircle,
  Loader2,
  Mic,
  Edit3,
  Lightbulb,
  RefreshCw,
  AlertTriangle
} from "lucide-react";
import type { DailySchedule, ActivityBlock } from "../App";
import { EventEditModal } from "./EventEditModal";
import { useAuth } from "../context/AuthContext";
import { TimelineGrid } from "./TimelineGrid";
import { LocationPickerDialog, candidateToMapPoint, type MapPoint } from "./LocationPickerDialog";
import {
  addRecentLocation,
  candidateToPlanningLocation,
  hasLocationCoordinates,
  loadPlanningPreferences,
  loadRecentLocations,
  mergePlanningPreferences,
  normalizePlanningPreferences,
  savePlanningPreferences,
  saveRecentLocations,
  savedLocationToPlanningLocation,
  toCanonicalTime,
  toDisplayTime,
  type PlanningLocation,
  type RecentLocation,
} from "../utils/planningPreferences";


type PlanningInputPageProps = {
  onScheduleGenerated: (schedule: DailySchedule) => void;
  onBack: () => void;
  onViewExplanation: (schedule: DailySchedule) => void;
  selectedDate: Date;
  initialSchedule: DailySchedule | null;
  onUpdateSchedule: (schedule: DailySchedule) => void;
};

type LocationResolutionRequest = {
  activity_id: string;
  request_type?: string;
  title: string;
  category?: string;
  current_guess?: string;
  expanded_query?: string;
  reason?: string;
  display_reason?: string;
  same_location_as?: string;
  related_activity_ids?: string[];
  related_titles?: string[];
  saved_matches?: GeocodeCandidate[];
  geocode_candidates?: GeocodeCandidate[];
  affected_transitions?: Array<{
    from_activity?: string;
    to_activity?: string;
    from_location?: string;
    to_location?: string;
  }>;
};

type ResolvedLocationSnapshot = NonNullable<ActivityBlock["resolved_location"]>;

type RepairSuggestion = {
  id?: string;
  type?: string;
  title?: string;
  from?: string;
  from_end?: string;
  to?: string;
  to_end?: string;
  reason?: string;
  impact?: string;
  impact_type?: string;
  repair_protection?: string;
  requires_user_confirmation?: boolean;
  requires_explicit_fixed_move_approval?: boolean;
  advisory_only?: boolean;
  would_change?: boolean;
};

function isAccurateTravelEnabled(schedule?: DailySchedule | null): boolean {
  return Boolean(
    schedule?.accurate_travel_time
    || schedule?.preferences?.accurate_travel_time
    || schedule?.travel_intent
    || schedule?.preferences?.travel_intent
  );
}

function hasRouteRepairPreview(schedule?: DailySchedule | null): boolean {
  return Boolean(schedule?.preview_id && schedule.preview_schedule);
}

function routePreviewStatus(schedule?: DailySchedule | null): string {
  return String(schedule?.preview_status || schedule?.preview_schedule?.travel_validation_status || "");
}

function clearRouteRepairPreview<T extends DailySchedule>(schedule: T): T {
  const {
    committed_schedule_blocks,
    preview_id,
    preview_base_version,
    preview_status,
    preview_reason,
    preview_schedule,
    ...rest
  } = schedule;
  void committed_schedule_blocks;
  void preview_id;
  void preview_base_version;
  void preview_status;
  void preview_reason;
  void preview_schedule;
  return {
    ...rest,
    pending_repair_suggestions: [],
  } as T;
}

function displayScheduleForTimeline(schedule: DailySchedule): DailySchedule {
  if (!hasRouteRepairPreview(schedule)) return schedule;
  const preview = schedule.preview_schedule || {};
  return {
    ...schedule,
    ...preview,
    date: schedule.date,
    activities: (preview.activities as ActivityBlock[] | undefined) || schedule.activities || [],
    schedule_blocks: (preview.schedule_blocks as ActivityBlock[] | undefined) || schedule.schedule_blocks || [],
    start_route_summary: preview.start_route_summary || schedule.start_route_summary,
    route_repair_actions: preview.route_repair_actions || schedule.route_repair_actions || [],
    pending_repair_suggestions: preview.pending_repair_suggestions || schedule.pending_repair_suggestions || [],
    unfit_activities: preview.unfit_activities || schedule.unfit_activities || [],
    optional_skipped: preview.optional_skipped || schedule.optional_skipped || [],
    blocked_activities: preview.blocked_activities || schedule.blocked_activities || [],
    unscheduled_activities: preview.unscheduled_activities || schedule.unscheduled_activities || [],
  };
}

function previewCanCommitLocally(status: string): boolean {
  return status === "partial_feasible_with_unfit"
    || status === "partial_feasible_with_fixed_route_conflicts"
    || status === "repaired_validated";
}

function isFixedOrProtectedActivityBlock(block: Partial<ActivityBlock>): boolean {
  return Boolean(block.is_user_fixed || block.user_fixed_start != null)
    || ["fixed", "protected", "protected_social", "critical"].includes(String(block.repair_protection || ""))
    || String(block.timing_mode || "").toLowerCase() === "fixed"
    || String(block.original_timing_mode || "").toLowerCase() === "fixed";
}

export function PlanningInputPage({
  onScheduleGenerated,
  onBack,
  onViewExplanation,
  selectedDate,
  initialSchedule,
  onUpdateSchedule,
}: PlanningInputPageProps) {
  const { user } = useAuth();
  const params = useParams<{ date: string }>();
  const routeDateStr = params.date;
  
  // Use route date if available, otherwise fallback to prop date
  const isoDateStr = routeDateStr || (selectedDate.getFullYear() + "-" +
    String(selectedDate.getMonth() + 1).padStart(2, '0') + "-" +
    String(selectedDate.getDate()).padStart(2, '0'));

  // State Management
  const [activeMode, setActiveMode] = useState<"assistant" | "manual">("assistant");
  const [previewSchedule, setPreviewSchedule] = useState<DailySchedule | null>(initialSchedule || (isoDateStr === (selectedDate.getFullYear() + "-" +
    String(selectedDate.getMonth() + 1).padStart(2, '0') + "-" +
    String(selectedDate.getDate()).padStart(2, '0')) ? initialSchedule : null) || {
    date: isoDateStr,
    activities: []
  });
  const [allowClash, setAllowClash] = useState<boolean>(Boolean(initialSchedule?.allow_clash));
  const [accurateTravelTime, setAccurateTravelTime] = useState<boolean>(isAccurateTravelEnabled(initialSchedule));
  const [locationInputs, setLocationInputs] = useState<Record<string, string>>({});
  const [locationCandidates, setLocationCandidates] = useState<Record<string, GeocodeCandidate[]>>({});
  const [resolvingLocationId, setResolvingLocationId] = useState<string | null>(null);
  const [mapPickerRequest, setMapPickerRequest] = useState<LocationResolutionRequest | null>(null);
  const [mapPickerCandidate, setMapPickerCandidate] = useState<GeocodeCandidate | null>(null);
  const [isSavingMapPin, setIsSavingMapPin] = useState(false);
  const [savedLocationsForPicker, setSavedLocationsForPicker] = useState<SavedLocation[]>([]);
  const [recentLocations, setRecentLocations] = useState<RecentLocation[]>([]);
  const [planningPreferences, setPlanningPreferences] = useState(loadPlanningPreferences(user?.id));
  const [isEditingPlanSettings, setIsEditingPlanSettings] = useState(false);
  const [draftDayStart, setDraftDayStart] = useState("08:00");
  const [draftDayEnd, setDraftDayEnd] = useState("22:00");
  const [draftStartLocationKey, setDraftStartLocationKey] = useState("__default__");

  // Auto-load data on mount/refresh if missing
  useEffect(() => {
    if (!initialSchedule && user && isoDateStr) {
      const loadData = async () => {
        try {
          const data = await getPlanByDate(isoDateStr, user.id);
          if (data) {
            setPreviewSchedule(data as DailySchedule);
            setAllowClash(Boolean((data as DailySchedule).allow_clash));
            setAccurateTravelTime(isAccurateTravelEnabled(data as DailySchedule));
          }
        } catch (err) {
          console.error("Failed to auto-load schedule:", err);
        }
      };
      loadData();
    }
  }, [isoDateStr, user, initialSchedule]);

  useEffect(() => {
    if (!user?.id) return;
    const localPrefs = loadPlanningPreferences(user.id);
    setPlanningPreferences(localPrefs);
    setRecentLocations(loadRecentLocations(user.id));
    getSavedLocations(user.id)
      .then(setSavedLocationsForPicker)
      .catch((error) => console.error("Failed to fetch saved locations for picker:", error));
    getPlanningPreferences(user.id)
      .then((remotePrefs) => {
        if (!remotePrefs) return;
        const normalized = normalizePlanningPreferences(remotePrefs);
        savePlanningPreferences(user.id, normalized);
        setPlanningPreferences(normalized);
      })
      .catch((error) => console.error("Failed to fetch planning preferences:", error));
    getRecentLocations(user.id)
      .then((locations) => {
        if (!locations.length) return;
        setRecentLocations(saveRecentLocations(user.id, locations));
      })
      .catch((error) => console.error("Failed to fetch recent locations:", error));
  }, [user?.id]);

  const withPlanningPreferences = (schedule: DailySchedule): DailySchedule => (
    mergePlanningPreferences(schedule, user?.id, planningPreferences)
  );

  const applyReturnedSchedule = (schedule: DailySchedule) => {
    const scheduleWithPrefs = materializeAutoRoutePreview(withPlanningPreferences(schedule));
    const nextAccurateTravelTime = isAccurateTravelEnabled(scheduleWithPrefs);
    setAccurateTravelTime(nextAccurateTravelTime);
    setPreviewSchedule({
      ...scheduleWithPrefs,
      allow_clash: allowClash,
      accurate_travel_time: nextAccurateTravelTime,
      preferences: {
        ...(scheduleWithPrefs.preferences || {}),
        allow_clash: allowClash,
        accurate_travel_time: nextAccurateTravelTime,
      },
    });
  };

  const rememberRecentLocation = (location: PlanningLocation) => {
    setRecentLocations(addRecentLocation(user?.id, location));
    if (user?.id && hasLocationCoordinates(location)) {
      addRecentLocationRemote(user.id, location)
        .then((locations) => {
          if (locations.length) setRecentLocations(saveRecentLocations(user.id, locations));
        })
        .catch((error) => console.error("Failed to save recent location:", error));
    }
  };
  // Store the original schedule string for comparison to detect changes
  const originalScheduleJson = useRef(JSON.stringify(initialSchedule || {
    date: isoDateStr,
    activities: []
  })).current;

  // Sync state with parent whenever it changes
  useEffect(() => {
    if (previewSchedule) {
      const scheduleWithPrefs = withPlanningPreferences(previewSchedule);
      onUpdateSchedule({
        ...scheduleWithPrefs,
        allow_clash: allowClash,
        accurate_travel_time: accurateTravelTime,
        preferences: {
          ...(scheduleWithPrefs.preferences || {}),
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        },
        planning_mode: allowClash ? "clash_allowed" : "feasibility_first",
      });
    }
  }, [previewSchedule, onUpdateSchedule, allowClash, accurateTravelTime, planningPreferences]);

  const [editingEvent, setEditingEvent] = useState<ActivityBlock | null>(null);
  const [showExitConfirmation, setShowExitConfirmation] = useState(false);

  // Manual form state
  const [activityName, setActivityName] = useState("");
  const [activityTime, setActivityTime] = useState("");
  const [activityDuration, setActivityDuration] = useState("");
  const [activityLocation, setActivityLocation] = useState("");
  const [manualResolvedLocation, setManualResolvedLocation] = useState<ResolvedLocationSnapshot | null>(null);
  const [isManualLocationPickerOpen, setIsManualLocationPickerOpen] = useState(false);
  const [manualActivityType, setManualActivityType] = useState<"activity" | "travel" | "buffer">("activity");
  const [isConflict, setIsConflict] = useState(false);

  // Chat state
  const [chatInput, setChatInput] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [isRunningScheduler, setIsRunningScheduler] = useState(false);
  const [saveWithoutRerunNotice, setSaveWithoutRerunNotice] = useState(false);
  const [progressSteps, setProgressSteps] = useState<string[]>([]);
  const [activeProgressIndex, setActiveProgressIndex] = useState(0);
  const [conversationHistory, setConversationHistory] = useState<Array<{ role: "user" | "assistant", message: string, status?: "success" | "partial" | "warning" | "location_pending" | "conflict" | "error" | "clarification_needed" | "not_applied" }>>([
    {
      role: "assistant",
      message: "Hi! I'm your planning assistant. I'll generate a draft schedule here first. Nothing is saved until you press Save & Implement Plan."
    }
  ]);

  // Voice Recognition state
  const [isRecording, setIsRecording] = useState(false);
  const recognitionRef = useRef<any>(null);

  const startSpeechRecognition = () => {
    const SpeechRecognition = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;

    if (!SpeechRecognition) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "Your browser doesn't support live speech recognition. Try using Chrome or Safari."
      }]);
      return;
    }

    const recognition = new SpeechRecognition();
    recognitionRef.current = recognition;

    recognition.lang = 'en-US';
    recognition.interimResults = true;
    recognition.continuous = true;

    recognition.onstart = () => {
      setIsRecording(true);
      setChatInput(""); // Clear before starting
    };

    recognition.onresult = (event: any) => {
      let finalTranscript = '';
      let interimTranscript = '';

      for (let i = 0; i < event.results.length; ++i) {
        if (event.results[i].isFinal) {
          finalTranscript += event.results[i][0].transcript;
        } else {
          interimTranscript += event.results[i][0].transcript;
        }
      }

      setChatInput(finalTranscript + interimTranscript);
    };

    recognition.onerror = (event: any) => {
      console.error("Speech recognition error", event.error);
      setIsRecording(false);
      recognition.stop();
    };

    recognition.onend = () => {
      setIsRecording(false);
    };

    recognition.start();
  };

  const stopSpeechRecognition = () => {
    if (recognitionRef.current) {
      recognitionRef.current.stop();
      setIsRecording(false);

      // Auto-send if we have text
      // We use a small timeout to ensure the state has updated with the final transcript
      setTimeout(() => {
        const sendBtn = document.getElementById('chat-send-button');
        if (sendBtn) sendBtn.click();
      }, 300);
    }
  };

  const chatEndRef = useRef<HTMLDivElement>(null);
  const locationConfirmationTitlesRef = useRef<string[]>([]);
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [conversationHistory]);

  const progressLabelsForMessage = (message: string) => {
    const text = message.toLowerCase();
    const isAdvice = /\b(should i|do you think|what do you suggest|is it better|is it okay|can i fit|why|too packed)\b/.test(text)
      && !/\b(can you|please|move|add|remove|delete|change|update|shift)\b/.test(text);
    if (isAdvice) {
      return ["Reviewing your current schedule...", "Waiting for advice response..."];
    }
    const activityMentions = (text.match(/\b(meeting|seminar|lunch|dinner|gym|shopping|grocery|fyp|class|workout|coffee)\b/g) || []).length;
    const isComplex = activityMentions >= 2 || /\b(generate|plan my day|busy workday|followed by|fit in|account for travel)\b/.test(text);
    if (isComplex) {
      return [
        "Sending request to JPlan...",
        "Waiting for the AI parser...",
        "Preparing the draft if parsing succeeds...",
        "Waiting for the final response...",
      ];
    }
    return ["Sending request to JPlan...", "Checking the schedule...", "Waiting for response..."];
  };

  useEffect(() => {
    if (!isProcessing || progressSteps.length <= 1) return;
    setActiveProgressIndex(0);
    const interval = window.setInterval(() => {
      setActiveProgressIndex(prev => Math.min(prev + 1, progressSteps.length - 1));
    }, 1500);
    return () => window.clearInterval(interval);
  }, [isProcessing, progressSteps]);

  const markDirtySchedule = (
    schedule: DailySchedule,
    reason: NonNullable<DailySchedule["reschedule_reason"]>,
    activityTitle?: string,
  ): DailySchedule => {
    console.debug(`[JPLAN][DIRTY] reason=${reason} activity=${activityTitle || "(plan)"}`);
    const baseDraft = clearRouteRepairPreview(schedule);
    return {
      ...baseDraft,
      travel_validation_status: accurateTravelTime ? "not_requested" : baseDraft.travel_validation_status,
      route_repair_actions: [],
      route_conflicts: [],
      pending_repair_suggestions: [],
      unfit_activities: [],
      blocked_activities: [],
      start_route_summary: null,
      needs_reschedule: true,
      reschedule_reason: reason,
      needs_travel_validation: accurateTravelTime ? true : Boolean(baseDraft.needs_travel_validation),
    };
  };

  const locationFingerprint = (event?: Partial<ActivityBlock> | null) => JSON.stringify({
    location: event?.location || "",
    location_label: event?.location_label || "",
    location_source: event?.location_source || "",
    resolved_location: event?.resolved_location || null,
  });

  const normalizeManualTimeEdit = (original: ActivityBlock, updated: ActivityBlock): ActivityBlock => {
    const start = timeToMinutes(updated.startTime);
    const rawEnd = timeToMinutes(updated.endTime || calculateEndTime(updated.startTime, updated.duration || "1h"));
    const end = rawEnd < start ? rawEnd + 24 * 60 : rawEnd;
    const isFixed = isFixedOrProtectedActivityBlock(original);

    if (isFixed) {
      return {
        ...updated,
        scheduled_start: start,
        scheduled_end: end,
        fixed_start: start,
        fixed_end: end,
        user_fixed_start: start,
        is_user_fixed: true,
        timing_mode: "fixed",
        original_timing_mode: updated.original_timing_mode || original.original_timing_mode || "fixed",
        can_move_for_repair: false,
        repair_protection: original.repair_protection || updated.repair_protection || "fixed",
      };
    }

    return {
      ...updated,
      scheduled_start: start,
      scheduled_end: end,
      preferred_start: start,
      fixed_start: null,
      fixed_end: null,
      user_fixed_start: null,
      is_user_fixed: false,
      timing_mode: updated.timing_mode && updated.timing_mode !== "fixed" ? updated.timing_mode : "preferred",
      can_move_for_repair: true,
      repair_protection: ["fixed", "protected", "protected_social", "critical"].includes(String(updated.repair_protection || ""))
        ? "flexible"
        : (updated.repair_protection || "flexible"),
    };
  };

  const handleAddManualActivity = () => {
    if (!activityName.trim() || !activityTime.trim()) return;

    const formattedStartTime = formatTo12Hour(activityTime);
    // standardize duration display for example "1" -> "1 hour"
    const displayDuration = activityDuration.includes('h') || activityDuration.includes('m')
      ? activityDuration
      : `${activityDuration} hour`;

    const newStartTimeMins = timeToMinutes(formattedStartTime);
    const endTimeStr = calculateEndTime(activityTime, activityDuration);
    let newEndTimeMins = timeToMinutes(endTimeStr);

    // Handle overnight activity (e.g. 11 PM to 1 AM)
    if (newEndTimeMins < newStartTimeMins) {
      newEndTimeMins += 24 * 60;
    }

    // Collision Detection
    const baseDraft = previewSchedule ? clearRouteRepairPreview(previewSchedule) : null;
    const currentActivities = baseDraft?.activities || [];
    const hasCollision = currentActivities.some(activity => {
      const actStart = timeToMinutes(activity.startTime);
      let actEnd = timeToMinutes(activity.endTime || calculateEndTime(activity.startTime, activity.duration || ""));

      // Handle existing overnight activity
      if (actEnd < actStart) {
        actEnd += 24 * 60;
      }

      // Check overlap: (StartA < EndB) and (EndA > StartB)
      return (newStartTimeMins < actEnd) && (newEndTimeMins > actStart);
    });

    if (hasCollision) {
      setIsConflict(true);
      // Fall through — we still allow adding the event, just flag it
    }

    const pickedLocationName = manualResolvedLocation?.display_name || manualResolvedLocation?.address;
    const newActivity: ActivityBlock = {
      id: Date.now().toString(),
      type: manualActivityType,
      title: activityName,
      startTime: formattedStartTime,
      endTime: endTimeStr,
      duration: displayDuration,
      location: manualActivityType === "activity" ? (pickedLocationName || undefined) : undefined,
      location_label: manualActivityType === "activity" ? (pickedLocationName || undefined) : undefined,
      location_status: manualActivityType === "activity" && manualResolvedLocation ? "resolved" : undefined,
      location_source: manualResolvedLocation?.source,
      saved_location_label: manualResolvedLocation?.saved_location_label,
      resolved_location: manualActivityType === "activity" ? (manualResolvedLocation || undefined) : undefined,
    };

    const updatedActivities = [...currentActivities, newActivity].sort(
      (a, b) => timeToMinutes(a.startTime) - timeToMinutes(b.startTime)
    );

    setPreviewSchedule(markDirtySchedule({
      ...(baseDraft || {}),
      date: isoDateStr,
      activities: updatedActivities,
      schedule_blocks: syncActivityBlocks(baseDraft?.schedule_blocks, updatedActivities),
      travel_validation_status: baseDraft?.travel_validation_status,
    } as DailySchedule, "event_added", newActivity.title));
    setSaveWithoutRerunNotice(false);

    // Reset form
    setActivityName(""); setActivityTime(""); setActivityDuration(""); setActivityLocation("");
    setManualResolvedLocation(null);
    setManualActivityType("activity");
    setIsConflict(false);
  };

  const handleEventClick = (event: ActivityBlock) => {
    setEditingEvent(event);
  };

  const handleSaveEdit = (updatedEvent: ActivityBlock) => {
    if (!previewSchedule) return;
    const baseDraft = clearRouteRepairPreview(previewSchedule);
    const originalEvent = editingEvent || baseDraft.activities.find(a => a.id === updatedEvent.id) || updatedEvent;
    const timeChanged = Boolean(
      originalEvent.startTime !== updatedEvent.startTime ||
      originalEvent.endTime !== updatedEvent.endTime
    );
    const locationChanged = locationFingerprint(originalEvent) !== locationFingerprint(updatedEvent);
    const normalizedEvent = timeChanged ? normalizeManualTimeEdit(originalEvent, updatedEvent) : {
      ...updatedEvent,
      timing_mode: isFixedOrProtectedActivityBlock(originalEvent) ? (updatedEvent.timing_mode || originalEvent.timing_mode || "fixed") : updatedEvent.timing_mode,
      is_user_fixed: originalEvent.is_user_fixed,
      user_fixed_start: originalEvent.user_fixed_start,
      fixed_start: originalEvent.fixed_start,
      fixed_end: originalEvent.fixed_end,
      can_move_for_repair: originalEvent.can_move_for_repair,
      repair_protection: originalEvent.repair_protection,
    };
    const newStartTimeMins = timeToMinutes(updatedEvent.startTime);
    const endTimeStr = updatedEvent.endTime || calculateEndTime(updatedEvent.startTime, updatedEvent.duration || "1h");
    let newEndTimeMins = timeToMinutes(endTimeStr);

    // handle overnight activity
    if (newEndTimeMins < newStartTimeMins) {
      newEndTimeMins += 24 * 60;
    }

    // Collision Detection
    const hasCollision = baseDraft.activities.some(activity => {
      if (activity.id === updatedEvent.id) return false; // skip self

      const actStart = timeToMinutes(activity.startTime);
      let actEnd = timeToMinutes(activity.endTime || calculateEndTime(activity.startTime, activity.duration || "1h"));

      if (actEnd < actStart) {
        actEnd += 24 * 60;
      }

      // check overlap formula: (StartA < EndB) and (EndA > StartB)
      return (newStartTimeMins < actEnd) && (newEndTimeMins > actStart);
    });

    if (hasCollision) {
      setIsConflict(true);
    }

    // 3. If there is no conflict, execute the update and sort
    const existingIndex = baseDraft.activities.findIndex(a => a.id === updatedEvent.id);
    const updatedActivities = (
      existingIndex >= 0
        ? baseDraft.activities.map(a => a.id === updatedEvent.id ? normalizedEvent : a)
        : [...baseDraft.activities, normalizedEvent]
    ).sort((a, b) => timeToMinutes(a.startTime) - timeToMinutes(b.startTime));

    const reason = timeChanged ? "time_changed" : (locationChanged ? "location_changed" : "manual_edit");
    if (locationChanged) {
      console.debug(
        `[JPLAN][MANUAL_LOCATION_EDIT] activity_id=${normalizedEvent.stable_activity_id || normalizedEvent.id} title=${normalizedEvent.title} updated_activities=true updated_schedule_blocks=true`
      );
    }
    setPreviewSchedule(markDirtySchedule({
      ...baseDraft,
      activities: updatedActivities,
      schedule_blocks: syncActivityBlocks(baseDraft.schedule_blocks, updatedActivities),
      travel_validation_status: baseDraft.travel_validation_status,
    }, reason, normalizedEvent.title));
    setSaveWithoutRerunNotice(false);
    setEditingEvent(null);
    setIsConflict(false);
  };

  const handleDeleteEvent = () => {
    if (!editingEvent || !previewSchedule) return;
    const baseDraft = clearRouteRepairPreview(previewSchedule);
    const updatedActivities = baseDraft.activities.filter(a => a.id !== editingEvent.id);
    setPreviewSchedule(markDirtySchedule({
      ...baseDraft,
      activities: updatedActivities,
      schedule_blocks: removeDeletedActivityBlock(baseDraft.schedule_blocks, editingEvent.id),
      travel_validation_status: previewSchedule.travel_validation_status,
    }, "event_deleted", editingEvent.title));
    setSaveWithoutRerunNotice(false);
    setEditingEvent(null);
  };

  const handleSendMessage = async () => {
    if (!chatInput.trim()) return;

    const userMessage = chatInput;
    const isPreviewConfirmation = /^(yes|y|apply|apply changes|accept|no|n|keep|keep current)/i.test(userMessage.trim());
    const scheduleForChat = previewSchedule && hasRouteRepairPreview(previewSchedule) && !isPreviewConfirmation
      ? clearRouteRepairPreview(previewSchedule)
      : previewSchedule;
    if (scheduleForChat !== previewSchedule) {
      setPreviewSchedule(scheduleForChat);
    }

    // Add user message to conversation
    const currentHistory = [...conversationHistory, { role: "user" as const, message: userMessage }];
    setConversationHistory(currentHistory);
    setChatInput("");
    setProgressSteps(progressLabelsForMessage(userMessage));
    setActiveProgressIndex(0);
    setIsProcessing(true);

    try {
      console.log("[JPLAN][CHAT_FLAGS]", { allow_clash: allowClash, accurate_travel_time: accurateTravelTime });
      const response = await fetch("http://127.0.0.1:8000/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: userMessage,
          history: currentHistory, // send full history for context
          current_schedule: scheduleForChat ? withPlanningPreferences(scheduleForChat) : scheduleForChat, // send current schedule if any
          user_id: user?.id,
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        }),
      });

      if (!response.ok) {
        throw new Error("Failed to connect to backend");
      }

      const data = await response.json();

      // Add assistant response to conversation
      setConversationHistory(prev => [...prev, {
        role: "assistant" as const,
        message: data.reply,
        status: data.reply_status,
      }]);

      if (data.schedule_data) {
        applyReturnedSchedule(data.schedule_data as DailySchedule);
      }
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "Sorry, I'm having trouble connecting to my brain (backend). Is the server running?"
      }]);
    } finally {
      setIsProcessing(false);
      setProgressSteps([]);
      setActiveProgressIndex(0);
    }
  };

  const hasPendingRoutePreview = hasRouteRepairPreview(previewSchedule);
  const activeDisplaySchedule = previewSchedule ? displayScheduleForTimeline(previewSchedule) : null;
  const isDirtySchedule = Boolean(previewSchedule?.needs_reschedule);
  const pendingLocationRequests = (previewSchedule?.location_resolution_requests || []) as LocationResolutionRequest[];
  const pendingRepairSuggestions = (activeDisplaySchedule?.pending_repair_suggestions || previewSchedule?.pending_repair_suggestions || []) as RepairSuggestion[];
  const routeUnfitActivities = (activeDisplaySchedule?.unfit_activities || previewSchedule?.unfit_activities || []) as Array<Record<string, unknown>>;
  const routeBlockedActivities = (activeDisplaySchedule?.blocked_activities || previewSchedule?.blocked_activities || []) as Array<Record<string, unknown>>;
  const fixedRouteConflicts = ((activeDisplaySchedule?.route_conflicts || previewSchedule?.route_conflicts || []) as Array<Record<string, unknown>>)
    .filter(conflict => String(conflict.reason_code || "") === "fixed_to_fixed_infeasible");
  const travelValidationStatus = String(previewSchedule?.travel_validation_status || previewSchedule?.preview_status || "");
  const isPartialFixedRouteConflict = travelValidationStatus === "partial_feasible_with_fixed_route_conflicts";
  const isLocationPending = Boolean(
    !isPartialFixedRouteConflict
    && (
      previewSchedule?.schedule_status === "location_pending"
      || previewSchedule?.status === "location_pending"
      || travelValidationStatus === "pending_locations"
      || previewSchedule?.needs_travel_validation
    )
  );
  useEffect(() => {
    if (isPartialFixedRouteConflict) {
      console.log("[JPLAN][UI_STATE] status=partial_feasible_with_fixed_route_conflicts action=save_partial_plan");
    }
  }, [isPartialFixedRouteConflict]);
  const repairSuggestionHasChange = (suggestion: RepairSuggestion) => {
    if (suggestion.would_change === false) return false;
    const sameStart = String(suggestion.from || "") === String(suggestion.to || "");
    const sameEnd = String(suggestion.from_end || "") === String(suggestion.to_end || "");
    return !(sameStart && sameEnd);
  };
  const isFixedMoveRepairSuggestion = (suggestion: RepairSuggestion) => (
    Boolean(suggestion.advisory_only || suggestion.requires_explicit_fixed_move_approval)
    || String(suggestion.impact_type || "") === "fixed_target_move"
    || ["fixed", "protected_social"].includes(String(suggestion.repair_protection || ""))
  );
  const actionableRepairSuggestions = pendingRepairSuggestions.filter(
    suggestion => repairSuggestionHasChange(suggestion) && !isFixedMoveRepairSuggestion(suggestion)
  );
  const compactUnfitReason = (item: Record<string, unknown>) => {
    const reasonCode = String(item.reason_code || (item.blocking_constraint as Record<string, unknown> | undefined)?.reason_code || "");
    if (reasonCode === "not_enough_time_after_travel") return "Not enough route-safe time.";
    if (reasonCode === "day_boundary") return "Outside the available day.";
    if (reasonCode === "overlap") return "No non-overlapping slot.";
    return String(item.reason || "Could not fit.");
  };

  const normalizeLocationTarget = (value?: string | null) => (value || "").trim().toLowerCase();

  const matchesLocationRequest = (item: Partial<ActivityBlock>, request: LocationResolutionRequest) => {
    if (request.request_type === "start_location") return false;
    const requestId = String(request.activity_id || "");
    const itemId = String(item.stable_activity_id || item.id || "");
    if (requestId && itemId && requestId === itemId) return true;
    if (requestId) {
      if (itemId && (request.related_activity_ids || []).map(String).includes(itemId)) return true;
      return false;
    }
    return normalizeLocationTarget(item.title) === normalizeLocationTarget(request.title);
  };

  const remainingLocationRequestsAfterConfirm = (
    requests: LocationResolutionRequest[],
    request: LocationResolutionRequest,
    candidate: GeocodeCandidate,
  ) => requests.filter(item => {
    if (item.activity_id === request.activity_id) return false;
    if ((request.related_activity_ids || []).map(String).includes(String(item.activity_id))) return false;
    if (
      candidate.source === "same_label_reuse" &&
      request.current_guess &&
      item.current_guess &&
      normalizeLocationTarget(item.current_guess) === normalizeLocationTarget(request.current_guess)
    ) return false;
    return true;
  });

  const shortTitleList = (titles: string[], maxVisible = 3) => {
    const cleanTitles = titles.map(title => title.trim()).filter(Boolean);
    if (cleanTitles.length <= maxVisible) return cleanTitles.join(", ");
    return `${cleanTitles.slice(0, maxVisible).join(", ")}, and ${cleanTitles.length - maxVisible} more`;
  };

  const upsertLocationConfirmationMessage = (
    message: string,
    status: "success" | "location_pending" = "location_pending",
  ) => {
    setConversationHistory(prev => {
      const next = [...prev];
      let existingIndex = -1;
      for (let index = next.length - 1; index >= 0; index -= 1) {
        const item = next[index];
        if (
          item.role === "assistant" &&
          (
            item.message.startsWith("Locations confirmed:") ||
            item.message.startsWith("All locations confirmed.")
          )
        ) {
          existingIndex = index;
          break;
        }
      }
      const entry = { role: "assistant" as const, message, status };
      if (existingIndex >= 0) {
        next[existingIndex] = entry;
        return next;
      }
      return [...next, entry];
    });
  };

  const travelValidationMessage = (schedule: DailySchedule) => {
    const firstUnfit = (schedule.unfit_activities || [])[0] as Record<string, unknown> | undefined;
    const firstOptionalSkipped = (schedule.optional_skipped || [])[0] as Record<string, unknown> | undefined;
    const optionalSkippedText = firstOptionalSkipped?.title
      ? ` Optional item skipped: ${String(firstOptionalSkipped.title)}.`
      : "";

    const suggestions = ((schedule.pending_repair_suggestions || []) as RepairSuggestion[])
      .filter(suggestion => repairSuggestionHasChange(suggestion) && !isFixedMoveRepairSuggestion(suggestion));
    if (suggestions.length > 0) {
      const suggestion = suggestions[0];
      if (suggestion.advisory_only || suggestion.requires_explicit_fixed_move_approval) {
        return {
          message: `Accurate travel time found a fixed-event conflict. ${suggestion.title || "One fixed event"} would need to move from ${suggestion.from || "its current time"} to ${suggestion.to || "a later time"}, but I will not move it without explicit permission.`,
          status: "warning" as const,
        };
      }
      return {
        message: `Accurate travel time found a route conflict. ${suggestion.title || "One activity"} needs to move to ${suggestion.to || "a later time"}. Apply this change?`,
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "repair_suggestion_pending") {
      return {
        message: "Accurate travel time found a route conflict that needs your confirmation before I change the plan.",
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "partial_feasible_with_fixed_route_conflicts") {
      const fixedConflict = ((schedule.route_conflicts || []) as Array<Record<string, unknown>>)
        .find(conflict => String(conflict.reason_code || "") === "fixed_to_fixed_infeasible");
      const fromTitle = String(fixedConflict?.from_activity || fixedConflict?.from || "one fixed event");
      const toTitle = String(fixedConflict?.to_activity || fixedConflict?.to || "another fixed event");
      const required = Number(fixedConflict?.required_travel_minutes || fixedConflict?.required_route_minutes || 0);
      const available = Number(fixedConflict?.available_gap_minutes || fixedConflict?.available_minutes || 0);
      return {
        message: `Route warning: ${fromTitle} -> ${toTitle} needs ${required} min, only ${available} min. Fixed times kept.`,
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "partial_feasible_with_unfit") {
      return {
        message: firstUnfit?.title
          ? `Accurate travel updated the schedule, but ${String(firstUnfit.title)} could not fit.${optionalSkippedText}`
          : `Accurate travel updated the schedule, but one flexible activity could not fit.${optionalSkippedText}`,
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "repaired_validated") {
      if (firstUnfit?.title) {
        return {
          message: `Accurate travel updated the schedule, but ${String(firstUnfit.title)} could not fit.${optionalSkippedText}`,
          status: "warning" as const,
        };
      }
      const actionCount = (schedule.route_repair_actions || []).length;
      const movementText = actionCount > 0
        ? " Some flexible items were moved to route-safe times."
        : "";
      const fitText = firstOptionalSkipped?.title
        ? "Required activities still fit."
        : "All activities still fit.";
      return {
        message: `Accurate travel updated the schedule. ${fitText}${movementText}${optionalSkippedText}`,
        status: "success" as const,
      };
    }
    if ((schedule.location_resolution_requests || []).length > 0 || schedule.travel_validation_status === "pending_locations") {
      return {
        message: "Please confirm the exact locations first so I can calculate accurate travel time.",
        status: "location_pending" as const,
      };
    }
    const fixedConflict = ((schedule.route_conflicts || []) as Array<Record<string, unknown>>)
      .find(conflict => String(conflict.reason_code || "") === "fixed_to_fixed_infeasible");
    if (fixedConflict) {
      const fromTitle = String(fixedConflict.from_activity || fixedConflict.from || "one fixed event");
      const toTitle = String(fixedConflict.to_activity || fixedConflict.to || "another fixed event");
      const required = Number(fixedConflict.required_travel_minutes || fixedConflict.required_route_minutes || 0);
      const available = Number(fixedConflict.available_gap_minutes || fixedConflict.available_minutes || 0);
      return {
        message: `${fromTitle} to ${toTitle} needs about ${required} min travel, but only ${available} min is available. I kept fixed times unchanged and scheduled flexible items where possible.`,
        status: "warning" as const,
      };
    }
    if (schedule.schedule_status === "route_conflict" || schedule.status === "route_conflict") {
      return {
        message: "Accurate travel time creates a timing conflict in this draft, so I marked the affected route.",
        status: "conflict" as const,
      };
    }
    if (schedule.travel_validation_status === "fallback_used") {
      return {
        message: "I couldn't get live route data, so I kept the draft using fallback travel estimates.",
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "validated") {
      return {
        message: "Accurate travel time is now applied to this plan.",
        status: "success" as const,
      };
    }
    return {
      message: "I checked the travel timing for this plan.",
      status: "success" as const,
    };
  };

  const formatLocationCardTitle = (request: LocationResolutionRequest) => {
    if (request.request_type === "start_location") {
      return "Where are you starting from for this plan?";
    }
    const titles = [request.title, ...(request.related_titles || [])].filter(Boolean);
    const uniqueTitles = Array.from(new Set(titles));
    const titleText = uniqueTitles.join(", ");
    const guess = request.current_guess ? ` — ${request.current_guess}` : "";
    if (request.same_location_as) {
      return `${titleText}${guess} needs exact map location`;
    }
    return `${titleText}${guess} needs exact map location`;
  };

  const formatLocationCardHint = (request: LocationResolutionRequest) => {
    if (request.request_type === "start_location") {
      return "Choose your starting point for this day. It will not change your default unless you save it in Preferences.";
    }
    if (request.same_location_as) {
      return `You can confirm the same place as ${request.same_location_as}, or choose a more exact place.`;
    }
    return "Please choose an exact place on the map or from saved places.";
  };

  const attachResolvedLocation = (
    item: ActivityBlock,
    request: LocationResolutionRequest,
    candidate: GeocodeCandidate,
  ): ActivityBlock => {
    const sameLabelMatch = Boolean(
      candidate.source === "same_label_reuse" &&
      request.current_guess &&
      (
        normalizeLocationTarget(item.location_label) === normalizeLocationTarget(request.current_guess) ||
        normalizeLocationTarget(item.location) === normalizeLocationTarget(request.current_guess)
      )
    );
    if (!matchesLocationRequest(item, request) && !sameLabelMatch) return item;

    const displayName = candidate.display_name || candidate.address || request.current_guess || request.title;
    const address = candidate.address || candidate.display_name || request.current_guess || request.title;
    const savedLocationLabel = candidate.label || (candidate.source === "saved_profile" ? candidate.display_name : undefined);
    const resolvedLocation = {
      label: savedLocationLabel || request.current_guess || request.title,
      display_name: displayName,
      address,
      category: request.category,
      latitude: candidate.latitude,
      longitude: candidate.longitude,
      source: candidate.source || "event_confirmed",
      confirmed_by_user: candidate.confirmed_by_user ?? true,
      resolved_for_activity_id: request.activity_id,
      saved_location_label: savedLocationLabel,
    };

    return {
      ...item,
      location: displayName,
      location_label: displayName,
      location_category: request.category || item.location_category,
      location_status: "resolved",
      location_source: candidate.source || "event_confirmed",
      location_warning: undefined,
      saved_location_label: savedLocationLabel || item.saved_location_label,
      resolved_location: resolvedLocation,
    };
  };

  const applyResolvedLocationToDraft = (request: LocationResolutionRequest, candidate: GeocodeCandidate) => {
    rememberRecentLocation(candidateToPlanningLocation(candidate, request.category));
    setPreviewSchedule(prev => {
      if (!prev) return prev;
      const baseDraft = clearRouteRepairPreview(prev);
      if (request.request_type === "start_location") {
        const remaining = ((baseDraft.location_resolution_requests || []) as LocationResolutionRequest[])
          .filter(item => item.activity_id !== request.activity_id);
        return {
          ...baseDraft,
          preferences: {
            ...(baseDraft.preferences || {}),
            day_start_location_override: candidateToPlanningLocation(candidate, "start_location"),
          },
          location_resolution_requests: remaining,
          schedule_status: remaining.length ? "location_pending" : (baseDraft.schedule_status === "location_pending" ? "ok" : baseDraft.schedule_status),
          travel_validation_status: remaining.length ? "pending_locations" : "not_requested",
          needs_travel_validation: true,
          needs_reschedule: Boolean(baseDraft.needs_reschedule),
          reschedule_reason: baseDraft.reschedule_reason,
        };
      }
      const remaining = ((baseDraft.location_resolution_requests || []) as LocationResolutionRequest[])
        .filter(item => {
          if (item.activity_id === request.activity_id) return false;
          if ((request.related_activity_ids || []).map(String).includes(String(item.activity_id))) return false;
          if (
            candidate.source === "same_label_reuse" &&
            request.current_guess &&
            item.current_guess &&
            normalizeLocationTarget(item.current_guess) === normalizeLocationTarget(request.current_guess)
          ) return false;
          return true;
        });
      const updatedActivities = (baseDraft.activities || []).map(item => attachResolvedLocation(item, request, candidate));
      const updatedScheduleBlocks = (baseDraft.schedule_blocks || []).map(item => attachResolvedLocation(item, request, candidate));
      const updatedActivitiesChanged = JSON.stringify(updatedActivities) !== JSON.stringify(baseDraft.activities || []);
      const updatedScheduleBlocksChanged = JSON.stringify(updatedScheduleBlocks) !== JSON.stringify(baseDraft.schedule_blocks || []);
      console.debug(
        `[JPLAN][MANUAL_LOCATION_EDIT] activity_id=${request.activity_id} title=${request.title} updated_activities=${updatedActivitiesChanged} updated_schedule_blocks=${updatedScheduleBlocksChanged}`
      );
      return {
        ...baseDraft,
        activities: updatedActivities,
        schedule_blocks: updatedScheduleBlocks,
        location_resolution_requests: remaining,
        validation_issues: (baseDraft.validation_issues || []).filter(issue => !String(issue).includes(request.title)),
        schedule_status: remaining.length ? "location_pending" : (baseDraft.schedule_status === "location_pending" ? "ok" : baseDraft.schedule_status),
        travel_validation_status: remaining.length ? "pending_locations" : "not_requested",
        needs_travel_validation: true,
        needs_reschedule: Boolean(baseDraft.needs_reschedule),
        reschedule_reason: baseDraft.reschedule_reason,
      };
    });
    setLocationCandidates(prev => {
      const next = { ...prev };
      delete next[request.activity_id];
      return next;
    });
  };

  const openLocationMapPicker = (request: LocationResolutionRequest, candidate?: GeocodeCandidate) => {
    setMapPickerRequest(request);
    setMapPickerCandidate(candidate || null);
  };

  const handleSearchLocation = async (request: LocationResolutionRequest) => {
    const query = locationInputs[request.activity_id] || request.current_guess || request.title;
    if (!query.trim()) return;
    setResolvingLocationId(request.activity_id);
    try {
      const result = await geocodeLocation(query, request.category);
      setLocationCandidates(prev => ({
        ...prev,
        [request.activity_id]: result.candidates || [],
      }));
      openLocationMapPicker(request, (result.candidates || [])[0]);
      if (!result.candidates?.length) {
        setConversationHistory(prev => [...prev, {
          role: "assistant",
          message: `I couldn't find candidates for ${request.title}. You can still pin it manually on the map.`,
          status: "warning",
        }]);
      }
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "I couldn't search that location right now. Try a more specific address or use a saved place.",
        status: "warning",
      }]);
    } finally {
      setResolvingLocationId(null);
    }
  };

  const handleConfirmLocation = async (request: LocationResolutionRequest, candidate: GeocodeCandidate) => {
    setResolvingLocationId(request.activity_id);
    try {
      const requestsBeforeConfirm = ((previewSchedule?.location_resolution_requests || []) as LocationResolutionRequest[]);
      const remainingRequests = remainingLocationRequestsAfterConfirm(requestsBeforeConfirm, request, candidate);
      applyResolvedLocationToDraft(request, candidate);
      locationConfirmationTitlesRef.current = Array.from(new Set([
        ...locationConfirmationTitlesRef.current,
        request.title,
      ].filter(Boolean)));
      if (remainingRequests.length > 0) {
        upsertLocationConfirmationMessage(
          `Locations confirmed: ${shortTitleList(locationConfirmationTitlesRef.current)}. ${remainingRequests.length} left.`,
          "location_pending",
        );
      } else {
        upsertLocationConfirmationMessage(
          "All locations confirmed. Complete travel validation when ready.",
          "success",
        );
        locationConfirmationTitlesRef.current = [];
      }
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: error instanceof Error ? error.message : "I couldn't attach that location to the draft.",
        status: "warning",
      }]);
    } finally {
      setResolvingLocationId(null);
    }
  };

  const handleConfirmMapLocation = async (candidate: GeocodeCandidate) => {
    if (!mapPickerRequest) return;
    setIsSavingMapPin(true);
    try {
      await handleConfirmLocation(mapPickerRequest, {
        ...candidate,
        source: candidate.source || "manual_map_pin",
      });
      setMapPickerRequest(null);
      setMapPickerCandidate(null);
    } finally {
      setIsSavingMapPin(false);
    }
  };

  const runTravelValidation = async (source: "toggle" | "manual" = "manual") => {
    const hasScheduleItems = Boolean((previewSchedule?.schedule_blocks?.length || 0) || (previewSchedule?.activities?.length || 0));
    if (!previewSchedule || !user?.id) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "Create or select a plan first, then I can calculate accurate travel time.",
        status: "clarification_needed",
      }]);
      return;
    }
    if (!hasScheduleItems) {
      if (source === "toggle") {
        const scheduleWithPrefs = withPlanningPreferences(previewSchedule);
        setPreviewSchedule({
          ...scheduleWithPrefs,
          allow_clash: allowClash,
          accurate_travel_time: true,
          preferences: {
            ...(scheduleWithPrefs.preferences || {}),
            accurate_travel_time: true,
          },
          travel_validation_status: "not_requested",
        });
      } else {
        setConversationHistory(prev => [...prev, {
          role: "assistant",
          message: "Add at least one scheduled item first, then I can calculate accurate travel time.",
          status: "clarification_needed",
        }]);
      }
      return;
    }
    setIsProcessing(true);
    setProgressSteps(["Checking travel and buffer time...", "Preparing explanation..."]);
    setActiveProgressIndex(0);
    try {
      const scheduleWithPrefs = withPlanningPreferences(previewSchedule);
      const validated = await completeTravelValidation({
        ...scheduleWithPrefs,
        accurate_travel_time: true,
        preferences: {
          ...(scheduleWithPrefs.preferences || {}),
          accurate_travel_time: true,
        },
      }, user.id, source);
      applyReturnedSchedule(validated);
      const reply = travelValidationMessage(validated);
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: reply.message,
        status: reply.status,
      }]);
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: error instanceof Error ? error.message : "I couldn't complete travel validation.",
        status: "warning",
      }]);
    } finally {
      setIsProcessing(false);
      setProgressSteps([]);
      setActiveProgressIndex(0);
    }
  };

  const sendRepairConfirmation = async (message: "yes" | "no") => {
    if (!previewSchedule || !user?.id) return;
    const currentHistory = [...conversationHistory, { role: "user" as const, message }];
    setConversationHistory(currentHistory);
    setIsProcessing(true);
    setProgressSteps(message === "yes" ? ["Applying repair suggestion...", "Rechecking accurate travel time..."] : ["Keeping current schedule..."]);
    setActiveProgressIndex(0);
    try {
      const response = await fetch("http://127.0.0.1:8000/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          history: currentHistory,
          current_schedule: withPlanningPreferences(previewSchedule),
          user_id: user.id,
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        }),
      });
      if (!response.ok) throw new Error("Failed to connect to backend");
      const data = await response.json();
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: data.reply,
        status: data.reply_status,
      }]);
      if (data.schedule_data) {
        applyReturnedSchedule(data.schedule_data as DailySchedule);
      }
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "I couldn't apply that repair response right now. Please try again.",
        status: "warning",
      }]);
    } finally {
      setIsProcessing(false);
      setProgressSteps([]);
      setActiveProgressIndex(0);
    }
  };

  const isFixedOrProtectedBlock = (block: Partial<ActivityBlock>) => (
    Boolean(block.is_user_fixed || block.user_fixed_start != null)
    || ["fixed", "protected_social"].includes(String(block.repair_protection || ""))
    || (String(block.original_timing_mode || "").toLowerCase() === "fixed")
  );

  const blockIdentity = (block: Partial<ActivityBlock>) => (
    String(block.stable_activity_id || block.id || block.title || "").trim().toLowerCase()
  );

  const preserveFixedCommittedTimes = (candidate: ActivityBlock[], committed: ActivityBlock[]) => {
    const committedByKey = new Map(
      (committed || [])
        .filter(isFixedOrProtectedBlock)
        .map(block => [blockIdentity(block), block])
    );
    return (candidate || []).map(block => {
      const committedBlock = committedByKey.get(blockIdentity(block));
      if (!committedBlock) return block;
      return {
        ...block,
        start: committedBlock.start || committedBlock.startTime,
        startTime: committedBlock.startTime || committedBlock.start,
        end: committedBlock.end || committedBlock.endTime,
        endTime: committedBlock.endTime || committedBlock.end,
        scheduled_start: committedBlock.scheduled_start,
        scheduled_end: committedBlock.scheduled_end,
      } as ActivityBlock;
    });
  };

  const materializeAutoRoutePreview = (schedule: DailySchedule): DailySchedule => {
    const status = routePreviewStatus(schedule);
    if (!previewCanCommitLocally(status) || !schedule.preview_schedule) return schedule;

    const preview = schedule.preview_schedule;
    const base = clearRouteRepairPreview(schedule);
    const previewActivities = (preview.activities as ActivityBlock[] | undefined) || base.activities || [];
    const previewBlocks = (preview.schedule_blocks as ActivityBlock[] | undefined) || base.schedule_blocks || [];
    const unfitActivities = (preview.unfit_activities || schedule.unfit_activities || []) as Array<Record<string, unknown>>;
    const optionalSkipped = (preview.optional_skipped || schedule.optional_skipped || []) as Array<Record<string, unknown>>;
    const blockedActivities = (preview.blocked_activities || schedule.blocked_activities || []) as Array<Record<string, unknown>>;
    const unscheduledActivities = (preview.unscheduled_activities || schedule.unscheduled_activities || []) as DailySchedule["unscheduled_activities"];
    const isPartial = status === "partial_feasible_with_unfit" || status === "partial_feasible_with_fixed_route_conflicts";

    return {
      ...base,
      activities: preserveFixedCommittedTimes(previewActivities, base.activities || []),
      schedule_blocks: preserveFixedCommittedTimes(previewBlocks, base.schedule_blocks || []),
      start_route_summary: preview.start_route_summary || base.start_route_summary,
      route_repair_actions: (preview.route_repair_actions || base.route_repair_actions || []) as Array<Record<string, unknown>>,
      pending_repair_suggestions: [],
      route_conflicts: status === "partial_feasible_with_fixed_route_conflicts"
        ? (preview.route_conflicts || base.route_conflicts || []) as Array<Record<string, unknown>>
        : [],
      travel_validation_status: isPartial ? status : "repaired_validated",
      schedule_status: isPartial ? "partial" : "ok",
      status: isPartial ? "partial" : "ok",
      unfit_activities: isPartial ? unfitActivities : [],
      optional_skipped: optionalSkipped,
      blocked_activities: isPartial ? blockedActivities : [],
      unscheduled_activities: isPartial ? unscheduledActivities : [],
    };
  };

  const handleCompleteTravelValidation = async () => {
    await runTravelValidation("manual");
  };

  const handleRunScheduler = async () => {
    if (!previewSchedule || !user?.id) return;
    setIsRunningScheduler(true);
    setIsProcessing(true);
    setProgressSteps([
      "Running schedule optimizer...",
      accurateTravelTime ? "Checking accurate travel routes..." : "Refreshing route-aware draft...",
      "Preparing updated plan...",
    ]);
    setActiveProgressIndex(0);
    try {
      const scheduleWithPrefs = withPlanningPreferences(previewSchedule);
      console.debug(`[JPLAN][RUN_SCHEDULER] source=manual_button accurate_travel_time=${Boolean(accurateTravelTime)}`);
      const replanned = await runScheduler({
        ...scheduleWithPrefs,
        accurate_travel_time: accurateTravelTime,
        preferences: {
          ...(scheduleWithPrefs.preferences || {}),
          accurate_travel_time: accurateTravelTime,
        },
      }, user.id);
      applyReturnedSchedule(replanned);
      setSaveWithoutRerunNotice(false);
      const pendingLocations = replanned.travel_validation_status === "pending_locations" || replanned.needs_travel_validation;
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: pendingLocations
          ? "I reran the scheduler, but travel validation still needs locations."
          : "Scheduler reran the plan. Fixed events stayed locked; flexible items were re-optimized.",
        status: pendingLocations ? "location_pending" : "success",
      }]);
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: error instanceof Error ? error.message : "I couldn't run the scheduler.",
        status: "warning",
      }]);
    } finally {
      setIsRunningScheduler(false);
      setIsProcessing(false);
      setProgressSteps([]);
      setActiveProgressIndex(0);
    }
  };

  const handleSaveCurrentPlan = () => {
    const baseSchedule = materializeAutoRoutePreview(withPlanningPreferences(previewSchedule || { date: isoDateStr, activities: [] }));
    const savingPartialFixedRouteConflict = String(baseSchedule.travel_validation_status || "") === "partial_feasible_with_fixed_route_conflicts";
    const finalSchedule = {
      ...baseSchedule,
      allow_clash: allowClash,
      accurate_travel_time: accurateTravelTime,
      preferences: {
        ...(baseSchedule.preferences || {}),
        allow_clash: allowClash,
        accurate_travel_time: accurateTravelTime,
      },
      planning_mode: allowClash ? "clash_allowed" : "feasibility_first",
    };
    if (savingPartialFixedRouteConflict) {
      console.log("[JPLAN][SAVE_PARTIAL] status=partial_feasible_with_fixed_route_conflicts warnings_preserved=true");
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "This plan was saved with fixed route conflicts. Fixed event times were kept, but some routes are not physically feasible.",
        status: "warning",
      }]);
    }
    if (finalSchedule.needs_reschedule) {
      setSaveWithoutRerunNotice(true);
    }
    onScheduleGenerated(finalSchedule);
  };

  const handleAccurateTravelToggle = async (checked: boolean) => {
    if (!checked) {
      setAccurateTravelTime(false);
      setPreviewSchedule(prev => prev ? clearRouteRepairPreview(prev) : prev);
      return;
    }
    setAccurateTravelTime(true);
    await runTravelValidation("toggle");
  };

  const manualLocationCandidate: GeocodeCandidate | null = manualResolvedLocation
    ? {
        label: manualResolvedLocation.saved_location_label || manualResolvedLocation.label,
        display_name: manualResolvedLocation.display_name || activityLocation,
        address: manualResolvedLocation.address || manualResolvedLocation.display_name || activityLocation,
        latitude: manualResolvedLocation.latitude,
        longitude: manualResolvedLocation.longitude,
        source: manualResolvedLocation.source || "manual_event_location",
        confirmed_by_user: manualResolvedLocation.confirmed_by_user,
      }
    : null;
  const manualLocationPoint = candidateToMapPoint(manualLocationCandidate);

  const handleConfirmManualLocation = async (candidate: GeocodeCandidate) => {
    const displayName = candidate.display_name || candidate.address || activityLocation || activityName || "Manual event location";
    const address = candidate.address || candidate.display_name || displayName;
    rememberRecentLocation(candidateToPlanningLocation(candidate, "manual_event"));
    setActivityLocation(displayName);
    setManualResolvedLocation({
      label: candidate.label || activityName || "manual event",
      display_name: displayName,
      address,
      category: "manual_event",
      latitude: candidate.latitude,
      longitude: candidate.longitude,
      source: candidate.source || "manual_map_pin",
      confirmed_by_user: candidate.confirmed_by_user ?? true,
      saved_location_label: candidate.label,
    });
    setIsManualLocationPickerOpen(false);
  };

  const mapDialogExistingCandidate = (() => {
    if (!mapPickerRequest || !previewSchedule) return null;
    const source = [
      ...(previewSchedule.activities || []),
      ...(previewSchedule.schedule_blocks || []),
    ].find(item => matchesLocationRequest(item, mapPickerRequest));
    const resolved = source?.resolved_location;
    if (!resolved) return null;
    return {
      display_name: resolved.display_name || source?.location_label || source?.location || mapPickerRequest.title,
      address: resolved.address || resolved.display_name || source?.location_label || mapPickerRequest.title,
      latitude: resolved.latitude,
      longitude: resolved.longitude,
      source: resolved.source || "event_confirmed",
    } satisfies GeocodeCandidate;
  })();

  const mapDialogCandidates = (() => {
    if (!mapPickerRequest) return [];
    const all = [
      ...(mapPickerCandidate ? [mapPickerCandidate] : []),
      ...(mapDialogExistingCandidate ? [mapDialogExistingCandidate] : []),
      ...(locationCandidates[mapPickerRequest.activity_id] || []),
      ...(mapPickerRequest.geocode_candidates || []),
    ];
    const seen = new Set<string>();
    return all.filter(candidate => {
      const key = `${candidate.latitude.toFixed(6)}:${candidate.longitude.toFixed(6)}:${candidate.display_name || candidate.address || ""}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  })();
  const mapDialogInitialCenter: MapPoint | null = candidateToMapPoint(mapPickerCandidate || mapDialogExistingCandidate);
  const mapDialogSavedLocations = (() => {
    if (!mapPickerRequest) return savedLocationsForPicker;
    const seen = new Set<string>();
    const combined = [
      ...savedLocationsForPicker,
      ...(mapPickerRequest.saved_matches || []),
    ];
    return combined.filter((location) => {
      const key = `${location.label || ""}:${location.display_name || ""}:${location.address || ""}:${location.latitude}:${location.longitude}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  })();
  const activeStartLocation = (
    (previewSchedule?.preferences || {}).day_start_location_override ||
    planningPreferences.default_start_location
  ) as PlanningLocation | undefined;
  const activeStartLocationLabel = activeStartLocation
    ? (activeStartLocation.label || activeStartLocation.display_name || activeStartLocation.address)
    : null;
  const activeDayStart = String((previewSchedule?.preferences || {}).day_start_time || planningPreferences.day_start_time || "08:00");
  const activeDayEnd = String((previewSchedule?.preferences || {}).day_end_time || planningPreferences.day_end_time || "22:00");
  const baseTimeOptions = ["06:00", "07:00", "08:00", "09:00", "10:00", "20:00", "21:00", "22:00", "23:00"];
  const timeOptions = Array.from(new Set([
    ...baseTimeOptions,
    toCanonicalTime(activeDayStart) || "08:00",
    toCanonicalTime(activeDayEnd) || "22:00",
  ])).sort();
  const savedStartLocationOptions = savedLocationsForPicker.filter((location) => hasLocationCoordinates(location as PlanningLocation));
  const locationOptionKey = (location?: Partial<PlanningLocation> | null) => {
    if (!location) return "__none__";
    if (location.label) return `label:${location.label}`;
    return `coord:${Number(location.latitude).toFixed(6)}:${Number(location.longitude).toFixed(6)}`;
  };
  const activeOverrideLocation = (previewSchedule?.preferences || {}).day_start_location_override as PlanningLocation | undefined;
  const activeStartLocationKey = activeOverrideLocation
    ? locationOptionKey(activeOverrideLocation)
    : "__default__";
  const activeOverrideIsSavedOption = activeOverrideLocation
    ? savedStartLocationOptions.some((location) => locationOptionKey(location) === activeStartLocationKey)
    : false;
  const displayedDayStart = isEditingPlanSettings ? draftDayStart : (toCanonicalTime(activeDayStart) || "08:00");
  const displayedDayEnd = isEditingPlanSettings ? draftDayEnd : (toCanonicalTime(activeDayEnd) || "22:00");
  const displayedStartLocationKey = isEditingPlanSettings ? draftStartLocationKey : activeStartLocationKey;
  const planSettingControlClass = `h-8 rounded-xl border border-border px-2 text-xs transition ${
    isEditingPlanSettings
      ? "bg-background text-foreground"
      : "cursor-not-allowed bg-muted/50 text-muted-foreground opacity-70"
  }`;
  const beginPlanSettingsEdit = () => {
    setDraftDayStart(toCanonicalTime(activeDayStart) || "08:00");
    setDraftDayEnd(toCanonicalTime(activeDayEnd) || "22:00");
    setDraftStartLocationKey(
      (previewSchedule?.preferences || {}).day_start_location_override
        ? locationOptionKey((previewSchedule?.preferences || {}).day_start_location_override as PlanningLocation)
        : "__default__",
    );
    setIsEditingPlanSettings(true);
  };
  const applyPlanSettingsEdit = () => {
    setPreviewSchedule((prev) => {
      if (!prev) return prev;
      const selectedSavedLocation = savedStartLocationOptions.find((location) => locationOptionKey(location) === draftStartLocationKey);
      const baseDraft = clearRouteRepairPreview(prev);
      const nextPreferences: Record<string, unknown> = {
        ...(baseDraft.preferences || {}),
        day_start_time: draftDayStart,
        day_end_time: draftDayEnd,
        day_start: draftDayStart,
        day_end: draftDayEnd,
      };
      if (draftStartLocationKey === "__default__") {
        delete (nextPreferences as Record<string, unknown>).day_start_location_override;
      } else if (selectedSavedLocation) {
        nextPreferences.day_start_location_override = savedLocationToPlanningLocation(selectedSavedLocation);
      }
      return markDirtySchedule({
        ...baseDraft,
        preferences: nextPreferences,
        travel_validation_status: baseDraft.travel_validation_status,
      }, "preferences_changed", "Plan settings");
    });
    setSaveWithoutRerunNotice(false);
    setIsEditingPlanSettings(false);
  };

  const timelineActivities = activeDisplaySchedule ? withDisplayOnlyStartRouteRow(activeDisplaySchedule) : [];

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/10">
      <div className="max-w-7xl mx-auto px-6 py-8">
        <Button
          variant="ghost"
          onClick={() => {
            const currentJson = JSON.stringify(previewSchedule);
            // Only show dialog if there are activities AND the schedule has changed from initial state
            if (currentJson !== originalScheduleJson) {
              setShowExitConfirmation(true);
            } else {
              onBack();
            }
          }}
          className="mb-8 rounded-xl"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Dashboard
        </Button>

        <div className="mb-8">
          <h2 className="mb-2">Plan Your Day</h2>
          <p className="text-muted-foreground">
            Build a draft plan with AI or manual edits, then save when you're happy with it
          </p>
        </div>

        <div className="grid lg:grid-cols-2 gap-6 items-stretch">
          {/* Left: Live Schedule Preview */}
          <div className="lg:col-span-7 flex flex-col bg-card rounded-2xl border border-border shadow-sm overflow-hidden" style={{ height: "550px" }}>
            <div className="p-5 border-b bg-secondary/10 flex justify-between items-center">
              <div className="min-w-0 flex-1">
                <h3 className="text-lg font-bold">Live Schedule</h3>
                <p className="text-xs text-muted-foreground">{previewSchedule?.date || "No activities yet"}</p>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
                  <span className="inline-flex items-center gap-1 text-muted-foreground">
                    <MapPin className="h-3.5 w-3.5" />
                    From
                  </span>
                  <select
                    value={displayedStartLocationKey}
                    onChange={(event) => setDraftStartLocationKey(event.target.value)}
                    disabled={!isEditingPlanSettings}
                    className={`${planSettingControlClass} max-w-[160px]`}
                    aria-label="Plan start location for this day"
                  >
                    <option value="__default__">
                      {planningPreferences.default_start_location
                        ? `Default: ${planningPreferences.default_start_location.label || planningPreferences.default_start_location.display_name || "start"}`
                        : "No default start"}
                    </option>
                    {activeOverrideLocation && !activeOverrideIsSavedOption && (
                      <option value={activeStartLocationKey}>
                        This plan: {activeOverrideLocation.label || activeOverrideLocation.display_name || activeOverrideLocation.address || "start"}
                      </option>
                    )}
                    {savedStartLocationOptions.map((location) => (
                      <option key={locationOptionKey(location)} value={locationOptionKey(location)}>
                        {location.label || location.display_name || location.address}
                      </option>
                    ))}
                  </select>
                  <Clock className="h-3.5 w-3.5 text-muted-foreground" />
                  <select
                    value={displayedDayStart}
                    onChange={(event) => setDraftDayStart(event.target.value)}
                    disabled={!isEditingPlanSettings}
                    className={planSettingControlClass}
                    aria-label="Plan start time"
                  >
                    {timeOptions.map((time) => (
                      <option key={`start-${time}`} value={time}>{toDisplayTime(time)}</option>
                    ))}
                  </select>
                  <span className="text-muted-foreground">to</span>
                  <select
                    value={displayedDayEnd}
                    onChange={(event) => setDraftDayEnd(event.target.value)}
                    disabled={!isEditingPlanSettings}
                    className={planSettingControlClass}
                    aria-label="Plan end time"
                  >
                    {timeOptions.map((time) => (
                      <option key={`end-${time}`} value={time}>{toDisplayTime(time)}</option>
                    ))}
                  </select>
                  <Button
                    type="button"
                    variant={isEditingPlanSettings ? "default" : "ghost"}
                    size="sm"
                    className="h-7 rounded-full px-2 text-xs"
                    onClick={isEditingPlanSettings ? applyPlanSettingsEdit : beginPlanSettingsEdit}
                  >
                    {isEditingPlanSettings ? "Apply" : "Edit"}
                  </Button>
                  {isEditingPlanSettings && (
                    <Button type="button" size="sm" variant="outline" className="h-7 rounded-full px-2 text-xs" onClick={() => setIsEditingPlanSettings(false)}>
                      Cancel
                    </Button>
                  )}
                </div>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onViewExplanation(previewSchedule || { date: isoDateStr, activities: [] })}
                className="rounded-xl gap-2 text-xs border-primary/20 hover:bg-primary/5"
              >
                <Lightbulb size={14} className="text-yellow-500" /> Explain Schedule
              </Button>
            </div>

            <div className="flex-1 overflow-y-auto p-6 bg-secondary/5">
              {previewSchedule && (previewSchedule.schedule_blocks?.length || previewSchedule.activities.length > 0) ? (
                <TimelineGrid
                  activities={timelineActivities}
                  interactive={!hasPendingRoutePreview}
                  onActivityClick={handleEventClick}
                  showEditIcon={!hasPendingRoutePreview}
                />
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-muted-foreground opacity-30 italic">
                  <Bot size={48} className="mb-2" />
                  <p>Your timeline is empty. Try saying "Plan my day"!</p>
                </div>
              )}
            </div>
          </div>

          {/* RIGHT: Control Panel (5 Columns) */}
          <div className="lg:col-span-5 flex flex-col gap-4">

            {/* Mode Switcher */}
            <div className="grid grid-cols-2 p-1 bg-secondary/30 rounded-2xl border border-border">
              <Button
                variant={activeMode === "assistant" ? "default" : "ghost"}
                onClick={() => setActiveMode("assistant")}
                className="rounded-xl gap-2"
                disabled={isProcessing}
              >
                <MessageSquare size={16} /> AI Assistant
              </Button>
              <Button
                variant={activeMode === "manual" ? "default" : "ghost"}
                onClick={() => setActiveMode("manual")}
                className="rounded-xl gap-2"
                disabled={isProcessing}
              >
                <Settings2 size={16} /> Manual Mode
              </Button>
            </div>

            {isDirtySchedule && (
              <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-amber-950 shadow-sm">
                <div className="flex items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">This plan has manual changes that have not been re-optimized.</p>
                    <p className="mt-1 text-xs text-amber-800">
                      Run scheduler reruns full optimization. Complete travel validation only refreshes routes.
                    </p>
                    {saveWithoutRerunNotice && (
                      <p className="mt-1 text-xs font-medium text-amber-900">Saved, but not re-optimized.</p>
                    )}
                  </div>
                </div>
                <div className="mt-3 grid grid-cols-2 gap-2">
                  <Button
                    type="button"
                    size="sm"
                    className="rounded-xl gap-2"
                    onClick={handleRunScheduler}
                    disabled={isProcessing || isRunningScheduler}
                  >
                    {isRunningScheduler ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                    Run scheduler
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="rounded-xl bg-white"
                    onClick={handleSaveCurrentPlan}
                    disabled={!previewSchedule || previewSchedule.activities.length === 0 || isProcessing}
                  >
                    Save without rerun
                  </Button>
                </div>
              </div>
            )}

            <div className="flex items-center justify-between rounded-2xl border border-border bg-card px-4 py-3 shadow-sm">
              <div>
                <p className="text-sm font-medium">Allow conflicting schedules</p>
                <p className="text-xs text-muted-foreground">
                  {allowClash ? "On: keep user-requested overlaps and mark them clearly." : "Off: only feasible schedules will be committed."}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted-foreground">Allow clash</span>
                <Switch
                  checked={allowClash}
                  onCheckedChange={setAllowClash}
                  disabled={isProcessing}
                  aria-label="Allow conflicting schedules"
                />
              </div>
            </div>

            <div className="flex items-center justify-between rounded-2xl border border-border bg-card px-4 py-3 shadow-sm">
              <div>
                <p className="text-sm font-medium">Accurate travel time</p>
                <p className="text-xs text-muted-foreground">
                  {accurateTravelTime
                    ? "On: confirm exact locations before final route validation."
                    : "Off: use fast heuristic travel estimates."}
                </p>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-muted-foreground">Accurate</span>
                <Switch
                  checked={accurateTravelTime}
                  onCheckedChange={handleAccurateTravelToggle}
                  disabled={isProcessing}
                  aria-label="Accurate travel time"
                />
              </div>
            </div>

            {/* Dynamic Content Area */}
            <div className="bg-card rounded-2xl border border-border shadow-sm flex flex-col overflow-hidden" style={{ height: "380px" }}>
              {activeMode === "assistant" ? (
                /* Assistant UI */
                <>
                  <div className="flex-1 overflow-y-auto p-4 space-y-4">
                    {conversationHistory.map((msg, i) => (
                      <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                        <div className={`max-w-[85%] p-3 rounded-2xl text-sm ${msg.role === 'user' ? 'bg-primary text-primary-foreground' : msg.status === 'conflict' ? 'bg-destructive/10 text-destructive border border-destructive/20' : msg.status === 'partial' || msg.status === 'warning' || msg.status === 'location_pending' || msg.status === 'clarification_needed' || msg.status === 'not_applied' ? 'bg-yellow-50 text-yellow-900 border border-yellow-200' : 'bg-secondary'
                          }`}>
                          {msg.message}
                        </div>
                      </div>
                    ))}
                    {!hasPendingRoutePreview && actionableRepairSuggestions.length > 0 && (
                      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">Accurate travel repair suggestion</p>
                            {actionableRepairSuggestions.map((suggestion) => (
                              <p key={suggestion.id || suggestion.title} className="mt-1 text-xs text-amber-800">
                                {suggestion.advisory_only || suggestion.requires_explicit_fixed_move_approval ? "Advisory only: " : ""}
                                Move {suggestion.title || "activity"} from {suggestion.from || "its current time"} to {suggestion.to || "a safer time"}.
                                {suggestion.reason ? ` ${suggestion.reason}.` : ""}
                                {suggestion.advisory_only || suggestion.requires_explicit_fixed_move_approval
                                  ? " This requires explicit permission because the event is fixed/protected."
                                  : ""}
                              </p>
                            ))}
                          </div>
                        </div>
                        <div className="mt-3 flex gap-2">
                          <Button
                            size="sm"
                            className="rounded-xl"
                            disabled={isProcessing}
                            onClick={() => sendRepairConfirmation("yes")}
                          >
                            Apply change
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            className="rounded-xl bg-white"
                            disabled={isProcessing}
                            onClick={() => sendRepairConfirmation("no")}
                          >
                            Keep current plan
                          </Button>
                        </div>
                      </div>
                    )}
                    {previewSchedule && fixedRouteConflicts.length > 0 && (
                      <div className="rounded-2xl border border-red-200 bg-red-50 p-3 text-sm text-red-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">Fixed route conflict</p>
                            {fixedRouteConflicts.map((conflict, index) => {
                              const fromTitle = String(conflict.from_activity || conflict.from || "Previous fixed event");
                              const toTitle = String(conflict.to_activity || conflict.to || "Next fixed event");
                              const required = Number(conflict.required_travel_minutes || conflict.required_route_minutes || 0);
                              const available = Number(conflict.available_gap_minutes || conflict.available_minutes || 0);
                              return (
                                <p key={`${fromTitle}-${toTitle}-${index}`} className="mt-1 text-xs text-red-800">
                                  {fromTitle} {"->"} {toTitle}: needs {required} min, only {available} min. Fixed times kept.
                                </p>
                              );
                            })}
                          </div>
                        </div>
                      </div>
                    )}
                    {previewSchedule && routeUnfitActivities.length > 0 && (
                      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">These activities could not fit</p>
                            {routeUnfitActivities.map((item, index) => (
                              <div key={`${item.title || "unfit"}-${index}`} className="mt-2 text-xs text-amber-800">
                                <p className="font-medium text-amber-900">
                                  {String(item.title || "One flexible activity")}
                                  {item.duration_minutes ? ` · ${String(item.duration_minutes)} min` : ""}
                                </p>
                                <p>{compactUnfitReason(item)}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                    {previewSchedule && routeBlockedActivities.length > 0 && (
                      <div className="rounded-2xl border border-red-200 bg-red-50 p-3 text-sm text-red-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">These activities are blocked by fixed route conflict</p>
                            {routeBlockedActivities.map((item, index) => (
                              <div key={`${item.title || "blocked"}-${index}`} className="mt-2 text-xs text-red-800">
                                <p className="font-medium text-red-900">
                                  {String(item.title || "One activity")}
                                  {item.duration_minutes ? ` · ${String(item.duration_minutes)} min` : ""}
                                </p>
                                <p>{String(item.reason || "A fixed route conflict must be resolved before this can be placed.")}</p>
                                {item.blocking_constraint && (
                                  <p>
                                    Blocking constraint: {String((item.blocking_constraint as Record<string, unknown>).reason_code || "fixed route conflict")}
                                  </p>
                                )}
                                {Array.isArray(item.suggested_resolution) && item.suggested_resolution.length > 0 && (
                                  <ul className="mt-1 list-disc pl-4">
                                    {(item.suggested_resolution as unknown[]).map((suggestion, suggestionIndex) => (
                                      <li key={suggestionIndex}>{String(suggestion)}</li>
                                    ))}
                                  </ul>
                                )}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                    {previewSchedule?.travel_validation_status === "route_conflict" && (
                      <div className="rounded-2xl border border-red-200 bg-red-50 p-3 text-sm text-red-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">Route timing needs attention</p>
                            <p className="mt-1 text-xs text-red-800">
                              Accurate travel time still creates an unresolved route conflict.
                            </p>
                          </div>
                        </div>
                      </div>
                    )}
                    {pendingLocationRequests.length > 0 && (
                      <div className="space-y-3">
                        {pendingLocationRequests.map((request) => {
                          return (
                            <div key={request.activity_id} className="rounded-2xl border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-950">
                              <div className="flex items-start gap-2">
                                <MapPin size={16} className="mt-0.5 shrink-0" />
                                <div className="min-w-0 flex-1">
                                  <p className="font-medium">{formatLocationCardTitle(request)}</p>
                                  <p className="text-xs text-yellow-800">{formatLocationCardHint(request)}</p>
                                </div>
                              </div>

                              <div className="mt-3 flex flex-wrap gap-2">
                                <Button
                                  size="sm"
                                  className="rounded-xl"
                                  disabled={resolvingLocationId === request.activity_id}
                                  onClick={() => openLocationMapPicker(request)}
                                >
                                  {request.request_type === "start_location" ? "Choose starting point" : "Choose exact location"}
                                </Button>
                              </div>
                            </div>
                          );
                        })}
                        <Button
                          variant="default"
                          className="w-full rounded-xl"
                          disabled={isProcessing || pendingLocationRequests.length > 0}
                          onClick={handleCompleteTravelValidation}
                        >
                          Complete travel validation
                        </Button>
                        {pendingLocationRequests.length > 0 && (
                          <p className="text-xs text-muted-foreground">
                            Confirm all pending locations first, then complete accurate travel validation.
                          </p>
                        )}
                      </div>
                    )}
                    {isLocationPending && pendingLocationRequests.length === 0 && (
                      <Button
                        variant="default"
                        className="w-full rounded-xl"
                        disabled={isProcessing}
                        onClick={handleCompleteTravelValidation}
                      >
                        Complete travel validation
                      </Button>
                    )}
                    {isProcessing && (
                      <div className="flex justify-start">
                        <div className="bg-secondary p-3 rounded-2xl text-xs">
                          <div className="flex items-center gap-2 font-medium">
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            <span>{progressSteps[activeProgressIndex] || "Understanding your request..."}</span>
                          </div>
                          {progressSteps.length > 1 && (
                            <div className="mt-2 flex gap-1">
                              {progressSteps.map((step, index) => (
                                <span
                                  key={`${step}-${index}`}
                                  className={`h-1.5 flex-1 rounded-full ${index <= activeProgressIndex ? "bg-primary" : "bg-muted-foreground/20"}`}
                                />
                              ))}
                            </div>
                          )}
                          <p className="mt-2 text-[11px] text-muted-foreground">
                            This is an estimated wait indicator; JPlan will show the real result when the backend responds.
                          </p>
                        </div>
                      </div>
                    )}
                    <div ref={chatEndRef} />
                  </div>
                  <div className="p-4 border-t bg-secondary/5">
                    <div className="flex gap-2 items-end">
                      <Button
                        variant={isRecording ? "destructive" : "ghost"}
                        size="icon"
                        className={`h-10 w-10 rounded-xl shrink-0 transition-all ${isRecording ? 'animate-pulse scale-110 shadow-lg shadow-destructive/20' : ''}`}
                        disabled={isProcessing}
                        onClick={isRecording ? stopSpeechRecognition : startSpeechRecognition}
                      >
                        <Mic size={20} className={isRecording ? "text-white" : "text-muted-foreground"} />
                      </Button>
                      <Textarea
                        value={chatInput}
                        onChange={(e) => setChatInput(e.target.value)}
                        placeholder={isRecording ? "Listening..." : "e.g. Move lunch to 1pm..."}
                        className="min-h-[80px] rounded-xl resize-none"
                        disabled={isProcessing}
                        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSendMessage(); } }}
                      />
                      <Button
                        id="chat-send-button"
                        onClick={handleSendMessage}
                        disabled={isProcessing || !chatInput.trim()}
                        className="h-10 w-10 rounded-xl shrink-0 p-0"
                      >
                        {isProcessing ? <Loader2 className="h-5 w-5 animate-spin" /> : <Send size={18} />}
                      </Button>
                    </div>
                  </div>
                </>
              ) : (
                /* Manual Planning UI */
                <div className={`flex-1 overflow-y-auto p-6 space-y-4 transition-colors ${isConflict ? "bg-destructive/5" : ""}`}>
                  <h3 className={`font-bold mb-2 flex items-center justify-between ${isConflict ? "text-destructive" : ""}`}>
                    {isConflict ? "Conflict Detected!" : "Add Activity Manually"}
                  </h3>
                  <div className="space-y-4">
                    <div className="space-y-1.5">
                      <Label>Block Type</Label>
                      <div className="grid grid-cols-3 gap-2">
                        <Button
                          variant={manualActivityType === "activity" ? "default" : "outline"}
                          size="sm"
                          onClick={() => {
                            setManualActivityType("activity");
                            if (!activityName) setActivityName("");
                          }}
                          className="rounded-xl text-xs"
                        >
                          Activity
                        </Button>
                        <Button
                          variant={manualActivityType === "travel" ? "default" : "outline"}
                          size="sm"
                          onClick={() => {
                            setManualActivityType("travel");
                            setActivityLocation("");
                            setManualResolvedLocation(null);
                          }}
                          className="rounded-xl text-xs"
                        >
                          Travel
                        </Button>
                        <Button
                          variant={manualActivityType === "buffer" ? "default" : "outline"}
                          size="sm"
                          onClick={() => {
                            setManualActivityType("buffer");
                            setActivityLocation("");
                            setManualResolvedLocation(null);
                          }}
                          className="rounded-xl text-xs"
                        >
                          Buffer
                        </Button>
                      </div>
                    </div>

                    <div className="space-y-1.5">
                      <Label className={isConflict ? "text-destructive" : ""}>
                        {manualActivityType === "activity" ? "Activity Name" : "Block Label"}
                      </Label>
                      <Input
                        value={activityName}
                        onChange={e => { setActivityName(e.target.value); setIsConflict(false); }}
                        placeholder={manualActivityType === "activity" ? "Meeting, Gym..." : "Travel, Break..."}
                        className={`rounded-xl ${isConflict ? "border-destructive focus-visible:ring-destructive" : ""}`}
                      />
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                      <div className="space-y-1.5">
                        <Label className={isConflict ? "text-destructive" : ""}>Start Time</Label>
                        <Input
                          type="time"
                          value={activityTime}
                          onChange={e => { setActivityTime(e.target.value); setIsConflict(false); }}
                          className={`rounded-xl ${isConflict ? "border-destructive focus-visible:ring-destructive" : ""}`}
                        />
                      </div>
                      <div className="space-y-1.5">
                        <Label>Duration</Label>
                        <Input
                          value={activityDuration}
                          onChange={e => { setActivityDuration(e.target.value); setIsConflict(false); }}
                          placeholder="1h 30m"
                          className={`rounded-xl ${isConflict ? "border-destructive focus-visible:ring-destructive" : ""}`}
                        />
                      </div>
                    </div>
                    {manualActivityType === "activity" && (
                      <div className="space-y-1.5">
                        <Label>Location</Label>
                        <div className="rounded-xl border border-border bg-secondary/20 p-3">
                          {manualResolvedLocation ? (
                            <div>
                              <p className="text-sm font-medium">
                                {manualResolvedLocation.display_name || manualResolvedLocation.address || activityLocation}
                              </p>
                              <p className="mt-1 text-xs text-muted-foreground">
                                Confirmed map point attached to this manual event.
                              </p>
                            </div>
                          ) : (
                            <p className="text-sm text-muted-foreground">No exact location selected.</p>
                          )}
                        </div>
                        <div className="flex gap-2">
                          <Button
                            type="button"
                            variant="outline"
                            className="rounded-xl"
                            onClick={() => setIsManualLocationPickerOpen(true)}
                          >
                            <MapPin className="mr-2 h-4 w-4" />
                            {manualResolvedLocation ? "Change Location" : "Pick Location"}
                          </Button>
                          {manualResolvedLocation && (
                            <Button
                              type="button"
                              variant="ghost"
                              className="rounded-xl"
                              onClick={() => {
                                setActivityLocation("");
                                setManualResolvedLocation(null);
                              }}
                            >
                              Clear
                            </Button>
                          )}
                        </div>
                      </div>
                    )}
                    <Button
                      onClick={handleAddManualActivity}
                      className={`w-full rounded-xl gap-2 mt-4 ${isConflict ? "bg-destructive hover:bg-destructive/90 animate-pulse" : ""}`}
                      disabled={(!activityName || !activityTime) && !isConflict}
                    >
                      {isConflict ? (
                        <>Conflict Detected</>
                      ) : (
                        <><Plus size={18} /> Add to Schedule</>
                      )}
                    </Button>
                  </div>
                </div>
              )}
            </div>

            {/* Hint Box */}
            <div className="p-4 bg-primary/5 border border-primary/10 rounded-2xl">
              <p className="text-xs text-muted-foreground leading-relaxed">
                💡 <b>Tip:</b> You can switch between AI and Manual mode anytime.
                Manual changes are preserved when you talk to the AI, and this page stays as a draft until you save.
              </p>
            </div>

            {/* Save Button */}
            <Button
              onClick={handleSaveCurrentPlan}
              className="w-full rounded-xl py-6 text-lg font-semibold shadow-lg hover:shadow-xl transition-all"
              disabled={!previewSchedule || previewSchedule.activities.length === 0 || isProcessing}
            >
              {isProcessing ? (
                <>
                  <div className="flex items-center gap-2">
                    <Loader2 className="h-5 w-5 animate-spin" />
                    <span>Processing...</span>
                  </div>
                </>
              ) : (
                <>
                  <CheckCircle className="mr-2 h-5 w-5" />
                  {isPartialFixedRouteConflict ? "Save Partial Plan" : (isDirtySchedule ? "Save without rerun" : "Save & Implement Plan")}
                </>
              )}
            </Button>
            {isDirtySchedule && (
              <p className="text-center text-xs text-muted-foreground">
                Saving now keeps the badge: Saved, but not re-optimized.
              </p>
            )}
          </div>

        </div>
      </div>
      {editingEvent && (
        <EventEditModal
          event={editingEvent}
          onSave={handleSaveEdit}
          onCancel={() => setEditingEvent(null)}
          onDelete={handleDeleteEvent}
          allActivities={previewSchedule?.activities || []}
          savedLocations={savedLocationsForPicker}
          recentLocations={recentLocations}
          onLocationConfirmed={(candidate) => {
            rememberRecentLocation(candidateToPlanningLocation(candidate, editingEvent.location_category || "event_location"));
          }}
        />
      )}

      {showExitConfirmation ? (
        <div style={{
          position: 'fixed',
          inset: 0,
          backgroundColor: 'rgba(0,0,0,0.5)',
          zIndex: 9999,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center'
        }}>
          <div style={{
            background: 'white',
            padding: '24px',
            borderRadius: '12px',
            maxWidth: '500px'
          }}>
            <h2 style={{ marginBottom: '16px' }}>
              {!previewSchedule?.activities?.length ? "Clear Schedule?" : "Save your plan?"}
            </h2>
            <p style={{ marginBottom: '24px', color: '#666' }}>
              {!previewSchedule?.activities?.length
                ? "Are you sure you want to clear your schedule for this day?"
                : "You have unsaved changes in your schedule. Do you want to save them before leaving?"
              }
            </p>
            <div style={{ display: 'flex', gap: '8px', justifyContent: 'flex-end' }}>
              <button
                onClick={() => setShowExitConfirmation(false)}
                style={{ padding: '8px 16px', border: '1px solid #ccc', borderRadius: '6px' }}
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  if (previewSchedule) {
                    onUpdateSchedule({ ...previewSchedule, activities: [] });
                  }
                  setShowExitConfirmation(false);
                  onBack();
                }}
                style={{ padding: '8px 16px', background: '#dc2626', color: 'white', borderRadius: '6px', border: 'none' }}
              >
                Discard
              </button>
              <button
                onClick={() => {
                  if (previewSchedule) {
                    onScheduleGenerated(withPlanningPreferences(previewSchedule));
                    setShowExitConfirmation(false);
                  }
                }}
                style={{ padding: '8px 16px', background: '#2563eb', color: 'white', borderRadius: '6px', border: 'none' }}
              >
                {!previewSchedule?.activities?.length ? "Confirm" : "Save & Exit"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <LocationPickerDialog
        open={isManualLocationPickerOpen}
        onOpenChange={setIsManualLocationPickerOpen}
        title={`Pick location for ${activityName || "manual activity"}`}
        description={`Search, choose a saved place, or click the exact point for ${activityName || "this manual activity"}.`}
        label={activityName || "manual activity"}
        initialCenter={manualLocationPoint}
        initialPin={manualLocationPoint}
        candidates={manualLocationCandidate ? [manualLocationCandidate] : []}
        savedLocations={savedLocationsForPicker}
        recentLocations={recentLocations}
        initialSearchQuery={activityLocation || activityName || ""}
        searchCategory="manual_event"
        confirmLabel="Use this point"
        onConfirm={handleConfirmManualLocation}
      />

      <LocationPickerDialog
        open={Boolean(mapPickerRequest)}
        onOpenChange={(open) => {
          if (!open) {
            setMapPickerRequest(null);
            setMapPickerCandidate(null);
          }
        }}
        title={mapPickerRequest?.request_type === "start_location" ? "Pick starting point" : (mapPickerRequest ? `Pick location for ${mapPickerRequest.title}` : "Pick location on map")}
        description={mapPickerRequest?.request_type === "start_location" ? "Search, choose a recent place, or click where this plan starts today." : (mapPickerRequest ? `Search nearby or click the exact place for ${mapPickerRequest.title}. This point stays attached to the current draft event.` : undefined)}
        label={mapPickerRequest?.request_type === "start_location" ? "starting point" : (mapPickerRequest?.title || "this event")}
        initialCenter={mapDialogInitialCenter}
        candidates={mapDialogCandidates}
        savedLocations={mapDialogSavedLocations}
        recentLocations={recentLocations}
        initialSearchQuery={
          mapPickerRequest
            ? (locationInputs[mapPickerRequest.activity_id] || mapPickerRequest.current_guess || mapPickerRequest.title)
            : ""
        }
        searchCategory={mapPickerRequest?.category}
        confirmLabel="Use this point for this event"
        saving={isSavingMapPin}
        onConfirm={handleConfirmMapLocation}
      />
    </div >
  );
}

// Helper functions
function calculateEndTime(startTime: string, durationStr: string): string {
  const startMins = timeToMinutes(startTime);
  let durationMins = 60;

  // Simple calculation - would need more sophisticated parsing in production
  const num = parseFloat(durationStr);
  if (!isNaN(num)) {
    if (durationStr.includes('h')) durationMins = num * 60;
    else if (durationStr.includes('m')) durationMins = num;
    else durationMins = num * 60; // default to hours
  }

  const endTotal = startMins + durationMins;
  const h = Math.floor(endTotal / 60) % 24;
  const m = endTotal % 60;
  return formatTo12Hour(`${h}:${m}`);
}

function withDisplayOnlyStartRouteRow(schedule: DailySchedule): ActivityBlock[] {
  const baseBlocks = [...(schedule.schedule_blocks || schedule.activities || [])];
  const blocksWithRouteWarnings = withDisplayOnlyFixedRouteConflictRows(baseBlocks, schedule);
  const summary = (schedule.start_route_summary || {}) as Record<string, unknown>;
  const hasUnresolvedStartRouteConflict = (schedule.route_conflicts || []).some((conflict) => {
    const reasonCode = String(conflict.reason_code || "");
    const hasStartRouteMarker = Boolean(
      conflict.leave_by ||
      conflict.first_physical_event ||
      conflict.blocker_activity_id ||
      conflict.blocker_activity_title,
    );
    return reasonCode === "start_route_blocker" || (reasonCode === "fixed_to_fixed_infeasible" && hasStartRouteMarker);
  });
  if (hasUnresolvedStartRouteConflict) {
    return blocksWithRouteWarnings;
  }
  const startLocation = String(summary.start_location || "").trim();
  const firstEvent = String(summary.first_physical_event || "").trim();
  const destinationLocation = String(
    summary.first_physical_event_location ||
    summary.destination_location ||
    summary.to_location ||
    firstEvent
  ).trim();
  const leaveBy = String(summary.leave_by || "").trim();
  const duration = Number(summary.travel_duration_minutes || 0);
  if (!startLocation || !destinationLocation || !leaveBy || !duration) {
    return blocksWithRouteWarnings;
  }

  const explicitEnd = String(summary.first_physical_event_start || "").trim();
  const endTime = explicitEnd || minutesTo12Hour(timeToMinutes(leaveBy) + duration);
  const startRouteBlock: ActivityBlock = {
    id: "__start_route__",
    type: "start_route",
    block_type: "start_route",
    title: `Leave ${startLocation} by ${leaveBy}`,
    startTime: leaveBy,
    endTime,
    start: leaveBy,
    end: endTime,
    duration_minutes: duration,
    display_label: `Leave ${startLocation} by ${leaveBy} · ${duration} min travel to ${destinationLocation}`,
    location: destinationLocation,
    is_start_route: true,
    display_only: true,
  };

  return [...blocksWithRouteWarnings, startRouteBlock];
}

function withDisplayOnlyFixedRouteConflictRows(blocks: ActivityBlock[], schedule: DailySchedule): ActivityBlock[] {
  const existingKeys = new Set(
    blocks
      .filter(block => block.is_route_conflict || block.reason_code === "fixed_to_fixed_infeasible" || block.block_type === "route_conflict")
      .map(block => String(block.id || block.title || "")),
  );
  const warningRows = ((schedule.route_conflicts || []) as Array<Record<string, unknown>>)
    .filter(conflict => String(conflict.reason_code || "") === "fixed_to_fixed_infeasible")
    .map((conflict, index): ActivityBlock | null => {
      const fromTitle = String(conflict.from_activity || conflict.from || "Previous fixed event");
      const toTitle = String(conflict.to_activity || conflict.to || "Next fixed event");
      const required = Number(conflict.required_travel_minutes || conflict.required_route_minutes || 0);
      const available = Number(conflict.available_gap_minutes || conflict.available_minutes || 0);
      if (required <= 0) return null;
      const id = `fixed_route_conflict_${index}_${fromTitle}_${toTitle}`;
      if (existingKeys.has(id)) return null;
      const start = String(conflict.from_end || conflict.to_start || "").trim();
      if (!start) return null;
      const toStart = String(conflict.to_start || "").trim();
      const startMinutes = timeToMinutes(start);
      const toStartMinutes = toStart ? timeToMinutes(toStart) : NaN;
      const visualDuration = Math.max(5, Math.min(required, 15));
      const visualStartMinutes = Number.isFinite(toStartMinutes) && toStartMinutes <= startMinutes
        ? Math.max(0, startMinutes - visualDuration)
        : startMinutes;
      const visualEndMinutes = Number.isFinite(toStartMinutes) && toStartMinutes > startMinutes
        ? toStartMinutes
        : startMinutes;
      const visualStart = minutesTo12Hour(visualStartMinutes);
      const end = minutesTo12Hour(visualEndMinutes);
      return {
        id,
        stable_activity_id: id,
        type: "route_conflict",
        block_type: "route_conflict",
        title: `Route conflict: ${fromTitle} -> ${toTitle}`,
        startTime: visualStart,
        endTime: end,
        start: visualStart,
        end,
        duration_minutes: Math.max(1, visualEndMinutes - visualStartMinutes),
        route_duration_minutes: required,
        display_label: `${fromTitle} -> ${toTitle}: needs ${required} min, only ${available} min.`,
        is_route_conflict: true,
        display_only: true,
        reason_code: "fixed_to_fixed_infeasible",
        from_activity: fromTitle,
        to_activity: toTitle,
        from_location: String(conflict.from_location || ""),
        to_location: String(conflict.to_location || ""),
      };
    })
    .filter((row): row is ActivityBlock => Boolean(row));

  return warningRows.length ? [...blocks, ...warningRows] : blocks;
}

function syncActivityBlocks(
  existingBlocks: ActivityBlock[] | undefined,
  activities: ActivityBlock[],
): ActivityBlock[] | undefined {
  if (!existingBlocks) return activities;

  const activityMap = new Map(activities.map((activity) => [activity.id, activity]));
  const usedIds = new Set<string>();

  const syncedBlocks = existingBlocks.map((block) => {
    const replacement = activityMap.get(block.id);
    const blockType = block.block_type || block.type;

    if (!replacement || (blockType && blockType !== "activity")) {
      return block;
    }

    usedIds.add(replacement.id);
    return {
      ...block,
      ...replacement,
      start: replacement.startTime,
      end: replacement.endTime,
    };
  });

  for (const activity of activities) {
    if (!usedIds.has(activity.id)) {
      syncedBlocks.push({
        ...activity,
        start: activity.startTime,
        end: activity.endTime,
      });
    }
  }

  return syncedBlocks.sort((a, b) => {
    const startA = timeToMinutes(a.startTime || a.start || "");
    const startB = timeToMinutes(b.startTime || b.start || "");
    return startA - startB;
  });
}

function removeDeletedActivityBlock(
  existingBlocks: ActivityBlock[] | undefined,
  deletedId: string,
): ActivityBlock[] | undefined {
  if (!existingBlocks) return existingBlocks;
  const deletedIndex = existingBlocks.findIndex((block) => blockMatchesDeletedActivity(block, deletedId));
  const fallbackSupportIndexes = new Set<number>();

  if (deletedIndex >= 0) {
    for (let index = deletedIndex - 1; index >= 0; index -= 1) {
      const block = existingBlocks[index];
      if (!isSupportCleanupBlock(block) || isDisplayOnlyStartRouteBlock(block)) break;
      fallbackSupportIndexes.add(index);
    }
    for (let index = deletedIndex + 1; index < existingBlocks.length; index += 1) {
      const block = existingBlocks[index];
      if (!isSupportCleanupBlock(block) || isDisplayOnlyStartRouteBlock(block)) break;
      fallbackSupportIndexes.add(index);
    }
  }

  return existingBlocks.filter((block, index) => {
    if (blockMatchesDeletedActivity(block, deletedId)) return false;
    if (isDisplayOnlyStartRouteBlock(block)) return true;
    if (isSupportLinkedToDeletedActivity(block, deletedId)) return false;
    if (fallbackSupportIndexes.has(index)) return false;
    return true;
  });
}

function blockMatchesDeletedActivity(block: ActivityBlock, deletedId: string): boolean {
  return String(block.id || "") === deletedId || String(block.stable_activity_id || "") === deletedId;
}

function isDisplayOnlyStartRouteBlock(block: ActivityBlock): boolean {
  return Boolean(block.is_start_route || block.display_only || block.type === "start_route" || block.block_type === "start_route");
}

function isSupportCleanupBlock(block: ActivityBlock): boolean {
  const blockType = String(block.block_type || block.type || "").toLowerCase();
  const title = String(block.title || "").toLowerCase();
  return blockType === "travel"
    || blockType === "transition"
    || blockType === "buffer"
    || title.startsWith("travel to")
    || title.includes("buffer");
}

function isSupportLinkedToDeletedActivity(block: ActivityBlock, deletedId: string): boolean {
  if (!isSupportCleanupBlock(block)) return false;
  const relatedIds = [
    block.source_activity_id,
    block.destination_activity_id,
    ...(Array.isArray(block.related_activity_ids) ? block.related_activity_ids : []),
  ].map((value) => String(value || "")).filter(Boolean);
  if (relatedIds.includes(deletedId)) return true;
  return Boolean(block.id && String(block.id).includes(deletedId));
}

function minutesTo12Hour(totalMinutes: number): string {
  const normalized = ((totalMinutes % (24 * 60)) + (24 * 60)) % (24 * 60);
  const hours = Math.floor(normalized / 60);
  const minutes = normalized % 60;
  const period = hours >= 12 ? "PM" : "AM";
  const displayHour = hours % 12 || 12;
  return `${displayHour}:${minutes.toString().padStart(2, "0")} ${period}`;
}

function formatTo12Hour(timeStr: string): string {
  if (!timeStr) return "";
  if (timeStr.includes("AM") || timeStr.includes("PM")) return timeStr;

  let [hours, minutes] = timeStr.split(':').map(Number);
  const period = hours >= 12 ? "PM" : "AM";
  const displayHour = hours % 12 || 12;
  return `${displayHour}:${minutes.toString().padStart(2, "0")} ${period}`;
}

function timeToMinutes(timeStr: string): number {
  let hours = 0;
  let minutes = 0;

  if (timeStr.includes("AM") || timeStr.includes("PM")) {
    const [time, period] = timeStr.split(" ");
    [hours, minutes] = time.split(":").map(Number);
    if (period === "PM" && hours !== 12) hours += 12;
    if (period === "AM" && hours === 12) hours = 0;
  } else {
    [hours, minutes] = timeStr.split(":").map(Number);
  }
  return hours * 60 + minutes;
}
