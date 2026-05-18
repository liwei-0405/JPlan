import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { AlertCircle, ArrowLeft, Bell, Calendar, Check, Home, Info, Loader2, MapPin, Search, Trash2 } from "lucide-react";
import { Switch } from "./ui/switch";
import { Checkbox } from "./ui/checkbox";
import { Tooltip, TooltipContent, TooltipTrigger } from "./ui/tooltip";
import { useAuth } from "../context/AuthContext";
import { LocationPickerDialog } from "./LocationPickerDialog";
import {
  deleteSavedLocation,
  getPlanningPreferences,
  getRecentLocations,
  geocodeLocation,
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
  toCanonicalTime,
  toDisplayTime,
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
) {
  return JSON.stringify({
    day_start_time: toCanonicalTime(dayStart) || "08:00",
    day_end_time: toCanonicalTime(dayEnd) || "22:00",
    use_day_boundary_preferences: useDayBoundaries,
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
  const [dayStart, setDayStart] = useState("8:00 AM");
  const [dayEnd, setDayEnd] = useState("10:00 PM");
  const [useDayBoundaries, setUseDayBoundaries] = useState(true);
  const [allowWeekends, setAllowWeekends] = useState(true);
  const [minimizeTravel, setMinimizeTravel] = useState(true);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminderBefore, setReminderBefore] = useState(true);
  const [calendarConnected, setCalendarConnected] = useState(true);
  const [defaultStartLocation, setDefaultStartLocation] = useState<PlanningLocation | null>(null);

  // --- Location Management State ---
  const [savedLocations, setSavedLocations] = useState<SavedLocation[]>([]);
  const [newLocLabel, setNewLocLabel] = useState("");
  const [newLocAddress, setNewLocAddress] = useState("");
  const [locationCandidates, setLocationCandidates] = useState<GeocodeCandidate[]>([]);
  const [locationNotice, setLocationNotice] = useState<string | null>(null);
  const [locationError, setLocationError] = useState<string | null>(null);
  const [isSearchingLocation, setIsSearchingLocation] = useState(false);
  const [savingCandidateKey, setSavingCandidateKey] = useState<string | null>(null);
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
      setDayStart(toDisplayTime(prefs.day_start_time) || "8:00 AM");
      setDayEnd(toDisplayTime(prefs.day_end_time) || "10:00 PM");
      setUseDayBoundaries(prefs.use_day_boundary_preferences ?? true);
      setDefaultStartLocation(prefs.default_start_location || null);
      setOriginalPreferencesJson(preferenceSnapshot(
        toDisplayTime(prefs.day_start_time) || "8:00 AM",
        toDisplayTime(prefs.day_end_time) || "10:00 PM",
        prefs.use_day_boundary_preferences ?? true,
        prefs.default_start_location || null,
      ));
      fetchLocations();
      getPlanningPreferences(user.id)
        .then((remotePrefs) => {
          if (!remotePrefs) return;
          const normalized = normalizePlanningPreferences(remotePrefs);
          savePlanningPreferences(user.id, normalized);
          setDayStart(toDisplayTime(normalized.day_start_time) || "8:00 AM");
          setDayEnd(toDisplayTime(normalized.day_end_time) || "10:00 PM");
          setUseDayBoundaries(normalized.use_day_boundary_preferences ?? true);
          setDefaultStartLocation(normalized.default_start_location || null);
          setOriginalPreferencesJson(preferenceSnapshot(
            toDisplayTime(normalized.day_start_time) || "8:00 AM",
            toDisplayTime(normalized.day_end_time) || "10:00 PM",
            normalized.use_day_boundary_preferences ?? true,
            normalized.default_start_location || null,
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

  const handleSearchLocation = async () => {
    if (!newLocLabel.trim() || !newLocAddress.trim()) return;

    setIsSearchingLocation(true);
    setLocationCandidates([]);
    setLocationError(null);
    setLocationNotice(null);

    try {
      const result = await geocodeLocation(newLocAddress.trim());
      setLocationCandidates(result.candidates || []);
      const warningText = result.warnings?.length ? result.warnings.join(" ") : result.warning;
      if (warningText && result.candidates?.length) {
        setLocationNotice(warningText);
      } else if (warningText) {
        setLocationError(warningText);
      } else if (!result.candidates?.length) {
        setLocationError("No location candidates found. Try a more specific place name or address.");
      } else if (result.expanded_query && result.expanded_query !== newLocAddress.trim()) {
        setLocationNotice(`Searched as: ${result.expanded_query}`);
      }
    } catch (err) {
      console.error("Failed to search location:", err);
      setLocationError("Location search failed. Please check the backend ORS configuration.");
    } finally {
      setIsSearchingLocation(false);
    }
  };

  const handleConfirmCandidate = async (candidate: GeocodeCandidate, index: number) => {
    if (!user?.id || !newLocLabel.trim()) return;
    const key = `${candidate.latitude}-${candidate.longitude}-${index}`;
    setSavingCandidateKey(key);
    setLocationError(null);

    try {
      await resolveLocation({
        user_id: user.id,
        label: newLocLabel.trim(),
        address: candidate.address || candidate.display_name || newLocAddress.trim(),
        display_name: candidate.display_name || candidate.address || newLocLabel.trim(),
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: candidate.source || "ors_geocoded",
        confirmed_by_user: true,
      });
      const recent = candidateToPlanningLocation(candidate, "saved_location");
      addRecentLocation(user.id, recent);
      addRecentLocationRemote(user.id, recent).catch((error) => console.error("Failed to save recent location:", error));
      setNewLocLabel("");
      setNewLocAddress("");
      setLocationCandidates([]);
      setLocationNotice("Location saved with a confirmed map point.");
      await fetchLocations();
    } catch (err) {
      console.error("Failed to confirm location:", err);
      setLocationError("Failed to save this location. Please try another candidate.");
    } finally {
      setSavingCandidateKey(null);
    }
  };

  const handleOpenMapPicker = () => {
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
        address: candidate.address || candidate.display_name || newLocAddress.trim() || "Pinned map point",
        display_name: candidate.display_name || candidate.address || newLocAddress.trim() || `${label} map point`,
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: candidate.source || "manual_map_pin",
        confirmed_by_user: true,
      });
      const recent = candidateToPlanningLocation(candidate, "saved_location");
      addRecentLocation(user.id, recent);
      addRecentLocationRemote(user.id, recent).catch((error) => console.error("Failed to save recent location:", error));
      setNewLocLabel("");
      setNewLocAddress("");
      setLocationCandidates([]);
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
    };
    setIsSavingPreferences(true);
    savePlanningPreferences(user?.id, nextPreferences);
    if (user?.id) {
      try {
        const saved = await savePlanningPreferencesRemote(user.id, nextPreferences);
        const normalized = normalizePlanningPreferences(saved);
        savePlanningPreferences(user.id, normalized);
        setOriginalPreferencesJson(preferenceSnapshot(
          toDisplayTime(normalized.day_start_time) || "8:00 AM",
          toDisplayTime(normalized.day_end_time) || "10:00 PM",
          normalized.use_day_boundary_preferences ?? true,
          normalized.default_start_location || null,
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
    const current = preferenceSnapshot(dayStart, dayEnd, useDayBoundaries, defaultStartLocation);
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

  const mapEmbedUrl = (lat: number, lng: number) => {
    const delta = 0.006;
    const bbox = [
      lng - delta,
      lat - delta,
      lng + delta,
      lat + delta,
    ].join(",");
    return `https://www.openstreetmap.org/export/embed.html?bbox=${encodeURIComponent(bbox)}&layer=mapnik&marker=${encodeURIComponent(`${lat},${lng}`)}`;
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-2xl mx-auto px-6 py-8">
        <Button 
          variant="ghost" 
          onClick={handleRequestBack}
          className="mb-8 rounded-xl"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back
        </Button>

        <div className="mb-8">
          <h2 className="mb-2">Settings & Preferences</h2>
          <p className="text-muted-foreground">
            Customize your planning experience
          </p>
        </div>

        <div className="space-y-6">
          {/* Calendar Sync Status */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
            <div className="flex items-center gap-3 mb-4">
              <Calendar className="h-5 w-5 text-primary" />
              <h3>Calendar Connection</h3>
            </div>

            <div className="flex items-center justify-between mb-4">
              <div>
                <p className="text-sm mb-1">Status</p>
                <div className="flex items-center gap-2">
                  {calendarConnected ? (
                    <>
                      <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                      <span className="text-sm text-muted-foreground">Connected</span>
                    </>
                  ) : (
                    <>
                      <div className="w-2 h-2 rounded-full bg-gray-400" />
                      <span className="text-sm text-muted-foreground">Disconnected</span>
                    </>
                  )}
                </div>
              </div>
              <Button 
                variant="outline" 
                size="sm"
                className="rounded-xl"
                onClick={() => setCalendarConnected(!calendarConnected)}
              >
                {calendarConnected ? "Disconnect" : "Connect"}
              </Button>
            </div>

            {calendarConnected && (
              <Button 
                variant="secondary" 
                className="w-full rounded-xl"
                size="sm"
              >
                <Check className="mr-2 h-4 w-4" />
                Sync Now
              </Button>
            )}
          </div>

          {/* Day Time Preferences */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
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
              className={`grid grid-cols-2 gap-3 rounded-2xl border p-3 transition-all ${
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
                <select
                  id="day-start"
                  value={dayStart}
                  onChange={(e) => setDayStart(e.target.value)}
                  disabled={!useDayBoundaries}
                  className={`w-full px-3 py-2 border border-border rounded-xl text-sm ${
                    useDayBoundaries ? "bg-background" : "cursor-not-allowed bg-muted text-muted-foreground"
                  }`}
                >
                  <option>6:00 AM</option>
                  <option>7:00 AM</option>
                  <option>8:00 AM</option>
                  <option>9:00 AM</option>
                  <option>10:00 AM</option>
                </select>
              </div>

              <div>
                <Label htmlFor="day-end" className="mb-2 block text-xs">
                  End time
                </Label>
                <select
                  id="day-end"
                  value={dayEnd}
                  onChange={(e) => setDayEnd(e.target.value)}
                  disabled={!useDayBoundaries}
                  className={`w-full px-3 py-2 border border-border rounded-xl text-sm ${
                    useDayBoundaries ? "bg-background" : "cursor-not-allowed bg-muted text-muted-foreground"
                  }`}
                >
                  <option>8:00 PM</option>
                  <option>9:00 PM</option>
                  <option>10:00 PM</option>
                  <option>11:00 PM</option>
                </select>
              </div>
            </div>
          </div>

          {/* Notifications */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
            <div className="flex items-center gap-3 mb-4">
              <Bell className="h-5 w-5 text-primary" />
              <h3>Notifications</h3>
            </div>

            <div className="space-y-4">
              <div className="flex items-center justify-between py-2">
                <div>
                  <Label htmlFor="notifications-enabled" className="text-sm">
                    Enable Notifications
                  </Label>
                  <p className="text-xs text-muted-foreground mt-1">
                    Get notified about your schedule
                  </p>
                </div>
                <Switch
                  id="notifications-enabled"
                  checked={notificationsEnabled}
                  onCheckedChange={setNotificationsEnabled}
                />
              </div>

              {notificationsEnabled && (
                <div className="flex items-center justify-between py-2 pl-4 border-l-2 border-primary/20">
                  <div>
                    <Label htmlFor="reminder-before" className="text-sm">
                      Reminder Before Activity
                    </Label>
                    <p className="text-xs text-muted-foreground mt-1">
                      Get a reminder 15 minutes before each activity
                    </p>
                  </div>
                  <Switch
                    id="reminder-before"
                    checked={reminderBefore}
                    onCheckedChange={setReminderBefore}
                  />
                </div>
              )}
            </div>
          </div>

          {/* Saved Locations Section */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
            <div className="flex items-center justify-between gap-3 mb-4">
              <div className="flex items-center gap-3">
                <MapPin className="h-5 w-5 text-primary" />
                <h3>Saved Locations</h3>
              </div>
              {isChoosingDefaultStart ? (
                <div className="flex gap-2">
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
              Search and confirm exact places so Accurate Travel Time can reuse the right map point later.
            </p>
            <p className="text-xs text-muted-foreground mb-4 rounded-xl border border-primary/10 bg-primary/5 px-3 py-2">
              Default start: {defaultStartLocation?.label || defaultStartLocation?.display_name || "Not set"}. This is used only when Accurate Travel Time is on.
            </p>

            {/* Location List */}
            <div className="space-y-2 mb-5">
              {savedLocations.map((loc) => (
                <div key={loc.label} className="flex items-start justify-between gap-3 p-3 bg-secondary/20 rounded-xl border border-border/50 group">
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
                    <div className="flex flex-wrap items-center gap-2 mb-1">
                      <p className="text-sm font-medium">{loc.label}</p>
                      {isDefaultStartLocation(loc) && (
                        <span className="inline-flex items-center gap-1 rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                          <Home className="h-3 w-3" />
                          Start
                        </span>
                      )}
                      <span className={`text-[10px] px-2 py-0.5 rounded-full border ${
                        hasCoordinates(loc)
                          ? "bg-emerald-500/10 text-emerald-700 border-emerald-500/20"
                          : "bg-amber-500/10 text-amber-700 border-amber-500/20"
                      }`}>
                        {hasCoordinates(loc) ? "Confirmed map point" : "Address only"}
                      </span>
                    </div>
                    <p className="text-xs text-muted-foreground truncate">
                      {loc.display_name || loc.address || "No address saved"}
                    </p>
                    <p className="text-[11px] text-muted-foreground/80 mt-1">
                      {hasCoordinates(loc) ? "Map point saved" : "No map point saved"}
                      {loc.source ? ` | ${loc.source}` : ""}
                      {loc.confirmed_by_user ? " | confirmed" : ""}
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
                    className="h-8 w-8 p-0 shrink-0 opacity-0 group-hover:opacity-100 text-destructive hover:text-destructive hover:bg-destructive/10"
                    onClick={() => handleDeleteLocation(loc.label)}
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
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mb-3">
                <div>
                  <Label htmlFor="location-label" className="text-xs mb-1.5 block">
                    Label
                  </Label>
                  <input
                    id="location-label"
                    placeholder="home, campus, gym"
                    value={newLocLabel}
                    onChange={(e) => setNewLocLabel(e.target.value)}
                    className="w-full bg-background border border-border rounded-xl px-3 py-2 text-sm focus:ring-1 focus:ring-primary outline-none"
                  />
                </div>
                <div>
                  <Label htmlFor="location-search" className="text-xs mb-1.5 block">
                    Place / Address
                  </Label>
                  <input
                    id="location-search"
                    placeholder="MMU Cyberjaya, Library..."
                    value={newLocAddress}
                    onChange={(e) => {
                      setNewLocAddress(e.target.value);
                      setLocationCandidates([]);
                      setLocationError(null);
                      setLocationNotice(null);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        handleSearchLocation();
                      }
                    }}
                    className="w-full bg-background border border-border rounded-xl px-3 py-2 text-sm focus:ring-1 focus:ring-primary outline-none"
                  />
                </div>
              </div>

              <Button
                variant="outline"
                className="w-full rounded-xl border-dashed hover:bg-primary/5 hover:border-primary/50"
                onClick={handleSearchLocation}
                disabled={!newLocLabel.trim() || !newLocAddress.trim() || isSearchingLocation}
              >
                {isSearchingLocation ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Search className="mr-2 h-4 w-4" />
                )}
                Search Location
              </Button>

              <Button
                type="button"
                variant="ghost"
                className="mt-2 w-full rounded-xl text-primary hover:bg-primary/5"
                onClick={handleOpenMapPicker}
                disabled={!newLocLabel.trim()}
              >
                <MapPin className="mr-2 h-4 w-4" />
                Not found? Pick on map
              </Button>

              <p className="mt-2 text-[11px] text-muted-foreground">
                Search and map data from OpenRouteService and OpenStreetMap contributors.
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

              {locationCandidates.length > 0 && (
                <div className="mt-4 space-y-2">
                  <p className="text-xs text-muted-foreground">
                    Choose the correct result by checking the map preview.
                  </p>
                  {locationCandidates.map((candidate, index) => {
                    const key = `${candidate.latitude}-${candidate.longitude}-${index}`;
                    return (
                      <div key={key} className="rounded-xl border border-border bg-card p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <p className="text-sm font-medium leading-snug">
                              {candidate.display_name || candidate.address || "Unnamed location"}
                            </p>
                            {candidate.address && candidate.address !== candidate.display_name && (
                              <p className="text-xs text-muted-foreground mt-1 truncate">
                                {candidate.address}
                              </p>
                            )}
                            <div className="mt-3 overflow-hidden rounded-xl border border-border">
                              <iframe
                                title={`Map preview for ${candidate.display_name || candidate.address || "location candidate"}`}
                                src={mapEmbedUrl(candidate.latitude, candidate.longitude)}
                                className="h-32 w-full border-0"
                                loading="lazy"
                              />
                            </div>
                          </div>
                          <Button
                            size="sm"
                            className="rounded-xl shrink-0"
                            onClick={() => handleConfirmCandidate(candidate, index)}
                            disabled={savingCandidateKey !== null}
                          >
                            {savingCandidateKey === key ? (
                              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                            ) : (
                              <Check className="mr-2 h-4 w-4" />
                            )}
                            Save
                          </Button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>

          {/* Planning Options */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
            <h3 className="mb-4">Planning Options</h3>
            {/* ... existing planning options ... */}

            <div className="space-y-4">
              <div className="flex items-center justify-between py-2">
                <div>
                  <Label htmlFor="minimize-travel" className="text-sm">
                    Minimize Travel Time
                  </Label>
                  <p className="text-xs text-muted-foreground mt-1">
                    Group activities by location when possible
                  </p>
                </div>
                <Switch
                  id="minimize-travel"
                  checked={minimizeTravel}
                  onCheckedChange={setMinimizeTravel}
                />
              </div>

              <div className="flex items-center justify-between py-2">
                <div>
                  <Label htmlFor="allow-weekends" className="text-sm">
                    Include Weekend Planning
                  </Label>
                  <p className="text-xs text-muted-foreground mt-1">
                    Allow schedules for Saturdays and Sundays
                  </p>
                </div>
                <Switch
                  id="allow-weekends"
                  checked={allowWeekends}
                  onCheckedChange={setAllowWeekends}
                />
              </div>
            </div>
          </div>
        </div>

        {/* Save Button */}
        <div className="mt-8 flex gap-3">
          <Button onClick={handleSave} className="flex-1 rounded-xl shadow-sm" disabled={isSavingPreferences}>
            {isSavingPreferences ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Save Preferences
          </Button>
          <Button onClick={handleRequestBack} variant="outline" className="flex-1 rounded-xl" disabled={isSavingPreferences}>
            Cancel
          </Button>
        </div>

        <div className="mt-6 bg-gradient-to-br from-[#d4e5ff]/20 to-[#d4e5ff]/5 rounded-2xl p-5 border border-[#d4e5ff]/30">
          <p className="text-sm text-muted-foreground">
            💡 These preferences help the system create schedules that align with your daily routines and constraints.
          </p>
        </div>
      </div>

      {showExitConfirmation && (
        <div
          className="fixed inset-0 z-[9999] flex items-center justify-center bg-black/50 px-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-6 shadow-xl">
            <h2 className="mb-2 text-xl font-semibold">Save preferences?</h2>
            <p className="mb-6 text-sm text-muted-foreground">
              You have unsaved preference changes. Save them before going back to the dashboard?
            </p>
            <div className="flex justify-end gap-2">
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
                disabled={isSavingPreferences}
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
        candidates={locationCandidates}
        savedLocations={savedLocations.filter(hasCoordinates)}
        initialSearchQuery={newLocAddress}
        confirmLabel="Save this map point"
        saving={isSavingMapPin}
        onConfirm={handleSaveMapPin}
      />
    </div>
  );
}
