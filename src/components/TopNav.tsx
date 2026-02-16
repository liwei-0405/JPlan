import { Button } from "./ui/button";
import { Settings, RefreshCw, Check } from "lucide-react";

type TopNavProps = {
  onSettingsClick?: () => void;
  showSyncButton?: boolean;
};

export function TopNav({ onSettingsClick, showSyncButton = true }: TopNavProps) {
  return (
    <div className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-lg">JPlan</h3>
          <span className="text-xs px-2 py-0.5 rounded-full bg-secondary text-secondary-foreground">
            Beta
          </span>
        </div>

        <div className="flex items-center gap-3">
          {showSyncButton && (
            <>
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
            </>
          )}

          {onSettingsClick && (
            <Button 
              variant="ghost" 
              size="icon"
              onClick={onSettingsClick}
              title="Settings"
            >
              <Settings className="h-5 w-5" />
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
