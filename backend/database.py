"""
Database layer for Supabase operations
"""
import os
import json
from typing import List, Optional, Dict, Any
from supabase import create_client, Client
from dotenv import load_dotenv

# Robust .env loading
env_path = os.path.join(os.path.dirname(__file__), '.env')
if not os.path.exists(env_path):
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')

load_dotenv(env_path)

# Initialize Supabase client
supabase_url = os.getenv("SUPABASE_URL")
if supabase_url: supabase_url = supabase_url.strip('"').strip("'")

supabase_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
if supabase_key: supabase_key = supabase_key.strip('"').strip("'")

supabase: Optional[Client] = None

if not supabase_url or not supabase_key:
    print(f"[ERROR] Supabase credentials missing! URL: {'Found' if supabase_url else 'Missing'}, Key: {'Found' if supabase_key else 'Missing'}")
else:
    try:
        supabase = create_client(supabase_url, supabase_key)
    except Exception as e:
        print(f"[ERROR] Failed to initialize Supabase client: {e}")

def _parse_activities(activities: Any) -> List[Dict[str, Any]]:
    """Helper to ensure activities is always a list of dicts"""
    if activities is None:
        return []
    if isinstance(activities, list):
        return activities
    if isinstance(activities, str):
        try:
            parsed = json.loads(activities)
            return parsed if isinstance(parsed, list) else []
        except:
            return []
    return []


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
        
        results = [{'date': p['date'], 'activities': _parse_activities(p['activities'])} for p in response.data]
        return results
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
        return {'date': plan['date'], 'activities': _parse_activities(plan.get('activities'))}
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
            return {'date': plan['date'], 'activities': _parse_activities(plan.get('activities'))}
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
