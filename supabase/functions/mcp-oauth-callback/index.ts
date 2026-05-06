/**
 * mcp-oauth-callback - Public OAuth redirect target.
 * Receives ?code=&state= from the OAuth provider, validates the state token
 * (CSRF protection), exchanges the code for tokens, updates the connector row,
 * and redirects/renders a success page.
 */
import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SUPABASE_SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const APP_BASE_URL = Deno.env.get("APP_BASE_URL") ?? "";
const REDIRECT_URI = `${SUPABASE_URL}/functions/v1/mcp-oauth-callback`;

function htmlPage(message: string, success: boolean): Response {
  const color = success ? "#10b981" : "#ef4444";
  const html = `<!doctype html><html><head><title>MCP OAuth</title><meta charset="utf-8"></head><body style="font-family:system-ui;padding:40px;text-align:center;background:#0f172a;color:#fff;"><div style="max-width:480px;margin:0 auto;padding:32px;background:#1e293b;border-radius:12px;border-top:4px solid ${color};"><h2>${success ? "Success" : "Error"}</h2><p style="color:#cbd5e1;">${message}</p></div></body></html>`;
  return new Response(html, { status: success ? 200 : 400, headers: { "Content-Type": "text/html" } });
}

serve(async (req) => {
  const url = new URL(req.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");
  const error = url.searchParams.get("error");

  if (error) return htmlPage(`OAuth error: ${error}`, false);
  if (!code || !state) return htmlPage("Missing code or state", false);

  const sb = createClient(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY);
  const { data: c } = await sb.from("mcp_connectors")
    .select("*").eq("oauth_state", state).single();
  if (!c) return htmlPage("State mismatch (possible CSRF)", false);

  const verifier = (c.metadata || {}).code_verifier;
  if (!verifier) return htmlPage("Missing code_verifier", false);

  const headers: Record<string, string> = { "Content-Type": "application/x-www-form-urlencoded" };
  if (c.oauth_client_secret) {
    headers.Authorization = `Basic ${btoa(`${c.oauth_client_id}:${c.oauth_client_secret}`)}`;
  }

  const tokenRes = await fetch(c.oauth_token_url, {
    method: "POST",
    headers,
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: REDIRECT_URI,
      client_id: c.oauth_client_id,
      code_verifier: verifier,
    }).toString(),
  });

  if (!tokenRes.ok) {
    const text = await tokenRes.text();
    await sb.from("mcp_connectors").update({
      auth_status: "failed",
      oauth_state: null,
      metadata: { ...(c.metadata || {}), code_verifier: null, last_error: text },
    }).eq("id", c.id);
    return htmlPage(`Token exchange failed: ${text}`, false);
  }

  const tokens = await tokenRes.json();
  const expiresAt = tokens.expires_in
    ? new Date(Date.now() + tokens.expires_in * 1000).toISOString()
    : null;

  const cleanMeta = { ...(c.metadata || {}) };
  delete cleanMeta.code_verifier;

  await sb.from("mcp_connectors").update({
    access_token: tokens.access_token,
    refresh_token: tokens.refresh_token ?? null,
    token_expires_at: expiresAt,
    auth_status: "active",
    last_connected_at: new Date().toISOString(),
    oauth_state: null,
    metadata: cleanMeta,
  }).eq("id", c.id);

  if (APP_BASE_URL) {
    return new Response(null, {
      status: 302,
      headers: { Location: `${APP_BASE_URL}/connectors?connected=${c.id}` },
    });
  }
  return htmlPage("Connected successfully. You can close this window.", true);
});
