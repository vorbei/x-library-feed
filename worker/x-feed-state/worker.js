// Per-user state store for the X Bookmarks Pages viewer.
// Auth: shared Bearer token in env.AUTH_TOKEN (set via `wrangler secret put AUTH_TOKEN`).
// Storage: single KV key `state` holding the full user state JSON.

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Max-Age": "86400",
};

const EMPTY = { items: {}, updated_at: null };

function withCors(resp) {
  for (const [k, v] of Object.entries(CORS_HEADERS)) {
    resp.headers.set(k, v);
  }
  return resp;
}

function jsonResponse(data, init = {}) {
  return withCors(
    new Response(JSON.stringify(data), {
      status: init.status || 200,
      headers: { "Content-Type": "application/json", ...(init.headers || {}) },
    }),
  );
}

function unauthorized(reason = "unauthorized") {
  return withCors(new Response(reason, { status: 401 }));
}

// Merge incoming item patches into the existing state. For numeric fields like
// reading_seconds take the max so dwell time accumulates monotonically; for
// flags (favorite, thumb) and timestamps take the new value when present.
function mergeState(existing, incoming) {
  const items = { ...(existing.items || {}) };
  for (const [id, patch] of Object.entries(incoming.items || {})) {
    if (!id) continue;
    const cur = items[id] || {};
    const next = { ...cur };
    if (patch.favorite !== undefined) next.favorite = !!patch.favorite;
    if (patch.thumb !== undefined) {
      next.thumb = patch.thumb === null ? null : String(patch.thumb);
    }
    if (typeof patch.reading_seconds === "number") {
      next.reading_seconds = Math.max(cur.reading_seconds || 0, patch.reading_seconds);
    }
    if (patch.last_read_at) next.last_read_at = String(patch.last_read_at);
    items[id] = next;
  }
  return { items };
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return withCors(new Response(null, { status: 204 }));
    }

    const auth = request.headers.get("Authorization") || "";
    const expected = `Bearer ${env.AUTH_TOKEN}`;
    if (!env.AUTH_TOKEN) {
      return new Response("AUTH_TOKEN not configured", { status: 500 });
    }
    if (auth !== expected) {
      return unauthorized();
    }

    const url = new URL(request.url);

    if (url.pathname === "/state") {
      if (request.method === "GET") {
        const data = (await env.USER_STATE.get("state", "json")) || EMPTY;
        return jsonResponse(data);
      }
      if (request.method === "POST") {
        let incoming;
        try { incoming = await request.json(); }
        catch { return withCors(new Response("Invalid JSON", { status: 400 })); }
        const existing = (await env.USER_STATE.get("state", "json")) || EMPTY;
        const merged = mergeState(existing, incoming);
        merged.updated_at = new Date().toISOString();
        await env.USER_STATE.put("state", JSON.stringify(merged));
        return jsonResponse(merged);
      }
      return withCors(new Response("Method not allowed", { status: 405 }));
    }

    // Translate-now: viewer button → dispatch the Translate Feed workflow on
    // GitHub with the requested item id as priority. Requires
    // `wrangler secret put GH_DISPATCH_TOKEN` for a fine-grained PAT with
    // `Actions: Read & Write` permission on the repo.
    if (url.pathname === "/translate-now" && request.method === "POST") {
      if (!env.GH_DISPATCH_TOKEN) {
        return jsonResponse({ error: "GH_DISPATCH_TOKEN not configured" }, { status: 500 });
      }
      let body;
      try { body = await request.json(); }
      catch { return withCors(new Response("Invalid JSON", { status: 400 })); }
      const id = (body && body.id ? String(body.id) : "").trim();
      if (!id) return jsonResponse({ error: "id required" }, { status: 400 });
      const repo = env.GH_REPO || "vorbei/x-library-feed";
      const workflow = env.GH_WORKFLOW || "translate-feed.yml";
      const ref = env.GH_REF || "main";
      const dispatchUrl = `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`;
      const resp = await fetch(dispatchUrl, {
        method: "POST",
        headers: {
          "Authorization": `Bearer ${env.GH_DISPATCH_TOKEN}`,
          "Accept": "application/vnd.github+json",
          "User-Agent": "x-feed-state-worker",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref, inputs: { priority_id: id } }),
      });
      if (!resp.ok) {
        const text = await resp.text();
        return jsonResponse({ error: "dispatch failed", status: resp.status, body: text.slice(0, 500) }, { status: 502 });
      }
      return jsonResponse({ ok: true, id, queued_at: new Date().toISOString() });
    }

    return withCors(new Response("Not found", { status: 404 }));
  },
};
