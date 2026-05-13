# x-feed-state Worker

Tiny Cloudflare Worker that stores per-user state for the Pages viewer:
favorites, thumbs-up/down, and reading-time accumulators. Auth is a shared
Bearer token; storage is one KV key (`state`).

## Endpoints

- `GET /state` → returns the full state JSON
- `POST /state` → merges the request body into stored state, returns merged result
- `OPTIONS /state` → CORS preflight

All requests require `Authorization: Bearer <AUTH_TOKEN>`.

## State shape

```json
{
  "items": {
    "<tweet_id>": {
      "favorite": true,
      "thumb": "up" | "down" | null,
      "reading_seconds": 42,
      "last_read_at": "2026-05-13T..."
    }
  },
  "updated_at": "2026-..."
}
```

Merge rules:
- `reading_seconds` takes the max (so dwell time accumulates monotonically)
- `favorite`, `thumb`, `last_read_at` take the incoming value when present

## Deploy

```bash
cd worker/x-feed-state

# 1) Create KV namespace once
npx wrangler kv namespace create USER_STATE
# → copy the id into wrangler.toml under [[kv_namespaces]]

# 2) Set the shared bearer token (any random string)
echo "$(uuidgen)" | npx wrangler secret put AUTH_TOKEN
# → save the same value; you'll paste it into the viewer's localStorage

# 3) Deploy
npx wrangler deploy
# → note the worker URL (https://x-feed-state.<account>.workers.dev)
```

## Wire up the viewer

Once deployed, set two values in the Pages viewer's localStorage (open the site
once, then in DevTools console):

```js
localStorage.setItem("X_STATE_URL", "https://x-feed-state.<account>.workers.dev/state");
localStorage.setItem("X_STATE_TOKEN", "<same value as AUTH_TOKEN>");
location.reload();
```

After that, favorites / thumbs / reading time sync automatically.
