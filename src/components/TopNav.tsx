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

type TopNavProps = {
  onSettingsClick?: () => void;
  showSyncButton?: boolean;
};

export function TopNav({ onSettingsClick, showSyncButton = true }: TopNavProps) {
  const { profile, signOut, isAdmin } = useAuth();

  const handleSignOut = async () => {
    if (typeof signOut === 'function') {
      await signOut();
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
              <div className="flex items-center gap-2 text-sm text-muted-foreground mr-2">
                <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
                <span>Calendar connected</span>
              </div>
              
              <Button 
                variant="ghost" 
                size="sm"
                className="gap-2"
              >
                <RefreshCw className="h-4 w-4" />
                Sync
              </Button>
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
                    <AvatarFallback className="bg-primary/10 text-primary">
                      {profile.full_name?.charAt(0) || <UserIcon className="h-4 w-4" />}
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

