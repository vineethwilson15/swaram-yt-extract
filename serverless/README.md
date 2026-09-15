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

The API key is stored as a Worker secret. Do not put it in `wrangler.toml`.

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
X-API-Key: change-me
```

## Configuration

`wrangler.toml` sets a preferred maximum audio bitrate of 96 kbps and a 50 MB maximum stream size. These values can be overridden with Worker variables named `MAX_AUDIO_BITRATE` and `MAX_AUDIO_BYTES`.

## Important limitations

- This uses YouTube's undocumented InnerTube API, not the official YouTube Data API.
- YouTube may block or challenge Cloudflare Worker IPs.
- Direct stream URLs expire and are fetched immediately by the Worker.
- There is no cookies or PO-token support.
- This does not transcode audio; the format is whatever YouTube provides.
- Library compatibility and YouTube behavior can change, so this branch should be tested before replacing the existing service.
