import { useState, useEffect, useRef } from "react";
import { useParams } from "react-router-dom";
import { getPlanByDate } from "../services/planService";
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


type PlanningInputPageProps = {
  onScheduleGenerated: (schedule: DailySchedule) => void;
  onBack: () => void;
  onViewExplanation: (schedule: DailySchedule) => void;
  selectedDate: Date;
  initialSchedule: DailySchedule | null;
  onUpdateSchedule: (schedule: DailySchedule) => void;
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

  // Auto-load data on mount/refresh if missing
  useEffect(() => {
    if (!initialSchedule && user && isoDateStr) {
      const loadData = async () => {
        try {
          const data = await getPlanByDate(isoDateStr, user.id);
          if (data) {
            setPreviewSchedule(data as DailySchedule);
            setAllowClash(Boolean((data as DailySchedule).allow_clash));
          }
        } catch (err) {
          console.error("Failed to auto-load schedule:", err);
        }
      };
      loadData();
    }
  }, [isoDateStr, user, initialSchedule]);
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
        planning_mode: allowClash ? "clash_allowed" : "feasibility_first",
      });
    }
  }, [previewSchedule, onUpdateSchedule, allowClash]);

  const [editingEvent, setEditingEvent] = useState<ActivityBlock | null>(null);
  const [showExitConfirmation, setShowExitConfirmation] = useState(false);

  // Manual form state
  const [activityName, setActivityName] = useState("");
  const [activityTime, setActivityTime] = useState("");
  const [activityDuration, setActivityDuration] = useState("");
  const [activityLocation, setActivityLocation] = useState("");
  const [manualActivityType, setManualActivityType] = useState<"activity" | "travel" | "buffer">("activity");
  const [isConflict, setIsConflict] = useState(false);

  // Chat state
  const [chatInput, setChatInput] = useState("");
  const [isProcessing, setIsProcessing] = useState(false);
  const [conversationHistory, setConversationHistory] = useState<Array<{ role: "user" | "assistant", message: string }>>([
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

    const newActivity: ActivityBlock = {
      id: Date.now().toString(),
      type: manualActivityType,
      title: activityName,
      startTime: formattedStartTime,
      endTime: endTimeStr,
      duration: displayDuration,
      location: manualActivityType === "activity" ? (activityLocation || undefined) : undefined,
    };

    const updatedActivities = [...currentActivities, newActivity].sort(
      (a, b) => timeToMinutes(a.startTime) - timeToMinutes(b.startTime)
    );

    setPreviewSchedule({
      date: isoDateStr,
      activities: updatedActivities,
    });

    // Reset form
    setActivityName(""); setActivityTime(""); setActivityDuration(""); setActivityLocation("");
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
    const updatedActivities = previewSchedule.activities.map(a =>
      a.id === updatedEvent.id ? updatedEvent : a
    ).sort((a, b) => timeToMinutes(a.startTime) - timeToMinutes(b.startTime));

    setPreviewSchedule({ ...previewSchedule, activities: updatedActivities });
    setEditingEvent(null);
    setIsConflict(false);
  };

  const handleDeleteEvent = () => {
    if (!editingEvent || !previewSchedule) return;
    const updatedActivities = previewSchedule.activities.filter(a => a.id !== editingEvent.id);
    setPreviewSchedule({ ...previewSchedule, activities: updatedActivities });
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
        }),
      });

      if (!response.ok) {
        throw new Error("Failed to connect to backend");
      }

      const data = await response.json();

      // Add assistant response to conversation
      setConversationHistory(prev => [...prev, {
        role: "assistant" as const,
        message: data.reply
      }]);

      if (data.schedule_data) {
        setPreviewSchedule({
          ...(data.schedule_data as DailySchedule),
          allow_clash: allowClash,
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

            {/* Dynamic Content Area */}
            <div className="bg-card rounded-2xl border border-border shadow-sm flex flex-col overflow-hidden" style={{ height: "380px" }}>
              {activeMode === "assistant" ? (
                /* Assistant UI */
                <>
                  <div className="flex-1 overflow-y-auto p-4 space-y-4">
                    {conversationHistory.map((msg, i) => (
                      <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                        <div className={`max-w-[85%] p-3 rounded-2xl text-sm ${msg.role === 'user' ? 'bg-primary text-primary-foreground' : 'bg-secondary'
                          }`}>
                          {msg.message}
                        </div>
                      </div>
                    ))}
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
                        <Input value={activityLocation} onChange={e => setActivityLocation(e.target.value)} placeholder="Optional" className="rounded-xl" />
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
                  planning_mode: allowClash ? "clash_allowed" : "feasibility_first",
                };
                onScheduleGenerated(finalSchedule);
              }}
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
                  Save & Implement Plan
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
