# JPlan Daily Planning Interface

JPlan is a modern daily planning interface designed to help users manage their schedules efficiently. Originally developed as a Final Year Project (FYP), it features a sleek UI with integration for AI-powered planning and Supabase for data management.

## 🚀 Features

- **Modern UI**: Built with React and Tailwind CSS for a responsive, premium experience.
- **AI-Powered Planning**: Integration with Google Gemini AI for intelligent scheduling assistance.
- **Real-time Sync**: Uses Supabase for robust and fast data persistence.
- **FastAPI Backend**: A high-performance Python backend for handling logic and API integrations.

## 🛠️ Tech Stack

- **Frontend**: React, Vite, Tailwind CSS, Lucide React, Radix UI.
- **Backend**: FastAPI, Pydantic, Uvicorn.
- **Database/Auth**: Supabase.
- **AI**: Google Generative AI (Gemini).

---

## 💻 Getting Started

### Prerequisites

- **Node.js**: Version 18.x or later.
- **Python**: Version 3.10 or later.
- **Supabase Account**: A project set up on Supabase.
- **Google AI API Key**: For Gemini AI features.

### 1. Clone the Repository

```bash
git clone https://github.com/liwei-0405/JPlan.git
cd JPlan
```

### 2. Frontend Setup

```bash
# Install dependencies
npm install

# Setup environment variables
cp .env.example .env
# Edit .env and add your Supabase and Gemini API keys
```

### 3. Backend Setup

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

# Setup environment variables
cp ../.env.example .env
# Ensure the backend .env also has the necessary keys
```

---

## 🏃 Running the Application

### Start the Backend

Make sure your virtual environment is activated:

```bash
cd backend
python main.py
```
The backend will typically run on `http://localhost:8000`.

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
- **RLS**: Ensure Supabase Row Level Security (RLS) is enabled and properly configured for your tables.

## 📄 License

This project is for educational purposes (Final Year Project).

---

Developed by Teh Li Wei (liwei-0405) Nickname: Levi
