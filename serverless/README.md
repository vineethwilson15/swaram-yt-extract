# Serverless YouTube extractor

This branch contains a Docker-free Cloudflare Worker implementation using the open-source `youtubei.js` library. It talks directly to YouTube's InnerTube API, selects an audio-only stream, and proxies the temporary stream URL back to the caller.

It does not use Piped, Docker, ffmpeg, Supervisor, a Node subprocess, or the bgutil sidecar.

## Deploy

From this directory:

```powershell
npm install
npx wrangler login
npx wrangler secret put API_KEY
npm run deploy
```

`API_KEY` remains available for trusted server-to-server callers. Browser requests
from the configured frontend origin use public mode and do not expose `API_KEY`.
Do not put the API key in `wrangler.toml`.

## Local development

```powershell
npm install
$env:API_KEY = "change-me"
npm run dev
```

The local Worker listens on the URL Wrangler prints. Test it with:

```text
GET /health
GET /extract?video_id=VIDEO_ID
X-API-Key: change-me                 # trusted backend mode
```

## Configuration

`wrangler.toml` configures a preferred maximum audio bitrate of 96 kbps, a 50 MB
maximum stream size, the GitHub Pages origin, and three extraction requests per
IP per minute. `PUBLIC_ACCESS` enables browser requests only when the `Origin`
header exactly matches `PUBLIC_FRONTEND_ORIGIN`.

Cloudflare Worker memory is not a durable rate-limit store. For stronger abuse
protection, also configure a Cloudflare WAF/rate-limiting rule for `/extract`.

## Important limitations

- This uses YouTube's undocumented InnerTube API, not the official YouTube Data API.
- YouTube may block or challenge Cloudflare Worker IPs.
- Direct stream URLs expire and are fetched immediately by the Worker.
- There is no cookies or PO-token support.
- This does not transcode audio; the format is whatever YouTube provides.
- Library compatibility and YouTube behavior can change, so this branch should be tested before replacing the existing service.
