"""
Database layer for Supabase operations
"""
import os
import json
import hashlib
from datetime import datetime, timezone
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
SCHEDULE_PAYLOAD_VERSION = 4

if not supabase_url or not supabase_key:
    print(f"[ERROR] Supabase credentials missing! URL: {'Found' if supabase_url else 'Missing'}, Key: {'Found' if supabase_key else 'Missing'}")
else:
    try:
        supabase = create_client(supabase_url, supabase_key)
    except Exception as e:
        print(f"[ERROR] Failed to initialize Supabase client: {e}")

def _generate_schedule_id(user_id: str, date: str) -> str:
    """Generate a stable schedule ID based on user and date."""
    seed = f"{user_id}:{date}"
    return hashlib.md5(seed.encode()).hexdigest()

def _parse_schedule_payload(payload: Any, user_id: str = "", date: str = "") -> Dict[str, Any]:
    """Normalize saved JSONB payload into a ScheduleEnvelope dict."""
    default_envelope = {
        "schema_version": SCHEDULE_PAYLOAD_VERSION,
        "scheduleId": _generate_schedule_id(user_id, date) if user_id and date else "",
        "date": date,
        "status": "ok",
        "planning_mode": "feasibility_first",
        "allow_clash": False,
        "version": 1,
        "preferences": {},
        "activities": [],
        "schedule_blocks": [],
        "explanations": [],
        "unscheduled_activities": [],
        "conflict": None,
        "conflicts": [],
        "unmet_items": [],
        "validation_issues": [],
    }

    if payload is None:
        return default_envelope

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return default_envelope

    # If it's already an envelope (schema_version >= 3)
    if isinstance(payload, dict) and payload.get("schema_version", 0) >= 3:
        return {
            "schema_version": payload.get("schema_version", SCHEDULE_PAYLOAD_VERSION),
            "scheduleId": payload.get("scheduleId") or _generate_schedule_id(user_id, date),
            "date": payload.get("date") or date,
            "status": payload.get("status", "ok"),
            "planning_mode": payload.get("planning_mode", "feasibility_first"),
            "allow_clash": bool(payload.get("allow_clash", False)),
            "version": payload.get("version", 1),
            "preferences": payload.get("preferences") or {},
            "activities": payload.get("activities") or payload.get("items") or [],
            "schedule_blocks": payload.get("schedule_blocks") or [],
            "explanations": payload.get("explanations") or [],
            "unscheduled_activities": payload.get("unscheduled_activities") or [],
            "conflict": payload.get("conflict"),
            "conflicts": payload.get("conflicts") or [],
            "unmet_items": payload.get("unmet_items") or [],
            "validation_issues": payload.get("validation_issues") or [],
        }

    # Backward compatibility
    if isinstance(payload, list):
        res = default_envelope.copy()
        res["activities"] = payload
        res["schema_version"] = 1
        return res

    if isinstance(payload, dict):
        activities = payload.get("activities") or payload.get("items")
        explanations = payload.get("explanations")
        unscheduled = payload.get("unscheduled_activities")
        return {
            "schema_version": payload.get("schema_version", 2),
            "scheduleId": payload.get("scheduleId") or _generate_schedule_id(user_id, date),
            "date": payload.get("date") or date,
            "status": payload.get("status", "ok"),
            "planning_mode": payload.get("planning_mode", "feasibility_first"),
            "allow_clash": bool(payload.get("allow_clash", False)),
            "version": payload.get("version", 1),
            "preferences": payload.get("preferences") or {},
            "activities": activities if isinstance(activities, list) else [],
            "schedule_blocks": payload.get("schedule_blocks") or [],
            "explanations": explanations if isinstance(explanations, list) else [],
            "unscheduled_activities": unscheduled if isinstance(unscheduled, list) else [],
            "conflict": payload.get("conflict"),
            "conflicts": payload.get("conflicts") or [],
            "unmet_items": payload.get("unmet_items") or [],
            "validation_issues": payload.get("validation_issues") or [],
        }

    return default_envelope

def get_all_plans(user_id: str) -> List[Dict[str, Any]]:
    """Fetch all saved plans for a specific user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    
    try:
        response = supabase.table('daily_plans').select('*').eq('user_id', user_id).order('date', desc=True).execute()
        return [_parse_schedule_payload(p.get('activities'), user_id, p.get('date', '')) for p in response.data]
    except Exception as e:
        print(f"Error fetching plans: {e}")
        raise

def get_plan_by_date(date: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a plan for a specific date and user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    
    try:
        response = supabase.table('daily_plans').select('*').eq('date', date).eq('user_id', user_id).execute()
        if not response.data: return None
        return _parse_schedule_payload(response.data[0].get('activities'), user_id, date)
    except Exception as e:
        print(f"Error fetching plan: {e}")
        raise

def save_plan(
    date: str,
    activities: List[Dict[str, Any]],
    user_id: str,
    schedule_blocks: Optional[List[Dict[str, Any]]] = None,
    explanations: Optional[List[str]] = None,
    unscheduled_activities: Optional[List[Dict[str, Any]]] = None,
    version: int = 1,
    schedule_id: Optional[str] = None,
    preferences: Optional[Dict[str, Any]] = None,
    status: str = "ok",
    conflict: Optional[Dict[str, Any]] = None,
    planning_mode: str = "feasibility_first",
    allow_clash: bool = False,
    conflicts: Optional[List[Dict[str, Any]]] = None,
    unmet_items: Optional[List[Dict[str, Any]]] = None,
    validation_issues: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Save or update a plan in the database."""
    if not supabase: raise Exception("Supabase client not initialized.")
    
    if not schedule_id:
        schedule_id = _generate_schedule_id(user_id, date)

    envelope = {
        'schema_version': SCHEDULE_PAYLOAD_VERSION,
        'scheduleId': schedule_id,
        'date': date,
        'status': status,
        'planning_mode': planning_mode,
        'allow_clash': allow_clash,
        'version': version,
        'preferences': preferences or {},
        'activities': activities,
        'schedule_blocks': schedule_blocks or [],
        'explanations': explanations or [],
        'unscheduled_activities': unscheduled_activities or [],
        'conflict': conflict,
        'conflicts': conflicts or [],
        'unmet_items': unmet_items or [],
        'validation_issues': validation_issues or [],
    }
    
    try:
        response = supabase.table('daily_plans').upsert({
            'user_id': user_id,
            'date': date,
            'activities': envelope
        }, on_conflict='user_id, date').execute()
        
        if response.data:
            print(f"[JPLAN][DATABASE] Successfully saved plan for user {user_id} on date {date}")
            return _parse_schedule_payload(response.data[0].get('activities'), user_id, date)
        else:
            raise Exception("Failed to save plan")
    except Exception as e:
        print(f"Error saving plan: {e}")
        raise

def get_user_locations(user_id: str) -> List[Dict[str, Any]]:
    """Fetch all saved locations for a specific user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        response = supabase.table('user_locations').select('*').eq('user_id', user_id).execute()
        return response.data or []
    except Exception as e:
        print(f"Error fetching user locations: {e}")
        return []

def add_user_location(user_id: str, label: str, address: str, lat: Optional[float] = None, lng: Optional[float] = None) -> Dict[str, Any]:
    """Add or update a saved location for a user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        response = supabase.table('user_locations').upsert({
            'user_id': user_id,
            'label': label,
            'address': address,
            'latitude': lat,
            'longitude': lng,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }, on_conflict='user_id, label').execute()
        return response.data[0] if response.data else {}
    except Exception as e:
        print(f"Error saving user location: {e}")
        raise

def delete_user_location(user_id: str, label: str) -> bool:
    """Delete a saved location."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        supabase.table('user_locations').delete().eq('user_id', user_id).eq('label', label).execute()
        return True
    except Exception as e:
        print(f"Error deleting location: {e}")
        raise

def delete_plan(date: str, user_id: str) -> bool:
    """Delete a plan for a specific date and user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        supabase.table('daily_plans').delete().eq('date', date).eq('user_id', user_id).execute()
        return True
    except Exception as e:
        print(f"Error deleting plan: {e}")
        raise
