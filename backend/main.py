import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from google import genai
from typing import List, Dict, Any
import database

# load environment variables from .env file
load_dotenv()

# Gemini API
client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))

# Calendar Service (Supabase initialed in database module)
import calendar_service
cal_service = calendar_service.CalendarService(database.supabase)

app = FastAPI()

# CORS
origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = [] 
    current_schedule: dict | None = None
    user_id: str | None = None

class ChatResponse(BaseModel):
    reply: str
    schedule_data: dict | None = None

class PlanRequest(BaseModel):
    date: str
    activities: List[Dict[str, Any]]
    user_id: str

SYSTEM_PROMPT = """
You are a daily scheduling assistant for an app called JPlan. 
Your goal is to parse user requests into a structured daily schedule.

When the user asks to plan activities, you should respond with a single JSON object containing both a friendly reply and the schedule data.

The JSON should follow this structure:
{
  "reply": "A friendly text reply to the user, summarizing what you planned.",
  "schedule_data": {
    "date": "YYYY-MM-DD",
    "activities": [
    {
      "id": "unique_string",
      "type": "activity" or "travel",
      "title": "Activity name",
      "startTime": "HH:MM AM/PM",
      "endTime": "HH:MM AM/PM",
      "duration": "e.g., 1 hour",
      "location": "optional location",
      "priority": "low" | "medium" | "high",
      "isMandatory": boolean
    }
  ]
}

Rules:
- Date MUST be in YYYY-MM-DD format.
- If times are not specified, estimate reasonable ones based on typical daily routines.
- Include travel blocks if locations are specified and require moving between them.
- If the user's request is a greeting or general question, you can return null for schedule_data.
- ALWAYS respond with a valid JSON block for the schedule part if planning is involved.
"""

@app.get("/")
async def read_root():
    return {"message": "Welcome to the JPlan Backend!"}

@app.post("/chat")
async def chat_with_llm(request: ChatRequest):
    print(f"Received message: {request.message} from user: {request.user_id}")
    
    try:
        context = ""
        if request.current_schedule:
            context = f"\nCURRENT_SCHEDULE: {json.dumps(request.current_schedule)}"

        full_prompt = f"{SYSTEM_PROMPT}\n{context}\n\nRespond with a valid JSON object only."

        for msg in request.history:
            role = "User" if msg["role"] == "user" else "Assistant"
            full_prompt += f"{role}: {msg['message']}\n"
            
        full_prompt += f"User: {request.message}\nAssistant: "
        
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=full_prompt,
            config={"response_mime_type": "application/json"}
        )
        
        raw_text = response.text
        
        try:
            clean_text = raw_text.replace('```json', '').replace('```', '').strip()
            result = json.loads(clean_text)
        except json.JSONDecodeError:
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1
            if start != -1 and end != 0:
                result = json.loads(raw_text[start:end])
            else:
                raise ValueError("No JSON found in response")
        
        # If AI generated a schedule and we have a user_id, save it
        schedule_data = result.get("schedule_data")
        if schedule_data and schedule_data.get("date") and schedule_data.get("activities") and request.user_id:
            try:
                database.save_plan(
                    date=schedule_data["date"],
                    activities=schedule_data["activities"],
                    user_id=request.user_id
                )
                print(f"Auto-saved plan for user: {request.user_id}")
            except Exception as db_error:
                print(f"Failed to auto-save plan: {db_error}")
        
        return ChatResponse(
            reply=result.get("reply", "I've created a plan for you based on your request."),
            schedule_data=schedule_data
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc() 
        return ChatResponse(
            reply=f"Sorry, I encountered an error: {str(e)}",
            schedule_data=None
        )


@app.get("/api/plans")
async def get_all_plans(user_id: str):
    """Get all saved plans for a user"""
    try:
        plans = database.get_all_plans(user_id)
        return plans
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/plans/{date}")
async def get_plan_by_date(date: str, user_id: str):
    """Get plan for a specific date and user"""
    try:
        plan = database.get_plan_by_date(date, user_id)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"No plan found")
        return plan
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/plans")
async def save_plan_endpoint(plan: PlanRequest):
    """Save or update a plan"""
    try:
        # NOTE: Here is where you would call Google Maps API and run your algorithms
        # before saving to the database.
        saved_plan = database.save_plan(
            date=plan.date,
            activities=plan.activities,
            user_id=plan.user_id
        )
        return saved_plan
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/plans/{date}")
async def delete_plan_endpoint(date: str, user_id: str):
    """Delete a plan for a specific date and user"""
    try:
        success = database.delete_plan(date, user_id)
        return {"success": success}
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
        # This fulfills the user's request: "同步未来的资料全部进来的意思"
        grouped_events = cal_service.sync_upcoming_events(user_id)
        
        if not grouped_events:
            return {"events": [], "message": "No upcoming events found or sync failed"}

        all_synced_days = []
        target_date_events = []

        # 2. Process each date
        for event_date, google_events in grouped_events.items():
            # Get existing plan
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
                existing_activities = existing_plan.get("activities", [])
                
                # If existing_activities is accidentally a string, parse it
                if isinstance(existing_activities, str):
                    try:
                        existing_activities = json.loads(existing_activities)
                    except:
                        existing_activities = []
                
                existing_ids = {a.get("id") for a in existing_activities if a.get("id")}
                
                merged = list(existing_activities)
                for na in new_activities:
                    # Avoid duplicates by ID
                    if na.get("id") not in existing_ids:
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


