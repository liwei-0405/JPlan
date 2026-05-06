"""
Database layer for Supabase operations
"""
import os
import json
import hashlib
from datetime import datetime, timezone, timedelta
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
        "schedule_status": "ok",
        "travel_validation_status": "not_requested",
        "planning_mode": "feasibility_first",
        "allow_clash": False,
        "accurate_travel_time": False,
        "version": 1,
        "preferences": {},
        "activities": [],
        "schedule_blocks": [],
        "explanations": [],
        "unscheduled_activities": [],
        "conflict": None,
        "conflicts": [],
        "warnings": [],
        "location_resolution_requests": [],
        "route_conflicts": [],
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
            "schedule_status": payload.get("schedule_status") or payload.get("status", "ok"),
            "travel_validation_status": payload.get("travel_validation_status", "not_requested"),
            "planning_mode": payload.get("planning_mode", "feasibility_first"),
            "allow_clash": bool(payload.get("allow_clash", False)),
            "accurate_travel_time": bool(payload.get("accurate_travel_time") or (payload.get("preferences") or {}).get("accurate_travel_time", False)),
            "version": payload.get("version", 1),
            "preferences": payload.get("preferences") or {},
            "activities": payload.get("activities") or payload.get("items") or [],
            "schedule_blocks": payload.get("schedule_blocks") or [],
            "explanations": payload.get("explanations") or [],
            "unscheduled_activities": payload.get("unscheduled_activities") or [],
            "conflict": payload.get("conflict"),
            "conflicts": payload.get("conflicts") or [],
            "warnings": payload.get("warnings") or [],
            "location_resolution_requests": payload.get("location_resolution_requests") or [],
            "route_conflicts": payload.get("route_conflicts") or [],
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
            "schedule_status": payload.get("schedule_status") or payload.get("status", "ok"),
            "travel_validation_status": payload.get("travel_validation_status", "not_requested"),
            "planning_mode": payload.get("planning_mode", "feasibility_first"),
            "allow_clash": bool(payload.get("allow_clash", False)),
            "accurate_travel_time": bool(payload.get("accurate_travel_time") or (payload.get("preferences") or {}).get("accurate_travel_time", False)),
            "version": payload.get("version", 1),
            "preferences": payload.get("preferences") or {},
            "activities": activities if isinstance(activities, list) else [],
            "schedule_blocks": payload.get("schedule_blocks") or [],
            "explanations": explanations if isinstance(explanations, list) else [],
            "unscheduled_activities": unscheduled if isinstance(unscheduled, list) else [],
            "conflict": payload.get("conflict"),
            "conflicts": payload.get("conflicts") or [],
            "warnings": payload.get("warnings") or [],
            "location_resolution_requests": payload.get("location_resolution_requests") or [],
            "route_conflicts": payload.get("route_conflicts") or [],
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
    schedule_status: Optional[str] = None,
    travel_validation_status: str = "not_requested",
    conflict: Optional[Dict[str, Any]] = None,
    planning_mode: str = "feasibility_first",
    allow_clash: bool = False,
    accurate_travel_time: bool = False,
    conflicts: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[Dict[str, Any]]] = None,
    location_resolution_requests: Optional[List[Dict[str, Any]]] = None,
    route_conflicts: Optional[List[Dict[str, Any]]] = None,
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
        'schedule_status': schedule_status or status,
        'travel_validation_status': travel_validation_status,
        'planning_mode': planning_mode,
        'allow_clash': allow_clash,
        'accurate_travel_time': accurate_travel_time,
        'version': version,
        'preferences': {
            **(preferences or {}),
            'allow_clash': allow_clash,
            'planning_mode': planning_mode,
            'accurate_travel_time': accurate_travel_time,
        },
        'activities': activities,
        'schedule_blocks': schedule_blocks or [],
        'explanations': explanations or [],
        'unscheduled_activities': unscheduled_activities or [],
        'conflict': conflict,
        'conflicts': conflicts or [],
        'warnings': warnings or [],
        'location_resolution_requests': location_resolution_requests or [],
        'route_conflicts': route_conflicts or [],
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

def add_user_location(
    user_id: str,
    label: str,
    address: str,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    display_name: Optional[str] = None,
    source: Optional[str] = None,
    confirmed_by_user: bool = True,
) -> Dict[str, Any]:
    """Add or update a saved location for a user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        payload = {
            'user_id': user_id,
            'label': label,
            'display_name': display_name or label,
            'address': address,
            'latitude': lat,
            'longitude': lng,
            'source': source or 'manual',
            'confirmed_by_user': confirmed_by_user,
            'updated_at': datetime.now(timezone.utc).isoformat()
        }
        try:
            response = supabase.table('user_locations').upsert(payload, on_conflict='user_id, label').execute()
        except Exception:
            # Backward compatibility for deployments whose user_locations table
            # does not yet have display_name/source/confirmed_by_user columns.
            legacy_payload = {
                'user_id': user_id,
                'label': label,
                'address': address,
                'latitude': lat,
                'longitude': lng,
                'updated_at': payload['updated_at'],
            }
            response = supabase.table('user_locations').upsert(legacy_payload, on_conflict='user_id, label').execute()
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

def _cache_query(normalized_query: str, provider: str, country_hint: Optional[str], category_hint: Optional[str]):
    query = (
        supabase.table('geocode_cache')
        .select('*')
        .eq('normalized_query', normalized_query)
        .eq('provider', provider)
        .limit(1)
    )
    if country_hint is None:
        query = query.is_('country_hint', 'null')
    else:
        query = query.eq('country_hint', country_hint)
    if category_hint is None:
        query = query.is_('category_hint', 'null')
    else:
        query = query.eq('category_hint', category_hint)
    return query

def _parse_timestamptz(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    return None

def get_geocode_cache(
    normalized_query: str,
    provider: str,
    country_hint: Optional[str] = None,
    category_hint: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Return cached geocoding candidates if Supabase cache is available."""
    if not supabase:
        return None
    try:
        response = _cache_query(normalized_query, provider, country_hint, category_hint).execute()
        row = (response.data or [None])[0]
        if not row:
            return None

        expires_at = _parse_timestamptz(row.get('expires_at'))
        if expires_at and expires_at < datetime.now(timezone.utc):
            return None

        try:
            supabase.table('geocode_cache').update({
                'hit_count': int(row.get('hit_count') or 0) + 1,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }).eq('id', row.get('id')).execute()
        except Exception as update_error:
            print(f"[JPLAN][DATABASE] Geocode cache hit_count update skipped: {update_error}")

        result = row.get('result_json')
        return result if isinstance(result, list) else []
    except Exception as e:
        print(f"[JPLAN][DATABASE] Geocode cache read skipped: {e}")
        return None

def save_geocode_cache(
    normalized_query: str,
    provider: str,
    result_json: List[Dict[str, Any]],
    country_hint: Optional[str] = None,
    category_hint: Optional[str] = None,
    ttl_days: Optional[int] = 30,
) -> bool:
    """Persist geocoding candidates when the optional geocode_cache table exists."""
    if not supabase:
        return False
    now = datetime.now(timezone.utc)
    expires_at = None
    if ttl_days is not None and ttl_days >= 0:
        expires_at = (now + timedelta(days=ttl_days)).isoformat()

    payload = {
        'normalized_query': normalized_query,
        'provider': provider,
        'country_hint': country_hint,
        'category_hint': category_hint,
        'result_json': result_json,
        'expires_at': expires_at,
        'updated_at': now.isoformat(),
    }
    try:
        existing = _cache_query(normalized_query, provider, country_hint, category_hint).execute()
        row = (existing.data or [None])[0]
        if row:
            supabase.table('geocode_cache').update(payload).eq('id', row.get('id')).execute()
        else:
            supabase.table('geocode_cache').insert(payload).execute()
        return True
    except Exception as e:
        print(f"[JPLAN][DATABASE] Geocode cache write skipped: {e}")
        return False

def delete_plan(date: str, user_id: str) -> bool:
    """Delete a plan for a specific date and user."""
    if not supabase: raise Exception("Supabase client not initialized.")
    try:
        supabase.table('daily_plans').delete().eq('date', date).eq('user_id', user_id).execute()
        return True
    except Exception as e:
        print(f"Error deleting plan: {e}")
        raise
