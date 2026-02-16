import type { DailySchedule } from '../App';

const API_BASE_URL = 'http://127.0.0.1:8000';

/**
 * Save or update a daily plan via backend API
 */
export async function savePlan(schedule: DailySchedule): Promise<{ success: boolean; error?: string }> {
    try {
        const response = await fetch(`${API_BASE_URL}/api/plans`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                date: schedule.date,
                activities: schedule.activities,
            }),
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            console.error('Error saving plan:', errorData);
            return { success: false, error: errorData.detail || 'Failed to save plan' };
        }

        return { success: true };
    } catch (err) {
        console.error('Exception saving plan:', err);
        return { success: false, error: 'Failed to connect to backend' };
    }
}

/**
 * Get a plan for a specific date via backend API
 */
export async function getPlanByDate(date: string): Promise<DailySchedule | null> {
    try {
        const response = await fetch(`${API_BASE_URL}/api/plans/${date}`);

        if (response.status === 404) {
            // No plan for this date - this is not an error
            return null;
        }

        if (!response.ok) {
            console.error('Error fetching plan:', response.statusText);
            return null;
        }

        const plan = await response.json();
        return {
            date: plan.date,
            activities: plan.activities,
        };
    } catch (err) {
        console.error('Exception fetching plan:', err);
        return null;
    }
}

/**
 * Get all saved plans via backend API
 */
export async function getAllPlans(): Promise<DailySchedule[]> {
    try {
        const response = await fetch(`${API_BASE_URL}/api/plans`);

        if (!response.ok) {
            console.error('Error fetching all plans:', response.statusText);
            return [];
        }

        const plans = await response.json();
        return plans.map((plan: any) => ({
            date: plan.date,
            activities: plan.activities,
        }));
    } catch (err) {
        console.error('Exception fetching all plans:', err);
        return [];
    }
}

/**
 * Delete a plan for a specific date via backend API
 */
export async function deletePlan(date: string): Promise<{ success: boolean; error?: string }> {
    try {
        const response = await fetch(`${API_BASE_URL}/api/plans/${date}`, {
            method: 'DELETE',
        });

        if (!response.ok) {
            const errorData = await response.json().catch(() => ({ detail: 'Unknown error' }));
            console.error('Error deleting plan:', errorData);
            return { success: false, error: errorData.detail || 'Failed to delete plan' };
        }

        return { success: true };
    } catch (err) {
        console.error('Exception deleting plan:', err);
        return { success: false, error: 'Failed to connect to backend' };
    }
}
