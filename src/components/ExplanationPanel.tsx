import { Button } from "./ui/button";
import { ArrowLeft, Info, Sparkles } from "lucide-react";
import type { DailySchedule } from "../App";

type ExplanationPanelProps = {
  schedule: DailySchedule;
  onBack: () => void;
};

export function ExplanationPanel({ schedule, onBack }: ExplanationPanelProps) {
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
  const collected = new Set<string>();

  for (const explanation of schedule.explanations || []) {
    if (explanation?.trim()) collected.add(explanation.trim());
  }

  for (const activity of schedule.activities) {
    if (activity.explanation?.trim()) {
      collected.add(activity.explanation.trim());
    }
    for (const trace of activity.trace || []) {
      if (trace?.trim()) collected.add(trace.trim());
    }
  }

  for (const item of schedule.unscheduled_activities || []) {
    if (item.reason?.trim()) {
      collected.add(`${item.title} was left out because ${item.reason.charAt(0).toLowerCase()}${item.reason.slice(1)}`);
    }
  }

  if (collected.size === 0) {
    collected.add("This schedule currently has no planner trace, so only the final timeline is available.");
  }

  return Array.from(collected);
}
