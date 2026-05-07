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
    reply_status?: 'success' | 'partial' | 'warning' | 'location_pending' | 'conflict' | 'error' | 'clarification_needed' | 'not_applied';
    recommend_allow_clash?: boolean;
    reply_reason?: string | null;
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
    history: any[] = [],
    allowClash: boolean = false,
    accurateTravelTime: boolean = false,
): Promise<ChatResponse> {
    try {
        console.log('[JPLAN][CHAT_FLAGS]', { allow_clash: allowClash, accurate_travel_time: accurateTravelTime });
        const response = await fetch(`${API_BASE_URL}/chat`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                message,
                user_id: userId,
                current_schedule: currentSchedule,
                history,
                allow_clash: allowClash,
                accurate_travel_time: accurateTravelTime,
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
                schedule_blocks: schedule.schedule_blocks || [],
                explanations: schedule.explanations || [],
                unscheduled_activities: schedule.unscheduled_activities || [],
                user_id: userId,
                status: schedule.status || "ok",
                planning_mode: schedule.planning_mode || "feasibility_first",
                allow_clash: Boolean(schedule.allow_clash),
                accurate_travel_time: Boolean(schedule.accurate_travel_time),
                schedule_status: schedule.schedule_status || schedule.status || "ok",
                travel_validation_status: schedule.travel_validation_status || "not_requested",
                warnings: schedule.warnings || [],
                location_resolution_requests: schedule.location_resolution_requests || [],
                route_conflicts: schedule.route_conflicts || [],
                conflicts: schedule.conflicts || [],
                conflict: schedule.conflict || null,
                unmet_items: schedule.unmet_items || [],
                validation_issues: schedule.validation_issues || [],
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

export type GeocodeCandidate = {
    label?: string;
    display_name?: string;
    address?: string;
    latitude: number;
    longitude: number;
    confidence?: number;
    source?: string;
    confirmed_by_user?: boolean;
};

export type SavedLocation = {
    user_id?: string;
    label: string;
    display_name?: string;
    address?: string;
    latitude?: number | null;
    longitude?: number | null;
    source?: string;
    confirmed_by_user?: boolean;
    updated_at?: string;
};

export async function getSavedLocations(userId: string): Promise<SavedLocation[]> {
    const response = await fetch(`${API_BASE_URL}/api/locations?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) return [];
    return await response.json();
}

export async function deleteSavedLocation(userId: string, label: string): Promise<{ success: boolean }> {
    const response = await fetch(
        `${API_BASE_URL}/api/locations?user_id=${encodeURIComponent(userId)}&label=${encodeURIComponent(label)}`,
        { method: 'DELETE' },
    );
    if (!response.ok) return { success: false };
    return await response.json();
}

export type GeocodeResponse = {
    candidates: GeocodeCandidate[];
    warning?: string;
    warnings?: string[];
    expanded_query?: string;
    geocode_status?: 'ok' | 'partial' | 'fallback_unavailable' | 'rate_limited';
    providers_used?: string[];
};

export async function geocodeLocation(query: string, category?: string): Promise<GeocodeResponse> {
    const params = new URLSearchParams({ query });
    if (category) params.set('category', category);
    const response = await fetch(`${API_BASE_URL}/api/locations/geocode?${params.toString()}`);
    if (!response.ok) return { candidates: [], warning: 'Geocoding failed', warnings: ['Geocoding failed'], geocode_status: 'fallback_unavailable' };
    return await response.json();
}

export async function resolveLocation(payload: {
    user_id: string;
    label: string;
    address: string;
    display_name?: string;
    category?: string;
    latitude?: number;
    longitude?: number;
    source?: string;
    confirmed_by_user?: boolean;
}): Promise<any> {
    const response = await fetch(`${API_BASE_URL}/api/locations/resolve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Location resolution failed' }));
        throw new Error(error.detail || 'Location resolution failed');
    }
    return await response.json();
}

export async function completeTravelValidation(schedule: DailySchedule, userId: string): Promise<DailySchedule> {
    const response = await fetch(`${API_BASE_URL}/api/travel/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, schedule }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Travel validation failed' }));
        throw new Error(error.detail || 'Travel validation failed');
    }
    return await response.json();
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
