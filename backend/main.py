import os
import json
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from typing import List, Dict, Any, Optional
import database
from scheduling_engine import SchedulingEngine, VersionMismatchError

# load environment variables from .env file
load_dotenv()

# Gemini API
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
scheduling_engine = SchedulingEngine(client)

# Calendar Service (Supabase initialed in database module)
import calendar_service
cal_service = calendar_service.CalendarService(database.supabase)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Models ---

class ScheduleItem(BaseModel):
    id: Optional[str] = None
    type: Optional[str] = "activity"
    block_type: Optional[str] = None # V3 support
    title: str
    startTime: Optional[str] = None  # Frontend compat
    endTime: Optional[str] = None    # Frontend compat
    start: Optional[str] = None      # Backend V3 compat
    end: Optional[str] = None        # Backend V3 compat
    location: Optional[str] = None
    duration: Optional[str] = None
    duration_minutes: Optional[int] = None
    priority: Optional[str] = "medium"
    isMandatory: Optional[bool] = True
    notes: Optional[str] = None
    explanation: Optional[str] = None
    trace: Optional[List[str]] = []
    source: Optional[str] = None
    isConflict: Optional[bool] = False
    conflictWith: Optional[List[str]] = []
    conflictReason: Optional[str] = None
    conflictPriority: Optional[str] = None
    conflictSeverity: Optional[str] = None

class UnscheduledActivity(BaseModel):
    title: str
    reason: str
    priority: Optional[str] = "medium"
    isMandatory: Optional[bool] = True

class ScheduleEnvelope(BaseModel):
    scheduleId: Optional[str] = None
    date: str
    version: Optional[int] = 1
    schema_version: Optional[int] = 3
    activities: List[ScheduleItem]
    schedule_blocks: Optional[List[ScheduleItem]] = [] # Explicit timeline
    explanations: List[str] = []
    unscheduled_activities: List[UnscheduledActivity] = []

class SchedulePatchOperation(BaseModel):
    op: str # add, update, remove, move, replace, update_priority
    target_id: Optional[str] = None
    title: Optional[str] = None
    startTime: Optional[str] = None
    endTime: Optional[str] = None
    duration_minutes: Optional[int] = None
    location: Optional[str] = None
    priority: Optional[str] = None
    is_mandatory: Optional[bool] = None
    notes: Optional[str] = None

class SchedulePatchRequest(BaseModel):
    baseVersion: int
    operations: List[SchedulePatchOperation]
    user_id: str

class SchedulePatchResponse(BaseModel):
    scheduleId: Optional[str] = None
    version: int
    applied: bool
    updatedActivities: List[ScheduleItem]
    deletedItemIds: List[str]
    affectedRange: Optional[Dict[str, str]] = None 
    explanation: Optional[str] = None

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = [] 
    current_schedule: Optional[ScheduleEnvelope] = None
    user_id: str | None = None

class ChatResponse(BaseModel):
    reply: str
    patch: Optional[SchedulePatchResponse] = None
    full_schedule: Optional[ScheduleEnvelope] = None 
    schedule_data: Optional[ScheduleEnvelope] = None # Compatibility with legacy frontend
    transcription: str | None = None

class PlanRequest(BaseModel):
    date: str
    activities: List[ScheduleItem]
    explanations: List[str] = []
    unscheduled_activities: List[UnscheduledActivity] = []
    user_id: str
    version: int = 1
    scheduleId: Optional[str] = None

class ExportRequest(BaseModel):
    user_id: str
    date: str


def debug_log(message: str) -> None:
    print(f"[JPLAN][API] {message}")


def debug_json(label: str, payload: Any) -> None:
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=True)
    except Exception:
        serialized = repr(payload)
    print(f"[JPLAN][API] {label}:\n{serialized}")


def summarize_envelope(envelope: ScheduleEnvelope | dict | None) -> dict:
    if not envelope: return {}
    if isinstance(envelope, ScheduleEnvelope):
        activities = envelope.activities
        unscheduled = envelope.unscheduled_activities
    else:
        activities = envelope.get("activities") or envelope.get("items") or []
        unscheduled = envelope.get("unscheduled_activities") or []
    
    activity_count = sum(1 for item in activities if (isinstance(item, ScheduleItem) and item.type == "activity") or (isinstance(item, dict) and item.get("type") == "activity"))
    return {
        "scheduleId": envelope.scheduleId if isinstance(envelope, ScheduleEnvelope) else envelope.get("scheduleId"),
        "version": envelope.version if isinstance(envelope, ScheduleEnvelope) else envelope.get("version"),
        "activity_count": activity_count,
        "unscheduled_count": len(unscheduled),
    }

@app.post("/chat", response_model=ChatResponse)
async def chat_with_llm(request: ChatRequest):
    debug_log(f"Received chat | user={request.user_id} | message={request.message!r}")
    
    try:
        # Full envelope for internal processing
        current_envelope = request.current_schedule.model_dump() if request.current_schedule else None

        # Optimization: Create a lightweight context for the LLM
        lightweight_schedule = None
        if request.current_schedule:
            clean_activities = []
            for act in request.current_schedule.activities:
                if act.type != "activity": continue
                clean_activities.append({
                    "id": act.id,
                    "title": act.title,
                    "startTime": act.startTime,
                    "endTime": act.endTime
                })
            lightweight_schedule = {
                "date": request.current_schedule.date,
                "activities": clean_activities,
                "version": request.current_schedule.version
            }

        # Fetch saved locations for the user to provide context to LLM
        saved_locations = database.get_user_locations(request.user_id)

        parsed = scheduling_engine.parse_text_request(
            message=request.message,
            history=request.history,
            current_schedule=lightweight_schedule,
            saved_locations=saved_locations
        )
        
        intent = parsed.get("intent", "chat")
        reply = parsed.get("reply", "I've processed your request.")
        
        if intent == "chat" and not parsed.get("operations") and not parsed.get("activities"):
            return ChatResponse(reply=reply, transcription=parsed.get("transcription"))

        if request.current_schedule and (parsed.get("operations") or parsed.get("activities")):
            try:
                ops = parsed.get("operations") or []
                if not ops and parsed.get("activities"):
                    for act in parsed.get("activities"):
                        ops.append({**act, "op": "add"})
                
                patch_result = scheduling_engine.apply_operations(
                    envelope=current_envelope,
                    operations=ops,
                    base_version=request.current_schedule.version,
                    new_date=parsed.get("date")
                )
                
                final_reply = reply
                if patch_result.get("advisor_suggestion"):
                    final_reply = f"{reply}\n\n💡 {patch_result['advisor_suggestion']}"

                return ChatResponse(
                    reply=final_reply,
                    patch=SchedulePatchResponse(
                        scheduleId=patch_result["envelope"].get("scheduleId") or "temp-id",
                        version=patch_result["version"],
                        applied=True,
                        updatedActivities=patch_result["updatedActivities"],
                        deletedItemIds=patch_result["deletedItemIds"],
                        explanation=final_reply
                    ),
                    schedule_data=patch_result["envelope"],
                    transcription=parsed.get("transcription")
                )
            except VersionMismatchError:
                debug_log("Version mismatch during patch, fallback to full schedule.")

        result = scheduling_engine.build_schedule_response(
            parsed=parsed,
            current_schedule=current_envelope,
            latest_request=request.message,
        )
        
        schedule_dict = result.get("schedule_data")
        if schedule_dict:
            full_envelope_dict = database._parse_schedule_payload(schedule_dict, request.user_id, schedule_dict.get("date"))
            full_envelope = ScheduleEnvelope(**full_envelope_dict)
            return ChatResponse(
                reply=result.get("reply", reply),
                full_schedule=full_envelope,
                schedule_data=full_envelope,
                transcription=result.get("transcription")
            )
        
        return ChatResponse(
            reply=result.get("reply", reply), 
            schedule_data=None,
            transcription=result.get("transcription")
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc() 
        return ChatResponse(reply=f"Sorry, I encountered an error: {str(e)}")

@app.post("/api/schedules/{scheduleId}/operations", response_model=SchedulePatchResponse)
async def apply_operations_endpoint(scheduleId: str, request: SchedulePatchRequest):
    """Apply a batch of operations to a schedule."""
    try:
        plans = database.get_all_plans(request.user_id)
        current_plan = next((p for p in plans if p.get("scheduleId") == scheduleId), None)
        
        if not current_plan:
            raise HTTPException(status_code=404, detail="Schedule not found")
        
        ops = [op.model_dump() for op in request.operations]
        result = scheduling_engine.apply_operations(
            envelope=current_plan,
            operations=ops,
            base_version=request.baseVersion
        )
        
        updated_envelope = result["envelope"]
        database.save_plan(
            date=updated_envelope["date"],
            activities=updated_envelope["activities"],
            user_id=request.user_id,
            explanations=updated_envelope["explanations"],
            unscheduled_activities=updated_envelope["unscheduled_activities"],
            version=updated_envelope["version"],
            schedule_id=updated_envelope["scheduleId"]
        )
        
        return SchedulePatchResponse(
            scheduleId=scheduleId,
            version=updated_envelope["version"],
            applied=True,
            updatedActivities=result["updatedActivities"],
            deletedItemIds=result["deletedItemIds"]
        )
        
    except VersionMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plans", response_model=List[ScheduleEnvelope])
async def get_all_plans(user_id: str):
    """Get all saved plans for a user as envelopes"""
    try:
        plans = database.get_all_plans(user_id)
        results = []
        for p in plans:
            # Normalize database data to Envelope
            p["activities"] = p.get("activities") or p.get("items") or []
            p["unscheduled_activities"] = p.get("unscheduled_activities") or []
            results.append(ScheduleEnvelope(**p))
        return results
    except Exception as e:
        print(f"[JPLAN][API] Error in get_all_plans: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/plans/{date}", response_model=ScheduleEnvelope)
async def get_plan_by_date(date: str, user_id: str):
    """Get plan for a specific date and user as envelope"""
    try:
        plan = database.get_plan_by_date(date, user_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"No plan found for {date}")
        
        # Normalize database data to Envelope
        plan["activities"] = plan.get("activities") or plan.get("items") or []
        plan["unscheduled_activities"] = plan.get("unscheduled_activities") or []
        plan["schedule_blocks"] = plan.get("schedule_blocks") or []
        
        return ScheduleEnvelope(**plan)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[JPLAN][API] Error in get_plan_by_date: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/plans", response_model=ScheduleEnvelope)
async def save_plan_endpoint(plan: PlanRequest):
    """Save or update a plan (Full-Save compatibility)"""
    try:
        saved_plan = database.save_plan(
            date=plan.date,
            activities=[act.model_dump() for act in plan.activities],
            user_id=plan.user_id,
            explanations=plan.explanations,
            unscheduled_activities=[act.model_dump() for act in plan.unscheduled_activities],
            version=plan.version,
            schedule_id=plan.scheduleId
        )
        return ScheduleEnvelope(**saved_plan)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/sync-calendar")
async def sync_calendar(request: Dict[str, Any]):
    user_id = request.get("user_id")
    date = request.get("date") # Optional target date
    
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    
    try:
        # 1. Fetch upcoming from Google (next 60 days)
        grouped_events = cal_service.sync_upcoming_events(user_id)
        
        if not grouped_events:
            return {"events": [], "message": "No upcoming events found or sync failed"}

        all_synced_days = []
        target_date_events = []

        # 2. Process each date
        for event_date, google_events in grouped_events.items():
            # Get existing plan (envelope)
            existing_plan = database.get_plan_by_date(event_date, user_id)
            
            # Map to JPlan format
            new_activities = []
            for ge in google_events:
                new_activities.append({
                    "id": ge.get("id"),
                    "type": "activity",
                    "title": ge.get("activity"),
                    "startTime": ge.get("startTime"),
                    "endTime": ge.get("endTime"),
                    "category": "External",
                    "source": "google_calendar"
                })

            final_activities = []
            if existing_plan:
                # Envelope uses 'activities'
                existing_activities = existing_plan.get("activities", [])
                
                # Build fingerprints for existing activities: Title|Start|End
                def get_fingerprint(a):
                    title = str(a.get("title", "")).strip().lower()
                    start = str(a.get("startTime", "")).strip().lower()
                    end = str(a.get("endTime", "")).strip().lower()
                    
                    if start.startswith("0"): start = start[1:]
                    if end.startswith("0"): end = end[1:]
                    
                    return f"{title}|{start}|{end}"
                
                existing_ids = {a.get("id") for a in existing_activities if a.get("id")}
                existing_fingerprints = {get_fingerprint(a) for a in existing_activities}
                
                merged = list(existing_activities)
                for na in new_activities:
                    if na.get("id") not in existing_ids and get_fingerprint(na) not in existing_fingerprints:
                        merged.append(na)
                final_activities = merged
            else:
                final_activities = new_activities

            # Save to DB
            database.save_plan(event_date, final_activities, user_id)
            all_synced_days.append(event_date)
            
            if event_date == date:
                target_date_events = final_activities

        return {
            "synced_days": all_synced_days,
            "message": f"Successfully synced events for {len(all_synced_days)} days",
            "events": target_date_events if date else []
        }
        
    except Exception as e:
        error_msg = str(e)
        print(f"Sync error: {error_msg}")
        if "TOKEN_EXPIRED" in error_msg:
            try:
                database.supabase.table('profiles').update({
                    'google_refresh_token': None, 
                    'calendar_sync_enabled': False
                }).eq('id', user_id).execute()
            except Exception as db_err:
                print(f"Failed to clear token: {db_err}")
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
        raise HTTPException(status_code=500, detail=error_msg)


@app.post("/api/export-calendar")
async def export_calendar(request: ExportRequest):
    if not request.user_id or not request.date:
        raise HTTPException(status_code=400, detail="user_id and date are required")
    
    try:
        # Get existing plan (envelope)
        plan = database.get_plan_by_date(request.date, request.user_id)
        if not plan or not plan.get("activities"):
            raise HTTPException(status_code=404, detail="No activities found for this date")
            
        count = cal_service.export_schedule_to_google(request.user_id, request.date, plan.get("activities"))
            
        return {"message": "Success", "exported_count": count}
    except Exception as e:
        error_msg = str(e)
        print(f"Export error: {error_msg}")
        if "TOKEN_EXPIRED" in error_msg:
            try:
                database.supabase.table('profiles').update({
                    'google_refresh_token': None, 
                    'calendar_sync_enabled': False
                }).eq('id', request.user_id).execute()
            except Exception as db_err:
                print(f"Failed to clear token: {db_err}")
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
        
        if "403" in error_msg or "insufficientPermissions" in error_msg:
            raise HTTPException(status_code=401, detail="TOKEN_EXPIRED")
            
        raise HTTPException(status_code=500, detail=error_msg)

# --- Location Management ---

@app.get("/api/locations")
async def get_locations(user_id: str):
    return database.get_user_locations(user_id)

@app.post("/api/locations")
async def add_location(user_id: str, label: str, address: str, lat: float = None, lng: float = None):
    return database.add_user_location(user_id, label, address, lat, lng)

@app.delete("/api/locations")
async def delete_location(user_id: str, label: str):
    return {"success": database.delete_user_location(user_id, label)}
