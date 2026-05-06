/**
 * mcp-oauth - Auth-required actions for MCP connector OAuth setup.
 * Actions:
 *   - GET  ?action=discover&url=<mcp_url>  Returns OAuth metadata for an MCP server.
 *   - POST { connector_id }                Generates PKCE+state, returns authorize_url.
 * Callback comes back to public mcp-oauth-callback function.
 */
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient, SupabaseClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_ANON_KEY = Deno.env.get("SUPABASE_ANON_KEY") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const REDIRECT_URI = `${SUPABASE_URL}/functions/v1/mcp-oauth-callback`;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Content-Type": "application/json",
};

function b64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
function randomString(len: number): string {
  const a = new Uint8Array(len);
  crypto.getRandomValues(a);
  return b64url(a);
}
async function sha256(s: string): Promise<Uint8Array> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return new Uint8Array(buf);
}
function admin(): SupabaseClient {
  return createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
}
async function userId(req: Request): Promise<string | null> {
  const auth = req.headers.get("Authorization");
  if (!auth) return null;
  const sb = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
    global: { headers: { Authorization: auth } },
  });
  const { data } = await sb.auth.getUser();
  return data?.user?.id ?? null;
}

type OAuthMeta = {
  authorization_endpoint: string;
  token_endpoint: string;
  registration_endpoint?: string;
  scopes_supported?: string[];
};

async function fetchMetadata(mcpUrl: string): Promise<OAuthMeta | null> {
  const u = new URL(mcpUrl);
  const candidates = [
    `${u.origin}/.well-known/oauth-authorization-server${u.pathname.replace(/\/$/, "")}`,
    `${u.origin}/.well-known/oauth-authorization-server`,
    `${u.origin}/.well-known/openid-configuration`,
  ];
  for (const c of candidates) {
    try {
      const r = await fetch(c, { headers: { Accept: "application/json" } });
      if (r.ok) {
        const m = await r.json();
        if (m.authorization_endpoint && m.token_endpoint) return m;
      }
    } catch (_) {}
  }
  return null;
}

async function dcr(endpoint: string, redirectUri: string, name: string) {
  try {
    const r = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        redirect_uris: [redirectUri],
        token_endpoint_auth_method: "client_secret_basic",
        grant_types: ["authorization_code", "refresh_token"],
        response_types: ["code"],
        client_name: name,
      }),
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (_) {
    return null;
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: corsHeaders });
}

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: corsHeaders });
  const url = new URL(req.url);
  const action = url.searchParams.get("action");

  try {
    if (action === "discover") {
      const target = url.searchParams.get("url");
      if (!target) return json({ error: "Missing url" }, 400);
      const meta = await fetchMetadata(target);
      return json({ supports_oauth: !!meta, metadata: meta });
    }

    const uid = await userId(req);
    if (!uid) return json({ error: "Unauthorized" }, 401);
    const body = await req.json().catch(() => ({}));
    const connectorId = body.connector_id;
    if (!connectorId) return json({ error: "Missing connector_id" }, 400);

    const sb = admin();
    const { data: c, error: e } = await sb.from("mcp_connectors")
      .select("*").eq("id", connectorId).eq("user_id", uid).single();
    if (e || !c) return json({ error: "Connector not found" }, 404);

    const meta = await fetchMetadata(c.url);
    if (!meta) return json({ error: "MCP server does not advertise OAuth metadata" }, 400);

    let clientId = c.oauth_client_id;
    let clientSecret = c.oauth_client_secret;

    if (!clientId && meta.registration_endpoint) {
      const reg = await dcr(meta.registration_endpoint, REDIRECT_URI, c.name);
      if (!reg) return json({ error: "Dynamic client registration failed; please provide OAuth Client ID/Secret" }, 400);
      clientId = reg.client_id;
      clientSecret = reg.client_secret ?? null;
    }
    if (!clientId) return json({ error: "OAuth Client ID required (server does not support DCR)" }, 400);

    const verifier = randomString(48);
    const challenge = b64url(await sha256(verifier));
    const state = randomString(24);

    const params = new URLSearchParams({
      response_type: "code",
      client_id: clientId,
      redirect_uri: REDIRECT_URI,
      code_challenge: challenge,
      code_challenge_method: "S256",
      state,
    });
    if (c.oauth_scopes) params.set("scope", c.oauth_scopes);
    else if (meta.scopes_supported?.length) params.set("scope", meta.scopes_supported.join(" "));

    const authorizeUrl = `${meta.authorization_endpoint}?${params.toString()}`;

    await sb.from("mcp_connectors").update({
      oauth_client_id: clientId,
      oauth_client_secret: clientSecret,
      oauth_authorize_url: meta.authorization_endpoint,
      oauth_token_url: meta.token_endpoint,
      oauth_state: state,
      auth_status: "pending",
      metadata: { ...(c.metadata || {}), code_verifier: verifier },
    }).eq("id", connectorId);

    return json({ authorize_url: authorizeUrl });
  } catch (err) {
    console.error("[mcp-oauth] error", err);
    return json({ error: err instanceof Error ? err.message : "Internal error" }, 500);
  }
});
