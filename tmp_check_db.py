import os
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
supabase = create_client(url, key)

# Check constraints on daily_plans
query = """
SELECT
    conname as constraint_name,
    pg_get_constraintdef(c.oid) as constraint_definition
FROM
    pg_constraint c
JOIN
    pg_namespace n ON n.oid = c.connamespace
WHERE
    contype IN ('u', 'p') 
    AND conrelid = 'daily_plans'::regclass;
"""

try:
    res = supabase.rpc('get_table_constraints', {'table_name': 'daily_plans'}).execute()
    print("RPC Result:", res.data)
except Exception as e:
    # If RPC fails, try a direct raw query if possible (though Supabase doesn't allow raw SQL via client directly easily without RPC)
    print("RPC failed, we likely need to check via SQL Editor.")

print("\nSuggestions:")
print("1. Go to Supabase SQL Editor.")
print("2. Run: ALTER TABLE daily_plans ADD CONSTRAINT daily_plans_user_id_date_key UNIQUE (user_id, date);")
