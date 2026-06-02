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
    llm_fallback_reason?: string | null;
    patch?: PatchResponse;
    full_schedule?: DailySchedule;
    schedule_data?: DailySchedule;
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
                committed_schedule_blocks: schedule.committed_schedule_blocks || [],
                explanations: schedule.explanations || [],
                unscheduled_activities: schedule.unscheduled_activities || [],
                user_id: userId,
                status: schedule.status || "ok",
                planning_mode: schedule.planning_mode || "feasibility_first",
                allow_clash: Boolean(schedule.allow_clash),
                accurate_travel_time: Boolean(schedule.accurate_travel_time),
                travel_intent: Boolean(schedule.travel_intent || schedule.preferences?.travel_intent),
                preferences: schedule.preferences || {},
                schedule_constraints: schedule.schedule_constraints || {},
                schedule_status: schedule.schedule_status || schedule.status || "ok",
                travel_validation_status: schedule.travel_validation_status || "not_requested",
                warnings: schedule.warnings || [],
                location_resolution_requests: schedule.location_resolution_requests || [],
                route_conflicts: schedule.route_conflicts || [],
                pending_repair_suggestions: schedule.pending_repair_suggestions || [],
                unfit_activities: schedule.unfit_activities || [],
                optional_skipped: schedule.optional_skipped || [],
                blocked_activities: schedule.blocked_activities || [],
                route_repair_actions: schedule.route_repair_actions || [],
                route_efficiency: schedule.route_efficiency || {},
                route_total_before: schedule.route_total_before ?? null,
                route_total_after: schedule.route_total_after ?? null,
                route_minutes_saved: schedule.route_minutes_saved ?? null,
                location_revisits_count: schedule.location_revisits_count ?? null,
                same_location_split_penalty_before: schedule.same_location_split_penalty_before ?? null,
                same_location_split_penalty_after: schedule.same_location_split_penalty_after ?? null,
                revisit_penalty_before: schedule.revisit_penalty_before ?? null,
                revisit_penalty_after: schedule.revisit_penalty_after ?? null,
                start_route_summary: schedule.start_route_summary || null,
                preview_id: schedule.preview_id || null,
                preview_base_version: schedule.preview_base_version ?? null,
                preview_status: schedule.preview_status || null,
                preview_reason: schedule.preview_reason || null,
                preview_schedule: schedule.preview_schedule || null,
                failed_repair_attempt: schedule.failed_repair_attempt || null,
                needs_reschedule: Boolean(schedule.needs_reschedule),
                reschedule_reason: schedule.reschedule_reason || null,
                needs_travel_validation: Boolean(schedule.needs_travel_validation),
                last_rescheduled_at: schedule.last_rescheduled_at || null,
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

export type PlanningLocationPayload = {
    label?: string;
    display_name?: string;
    address?: string;
    latitude?: number | null;
    longitude?: number | null;
    category?: string;
    source?: string;
    confirmed_by_user?: boolean;
};

export type PlanningPreferencesPayload = {
    day_start_time: string;
    day_end_time: string;
    use_day_boundary_preferences?: boolean;
    default_start_location?: PlanningLocationPayload | null;
};

export type RecentLocationPayload = PlanningLocationPayload & {
    id?: string;
    location_key?: string;
    last_used_at?: string;
};

export async function getPlanningPreferences(userId: string): Promise<PlanningPreferencesPayload | null> {
    const response = await fetch(`${API_BASE_URL}/api/preferences?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) return null;
    return await response.json();
}

export async function savePlanningPreferencesRemote(
    userId: string,
    preferences: PlanningPreferencesPayload,
): Promise<PlanningPreferencesPayload> {
    const response = await fetch(`${API_BASE_URL}/api/preferences`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, ...preferences }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Failed to save preferences' }));
        throw new Error(error.detail || 'Failed to save preferences');
    }
    return await response.json();
}

export async function getRecentLocations(userId: string): Promise<RecentLocationPayload[]> {
    const response = await fetch(`${API_BASE_URL}/api/recent-locations?user_id=${encodeURIComponent(userId)}`);
    if (!response.ok) return [];
    return await response.json();
}

export async function addRecentLocationRemote(
    userId: string,
    location: PlanningLocationPayload,
): Promise<RecentLocationPayload[]> {
    const response = await fetch(`${API_BASE_URL}/api/recent-locations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, location }),
    });
    if (!response.ok) return [];
    return await response.json();
}

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

export async function completeTravelValidation(
    schedule: DailySchedule,
    userId: string,
    source: 'toggle' | 'chat' | 'manual' = 'manual',
): Promise<DailySchedule> {
    const response = await fetch(`${API_BASE_URL}/api/travel/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, schedule, source }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Travel validation failed' }));
        throw new Error(error.detail || 'Travel validation failed');
    }
    return await response.json();
}

export async function runScheduler(
    schedule: DailySchedule,
    userId: string,
    source: 'manual_button' = 'manual_button',
): Promise<DailySchedule> {
    const response = await fetch(`${API_BASE_URL}/api/schedules/replan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            user_id: userId,
            schedule,
            schedule_version: schedule.version || 1,
            source,
        }),
    });
    if (!response.ok) {
        const error = await response.json().catch(() => ({ detail: 'Run scheduler failed' }));
        throw new Error(error.detail || 'Run scheduler failed');
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
