# JPlan Daily Planning Interface

JPlan is a modern daily planning interface designed to help users manage their schedules efficiently. Originally developed as a Final Year Project (FYP), it features a sleek UI with integration for AI-powered planning and Supabase for data management.

## 🚀 Features

- **Modern UI**: Built with React and Tailwind CSS for a responsive, premium experience.
- **AI-Powered Planning**: Integration with Google Gemini AI for intelligent scheduling assistance.
- **Real-time Sync**: Uses Supabase for robust and fast data persistence.
- **FastAPI Backend**: A high-performance Python backend for handling logic and API integrations.
- **Accurate Travel Time Mode**: Optional OpenRouteService-based route validation with confirmed saved locations.

## 🛠️ Tech Stack

- **Frontend**: React, Vite, Tailwind CSS, Lucide React, Radix UI.
- **Backend**: FastAPI, Pydantic, Uvicorn, Python.
- **Database/Auth**: Supabase (PostgreSQL).
- **AI**: Google GenAI SDK (Gemini).
- **Travel Routing**: OpenRouteService (optional, only when Accurate Travel Time is enabled).

---

## 💻 Getting Started

### Prerequisites

- **Node.js**: Version 18.x or later.
- **Python**: Version 3.10 or later.
- **Supabase Account**: A project set up on Supabase.
- **Google AI API Key**: For Gemini AI features.
- **OpenRouteService API Key**: Optional, only required for Accurate Travel Time routing/geocoding.

### 1. Clone the Repository

```bash
git clone https://github.com/liwei-0405/JPlan.git
cd JPlan
```

### 2. Frontend Setup

```bash
# Install dependencies
npm install

# Setup frontend environment variables
cp .env.example .env
# Edit .env and fill in the "Frontend - Vite" section
```

### 3. Database & Auth Setup (Supabase)

Before running the app, go to your **Supabase Dashboard**:
1. Run the contents of `supabase_setup.sql` in the **SQL Editor**. 
2. Go to **Authentication** -> **Providers** -> **Email** and disable **Confirm email**.
3. **Google Auth & Calendar Setup**:
   - Go to **Authentication** -> **Providers** -> **Google**.
   - Enable Google provider.
   - Enter your **Client ID** and **Client Secret** (from Google Cloud Console).
   - In **Additional Scopes**, add: `https://www.googleapis.com/auth/calendar.readonly`.
   - (Optional but recommended) Enable **Skip nonce check**.

### 3.1 Google Cloud Console Setup

1. Enable **Google Calendar API**.
2. Configure **OAuth consent screen**:
   - Add `.../auth/calendar.readonly` to **Scopes**.
   - Add your test email address to **Test users** (important while in Testing mode).
3. Create **OAuth 2.0 Client ID** (Web application type).
   - Add Supabase Redirect URI to **Authorized redirect URIs**.

### 4. Backend Setup

```bash
cd backend

# Create a virtual environment
python -m venv venv

# Activate virtual environment
# On Windows:
.\venv\Scripts\activate
# On macOS/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Setup backend environment variables
cp .env.example .env
# Edit .env and fill in the backend, Google AI, Supabase, and optional ORS sections
```

### 4.0 AI Model Config

JPlan uses one shared Gemini model setting for Module A parsing, advisory replies, and Module 8 final replies. Add this to `backend/.env` if you want to change the model without editing code:

```env
GOOGLE_API_KEY=your_google_ai_api_key_here
JPLAN_GEMINI_MODEL=gemini-3.1-flash-lite
```

Optional per-module overrides are also supported when testing:

```env
MODULE_A_LLM_MODEL=gemini-3.1-flash-lite
MODULE8_LLM_MODEL=gemini-3.1-flash-lite
ADVISORY_LLM_MODEL=gemini-3.1-flash-lite
```

### 4.1 Accurate Travel Time Setup (Optional)

JPlan works without OpenRouteService by using the built-in heuristic travel estimates. When **Accurate Travel Time** is enabled, the backend first builds a fast draft schedule, then validates travel using confirmed coordinates and OpenRouteService routing.

Add these values to `backend/.env` when using location search or accurate routing:

```env
OPENROUTESERVICE_API_KEY=your_openrouteservice_api_key_here
JPLAN_NOMINATIM_USER_AGENT=JPlan-FYP/1.0 (location search; contact: your-email@example.com)
ENABLE_NOMINATIM_FALLBACK=true
NOMINATIM_MIN_INTERVAL_SECONDS=1.0
GEOCODE_CACHE_TTL_DAYS=30
```

Location resolution follows this order:

1. Reuse confirmed saved locations from Supabase.
2. Expand common aliases such as `MMU`, `campus`, `school`, `Main Office`, and `gym`.
3. Search OpenRouteService geocoding first.
4. Use Nominatim/OpenStreetMap only as a low-volume fallback when ORS results are missing or clearly unrelated.
5. Let the user confirm a candidate, or use the manual map pin fallback when search is still wrong.

When Accurate Travel Time is off, JPlan keeps using the fast heuristic travel-time matrix. When it is on, missing or ambiguous coordinates create a `location_pending` draft until the user confirms locations. ORS route validation runs after draft construction; if ORS is unavailable but coordinates already exist, JPlan falls back to heuristic travel estimates with a warning instead of silently claiming accurate routing.

Public Nominatim usage is intentionally limited for FYP/beta testing: it is never used for autocomplete, only runs after an explicit search/resolve action, is throttled to at least one request per second per backend process, and repeated queries are cached in Supabase `geocode_cache` when available. Search/map UI should show OpenStreetMap attribution, for example: `Search results © OpenStreetMap contributors`.

For a small beta of around 20 users, this setup is acceptable because Nominatim is fallback-only, user-triggered, throttled, and cached. For production/global scale, replace public Nominatim with a paid geocoder or a self-hosted geocoder. Route cache is currently in-memory only; move it to Supabase/Redis before larger public release.

### 4.2 Module D Implementation Status

Module D v1 is enabled as a safe deterministic refinement pass after initial schedule construction. It runs for complex initial generation and explicit optimize/regenerate requests, but skips simple edits so normal fast-path changes preserve the current schedule behavior.

Optional backend tuning:

```env
JPLAN_ENABLE_MODULE_D=true
MODULE_D_MAX_ITERATIONS=30
MODULE_D_TIME_BUDGET_MS=500
MODULE_D_MIN_IMPROVEMENT=0.01
```

Implemented in V1: deterministic bounded local refinement, safe run policy, feasible candidate relocation, optional unscheduled insertion, heuristic/cached travel scoring, fixed-event preservation, dependency-order preservation, and refinement metadata/logs.

Not implemented yet / future full ANSA: stochastic simulated annealing, temperature/cooling, adaptive neighborhood probabilities, full swap/insert/relocate/replace move set, replacement candidate pool, ILS perturbation, SPM-IR preference mining integration, route-service calls inside refinement, global optimality, and long-run optimization mode.

Module D v1 is an ANSA-style deterministic refinement subset. It should not be described as full ANSA until temperature-based probabilistic acceptance, adaptive move weighting, and the complete neighborhood set are implemented.

---

## 🏃 Running the Application

### Start the Backend

Make sure your virtual environment is activated:

```bash
cd backend
uvicorn main:app --reload
```
The backend typically runs on `http://localhost:8000`.

### Start the Frontend

Open a new terminal:

```bash
npm run dev
```
The frontend will typically run on `http://localhost:3000`.

---

## 🔒 Security Best Practices

> [!IMPORTANT]
> **Never commit your `.env` files.** They contain sensitive API keys and database credentials. This project is configured with a `.gitignore` to prevent these files from being uploaded.

- **Private Repository**: If you are using this for FYP, it is recommended to keep your repository **Private** until you are ready to reveal it.
- **Rotation**: If you ever accidentally commit a secret, rotate (change) your API keys immediately.
- **RLS**: Ensure Supabase Row Level Security (RLS) is enabled for data protection.

## 📄 License

This project is for educational purposes (Final Year Project).

---

Developed by Teh Li Wei (liwei-0405) Nickname: Levi
