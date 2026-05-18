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
  Lightbulb
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
  to?: string;
  reason?: string;
  impact?: string;
  requires_user_confirmation?: boolean;
};

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
  const [accurateTravelTime, setAccurateTravelTime] = useState<boolean>(Boolean(initialSchedule?.accurate_travel_time));
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
            setAccurateTravelTime(Boolean((data as DailySchedule).accurate_travel_time));
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
    const currentActivities = previewSchedule?.activities || [];
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

    setPreviewSchedule({
      ...(previewSchedule || {}),
      date: isoDateStr,
      activities: updatedActivities,
      schedule_blocks: syncActivityBlocks(previewSchedule?.schedule_blocks, updatedActivities),
    });

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
    const newStartTimeMins = timeToMinutes(updatedEvent.startTime);
    const endTimeStr = updatedEvent.endTime || calculateEndTime(updatedEvent.startTime, updatedEvent.duration || "1h");
    let newEndTimeMins = timeToMinutes(endTimeStr);

    // handle overnight activity
    if (newEndTimeMins < newStartTimeMins) {
      newEndTimeMins += 24 * 60;
    }

    // Collision Detection
    const hasCollision = previewSchedule.activities.some(activity => {
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
    const existingIndex = previewSchedule.activities.findIndex(a => a.id === updatedEvent.id);
    const updatedActivities = (
      existingIndex >= 0
        ? previewSchedule.activities.map(a => a.id === updatedEvent.id ? updatedEvent : a)
        : [...previewSchedule.activities, updatedEvent]
    ).sort((a, b) => timeToMinutes(a.startTime) - timeToMinutes(b.startTime));

    setPreviewSchedule({
      ...previewSchedule,
      activities: updatedActivities,
      schedule_blocks: syncActivityBlocks(previewSchedule.schedule_blocks, updatedActivities),
    });
    setEditingEvent(null);
    setIsConflict(false);
  };

  const handleDeleteEvent = () => {
    if (!editingEvent || !previewSchedule) return;
    const updatedActivities = previewSchedule.activities.filter(a => a.id !== editingEvent.id);
    setPreviewSchedule({
      ...previewSchedule,
      activities: updatedActivities,
      schedule_blocks: removeDeletedActivityBlock(previewSchedule.schedule_blocks, editingEvent.id),
    });
    setEditingEvent(null);
  };

  const handleSendMessage = async () => {
    if (!chatInput.trim()) return;

    const userMessage = chatInput;

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
          current_schedule: previewSchedule ? withPlanningPreferences(previewSchedule) : previewSchedule, // send current schedule if any
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
        setPreviewSchedule({
          ...(data.schedule_data as DailySchedule),
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        });
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

  const pendingLocationRequests = (previewSchedule?.location_resolution_requests || []) as LocationResolutionRequest[];
  const pendingRepairSuggestions = (previewSchedule?.pending_repair_suggestions || []) as RepairSuggestion[];
  const routeRepairActions = (previewSchedule?.route_repair_actions || []) as Array<Record<string, unknown>>;
  const routeUnfitActivities = (previewSchedule?.unfit_activities || []) as Array<Record<string, unknown>>;
  const isLocationPending = previewSchedule?.schedule_status === "location_pending" || previewSchedule?.status === "location_pending";
  const startRouteSummary = (previewSchedule?.start_route_summary || {}) as Record<string, unknown>;
  const startRouteSummaryText = (() => {
    const startLocation = String(startRouteSummary.start_location || "").trim();
    const firstEvent = String(startRouteSummary.first_physical_event || "").trim();
    const leaveBy = String(startRouteSummary.leave_by || "").trim();
    const duration = Number(startRouteSummary.travel_duration_minutes || 0);
    if (!startLocation || !firstEvent || !leaveBy || !duration) return "";
    return `Leave ${startLocation} by ${leaveBy} · ${duration} min travel to ${firstEvent}`;
  })();

  const normalizeLocationTarget = (value?: string | null) => (value || "").trim().toLowerCase();

  const matchesLocationRequest = (item: Partial<ActivityBlock>, request: LocationResolutionRequest) => {
    if (request.request_type === "start_location") return false;
    const requestId = String(request.activity_id || "");
    const itemId = String(item.stable_activity_id || item.id || "");
    if (requestId && itemId && requestId === itemId) return true;
    if (itemId && (request.related_activity_ids || []).map(String).includes(itemId)) return true;
    return normalizeLocationTarget(item.title) === normalizeLocationTarget(request.title);
  };

  const travelValidationMessage = (schedule: DailySchedule) => {
    const suggestions = (schedule.pending_repair_suggestions || []) as RepairSuggestion[];
    if (suggestions.length > 0) {
      const suggestion = suggestions[0];
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
    if (schedule.travel_validation_status === "partial_feasible_with_unfit") {
      const firstUnfit = (schedule.unfit_activities || [])[0] as Record<string, unknown> | undefined;
      return {
        message: firstUnfit?.title
          ? `Most of the plan is route-safe, but ${String(firstUnfit.title)} could not fit after accurate travel time.`
          : "Most of the plan is route-safe, but one flexible activity could not fit after accurate travel time.",
        status: "warning" as const,
      };
    }
    if (schedule.travel_validation_status === "repaired_validated") {
      const firstAction = (schedule.route_repair_actions || [])[0] as Record<string, unknown> | undefined;
      return {
        message: firstAction?.title
          ? `Adjusted for accurate travel: ${String(firstAction.title)} shifted to ${String(firstAction.to || "a route-safe time")}.`
          : "Adjusted the plan for accurate travel time.",
        status: "success" as const,
      };
    }
    if ((schedule.location_resolution_requests || []).length > 0 || schedule.travel_validation_status === "pending_locations") {
      return {
        message: "Please confirm the exact locations first so I can calculate accurate travel time.",
        status: "location_pending" as const,
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
      if (request.request_type === "start_location") {
        const remaining = ((prev.location_resolution_requests || []) as LocationResolutionRequest[])
          .filter(item => item.activity_id !== request.activity_id);
        return {
          ...prev,
          preferences: {
            ...(prev.preferences || {}),
            day_start_location_override: candidateToPlanningLocation(candidate, "start_location"),
          },
          location_resolution_requests: remaining,
          schedule_status: remaining.length ? prev.schedule_status : "location_pending",
          travel_validation_status: remaining.length ? prev.travel_validation_status : "pending_locations",
        };
      }
      const remaining = ((prev.location_resolution_requests || []) as LocationResolutionRequest[])
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
      return {
        ...prev,
        activities: (prev.activities || []).map(item => attachResolvedLocation(item, request, candidate)),
        schedule_blocks: (prev.schedule_blocks || []).map(item => attachResolvedLocation(item, request, candidate)),
        location_resolution_requests: remaining,
        schedule_status: remaining.length ? prev.schedule_status : "location_pending",
        travel_validation_status: remaining.length ? prev.travel_validation_status : "pending_locations",
        validation_issues: (prev.validation_issues || []).filter(issue => !String(issue).includes(request.title)),
      };
    });
    setLocationCandidates(prev => {
      const next = { ...prev };
      delete next[request.activity_id];
      return next;
    });
  };

  const reusableLocationOptions = (request: LocationResolutionRequest) => {
    if (!previewSchedule || request.request_type === "start_location") return [];
    const candidates = [
      ...(previewSchedule.activities || []),
      ...(previewSchedule.schedule_blocks || []),
    ]
      .filter(item => !matchesLocationRequest(item, request))
      .map(item => {
        const resolved = item.resolved_location;
        if (!hasLocationCoordinates(resolved)) return null;
        const candidate: GeocodeCandidate = {
          label: resolved?.saved_location_label || resolved?.label || item.location_label || item.location,
          display_name: resolved?.display_name || item.location_label || item.location || item.title,
          address: resolved?.address || resolved?.display_name || item.location_label || item.location || item.title,
          latitude: Number(resolved?.latitude),
          longitude: Number(resolved?.longitude),
          source: "same_as_activity",
          confirmed_by_user: true,
        };
        const title = item.title || "another event";
        const sameAsAnchor = request.same_location_as && normalizeLocationTarget(title) === normalizeLocationTarget(request.same_location_as);
        const sameLabel = request.current_guess && (
          normalizeLocationTarget(item.location_label) === normalizeLocationTarget(request.current_guess) ||
          normalizeLocationTarget(item.location) === normalizeLocationTarget(request.current_guess) ||
          normalizeLocationTarget(resolved?.label) === normalizeLocationTarget(request.current_guess)
        );
        if (sameAsAnchor) {
          return {
            key: `anchor:${title}`,
            label: `Use same location as ${title}`,
            source: "inferred_from_anchor",
            candidate,
          };
        }
        if (sameLabel) {
          return {
            key: `label:${request.current_guess}:${title}`,
            label: `Apply this ${request.current_guess} location to all matching events`,
            source: "same_label_reuse",
            candidate,
          };
        }
        return {
          key: `same:${title}`,
          label: `Same as ${title}`,
          source: "same_as_activity",
          candidate,
        };
      })
      .filter(Boolean) as Array<{ key: string; label: string; source: string; candidate: GeocodeCandidate }>;

    const seen = new Set<string>();
    return candidates.filter(option => {
      const coordKey = `${option.source}:${option.candidate.latitude.toFixed(6)}:${option.candidate.longitude.toFixed(6)}:${option.label}`;
      if (seen.has(coordKey)) return false;
      seen.add(coordKey);
      return true;
    }).slice(0, 4);
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
      applyResolvedLocationToDraft(request, candidate);
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: `Confirmed ${request.title} at ${candidate.display_name || candidate.address || request.current_guess || request.title}. This map point is attached to the current draft.`,
        status: "success",
      }]);
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
    if (!previewSchedule || !user?.id || !hasScheduleItems) {
      setAccurateTravelTime(false);
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: "Create or select a plan first, then I can calculate accurate travel time.",
        status: "clarification_needed",
      }]);
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
      setPreviewSchedule({
        ...withPlanningPreferences(validated),
        allow_clash: allowClash,
        accurate_travel_time: true,
      });
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
        setPreviewSchedule({
          ...withPlanningPreferences(data.schedule_data as DailySchedule),
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        });
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

  const handleCompleteTravelValidation = async () => {
    await runTravelValidation("manual");
  };

  const handleAccurateTravelToggle = async (checked: boolean) => {
    if (!checked) {
      setAccurateTravelTime(false);
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
      const nextPreferences: Record<string, unknown> = {
        ...(prev.preferences || {}),
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
      return {
        ...prev,
        preferences: nextPreferences,
      };
    });
    setIsEditingPlanSettings(false);
  };

  const timelineActivities = previewSchedule ? [...(previewSchedule.schedule_blocks || previewSchedule.activities || [])] : [];

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
                  interactive={true}
                  onActivityClick={handleEventClick}
                  showEditIcon={true}
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
                    {pendingRepairSuggestions.length > 0 && (
                      <div className="rounded-2xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">Accurate travel repair suggestion</p>
                            {pendingRepairSuggestions.map((suggestion) => (
                              <p key={suggestion.id || suggestion.title} className="mt-1 text-xs text-amber-800">
                                Move {suggestion.title || "activity"} from {suggestion.from || "its current time"} to {suggestion.to || "a safer time"}.
                                {suggestion.reason ? ` ${suggestion.reason}.` : ""}
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
                    {previewSchedule && (startRouteSummaryText || routeRepairActions.length > 0 || routeUnfitActivities.length > 0 || previewSchedule.travel_validation_status === "route_conflict") && (
                      <div className={`rounded-2xl border p-3 text-sm ${
                        previewSchedule.travel_validation_status === "route_conflict"
                          ? "border-red-200 bg-red-50 text-red-950"
                          : previewSchedule.travel_validation_status === "partial_feasible_with_unfit"
                            ? "border-amber-200 bg-amber-50 text-amber-950"
                            : "border-emerald-200 bg-emerald-50 text-emerald-950"
                      }`}>
                        <div className="flex items-start gap-2">
                          <Clock size={16} className="mt-0.5 shrink-0" />
                          <div className="min-w-0 flex-1">
                            <p className="font-medium">
                              {previewSchedule.travel_validation_status === "repaired_validated"
                                ? "Adjusted for accurate travel"
                                : previewSchedule.travel_validation_status === "partial_feasible_with_unfit"
                                  ? "Some items could not fit"
                                  : previewSchedule.travel_validation_status === "route_conflict"
                                    ? "Route timing needs attention"
                                    : "Accurate travel start route"}
                            </p>
                            {startRouteSummaryText && (
                              <p className="mt-1 text-xs opacity-90">
                                {startRouteSummaryText}
                              </p>
                            )}
                            {routeRepairActions.map((action, index) => (
                              <p key={`${action.title || "repair"}-${index}`} className="mt-1 text-xs opacity-90">
                                {String(action.title || "Activity")} moved from {String(action.from || "its original time")} to {String(action.to || "a route-safe time")}.
                                {action.reason ? ` ${String(action.reason)}.` : ""}
                              </p>
                            ))}
                            {routeUnfitActivities.map((item, index) => (
                              <p key={`${item.title || "unfit"}-${index}`} className="mt-1 text-xs opacity-90">
                                {String(item.title || "One flexible activity")} could not fit after accurate travel time.
                              </p>
                            ))}
                            {previewSchedule.travel_validation_status === "route_conflict" && routeRepairActions.length === 0 && routeUnfitActivities.length === 0 && (
                              <p className="mt-1 text-xs opacity-90">
                                Accurate travel time still creates an unresolved route conflict.
                              </p>
                            )}
                          </div>
                        </div>
                      </div>
                    )}
                    {pendingLocationRequests.length > 0 && (
                      <div className="space-y-3">
                        {pendingLocationRequests.map((request) => {
                          const primaryReuseOption = reusableLocationOptions(request)[0];
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
                                {primaryReuseOption && (
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    className="rounded-xl bg-white"
                                    disabled={resolvingLocationId === request.activity_id}
                                    onClick={() => {
                                      const applied = [
                                        request.title,
                                        ...(request.related_titles || []),
                                      ].filter(Boolean);
                                      console.debug("[JPLAN][LOCATION_REUSE]", { source: primaryReuseOption.source, applied_to: applied });
                                      handleConfirmLocation(request, {
                                        ...primaryReuseOption.candidate,
                                        source: primaryReuseOption.source,
                                      });
                                    }}
                                  >
                                    {primaryReuseOption.label}
                                  </Button>
                                )}
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
                          Complete travel-aware schedule
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
                        Complete travel-aware schedule
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
              onClick={() => {
                const baseSchedule = withPlanningPreferences(previewSchedule || { date: isoDateStr, activities: [] });
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
                onScheduleGenerated(finalSchedule);
              }}
              className="w-full rounded-xl py-6 text-lg font-semibold shadow-lg hover:shadow-xl transition-all"
              disabled={!previewSchedule || previewSchedule.activities.length === 0 || isProcessing || isLocationPending}
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
                  {isLocationPending ? "Resolve locations before saving" : "Save & Implement Plan"}
                </>
              )}
            </Button>
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
  return existingBlocks.filter((block) => block.id !== deletedId);
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
