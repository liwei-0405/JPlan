import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
import google.generativeai as genai
from typing import List, Dict, Any
import database

# load environment variables from .env file
load_dotenv()

# Gemini API
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

model = genai.GenerativeModel('gemini-2.5-flash')

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

class ChatResponse(BaseModel):
    reply: str
    schedule_data: dict | None = None

class PlanRequest(BaseModel):
    date: str
    activities: List[Dict[str, Any]]

class PlanResponse(BaseModel):
    date: str
    activities: List[Dict[str, Any]]

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
    print(f"Received message: {request.message}")
    
    try:

        context = ""
        if request.current_schedule: # If there's an existing schedule, include it in the context
            context = f"\nCURRENT_SCHEDULE: {json.dumps(request.current_schedule)}"

        full_prompt = f"{SYSTEM_PROMPT}\n{context}\n\nRespond with a valid JSON object only."

        for msg in request.history:
            role = "User" if msg["role"] == "user" else "Assistant"
            full_prompt += f"{role}: {msg['message']}\n"
            
        full_prompt += f"User: {request.message}\nAssistant: "
        print(f"Calling Gemini API with message: {full_prompt}")
        
        response = model.generate_content(
            full_prompt,
            generation_config={"response_mime_type": "application/json"}
        )
        
        # print raw response for debugging
        raw_text = response.text
        print(f"Gemini raw response: {raw_text}")
        
        # stronger JSON extraction
        try:
            # try to cleanly extract JSON block
            clean_text = raw_text.replace('```json', '').replace('```', '').strip()
            result = json.loads(clean_text)
        except json.JSONDecodeError:
            # find first and last braces to extract JSON
            start = raw_text.find('{')
            end = raw_text.rfind('}') + 1
            if start != -1 and end != 0:
                result = json.loads(raw_text[start:end])
            else:
                raise ValueError("No JSON found in response")
        
        # If AI generated a schedule, save it to database
        schedule_data = result.get("schedule_data")
        if schedule_data and schedule_data.get("date") and schedule_data.get("activities"):
            try:
                saved_plan = database.save_plan(
                    date=schedule_data["date"],
                    activities=schedule_data["activities"]
                )
                print(f"Auto-saved plan to database for date: {schedule_data['date']}")
            except Exception as db_error:
                print(f"Failed to auto-save plan: {db_error}")
                # Continue even if save fails
        
        return ChatResponse(
            reply=result.get("reply", "I've created a plan for you based on your request."),
            schedule_data=schedule_data
        )
        
    except Exception as e:
        import traceback
        print(f"Error occurred: {e}")
        traceback.print_exc() 
        return ChatResponse(
            reply=f"Sorry, I encountered an error: {str(e)}",
            schedule_data=None
        )


# ============================================
# Plan Management API Endpoints
# ============================================

@app.get("/api/plans")
async def get_all_plans():
    """Get all saved plans"""
    try:
        plans = database.get_all_plans()
        return plans
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/plans/{date}")
async def get_plan_by_date(date: str):
    """Get plan for a specific date (YYYY-MM-DD)"""
    try:
        plan = database.get_plan_by_date(date)
        if plan is None:
            raise HTTPException(status_code=404, detail=f"No plan found for date {date}")
        return plan
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/plans")
async def save_plan_endpoint(plan: PlanRequest):
    """Save or update a plan"""
    try:
        saved_plan = database.save_plan(
            date=plan.date,
            activities=plan.activities
        )
        return saved_plan
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/plans/{date}")
async def delete_plan_endpoint(date: str):
    """Delete a plan for a specific date"""
    try:
        success = database.delete_plan(date)
        return {"success": success, "message": f"Plan for {date} deleted successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
