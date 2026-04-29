import type { DailySchedule, ActivityBlock } from '../App';

const API_BASE_URL = 'http://127.0.0.1:8000';

export type PatchOperation = {
    op: 'add' | 'update' | 'remove' | 'move' | 'replace' | 'update_priority';
    target_id?: string;
    title?: string;
    startTime?: string;
    endTime?: string;
    duration_minutes?: number;
    location?: string;
    priority?: 'low' | 'medium' | 'high';
    is_mandatory?: boolean;
    notes?: string;
};

export type PatchResponse = {
    scheduleId: string;
    version: number;
    applied: boolean;
    updatedActivities: ActivityBlock[];
    deletedItemIds: string[];
    affectedRange?: { start: string; end: string };
    explanation?: string;
};

export type ChatResponse = {
    reply: string;
    patch?: PatchResponse;
    full_schedule?: DailySchedule;
    transcription?: string;
};

/**
 * Send a chat message and receive a patch or full schedule
 */
export async function chatWithLLM(
    message: string, 
    userId: string | undefined, 
    currentSchedule: DailySchedule | null,
    history: any[] = []
): Promise<ChatResponse> {
    try {
        const response = await fetch(`${API_BASE_URL}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                user_id: userId,
                current_schedule: currentSchedule,
                history
            }),
        });

        if (!response.ok) {
            throw new Error(`Chat error: ${response.statusText}`);
        }

        return await response.json();
    } catch (err) {
        console.error('Chat exception:', err);
        return { reply: "I'm having trouble connecting to my brain right now." };
    }
}

/**
 * Apply a batch of manual operations to the server-side state
 */
export async function applyOperations(
    scheduleId: string,
    userId: string,
    baseVersion: number,
    operations: PatchOperation[]
): Promise<{ success: boolean; result?: PatchResponse; error?: string }> {
    try {
        const response = await fetch(`${API_BASE_URL}/api/schedules/${scheduleId}/operations`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                user_id: userId,
                baseVersion,
                operations
            }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            return { success: false, error: errorData.detail };
        }

        const result = await response.json();
        return { success: true, result };
    } catch (err) {
        console.error('Apply operations exception:', err);
        return { success: false, error: 'Connection failed' };
    }
}

/**
 * Save or update a daily plan via backend API (Full-Save compatibility)
 */
export async function savePlan(schedule: DailySchedule, userId: string): Promise<{ success: boolean; savedPlan?: DailySchedule; error?: string }> {
    try {
        if (!userId) return { success: false, error: 'User not authenticated' };

        const response = await fetch(`${API_BASE_URL}/api/plans`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                scheduleId: schedule.scheduleId,
                date: schedule.date,
                version: schedule.version,
                activities: schedule.activities,
                explanations: schedule.explanations || [],
                unscheduled_activities: schedule.unscheduled_activities || [],
                user_id: userId
            }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            return { success: false, error: errorData.detail || 'Failed to save plan' };
        }

        const savedPlan = await response.json();
        return { success: true, savedPlan };
    } catch (err) {
        console.error('Exception saving plan:', err);
        return { success: false, error: 'Failed to connect to backend' };
    }
}

/**
 * Get a plan for a specific date via backend API
 */
export async function getPlanByDate(date: string, userId: string): Promise<DailySchedule | null> {
    try {
        if (!userId) return null;
        const response = await fetch(`${API_BASE_URL}/api/plans/${date}?user_id=${userId}`);
        if (!response.ok) return null;
        return await response.json();
    } catch (err) {
        console.error('Exception fetching plan:', err);
        return null;
    }
}

/**
 * Get all saved plans via backend API
 */
export async function getAllPlans(userId: string): Promise<DailySchedule[]> {
    try {
        if (!userId) return [];
        const response = await fetch(`${API_BASE_URL}/api/plans?user_id=${userId}`);
        if (!response.ok) return [];
        return await response.json();
    } catch (err) {
        console.error('Exception fetching all plans:', err);
        return [];
    }
}

/**
 * Delete a plan for a specific date via backend API
 */
export async function deletePlan(date: string, userId: string): Promise<{ success: boolean; error?: string }> {
    try {
        if (!userId) return { success: false, error: 'User not authenticated' };
        const response = await fetch(`${API_BASE_URL}/api/plans/${date}?user_id=${userId}`, {
            method: 'DELETE',
        });
        if (!response.ok) return { success: false, error: 'Failed' };
        return { success: true };
    } catch (err) {
        return { success: false, error: 'Connection failed' };
    }
}

/**
 * Export a daily schedule to Google Calendar via backend API
 */
export async function exportPlanToGoogle(date: string, userId: string): Promise<{ success: boolean; exportedCount?: number; error?: string }> {
    try {
        if (!userId) return { success: false, error: 'User not authenticated' };
        const response = await fetch(`${API_BASE_URL}/api/export-calendar`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user_id: userId, date }),
        });
        if (!response.ok) return { success: false, error: 'Failed' };
        const data = await response.json();
        return { success: true, exportedCount: data.exported_count };
    } catch (err) {
        return { success: false, error: 'Connection failed' };
    }
}
