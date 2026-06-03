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

type TopNavProps = {
  onSettingsClick?: () => void;
  showSyncButton?: boolean;
  onSyncComplete?: () => void;
  syncDate?: string;
};

export function TopNav({ 
  onSettingsClick, 
  showSyncButton = true, 
  onSyncComplete,
  syncDate 
}: TopNavProps) {
  const { profile, signOut, isAdmin, isGoogleLinked } = useAuth();

  const handleSignOut = async () => {
    if (typeof signOut === 'function') {
      await signOut();
    }
  };

  const handleLinkGoogle = async () => {
    try {
      // Link Google account with Calendar scopes
      const { error } = await supabase.auth.signInWithOAuth({
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
      if (error) throw error;
    } catch (err: any) {
      console.error('Error linking Google:', err);
    }
  };

  const handleSync = async () => {
    if (!profile?.id) return;

    // Get target date
    const todayStr = new Date().toISOString().split('T')[0];
    const targetDate = syncDate || todayStr;

    const syncToast = toast.loading(`Syncing calendar for ${targetDate}...`);

    try {
      const response = await fetch('http://localhost:8000/api/sync-calendar', {
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
        if (errData.detail === 'TOKEN_EXPIRED') {
          toast.error("Google Session Expired. Re-linking...", {
            id: syncToast,
            duration: 3000
          });
          setTimeout(() => {
            handleLinkGoogle();
          }, 1500);
          return;
        }
        throw new Error(errData.detail || 'Sync failed');
      }

      const data = await response.json();

      toast.success(`Successfully synced ${data.events.length} events!`, {
        id: syncToast,
        duration: 5000
      });

      // Trigger refresh-less update if possible
      if (onSyncComplete) {
        onSyncComplete();
      } else {
        // Fallback to reload if for some reason the callback isn't passed
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
      <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-xl font-bold text-primary">JPlan</h3>
          <span className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary font-medium uppercase tracking-wider">
            {isAdmin ? 'Admin' : 'Beta'}
          </span>
        </div>

        <div className="flex items-center gap-3">
          {showSyncButton && (
            <div className="hidden md:flex items-center gap-4">
              {isGoogleLinked ? (
                <>
                  <div className="flex items-center gap-2 text-sm text-muted-foreground mr-2">
                    <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                    <span>Linked to Google</span>
                  </div>

                  <Button
                    variant="ghost"
                    size="sm"
                    className="gap-2 hover:bg-primary/5 hover:text-primary transition-colors"
                    onClick={handleSync}
                  >
                    <RefreshCw className="h-4 w-4" />
                    Sync
                  </Button>
                </>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2 border-primary/20 hover:bg-primary/5 hover:border-primary/40 transition-all font-medium"
                  onClick={handleLinkGoogle}
                >
                  <svg className="h-4 w-4" viewBox="0 0 24 24">
                    <path
                      d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"
                      fill="#4285F4"
                    />
                    <path
                      d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-1 .67-2.28 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"
                      fill="#34A853"
                    />
                    <path
                      d="M5.84 14.09c-.22-.67-.35-1.39-.35-2.09s.13-1.42.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"
                      fill="#FBBC05"
                    />
                    <path
                      d="M12 5.38c1.62 0 3.06.56 4.21 1.66l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"
                      fill="#EA4335"
                    />
                  </svg>
                  Link Google Calendar
                </Button>
              )}
            </div>
          )}

          <div className="h-4 w-[1px] bg-border mx-2 hidden md:block" />

          {profile && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" className="relative h-10 w-10 rounded-full">
                  <Avatar className="h-10 w-10 border border-border">
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
              <DropdownMenuContent className="w-56" align="end" forceMount>
                <DropdownMenuLabel className="font-normal">
                  <div className="flex flex-col space-y-1">
                    <p className="text-sm font-medium leading-none">{profile.full_name}</p>
                    <p className="text-xs leading-none text-muted-foreground">
                      {profile.email}
                    </p>
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
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
