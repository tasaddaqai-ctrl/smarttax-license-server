-- ==============================================================================
-- SMARTTAX FBR DIGITAL INVOICER PRO - SUPABASE CLOUD INITIALIZATION SCHEMA
-- Run this in your Free Supabase SQL Editor (https://supabase.com/dashboard)
-- 100% Free Tier (500MB PostgreSQL, 1GB Cloud Storage Bucket, 50k Active Users)
-- ==============================================================================

-- 1. Shops & Subscriptions Table
CREATE TABLE IF NOT EXISTS public.smarttax_shops (
    id BIGSERIAL PRIMARY KEY,
    mobile_number TEXT UNIQUE NOT NULL,
    pin_hash TEXT NOT NULL,
    business_name TEXT NOT NULL,
    city_province TEXT NOT NULL,
    plan_type TEXT NOT NULL DEFAULT '30-Day Free Trial',
    subscription_status TEXT NOT NULL DEFAULT 'TRIAL',
    trial_start_date DATE NOT NULL DEFAULT CURRENT_DATE,
    expiry_date DATE NOT NULL,
    max_devices INTEGER NOT NULL DEFAULT 2,
    whatsapp_verified INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 2. Multi-PC Device Sessions Table (Enforces 2 PCs per shop)
CREATE TABLE IF NOT EXISTS public.smarttax_devices (
    id BIGSERIAL PRIMARY KEY,
    mobile_number TEXT NOT NULL REFERENCES public.smarttax_shops(mobile_number) ON DELETE CASCADE,
    device_id TEXT NOT NULL,
    device_name TEXT NOT NULL,
    ip_address TEXT,
    last_heartbeat TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL,
    UNIQUE(mobile_number, device_id)
);

-- 3. Cloud Backups Metadata Table
CREATE TABLE IF NOT EXISTS public.smarttax_backups (
    id BIGSERIAL PRIMARY KEY,
    mobile_number TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    bucket_name TEXT NOT NULL DEFAULT 'smarttax-backups',
    backup_size_bytes BIGINT NOT NULL DEFAULT 0,
    backup_timestamp TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc'::text, now()) NOT NULL
);

-- 4. Enable Row Level Security (RLS)
ALTER TABLE public.smarttax_shops ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.smarttax_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.smarttax_backups ENABLE ROW LEVEL SECURITY;

-- 5. Open API Access for Anon / Authenticated keys (Protected by application logic)
DROP POLICY IF EXISTS "Public Read/Write for smarttax_shops" ON public.smarttax_shops;
CREATE POLICY "Public Read/Write for smarttax_shops" ON public.smarttax_shops FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Public Read/Write for smarttax_devices" ON public.smarttax_devices;
CREATE POLICY "Public Read/Write for smarttax_devices" ON public.smarttax_devices FOR ALL USING (true) WITH CHECK (true);

DROP POLICY IF EXISTS "Public Read/Write for smarttax_backups" ON public.smarttax_backups;
CREATE POLICY "Public Read/Write for smarttax_backups" ON public.smarttax_backups FOR ALL USING (true) WITH CHECK (true);

-- 6. Create Free Storage Bucket for Backups
INSERT INTO storage.buckets (id, name, public) 
VALUES ('smarttax-backups', 'smarttax-backups', false)
ON CONFLICT (id) DO NOTHING;
