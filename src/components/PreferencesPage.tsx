import { useState, useEffect } from "react";
import { Button } from "./ui/button";
import { Label } from "./ui/label";
import { ArrowLeft, Bell, Calendar, Check, Trash2, Plus } from "lucide-react";
import { Switch } from "./ui/switch";
import { useAuth } from "../context/AuthContext";

type PreferencesPageProps = {
  onBack: () => void;
};

export function PreferencesPage({ onBack }: PreferencesPageProps) {
  const { user } = useAuth();
  const [dayStart, setDayStart] = useState("8:00 AM");
  const [dayEnd, setDayEnd] = useState("10:00 PM");
  const [preferredHomeReturn, setPreferredHomeReturn] = useState("6:00 PM");
  const [useHomeReturn, setUseHomeReturn] = useState(false);
  const [allowWeekends, setAllowWeekends] = useState(true);
  const [minimizeTravel, setMinimizeTravel] = useState(true);
  const [notificationsEnabled, setNotificationsEnabled] = useState(true);
  const [reminderBefore, setReminderBefore] = useState(true);
  const [calendarConnected, setCalendarConnected] = useState(true);

  // --- Location Management State ---
  const [savedLocations, setSavedLocations] = useState<any[]>([]);
  const [newLocLabel, setNewLocLabel] = useState("");
  const [newLocAddress, setNewLocAddress] = useState("");

  useEffect(() => {
    if (user) {
      fetchLocations();
    }
  }, [user]);

  const fetchLocations = async () => {
    try {
      const response = await fetch(`http://127.0.0.1:8000/api/locations?user_id=${user?.id}`);
      if (response.ok) {
        const data = await response.json();
        setSavedLocations(data);
      }
    } catch (err) {
      console.error("Failed to fetch locations:", err);
    }
  };

  const handleAddLocation = async () => {
    if (!user || !newLocLabel || !newLocAddress) return;
    try {
      const response = await fetch(`http://127.0.0.1:8000/api/locations?user_id=${user.id}&label=${encodeURIComponent(newLocLabel)}&address=${encodeURIComponent(newLocAddress)}`, {
        method: "POST"
      });
      if (response.ok) {
        setNewLocLabel("");
        setNewLocAddress("");
        fetchLocations();
      }
    } catch (err) {
      console.error("Failed to add location:", err);
    }
  };

  const handleDeleteLocation = async (label: string) => {
    if (!user) return;
    try {
      await fetch(`http://127.0.0.1:8000/api/locations?user_id=${user.id}&label=${encodeURIComponent(label)}`, {
        method: "DELETE"
      });
      fetchLocations();
    } catch (err) {
      console.error("Failed to delete location:", err);
    }
  };

  const handleSave = () => {
    // In a real app, this would save to backend/local storage
    onBack();
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-2xl mx-auto px-6 py-8">
        <Button 
          variant="ghost" 
          onClick={onBack}
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
            <h3 className="mb-4">Daily Schedule Preferences</h3>
            
            <div className="space-y-4">
              <div>
                <Label htmlFor="day-start" className="mb-2 block text-sm">
                  Preferred Day Start Time
                </Label>
                <select
                  id="day-start"
                  value={dayStart}
                  onChange={(e) => setDayStart(e.target.value)}
                  className="w-full px-4 py-2 border border-border rounded-xl bg-background"
                >
                  <option>6:00 AM</option>
                  <option>7:00 AM</option>
                  <option>8:00 AM</option>
                  <option>9:00 AM</option>
                  <option>10:00 AM</option>
                </select>
              </div>

              <div>
                <Label htmlFor="day-end" className="mb-2 block text-sm">
                  Preferred Day End Time
                </Label>
                <select
                  id="day-end"
                  value={dayEnd}
                  onChange={(e) => setDayEnd(e.target.value)}
                  className="w-full px-4 py-2 border border-border rounded-xl bg-background"
                >
                  <option>8:00 PM</option>
                  <option>9:00 PM</option>
                  <option>10:00 PM</option>
                  <option>11:00 PM</option>
                  <option>12:00 AM</option>
                </select>
              </div>
            </div>
          </div>

          {/* Home Return Time */}
          <div className="bg-card rounded-2xl border border-border p-5 shadow-sm">
            <div className="flex items-center justify-between mb-3">
              <div>
                <Label htmlFor="home-return-toggle" className="text-sm">
                  Preferred Return Home Time
                </Label>
                <p className="text-xs text-muted-foreground mt-1">
                  Schedule activities to end by this time
                </p>
              </div>
              <Switch
                id="home-return-toggle"
                checked={useHomeReturn}
                onCheckedChange={setUseHomeReturn}
              />
            </div>
            
            {useHomeReturn && (
              <select
                value={preferredHomeReturn}
                onChange={(e) => setPreferredHomeReturn(e.target.value)}
                className="w-full px-4 py-2 border border-border rounded-xl bg-background mt-3"
              >
                <option>5:00 PM</option>
                <option>6:00 PM</option>
                <option>7:00 PM</option>
                <option>8:00 PM</option>
                <option>9:00 PM</option>
              </select>
            )}
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
            <div className="flex items-center gap-3 mb-4">
              <Check className="h-5 w-5 text-primary" />
              <h3>Saved Locations</h3>
            </div>
            
            <p className="text-xs text-muted-foreground mb-4">
              Save places like "Home" or "Campus" to help JPlan calculate travel times.
            </p>

            {/* Location List */}
            <div className="space-y-2 mb-4">
              {savedLocations.map((loc) => (
                <div key={loc.label} className="flex items-center justify-between p-3 bg-secondary/20 rounded-xl border border-border/50 group">
                  <div className="overflow-hidden">
                    <p className="text-sm font-medium">{loc.label}</p>
                    <p className="text-xs text-muted-foreground truncate">{loc.address}</p>
                  </div>
                  <Button 
                    variant="ghost" 
                    size="sm" 
                    className="h-8 w-8 p-0 opacity-0 group-hover:opacity-100 text-destructive hover:text-destructive hover:bg-destructive/10"
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
            <div className="grid grid-cols-2 gap-2 mb-2">
              <input 
                placeholder="Label (e.g. Home)"
                value={newLocLabel}
                onChange={(e) => setNewLocLabel(e.target.value)}
                className="bg-background border border-border rounded-xl px-3 py-2 text-sm focus:ring-1 focus:ring-primary outline-none"
              />
              <input 
                placeholder="Address / Area"
                value={newLocAddress}
                onChange={(e) => setNewLocAddress(e.target.value)}
                className="bg-background border border-border rounded-xl px-3 py-2 text-sm focus:ring-1 focus:ring-primary outline-none"
              />
            </div>
            <Button 
              variant="outline" 
              className="w-full rounded-xl border-dashed hover:bg-primary/5 hover:border-primary/50"
              onClick={handleAddLocation}
              disabled={!newLocLabel || !newLocAddress}
            >
              <Plus className="mr-2 h-4 w-4" />
              Add Location
            </Button>
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
          <Button onClick={handleSave} className="flex-1 rounded-xl shadow-sm">
            Save Preferences
          </Button>
          <Button onClick={onBack} variant="outline" className="flex-1 rounded-xl">
            Cancel
          </Button>
        </div>

        <div className="mt-6 bg-gradient-to-br from-[#d4e5ff]/20 to-[#d4e5ff]/5 rounded-2xl p-5 border border-[#d4e5ff]/30">
          <p className="text-sm text-muted-foreground">
            💡 These preferences help the system create schedules that align with your daily routines and constraints.
          </p>
        </div>
      </div>
    </div>
  );
}