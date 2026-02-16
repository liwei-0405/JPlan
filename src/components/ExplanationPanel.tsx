import { Button } from "./ui/button";
import { ArrowLeft, Info, Sparkles } from "lucide-react";
import type { DailySchedule } from "../App";

type ExplanationPanelProps = {
  schedule: DailySchedule;
  onBack: () => void;
};

export function ExplanationPanel({ schedule, onBack }: ExplanationPanelProps) {
  // Generate explanations based on schedule
  const explanations = generateExplanations(schedule);

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-secondary/20">
      <div className="max-w-3xl mx-auto px-6 py-8">
        <Button 
          variant="ghost" 
          onClick={onBack}
          className="mb-8 rounded-xl"
        >
          <ArrowLeft className="mr-2 h-4 w-4" />
          Back to Schedule
        </Button>

        <div className="mb-8">
          <div className="flex items-center gap-2 mb-2">
            <Sparkles className="h-6 w-6 text-primary" />
            <h2>Schedule Explanation</h2>
          </div>
          <p className="text-muted-foreground">
            Understanding how your schedule was planned
          </p>
        </div>

        <div className="space-y-4">
          {explanations.map((explanation, index) => (
            <div 
              key={index}
              className="bg-card border border-border rounded-2xl p-5 shadow-sm hover:shadow-md transition-shadow"
            >
              <div className="flex gap-3">
                <div className="flex-shrink-0 mt-0.5">
                  <div className="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center">
                    <Info className="h-5 w-5 text-primary" />
                  </div>
                </div>
                <div>
                  <p>{explanation}</p>
                </div>
              </div>
            </div>
          ))}

          {explanations.length === 0 && (
            <div className="text-center py-12 bg-card rounded-2xl border border-dashed border-border">
              <p className="text-muted-foreground">
                No explanations available for this schedule.
              </p>
            </div>
          )}
        </div>

        <div className="mt-8 bg-gradient-to-br from-[#fff9d4]/20 to-[#fff9d4]/5 rounded-2xl p-5 border border-[#fff9d4]/30">
          <p className="text-sm text-muted-foreground">
            💡 These explanations help you understand the planning decisions made to create a feasible schedule based on your activities, locations, and time constraints.
          </p>
        </div>
      </div>
    </div>
  );
}

function generateExplanations(schedule: DailySchedule): string[] {
  const explanations: string[] = [];
  
  // Check for travel blocks
  const hasTravelBlocks = schedule.activities.some(a => a.type === "travel");
  if (hasTravelBlocks) {
    explanations.push("Extra travel time was added between locations to ensure you can move between activities comfortably.");
  }

  // Check for morning activities
  const morningActivities = schedule.activities.filter(a => {
    const time = a.startTime.toLowerCase();
    return time.includes("am") || time.startsWith("12:");
  });
  
  if (morningActivities.length > 0 && morningActivities[0].title.toLowerCase().includes("gym")) {
    explanations.push("The gym session was scheduled early in the morning when the facility is less crowded and fits better before other commitments.");
  }

  // Check for specific time activities
  const hasFixedTime = schedule.activities.some(a => 
    a.title.toLowerCase().includes("meeting") || 
    a.title.toLowerCase().includes("appointment")
  );
  
  if (hasFixedTime) {
    explanations.push("Activities with specific time requirements were prioritized and scheduled at their designated times.");
  }

  // Check for lunch timing
  const lunchActivity = schedule.activities.find(a => 
    a.title.toLowerCase().includes("lunch")
  );
  
  if (lunchActivity) {
    explanations.push("Lunch was scheduled during typical meal hours to align with restaurant availability and social dining norms.");
  }

  // Check for afternoon/evening activities
  const afternoonActivities = schedule.activities.filter(a => {
    const startTime = a.startTime.toLowerCase();
    if (startTime.includes("pm")) {
      const hour = parseInt(startTime.split(":")[0]);
      return hour >= 2 && hour < 6;
    }
    return false;
  });

  if (afternoonActivities.some(a => a.title.toLowerCase().includes("dentist"))) {
    explanations.push("Medical appointments were scheduled based on typical clinic operating hours.");
  }

  // General feasibility note
  explanations.push("All activities were arranged to ensure adequate time for completion without overlap or rushing between tasks.");

  // Check for activities that might need special consideration
  const workActivity = schedule.activities.find(a => 
    a.title.toLowerCase().includes("work") || 
    a.title.toLowerCase().includes("project")
  );
  
  if (workActivity) {
    explanations.push("Longer focus-based activities were scheduled in continuous blocks to maintain productivity and avoid interruptions.");
  }

  return explanations;
}