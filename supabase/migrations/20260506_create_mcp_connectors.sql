-- Custom MCP connectors registered by users (FastMCP-style remote MCP servers).
-- Mirrors the "Add custom connector" UX: name + URL + optional OAuth credentials.
-- OAuth flow is driven by the mcp-oauth / mcp-oauth-callback edge functions.

CREATE TABLE IF NOT EXISTS public.mcp_connectors (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  brand_id uuid REFERENCES public.brands(id) ON DELETE SET NULL,
  name text NOT NULL,
  url text NOT NULL,
  oauth_client_id text,
  oauth_client_secret text,
  auth_status text NOT NULL DEFAULT 'none'
    CHECK (auth_status IN ('none','pending','active','failed','expired')),
  access_token text,
  refresh_token text,
  token_expires_at timestamptz,
  oauth_authorize_url text,
  oauth_token_url text,
  oauth_scopes text,
  oauth_state text,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_connected_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mcp_connectors_user_id ON public.mcp_connectors(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_connectors_brand_id ON public.mcp_connectors(brand_id);
CREATE INDEX IF NOT EXISTS idx_mcp_connectors_status ON public.mcp_connectors(auth_status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mcp_connectors_user_url
  ON public.mcp_connectors(user_id, url);

ALTER TABLE public.mcp_connectors ENABLE ROW LEVEL SECURITY;

CREATE POLICY "mcp_connectors_select_own" ON public.mcp_connectors
  FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "mcp_connectors_insert_own" ON public.mcp_connectors
  FOR INSERT WITH CHECK (auth.uid() = user_id);

CREATE POLICY "mcp_connectors_update_own" ON public.mcp_connectors
  FOR UPDATE USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

CREATE POLICY "mcp_connectors_delete_own" ON public.mcp_connectors
  FOR DELETE USING (auth.uid() = user_id);

CREATE TRIGGER mcp_connectors_set_updated_at
  BEFORE UPDATE ON public.mcp_connectors
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();
