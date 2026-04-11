import os
import requests
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from supabase import Client

class CalendarService:
    def __init__(self, supabase_client: Client):
        self.supabase = supabase_client
        self.client_id = os.getenv("GOOGLE_CLIENT_ID")
        self.client_secret = os.getenv("GOOGLE_CLIENT_SECRET")

    def list_calendars(self, access_token: str) -> List[Dict[str, Any]]:
        """List all calendars available to the user"""
        url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            return response.json().get("items", [])
        except Exception as e:
            print(f"Error listing calendars: {e}")
            return []

    def refresh_access_token(self, refresh_token: str) -> Optional[str]:
        """
        Exchange a refresh token for a new access token
        """
        if not self.client_id or not self.client_secret:
            print("Error: GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET not set")
            return None

        url = "https://oauth2.googleapis.com/token"
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }

        try:
            response = requests.post(url, data=data)
            response.raise_for_status()
            return response.json().get("access_token")
        except Exception as e:
            print(f"Error refreshing access token: {e}")
            if hasattr(e, 'response') and getattr(e.response, 'status_code', None) == 400:
                raise Exception("TOKEN_EXPIRED")
            return None

    def get_calendar_events(self, access_token: str, time_min: str, time_max: str) -> List[Dict[str, Any]]:
        """
        Fetch events from Google Calendar
        """
        url = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
        }

        try:
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.json().get("items", [])
        except Exception as e:
            print(f"Error fetching calendar events: {e}")
            return []

    def sync_user_calendar(self, user_id: str, date_str: str) -> List[Dict[str, Any]]:
        """
        Main entry point to sync a user's calendar for a specific day
        """
        # 1. Get refresh token from DB
        try:
            # Use execute() instead of single() to handle "0 rows" gracefully
            res = self.supabase.table("profiles").select("google_refresh_token").eq("id", user_id).execute()
            
            if not res.data or len(res.data) == 0:
                print(f"[ERROR] No profile found for user {user_id}")
                return []
                
            refresh_token = res.data[0].get("google_refresh_token")
            
            if not refresh_token:
                print(f"[DEBUG] Profile found but no refresh token for user {user_id}")
                return []

            # 2. Refresh access token
            access_token = self.refresh_access_token(refresh_token)
            print(f"[DEBUG] Access token obtained: {'Yes' if access_token else 'No'}")
            if not access_token:
                print("[DEBUG] Failed to get access token")
                return []

            # 3. Define time range for the date (fetching with a buffer to handle timezones)
            # Fetching from 12 hours before to 12 hours after the date to be safe
            time_min = f"{date_str}T00:00:00Z"
            time_max = f"{date_str}T23:59:59Z"
            
            # IMPROVEMENT: Fetch a 48-hour window surrounding the target date and filter locally
            # This is more robust against timezone shifts
            fetch_min = f"{date_str}T00:00:00Z" # We'll still use this but backend will filter
            
            # Actually, let's just fetch everything for the given "day" at UTC 
            # and let the frontend/backend logic be a bit more flexible.
            # Most users in Asia/Europe will be missed by a strict "T00:00:00Z" if their event is early.
            
            # Dynamic calculation to cover the full local day regardless of timezone (-12 to +14)
            time_min = f"{date_str}T00:00:00Z" # Starting from UTC start
            # To be safe for users in GMT+8 (like user), subtract 12 hours
            from datetime import timedelta
            target_date = datetime.fromisoformat(date_str)
            start_utc = (target_date - timedelta(hours=14)).isoformat() + "Z"
            end_utc = (target_date + timedelta(hours=38)).isoformat() + "Z"
            
            print(f"[DEBUG] Fetching events from {start_utc} to {end_utc}")

            # 4. Fetch events
            # DEBUG: List all calendars first to see if primary is correct
            calendars = self.list_calendars(access_token)
            print(f"[DEBUG] Available calendars: {[c.get('summary') for c in calendars]}")
            
            events = self.get_calendar_events(access_token, start_utc, end_utc)
            print(f"[DEBUG] Raw events fetched from primary: {len(events)}")

            # 5. Filter events that actually fall into the target date (local time)
            formatted_activities = []
            for event in events:
                start_obj = event.get("start", {})
                end_obj = event.get("end", {})
                
                start_raw = start_obj.get("dateTime") or start_obj.get("date")
                end_raw = end_obj.get("dateTime") or end_obj.get("date")
                summary = event.get("summary", "Untitled Event")
                
                print(f"[DEBUG] Evaluating event: '{summary}' | Start: {start_raw}")
                
                event_date = start_raw.split("T")[0] if "T" in start_raw else start_raw
                
                if event_date != date_str:
                    print(f"[DEBUG] Skipping: event_date {event_date} != target {date_str}")
                    continue
                
                print(f"[DEBUG] Including: {summary}")
                
                if "T" in start_raw:
                    try:
                        dt_start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                        start_time = dt_start.strftime("%I:%M %p")
                        
                        dt_end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        end_time = dt_end.strftime("%I:%M %p")
                    except Exception:
                        start_time = start_raw[11:16]
                        end_time = end_raw[11:16]
                else:
                    start_time = "All Day"
                    end_time = "All Day"

                formatted_activities.append({
                    "id": event.get("id"),
                    "startTime": start_time,
                    "endTime": end_time,
                    "activity": summary,
                    "category": "External",
                    "source": "google_calendar"
                })

            return formatted_activities

        except Exception as e:
            print(f"Sync failed for user {user_id}: {e}")
            return []
    def sync_upcoming_events(self, user_id: str, days_ahead: int = 60) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch all events from now to X days in the future and group by date
        """
        try:
            # 1. Get refresh token
            res = self.supabase.table("profiles").select("google_refresh_token").eq("id", user_id).execute()
            if not res.data or len(res.data) == 0:
                print(f"[ERROR] No profile found for user {user_id}")
                return {}
            
            refresh_token = res.data[0].get("google_refresh_token")
            if not refresh_token:
                return {}

            # 2. Get access token
            try:
                access_token = self.refresh_access_token(refresh_token)
                if not access_token:
                    raise Exception("TOKEN_EXPIRED")
            except Exception as e:
                if str(e) == "TOKEN_EXPIRED":
                    raise e
                return {}

            # 3. Time range
            now = datetime.utcnow()
            time_min = now.isoformat() + "Z"
            time_max = (now + timedelta(days=days_ahead)).isoformat() + "Z"
            
            print(f"[DEBUG] Fetching future events from {time_min} to {time_max}")

            # 4. Fetch
            events = self.get_calendar_events(access_token, time_min, time_max)
            print(f"[DEBUG] Total future events fetched: {len(events)}")

            # 5. Group and format
            grouped_data = {}
            for event in events:
                start_obj = event.get("start", {})
                end_obj = event.get("end", {})
                
                start_raw = start_obj.get("dateTime") or start_obj.get("date")
                end_raw = end_obj.get("dateTime") or end_obj.get("date")
                summary = event.get("summary", "Untitled Event")
                
                # Extract date "YYYY-MM-DD"
                event_date = start_raw.split("T")[0] if "T" in start_raw else start_raw
                
                if event_date not in grouped_data:
                    grouped_data[event_date] = []
                
                # Format times
                if "T" in start_raw:
                    try:
                        dt_start = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                        start_time = dt_start.strftime("%I:%M %p")
                        
                        dt_end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
                        end_time = dt_end.strftime("%I:%M %p")
                    except Exception:
                        start_time = start_raw[11:16]
                        end_time = end_raw[11:16]
                else:
                    start_time = "All Day"
                    end_time = "All Day"

                grouped_data[event_date].append({
                    "id": event.get("id"),
                    "startTime": start_time,
                    "endTime": end_time,
                    "activity": summary,
                    "category": "External",
                    "source": "google_calendar"
                })

            return grouped_data

        except Exception as e:
            if str(e) == "TOKEN_EXPIRED":
                raise e
            print(f"Bulk sync failed for user {user_id}: {e}")
            return {}
