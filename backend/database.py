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


def get_all_plans(user_id: str) -> List[Dict[str, Any]]:
    """
    Fetch all saved plans for a specific user from the database
    """
    if not supabase:
        raise Exception("Supabase client not initialized.")
    
    try:
        response = supabase.table('daily_plans')\
            .select('*')\
            .eq('user_id', user_id)\
            .order('date', desc=True)\
            .execute()
        
        return [{'date': p['date'], 'activities': p['activities']} for p in response.data]
    except Exception as e:
        print(f"Error fetching plans: {e}")
        raise


def get_plan_by_date(date: str, user_id: str) -> Optional[Dict[str, Any]]:
    """
    Fetch a plan for a specific date and user
    """
    if not supabase:
        raise Exception("Supabase client not initialized.")
    
    try:
        response = supabase.table('daily_plans')\
            .select('*')\
            .eq('date', date)\
            .eq('user_id', user_id)\
            .execute()
        
        if not response.data:
            return None
        
        plan = response.data[0]
        return {'date': plan['date'], 'activities': plan['activities']}
    except Exception as e:
        print(f"Error fetching plan: {e}")
        raise


def save_plan(date: str, activities: List[Dict[str, Any]], user_id: str) -> Dict[str, Any]:
    """
    Save or update a plan in the database for a specific user
    """
    if not supabase:
        raise Exception("Supabase client not initialized.")
    
    try:
        response = supabase.table('daily_plans')\
            .upsert({
                'user_id': user_id,
                'date': date,
                'activities': activities
            }, on_conflict='user_id, date')\
            .execute()
        
        if response.data:
            plan = response.data[0]
            return {'date': plan['date'], 'activities': plan['activities']}
        else:
            raise Exception("Failed to save plan")
    except Exception as e:
        print(f"Error saving plan: {e}")
        raise


def delete_plan(date: str, user_id: str) -> bool:
    """
    Delete a plan for a specific date and user
    """
    if not supabase:
        raise Exception("Supabase client not initialized.")
    
    try:
        supabase.table('daily_plans')\
            .delete()\
            .eq('date', date)\
            .eq('user_id', user_id)\
            .execute()
        
        return True
    except Exception as e:
        print(f"Error deleting plan: {e}")
        raise
