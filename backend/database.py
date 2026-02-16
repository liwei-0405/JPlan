"""
Database layer for Supabase operations
"""
import os
from typing import List, Optional, Dict, Any
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# Initialize Supabase client
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_KEY")

if not supabase_url or not supabase_key:
    print("Warning: Supabase credentials not found in environment variables")
    supabase: Optional[Client] = None
else:
    supabase: Client = create_client(supabase_url, supabase_key)


def get_all_plans() -> List[Dict[str, Any]]:
    """
    Fetch all saved plans from the database
    Returns a list of plan objects sorted by date (descending)
    """
    if not supabase:
        raise Exception("Supabase client not initialized. Check your environment variables.")
    
    try:
        response = supabase.table('daily_plans')\
            .select('*')\
            .order('date', desc=True)\
            .execute()
        
        # Convert to frontend format
        plans = []
        for plan in response.data:
            plans.append({
                'date': plan['date'],
                'activities': plan['activities']
            })
        
        return plans
    except Exception as e:
        print(f"Error fetching all plans: {e}")
        raise


def get_plan_by_date(date: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a plan for a specific date
    
    Args:
        date: Date string in YYYY-MM-DD format
    
    Returns:
        Plan object or None if not found
    """
    if not supabase:
        raise Exception("Supabase client not initialized. Check your environment variables.")
    
    try:
        response = supabase.table('daily_plans')\
            .select('*')\
            .eq('date', date)\
            .execute()
        
        if not response.data:
            return None
        
        plan = response.data[0]
        return {
            'date': plan['date'],
            'activities': plan['activities']
        }
    except Exception as e:
        print(f"Error fetching plan for date {date}: {e}")
        raise


def save_plan(date: str, activities: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Save or update a plan in the database
    
    Args:
        date: Date string in YYYY-MM-DD format
        activities: List of activity objects
    
    Returns:
        Saved plan object
    """
    if not supabase:
        raise Exception("Supabase client not initialized. Check your environment variables.")
    
    try:
        # Use upsert to insert or update
        response = supabase.table('daily_plans')\
            .upsert({
                'date': date,
                'activities': activities
            }, on_conflict='date')\
            .execute()
        
        if response.data:
            plan = response.data[0]
            return {
                'date': plan['date'],
                'activities': plan['activities']
            }
        else:
            raise Exception("Failed to save plan")
    except Exception as e:
        print(f"Error saving plan: {e}")
        raise


def delete_plan(date: str) -> bool:
    """
    Delete a plan for a specific date
    
    Args:
        date: Date string in YYYY-MM-DD format
    
    Returns:
        True if successful
    """
    if not supabase:
        raise Exception("Supabase client not initialized. Check your environment variables.")
    
    try:
        supabase.table('daily_plans')\
            .delete()\
            .eq('date', date)\
            .execute()
        
        return True
    except Exception as e:
        print(f"Error deleting plan for date {date}: {e}")
        raise
