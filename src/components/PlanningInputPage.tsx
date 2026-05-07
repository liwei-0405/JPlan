import { useState, useEffect, useRef } from "react";
import { useParams } from "react-router-dom";
import { completeTravelValidation, geocodeLocation, getPlanByDate, getSavedLocations, type GeocodeCandidate, type SavedLocation } from "../services/planService";
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
  title: string;
  category?: string;
  current_guess?: string;
  expanded_query?: string;
  reason?: string;
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
    getSavedLocations(user.id)
      .then(setSavedLocationsForPicker)
      .catch((error) => console.error("Failed to fetch saved locations for picker:", error));
  }, [user?.id]);
  // Store the original schedule string for comparison to detect changes
  const originalScheduleJson = useRef(JSON.stringify(initialSchedule || {
    date: isoDateStr,
    activities: []
  })).current;

  // Sync state with parent whenever it changes
  useEffect(() => {
    if (previewSchedule) {
      onUpdateSchedule({
        ...previewSchedule,
        allow_clash: allowClash,
        accurate_travel_time: accurateTravelTime,
        preferences: {
          ...(previewSchedule.preferences || {}),
          allow_clash: allowClash,
          accurate_travel_time: accurateTravelTime,
        },
        planning_mode: allowClash ? "clash_allowed" : "feasibility_first",
      });
    }
  }, [previewSchedule, onUpdateSchedule, allowClash, accurateTravelTime]);

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
  const [conversationHistory, setConversationHistory] = useState<Array<{ role: "user" | "assistant", message: string, status?: "success" | "partial" | "warning" | "location_pending" | "conflict" | "error" }>>([
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
    setIsProcessing(true);

    try {
      const response = await fetch("http://127.0.0.1:8000/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: userMessage,
          history: currentHistory, // send full history for context
          current_schedule: previewSchedule, // send current schedule if any
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
    }
  };

  const pendingLocationRequests = (previewSchedule?.location_resolution_requests || []) as LocationResolutionRequest[];
  const isLocationPending = previewSchedule?.schedule_status === "location_pending" || previewSchedule?.status === "location_pending";

  const normalizeLocationTarget = (value?: string | null) => (value || "").trim().toLowerCase();

  const matchesLocationRequest = (item: Partial<ActivityBlock>, request: LocationResolutionRequest) => {
    const requestId = String(request.activity_id || "");
    const itemId = String(item.stable_activity_id || item.id || "");
    if (requestId && itemId && requestId === itemId) return true;
    return normalizeLocationTarget(item.title) === normalizeLocationTarget(request.title);
  };

  const attachResolvedLocation = (
    item: ActivityBlock,
    request: LocationResolutionRequest,
    candidate: GeocodeCandidate,
  ): ActivityBlock => {
    if (!matchesLocationRequest(item, request)) return item;

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
    setPreviewSchedule(prev => {
      if (!prev) return prev;
      const remaining = ((prev.location_resolution_requests || []) as LocationResolutionRequest[])
        .filter(item => item.activity_id !== request.activity_id);
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

  const handleCompleteTravelValidation = async () => {
    if (!previewSchedule || !user?.id) return;
    setIsProcessing(true);
    try {
      const validated = await completeTravelValidation({
        ...previewSchedule,
        accurate_travel_time: true,
        preferences: {
          ...(previewSchedule.preferences || {}),
          accurate_travel_time: true,
        },
      }, user.id);
      setPreviewSchedule({
        ...validated,
        allow_clash: allowClash,
        accurate_travel_time: true,
      });
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: validated.travel_validation_status === "validated"
          ? "Travel-aware validation is complete with accurate route timing."
          : "I rechecked travel timing. Some route timing still needs attention, so I marked it in the draft.",
        status: validated.schedule_status === "route_conflict" ? "conflict" : validated.travel_validation_status === "fallback_used" ? "warning" : "success",
      }]);
    } catch (error) {
      setConversationHistory(prev => [...prev, {
        role: "assistant",
        message: error instanceof Error ? error.message : "I couldn't complete travel validation.",
        status: "warning",
      }]);
    } finally {
      setIsProcessing(false);
    }
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
              <div>
                <h3 className="text-lg font-bold">Live Schedule</h3>
                <p className="text-xs text-muted-foreground">{previewSchedule?.date || "No activities yet"}</p>
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
                  activities={previewSchedule.schedule_blocks || previewSchedule.activities}
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
                  onCheckedChange={setAccurateTravelTime}
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
                        <div className={`max-w-[85%] p-3 rounded-2xl text-sm ${msg.role === 'user' ? 'bg-primary text-primary-foreground' : msg.status === 'conflict' ? 'bg-destructive/10 text-destructive border border-destructive/20' : msg.status === 'partial' || msg.status === 'warning' || msg.status === 'location_pending' ? 'bg-yellow-50 text-yellow-900 border border-yellow-200' : 'bg-secondary'
                          }`}>
                          {msg.message}
                        </div>
                      </div>
                    ))}
                    {pendingLocationRequests.length > 0 && (
                      <div className="space-y-3">
                        {pendingLocationRequests.map((request) => {
                          const savedMatches = request.saved_matches || [];
                          return (
                            <div key={request.activity_id} className="rounded-2xl border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-950">
                              <div className="flex items-start gap-2">
                                <MapPin size={16} className="mt-0.5 shrink-0" />
                                <div className="min-w-0 flex-1">
                                  <p className="font-medium">Select location for {request.title}</p>
                                  <p className="text-xs text-yellow-800">{request.reason}</p>
                                  {request.affected_transitions?.length ? (
                                    <p className="mt-1 text-xs text-yellow-800">
                                      Affects: {request.affected_transitions.map(t => `${t.from_activity || "Previous"} → ${t.to_activity || "Next"}`).join(", ")}
                                    </p>
                                  ) : null}
                                </div>
                              </div>

                              {savedMatches.length > 0 && (
                                <div className="mt-3 space-y-1">
                                  <p className="text-xs font-medium">Saved places</p>
                                  {savedMatches.map((candidate, index) => (
                                    <Button
                                      key={`${candidate.display_name}-${index}`}
                                      variant="outline"
                                      size="sm"
                                      className="mr-2 mt-1 rounded-xl bg-white"
                                      disabled={resolvingLocationId === request.activity_id}
                                      onClick={() => handleConfirmLocation(request, candidate)}
                                    >
                                      {candidate.label || candidate.display_name || candidate.address || "Saved location"}
                                    </Button>
                                  ))}
                                </div>
                              )}

                              <div className="mt-3 flex gap-2">
                                <Input
                                  value={locationInputs[request.activity_id] ?? request.current_guess ?? ""}
                                  onChange={(event) => setLocationInputs(prev => ({ ...prev, [request.activity_id]: event.target.value }))}
                                  placeholder="Search address or place name"
                                  className="h-9 rounded-xl bg-white"
                                />
                                <Button
                                  size="sm"
                                  variant="outline"
                                  className="rounded-xl bg-white"
                                  disabled={resolvingLocationId === request.activity_id}
                                  onClick={() => handleSearchLocation(request)}
                                >
                                  Search / pick on map
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
                        <div className="bg-secondary p-3 rounded-2xl animate-pulse text-xs italic">AI is thinking...</div>
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
                const finalSchedule = {
                  ...(previewSchedule || { date: isoDateStr, activities: [] }),
                  allow_clash: allowClash,
                  accurate_travel_time: accurateTravelTime,
                  preferences: {
                    ...((previewSchedule || {}) as DailySchedule).preferences,
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
                    onScheduleGenerated(previewSchedule);
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
        title={mapPickerRequest ? `Pick location for ${mapPickerRequest.title}` : "Pick location on map"}
        description={mapPickerRequest ? `Search nearby or click the exact place for ${mapPickerRequest.title}. This point stays attached to the current draft event.` : undefined}
        label={mapPickerRequest?.title || "this event"}
        initialCenter={mapDialogInitialCenter}
        candidates={mapDialogCandidates}
        savedLocations={mapDialogSavedLocations}
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

function parseTime(timeStr: string): number {
  // Handle both 24-hour and 12-hour formats
  if (timeStr.includes('AM') || timeStr.includes('PM')) {
    const [time, period] = timeStr.split(" ");
    const [hours, minutes] = time.split(":").map(Number);
    let hour24 = hours;
    if (period === "PM" && hours !== 12) hour24 += 12;
    if (period === "AM" && hours === 12) hour24 = 0;
    return hour24 + (minutes || 0) / 60;
  } else {
    const [hours, minutes] = timeStr.split(":").map(Number);
    return hours + (minutes || 0) / 60;
  }
}

// Helper function to generate mock schedule
function generateMockSchedule(input: string, additionalContext: string = ""): DailySchedule {
  const today = new Date();
  const dateStr = today.toLocaleDateString("en-US", {
    weekday: "long",
    day: "numeric",
    month: "long",
    year: "numeric",
  });

  const fullInput = input + " " + additionalContext;

  // Parse input to create activities (simplified mock logic)
  const activities: any[] = [];
  let currentTime = 9; // Start at 9 AM

  if (fullInput.toLowerCase().includes("gym")) {
    activities.push({
      id: "1",
      type: "activity",
      title: "Gym Session",
      startTime: formatTime(currentTime),
      endTime: formatTime(currentTime + 1),
      location: "Local Gym",
      duration: "1 hour",
    });
    currentTime += 1;

    // Add travel buffer
    activities.push({
      id: "2",
      type: "travel",
      title: "Travel time",
      startTime: formatTime(currentTime),
      endTime: formatTime(currentTime + 0.5),
      duration: "30 min",
    });
    currentTime += 0.5;
  }

  if (fullInput.toLowerCase().includes("meeting")) {
    activities.push({
      id: "3",
      type: "activity",
      title: "Team Meeting",
      startTime: formatTime(14),
      endTime: formatTime(15),
      location: "Conference Room B",
      duration: "1 hour",
    });

    activities.push({
      id: "4",
      type: "travel",
      title: "Travel time",
      startTime: formatTime(15),
      endTime: formatTime(15.25),
      duration: "15 min",
    });
    currentTime = 15.25;
  }

  if (fullInput.toLowerCase().includes("groceries") || fullInput.toLowerCase().includes("shopping")) {
    activities.push({
      id: "5",
      type: "activity",
      title: "Grocery Shopping",
      startTime: formatTime(currentTime),
      endTime: formatTime(currentTime + 0.75),
      location: "SuperMart Downtown",
      duration: "45 min",
    });
    currentTime += 0.75;
  }

  if (fullInput.toLowerCase().includes("lunch")) {
    const lunchTime = 12.5;
    activities.push({
      id: "7",
      type: "activity",
      title: "Lunch with Sarah",
      startTime: formatTime(lunchTime),
      endTime: formatTime(lunchTime + 1),
      location: "Cafe Centro",
      duration: "1 hour",
    });
  }

  // Sort by start time
  activities.sort((a, b) => {
    const timeA = parseTime(a.startTime);
    const timeB = parseTime(b.startTime);
    return timeA - timeB;
  });

  return {
    date: dateStr,
    activities,
  };
}

function formatTime(hour: number): string {
  const h = Math.floor(hour);
  const m = Math.round((hour - h) * 60);
  const period = h >= 12 ? "PM" : "AM";
  const displayHour = h > 12 ? h - 12 : h === 0 ? 12 : h;
  return `${displayHour}:${m.toString().padStart(2, "0")} ${period}`;
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
