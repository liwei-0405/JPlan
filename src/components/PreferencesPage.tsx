import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { AlertCircle, ArrowLeft, Home, Info, Loader2, MapPin, Trash2 } from "lucide-react";
import { Switch } from "./ui/switch";
import { Checkbox } from "./ui/checkbox";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import { useAuth } from "../context/AuthContext";
import { LocationPickerDialog } from "./LocationPickerDialog";
import {
  deleteSavedLocation,
  getPlanningPreferences,
  getRecentLocations,
  getSavedLocations,
  addRecentLocationRemote,
  resolveLocation,
  savePlanningPreferencesRemote,
  type GeocodeCandidate,
  type SavedLocation,
} from "../services/planService";
import {
  addRecentLocation,
  candidateToPlanningLocation,
  loadPlanningPreferences,
  normalizePlanningPreferences,
  savePlanningPreferences,
  saveRecentLocations,
  savedLocationToPlanningLocation,
  normalizeBufferMinutes,
  isValidDayWindow,
  toCanonicalTime,
  type PlanningLocation,
} from "../utils/planningPreferences";

type PreferencesPageProps = {
  onBack: () => void;
};

function preferenceSnapshot(
  dayStart: string,
  dayEnd: string,
  useDayBoundaries: boolean,
  defaultStartLocation: PlanningLocation | null,
  defaultBufferMinutes: number,
) {
  return JSON.stringify({
    day_start_time: toCanonicalTime(dayStart) || "08:00",
    day_end_time: toCanonicalTime(dayEnd) || "22:00",
    use_day_boundary_preferences: useDayBoundaries,
    default_buffer_minutes: normalizeBufferMinutes(defaultBufferMinutes),
    default_start_location: defaultStartLocation
      ? {
          label: defaultStartLocation.label,
          display_name: defaultStartLocation.display_name,
          address: defaultStartLocation.address,
          latitude: defaultStartLocation.latitude,
          longitude: defaultStartLocation.longitude,
          source: defaultStartLocation.source,
        }
      : null,
  });
}

export function PreferencesPage({ onBack }: PreferencesPageProps) {
  const { user } = useAuth();
  const [dayStart, setDayStart] = useState("08:00");
  const [dayEnd, setDayEnd] = useState("22:00");
  const [useDayBoundaries, setUseDayBoundaries] = useState(true);
  const [defaultStartLocation, setDefaultStartLocation] = useState<PlanningLocation | null>(null);
  const [defaultBufferMinutes, setDefaultBufferMinutes] = useState(5);

  // --- Location Management State ---
  const [savedLocations, setSavedLocations] = useState<SavedLocation[]>([]);
  const [newLocLabel, setNewLocLabel] = useState("");
  const [locationNotice, setLocationNotice] = useState<string | null>(null);
  const [locationError, setLocationError] = useState<string | null>(null);
  const [isMapPickerOpen, setIsMapPickerOpen] = useState(false);
  const [isSavingMapPin, setIsSavingMapPin] = useState(false);
  const [isChoosingDefaultStart, setIsChoosingDefaultStart] = useState(false);
  const [pendingDefaultStartLocation, setPendingDefaultStartLocation] = useState<PlanningLocation | null>(null);
  const [originalPreferencesJson, setOriginalPreferencesJson] = useState("");
  const [showExitConfirmation, setShowExitConfirmation] = useState(false);
  const [isSavingPreferences, setIsSavingPreferences] = useState(false);

  useEffect(() => {
    if (user) {
      const prefs = loadPlanningPreferences(user.id);
      setDayStart(toCanonicalTime(prefs.day_start_time) || "08:00");
      setDayEnd(toCanonicalTime(prefs.day_end_time) || "22:00");
      setUseDayBoundaries(prefs.use_day_boundary_preferences ?? true);
      setDefaultStartLocation(prefs.default_start_location || null);
      setDefaultBufferMinutes(normalizeBufferMinutes(prefs.default_buffer_minutes));
      setOriginalPreferencesJson(preferenceSnapshot(
        toCanonicalTime(prefs.day_start_time) || "08:00",
        toCanonicalTime(prefs.day_end_time) || "22:00",
        prefs.use_day_boundary_preferences ?? true,
        prefs.default_start_location || null,
        normalizeBufferMinutes(prefs.default_buffer_minutes),
      ));
      fetchLocations();
      getPlanningPreferences(user.id)
        .then((remotePrefs) => {
          if (!remotePrefs) return;
          const normalized = normalizePlanningPreferences(remotePrefs);
          savePlanningPreferences(user.id, normalized);
          setDayStart(toCanonicalTime(normalized.day_start_time) || "08:00");
          setDayEnd(toCanonicalTime(normalized.day_end_time) || "22:00");
          setUseDayBoundaries(normalized.use_day_boundary_preferences ?? true);
          setDefaultStartLocation(normalized.default_start_location || null);
          setDefaultBufferMinutes(normalizeBufferMinutes(normalized.default_buffer_minutes));
          setOriginalPreferencesJson(preferenceSnapshot(
            toCanonicalTime(normalized.day_start_time) || "08:00",
            toCanonicalTime(normalized.day_end_time) || "22:00",
            normalized.use_day_boundary_preferences ?? true,
            normalized.default_start_location || null,
            normalizeBufferMinutes(normalized.default_buffer_minutes),
          ));
        })
        .catch((error) => console.error("Failed to fetch planning preferences:", error));
      getRecentLocations(user.id)
        .then((locations) => {
          if (locations.length) saveRecentLocations(user.id, locations);
        })
        .catch((error) => console.error("Failed to fetch recent locations:", error));
    }
  }, [user]);

  const fetchLocations = async () => {
    if (!user?.id) return;
    try {
      const data = await getSavedLocations(user.id);
      setSavedLocations(data);
    } catch (err) {
      console.error("Failed to fetch locations:", err);
    }
  };

  const handleOpenMapPicker = () => {
    if (!newLocLabel.trim()) {
      setLocationError("Enter a short label first, such as home, school, gym, or mall.");
      return;
    }
    setLocationError(null);
    setLocationNotice(null);
    setIsMapPickerOpen(true);
  };

  const handleSaveMapPin = async (candidate: GeocodeCandidate) => {
    if (!user?.id || !newLocLabel.trim()) return;
    setIsSavingMapPin(true);
    setLocationError(null);

    try {
      const label = newLocLabel.trim();
      await resolveLocation({
        user_id: user.id,
        label,
        address: candidate.address || candidate.display_name || "Pinned map point",
        display_name: candidate.display_name || candidate.address || `${label} map point`,
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: candidate.source || "manual_map_pin",
        confirmed_by_user: true,
      });
      const recent = candidateToPlanningLocation(candidate, "saved_location");
      addRecentLocation(user.id, recent);
      addRecentLocationRemote(user.id, recent).catch((error) => console.error("Failed to save recent location:", error));
      setNewLocLabel("");
      setLocationNotice("Location saved from your map selection.");
      setIsMapPickerOpen(false);
      await fetchLocations();
    } catch (err) {
      console.error("Failed to save map pin:", err);
      setLocationError("Failed to save this map point. Please try again.");
    } finally {
      setIsSavingMapPin(false);
    }
  };

  const handleDeleteLocation = async (label: string) => {
    if (!user?.id) return;
    try {
      await deleteSavedLocation(user.id, label);
      fetchLocations();
    } catch (err) {
      console.error("Failed to delete location:", err);
    }
  };

  const beginDefaultStartSelection = () => {
    setPendingDefaultStartLocation(defaultStartLocation);
    setIsChoosingDefaultStart(true);
  };

  const confirmDefaultStartSelection = () => {
    setDefaultStartLocation(pendingDefaultStartLocation);
    setIsChoosingDefaultStart(false);
  };

  const cancelDefaultStartSelection = () => {
    setPendingDefaultStartLocation(null);
    setIsChoosingDefaultStart(false);
  };

  const handleSave = async () => {
    const nextPreferences = {
      day_start_time: toCanonicalTime(dayStart) || "08:00",
      day_end_time: toCanonicalTime(dayEnd) || "22:00",
      use_day_boundary_preferences: useDayBoundaries,
      default_start_location: defaultStartLocation,
      default_buffer_minutes: normalizeBufferMinutes(defaultBufferMinutes),
    };
    setIsSavingPreferences(true);
    savePlanningPreferences(user?.id, nextPreferences);
    if (user?.id) {
      try {
        const saved = await savePlanningPreferencesRemote(user.id, nextPreferences);
        const normalized = normalizePlanningPreferences(saved);
        savePlanningPreferences(user.id, normalized);
        setOriginalPreferencesJson(preferenceSnapshot(
          toCanonicalTime(normalized.day_start_time) || "08:00",
          toCanonicalTime(normalized.day_end_time) || "22:00",
          normalized.use_day_boundary_preferences ?? true,
          normalized.default_start_location || null,
          normalizeBufferMinutes(normalized.default_buffer_minutes),
        ));
      } catch (error) {
        console.error("Failed to save planning preferences:", error);
        setLocationError(error instanceof Error ? error.message : "Failed to save planning preferences.");
        setIsSavingPreferences(false);
        return;
      }
    }
    setIsSavingPreferences(false);
    onBack();
  };

  const hasUnsavedPreferenceChanges = () => {
    const current = preferenceSnapshot(dayStart, dayEnd, useDayBoundaries, defaultStartLocation, defaultBufferMinutes);
    return Boolean(originalPreferencesJson && current !== originalPreferencesJson);
  };

  const handleRequestBack = () => {
    if (hasUnsavedPreferenceChanges()) {
      setShowExitConfirmation(true);
      return;
    }
    onBack();
  };

  const hasCoordinates = (loc: SavedLocation) => (
    loc.latitude !== null &&
    loc.latitude !== undefined &&
    loc.longitude !== null &&
    loc.longitude !== undefined
  );

  const isDefaultStartLocation = (loc: SavedLocation) => {
    if (!defaultStartLocation) return false;
    if (defaultStartLocation.label && loc.label && defaultStartLocation.label === loc.label) return true;
    return (
      Number(defaultStartLocation.latitude).toFixed(6) === Number(loc.latitude).toFixed(6) &&
      Number(defaultStartLocation.longitude).toFixed(6) === Number(loc.longitude).toFixed(6)
    );
  };

  const isPendingDefaultStartLocation = (loc: SavedLocation) => {
    if (!pendingDefaultStartLocation) return false;
    if (pendingDefaultStartLocation.label && loc.label && pendingDefaultStartLocation.label === loc.label) return true;
    return (
      Number(pendingDefaultStartLocation.latitude).toFixed(6) === Number(loc.latitude).toFixed(6) &&
      Number(pendingDefaultStartLocation.longitude).toFixed(6) === Number(loc.longitude).toFixed(6)
    );
  };

  const hasInvalidDayWindow = useDayBoundaries && !isValidDayWindow(dayStart, dayEnd);

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-2xl mx-auto px-4 py-5 sm:px-6 sm:py-8">
        <Button 
          variant="ghost" 
          onClick={handleRequestBack}
          className="mb-5 rounded-xl sm:mb-8"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back
        </Button>

        <div className="mb-5 sm:mb-8">
          <h2 className="mb-2">Settings & Preferences</h2>
          <p className="text-muted-foreground">
            Customize your planning experience
          </p>
        </div>

        <div className="space-y-4 sm:space-y-6">
          {/* Day Time Preferences */}
          <div className="bg-card rounded-2xl border border-border p-4 shadow-sm sm:p-5">
            <div className="mb-4 flex items-center justify-between gap-3">
              <div className="flex items-center gap-2">
                <h3>Daily Schedule Preferences</h3>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <button
                      type="button"
                      className="inline-flex h-6 w-6 items-center justify-center rounded-full border border-border text-muted-foreground hover:bg-secondary"
                      aria-label="Daily schedule preferences info"
                    >
                      <Info className="h-3.5 w-3.5" />
                    </button>
                  </TooltipTrigger>
                  <TooltipContent className="max-w-xs text-xs">
                    These times set your normal planning window. JPlan uses them as the day boundary unless your request gives a more specific time.
                  </TooltipContent>
                </Tooltip>
              </div>
              <Switch
                id="daily-schedule-toggle"
                checked={useDayBoundaries}
                onCheckedChange={setUseDayBoundaries}
              />
            </div>

            <div
              className={`grid grid-cols-1 gap-3 rounded-2xl border p-3 transition-all sm:grid-cols-2 ${
                useDayBoundaries
                  ? "border-transparent bg-transparent"
                  : "pointer-events-none border-border/60 bg-secondary/30 opacity-45 grayscale"
              }`}
              aria-disabled={!useDayBoundaries}
            >
              <div>
                <Label htmlFor="day-start" className="mb-2 block text-xs">
                  Start time
                </Label>
                <input
                  id="day-start"
                  type="time"
                  value={dayStart}
                  onChange={(e) => setDayStart(e.target.value)}
                  disabled={!useDayBoundaries}
                  className={`h-10 w-full rounded-xl border px-3 text-sm ${
                    hasInvalidDayWindow ? "border-destructive focus-visible:outline-destructive" : "border-border"
                  } ${
                    useDayBoundaries ? "bg-background" : "cursor-not-allowed bg-muted text-muted-foreground"
                  }`}
                />
              </div>

              <div>
                <Label htmlFor="day-end" className="mb-2 block text-xs">
                  End time
                </Label>
                <input
                  id="day-end"
                  type="time"
                  value={dayEnd}
                  onChange={(e) => setDayEnd(e.target.value)}
                  disabled={!useDayBoundaries}
                  className={`h-10 w-full rounded-xl border px-3 text-sm ${
                    hasInvalidDayWindow ? "border-destructive focus-visible:outline-destructive" : "border-border"
                  } ${
                    useDayBoundaries ? "bg-background" : "cursor-not-allowed bg-muted text-muted-foreground"
                  }`}
                />
              </div>
            </div>
            {hasInvalidDayWindow && (
              <p className="mt-2 text-xs text-destructive">
                Start time must be earlier than end time.
              </p>
            )}

            <div className="mt-4 rounded-2xl border border-border/60 bg-background/70 p-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <Label htmlFor="default-buffer-minutes" className="text-sm">
                    Default buffer time
                  </Label>
                  <p className="text-xs text-muted-foreground mt-1">
                    Used between travel-linked activities unless a plan overrides it.
                  </p>
                </div>
                <div className="flex items-center gap-2">
                <input
                  id="default-buffer-minutes"
                  type="number"
                  min={0}
                  max={60}
                  step={1}
                  value={defaultBufferMinutes}
                  onChange={(event) => setDefaultBufferMinutes(normalizeBufferMinutes(event.target.value))}
                  className="h-9 w-20 rounded-xl border border-border bg-background px-3 text-sm"
                />
                <span className="text-xs text-muted-foreground">min</span>
                </div>
              </div>
            </div>
          </div>

          {/* Saved Locations Section */}
          <div className="bg-card rounded-2xl border border-border p-4 shadow-sm sm:p-5">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
              <div className="flex items-center gap-3">
                <MapPin className="h-5 w-5 text-primary" />
                <h3>Saved Locations</h3>
              </div>
              {isChoosingDefaultStart ? (
                <div className="flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    className="rounded-xl"
                    onClick={confirmDefaultStartSelection}
                    disabled={!pendingDefaultStartLocation}
                  >
                    Confirm
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="rounded-xl"
                    onClick={cancelDefaultStartSelection}
                  >
                    Cancel
                  </Button>
                </div>
              ) : (
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-xl"
                  onClick={beginDefaultStartSelection}
                >
                  Choose start
                </Button>
              )}
            </div>
            
            <p className="text-xs text-muted-foreground mb-4">
              Save exact places once so JPlan can reuse their map points.
            </p>

            {/* Location List */}
            <div className="space-y-2 mb-5">
              {savedLocations.map((loc) => (
                <div key={loc.label} className="flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-background px-3 py-3 shadow-sm transition-colors hover:border-primary/25">
                  <div className="flex min-w-0 flex-1 items-start gap-3">
                    {isChoosingDefaultStart && hasCoordinates(loc) && (
                      <Checkbox
                        checked={isPendingDefaultStartLocation(loc)}
                        onCheckedChange={(checked) => {
                          setPendingDefaultStartLocation(checked ? savedLocationToPlanningLocation(loc) : null);
                        }}
                        className="mt-1"
                        aria-label={`Choose ${loc.label} as default start location`}
                      />
                    )}
                    <div className="min-w-0">
                    <div className="mb-1 flex min-w-0 items-center gap-2">
                      <p className="truncate text-sm font-semibold">{loc.label}</p>
                      {isDefaultStartLocation(loc) && (
                        <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                          <Home className="h-3 w-3" />
                          Start
                        </span>
                      )}
                      <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] ${
                        hasCoordinates(loc)
                          ? "bg-emerald-500/10 text-emerald-700 border-emerald-500/20"
                          : "bg-amber-500/10 text-amber-700 border-amber-500/20"
                      }`}>
                        {hasCoordinates(loc) ? "Map point" : "Needs map point"}
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground truncate">
                      {loc.display_name || loc.address || "No address saved"}
                    </p>
                    {!hasCoordinates(loc) && isChoosingDefaultStart && (
                      <p className="mt-1 text-[11px] text-amber-700">
                        Add a map point before using this as your start.
                      </p>
                    )}
                    </div>
                  </div>
                  <Button 
                    variant="ghost" 
                    size="sm" 
                    className="h-8 w-8 shrink-0 p-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                    onClick={() => handleDeleteLocation(loc.label)}
                    aria-label={`Delete ${loc.label}`}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
              {savedLocations.length === 0 && (
                <p className="text-center py-4 text-xs text-muted-foreground italic bg-secondary/10 rounded-xl border border-dashed">
                  No saved locations yet.
                </p>
              )}
            </div>

            {/* Add Location Form */}
            <div className="rounded-2xl border border-dashed border-border bg-background/60 p-4">
              <div className="mb-3">
                <Label htmlFor="location-label" className="mb-1.5 block text-xs">
                  Label
                </Label>
                <input
                  id="location-label"
                  placeholder="home, campus, gym"
                  value={newLocLabel}
                  onChange={(e) => {
                    setNewLocLabel(e.target.value);
                    setLocationError(null);
                    setLocationNotice(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      handleOpenMapPicker();
                    }
                  }}
                  className="w-full rounded-xl border border-border bg-background px-3 py-2 text-sm outline-none focus:ring-1 focus:ring-primary"
                />
              </div>

              <Button
                className="w-full rounded-xl"
                onClick={handleOpenMapPicker}
                disabled={!newLocLabel.trim()}
              >
                <MapPin className="mr-2 h-4 w-4" />
                Pick exact location
              </Button>

              <p className="mt-2 text-[11px] text-muted-foreground">
                Search for a place, use your current location, or click the exact point on the map.
              </p>

              {locationNotice && (
                <div className="mt-3 rounded-xl border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-700">
                  {locationNotice}
                </div>
              )}

              {locationError && (
                <div className="mt-3 flex items-start gap-2 rounded-xl border border-destructive/20 bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  <AlertCircle className="h-4 w-4 shrink-0 mt-0.5" />
                  <span>{locationError}</span>
                </div>
              )}

            </div>
          </div>

        </div>

        {/* Save Button */}
        <div className="mt-6 flex gap-3 sm:mt-8">
          <Button onClick={handleSave} className="flex-1 rounded-xl shadow-sm" disabled={isSavingPreferences || hasInvalidDayWindow}>
            {isSavingPreferences ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Save Preferences
          </Button>
          <Button onClick={handleRequestBack} variant="outline" className="flex-1 rounded-xl" disabled={isSavingPreferences}>
            Cancel
          </Button>
        </div>

      </div>

      {showExitConfirmation && (
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-4 shadow-xl sm:p-6">
            <h2 className="mb-2 text-xl font-semibold">Save preferences?</h2>
            <p className="mb-6 text-sm text-muted-foreground">
              You have unsaved preference changes. Save them before going back to the dashboard?
            </p>
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="outline"
                className="rounded-xl"
                onClick={() => setShowExitConfirmation(false)}
                disabled={isSavingPreferences}
              >
                Cancel
              </Button>
              <Button
                variant="outline"
                className="rounded-xl"
                onClick={() => {
                  setShowExitConfirmation(false);
                  onBack();
                }}
                disabled={isSavingPreferences}
              >
                Don&apos;t save
              </Button>
              <Button
                className="rounded-xl"
                onClick={handleSave}
                disabled={isSavingPreferences || hasInvalidDayWindow}
              >
                {isSavingPreferences ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                Save
              </Button>
            </div>
          </div>
        </div>
      )}

      <LocationPickerDialog
        open={isMapPickerOpen}
        onOpenChange={setIsMapPickerOpen}
        title="Pick saved location on map"
        description={`Search nearby or click the exact place for ${newLocLabel.trim() || "this saved location"}.`}
        label={newLocLabel.trim() || "saved location"}
        initialSearchQuery={newLocLabel.trim()}
        confirmLabel="Save this map point"
        saving={isSavingMapPin}
        onConfirm={handleSaveMapPin}
      />
    </div>
  );
}
