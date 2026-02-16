-- Create daily_plans table
CREATE TABLE IF NOT EXISTS daily_plans (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  date DATE NOT NULL UNIQUE,
  activities JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
  updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- Create index on date for faster queries
CREATE INDEX IF NOT EXISTS idx_daily_plans_date ON daily_plans(date);

-- Create function to automatically update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = timezone('utc'::text, now());
  RETURN NEW;
END;
$$ language 'plpgsql';

-- Create trigger to update updated_at on row update
DROP TRIGGER IF EXISTS update_daily_plans_updated_at ON daily_plans;
CREATE TRIGGER update_daily_plans_updated_at
  BEFORE UPDATE ON daily_plans
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- Enable Row Level Security (RLS) - currently disabled for single-user app
-- Uncomment these lines when you add user authentication:
-- ALTER TABLE daily_plans ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Users can manage their own plans" ON daily_plans
--   FOR ALL USING (auth.uid() = user_id);

-- Grant permissions (adjust based on your needs)
-- For now, allowing public access since there's no auth
-- GRANT ALL ON daily_plans TO anon;
-- GRANT ALL ON daily_plans TO authenticated;
