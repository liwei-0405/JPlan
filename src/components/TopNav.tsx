import { useEffect, useState } from "react";
import { Button } from "./ui/button";
import { Settings, RefreshCw, LogOut, User as UserIcon } from "lucide-react";
import { useAuth } from "../context/AuthContext";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from "./ui/dropdown-menu";
import { Avatar, AvatarFallback, AvatarImage } from "./ui/avatar";
import { toast } from "sonner";
import { supabase } from "../lib/supabase";
import { apiUrl } from "../services/apiConfig";
import { FRONTEND_VERSION, jplanLogoUrl } from "../brand";

type TopNavProps = {
  onSettingsClick?: () => void;
  showSyncButton?: boolean;
  onSyncComplete?: () => void;
  syncDate?: string;
};

const formatLocalDate = (date: Date) => {
  const yyyy = date.getFullYear();
  const mm = String(date.getMonth() + 1).padStart(2, "0");
  const dd = String(date.getDate()).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
};

const addDays = (date: Date, days: number) => {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
};

const getVersionBuildNumber = (version?: string | null) => {
  if (!version) return null;
  const parts = version.replace(/^v/, "").split("-");
  return parts[parts.length - 1] || null;
};

export function TopNav({ 
  onSettingsClick, 
  showSyncButton = true, 
  onSyncComplete,
  syncDate 
}: TopNavProps) {
  const { profile, signOut, isAdmin, isGoogleLinked } = useAuth();
  const [backendVersion, setBackendVersion] = useState<string | null>(null);
  const [calendarLinked, setCalendarLinked] = useState(isGoogleLinked);

  useEffect(() => {
    let cancelled = false;

    fetch(apiUrl("/health"), { cache: "no-store" })
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!cancelled && data?.version) {
          setBackendVersion(String(data.version));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBackendVersion(null);
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setCalendarLinked(isGoogleLinked);
  }, [isGoogleLinked]);

  useEffect(() => {
    if (!showSyncButton || !profile?.id) return;
    let cancelled = false;

    fetch(apiUrl(`/api/google-calendar/link-status?user_id=${encodeURIComponent(profile.id)}`), {
      cache: "no-store",
    })
      .then((response) => response.ok ? response.json() : null)
      .then((data) => {
        if (!cancelled && typeof data?.linked === "boolean") {
          setCalendarLinked(data.linked);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCalendarLinked(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [profile?.id, showSyncButton]);

  const backendBuildNumber = getVersionBuildNumber(backendVersion);
  const combinedVersion = `v${FRONTEND_VERSION}${backendBuildNumber ? `-${backendBuildNumber}` : ""}`;

  const handleSignOut = async () => {
    if (typeof signOut === 'function') {
      await signOut();
    }
  };

  const handleLinkGoogle = async () => {
    try {
      const { error } = await supabase.auth.signInWithOAuth({
        provider: 'google',
        options: {
          queryParams: {
            access_type: 'offline',
            prompt: 'consent',
          },
          scopes: 'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly https://www.googleapis.com/auth/userinfo.email https://www.googleapis.com/auth/userinfo.profile',
          redirectTo: window.location.origin + window.location.pathname,
        }
      });
      if (error) throw error;
    } catch (err: any) {
      console.error('Error linking Google:', err);
    }
  };

  const handleSync = async () => {
    if (!profile?.id) return;
    if (!calendarLinked) {
      toast.info("Linking Google Calendar before import...");
      handleLinkGoogle();
      return;
    }

    const today = new Date();
    const todayStr = formatLocalDate(today);
    const defaultRangeEnd = formatLocalDate(addDays(today, 60));
    const targetDate = syncDate || todayStr;
    const importRangeLabel = `${todayStr} to ${defaultRangeEnd}`;

    const syncToast = toast.loading(`Importing Google Calendar data for ${importRangeLabel}...`);

    try {
      const response = await fetch(apiUrl('/api/sync-calendar'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          user_id: profile.id,
          date: targetDate
        }),
      });

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        const detail = errData.detail || "";

        if (detail === "GOOGLE_OAUTH_CONFIG_MISSING") {
          toast.error("Google Calendar import is not configured on the backend. Check Render GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.", {
            id: syncToast,
            duration: 7000
          });
          return;
        }

        if (detail === "GOOGLE_OAUTH_CONFIG_MISMATCH") {
          toast.error("Google Calendar import cannot refresh this link. Make sure Render uses the same Google OAuth client as Supabase, then link Google again.", {
            id: syncToast,
            duration: 9000
          });
          return;
        }

        if (detail === "GOOGLE_TOKEN_REFRESH_FAILED") {
          toast.error("Google Calendar import could not refresh the Google session. Please try again in a moment.", {
            id: syncToast,
            duration: 6000
          });
          return;
        }

        if (response.status === 401) {
          setCalendarLinked(false);
          toast.error("Google Calendar needs to be linked again. Redirecting to Google...", {
            id: syncToast,
            duration: 4000
          });
          setTimeout(() => {
            handleLinkGoogle();
          }, 1200);
          return;
        }
        if (detail === 'TOKEN_EXPIRED') {
          setCalendarLinked(false);
          toast.error("Google Calendar needs to be linked again. Redirecting to Google...", {
            id: syncToast,
            duration: 4000
          });
          setTimeout(() => {
            handleLinkGoogle();
          }, 1200);
          return;
        }
        throw new Error(errData.detail || 'Sync failed');
      }

      const data = await response.json();

      const syncedDayCount = Array.isArray(data.synced_days) ? data.synced_days.length : 0;
      const targetEventCount = Array.isArray(data.events) ? data.events.length : 0;
      const range = data.sync_range;
      const rangeLabel = range?.start && range?.end
        ? `${range.start} to ${range.end}`
        : importRangeLabel;
      const foundNoEvents = syncedDayCount === 0 && targetEventCount === 0;
      toast[foundNoEvents ? "info" : "success"](
        foundNoEvents
          ? `No Google Calendar events found from ${rangeLabel}. Nothing was imported for ${targetDate}.`
          : `Updated Google Calendar external layer for ${rangeLabel}. ${syncedDayCount} day${syncedDayCount === 1 ? "" : "s"} refreshed; ${targetEventCount} event${targetEventCount === 1 ? "" : "s"} are available for ${targetDate}. Open View Full Schedule to import selected events.`,
        {
          id: syncToast,
          duration: foundNoEvents ? 5000 : 7000,
        }
      );

      if (onSyncComplete) {
        onSyncComplete();
      } else {
        setTimeout(() => {
          window.location.reload();
        }, 1500);
      }
    } catch (err: any) {
      console.error("Sync error:", err);
      toast.error("Failed to sync calendar", {
        id: syncToast,
        duration: 4000
      });
    }
  };

  return (
    <div className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-50">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
        <div className="flex items-center gap-2">
          <img
            src={jplanLogoUrl}
            alt="JPlan logo"
            className="brand-logo-nav rounded-md object-cover shadow-sm"
          />
          <h3 className="text-xl font-bold text-primary">JPlan</h3>
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary font-medium uppercase tracking-wider">
            {isAdmin ? 'Admin' : 'Beta'} <span className="text-[8px] font-semibold opacity-70">{combinedVersion}</span>
          </span>
        </div>

        <div className="flex min-w-0 items-center gap-2 sm:gap-3">
          {showSyncButton && (
            <div className="calendar-sync-desktop items-center gap-4">
              <div className="flex items-center gap-2 text-sm text-muted-foreground mr-2">
                <div className={`w-2 h-2 rounded-full ${calendarLinked ? "bg-green-500 animate-pulse" : "bg-muted-foreground/40"}`} />
                <span>{calendarLinked ? "Linked to Google" : "Google not linked"}</span>
              </div>

              <Button
                variant={calendarLinked ? "ghost" : "outline"}
                size="sm"
                className="gap-2 hover:bg-primary/5 hover:text-primary transition-colors"
                onClick={handleSync}
              >
                <RefreshCw className="h-4 w-4" />
                Import Calendar
              </Button>
            </div>
          )}

          <div className="h-4 w-[1px] bg-border mx-2 hidden md:block" />

          {profile && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" className="relative h-9 w-9 rounded-full sm:h-10 sm:w-10">
                  <Avatar className="h-9 w-9 border border-border sm:h-10 sm:w-10">
                    <AvatarImage
                      src={profile.avatar_url || ''}
                      alt={profile.full_name || ''}
                      referrerPolicy="no-referrer"
                    />
                    <AvatarFallback className="bg-primary/50 ">
                      {<UserIcon className="h-10 w-20" />}
                    </AvatarFallback>
                  </Avatar>
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent className="jplan-user-menu w-56" align="end" forceMount>
                <DropdownMenuLabel className="font-normal">
                  <div className="flex min-w-0 flex-col space-y-1">
                    <p className="truncate text-sm font-medium leading-none">{profile.full_name}</p>
                    <p className="truncate text-xs leading-none text-muted-foreground">
                      {profile.email}
                    </p>
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                {showSyncButton && (
                  <DropdownMenuItem onSelect={handleSync} className="mobile-sync-menu-item">
                    <RefreshCw className="mr-2 h-4 w-4" />
                    <span>Import Calendar</span>
                  </DropdownMenuItem>
                )}
                {onSettingsClick && (
                  <DropdownMenuItem onSelect={onSettingsClick}>
                    <Settings className="mr-2 h-4 w-4" />
                    <span>Settings</span>
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem onSelect={handleSignOut} className="text-destructive focus:text-destructive">
                  <LogOut className="mr-2 h-4 w-4" />
                  <span>Log out</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>
    </div>
  );
}
