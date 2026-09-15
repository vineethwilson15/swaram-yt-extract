# Deno Deploy extractor

This is the serverless extractor for Deno Deploy. It keeps the same routes as the other adapters:

- `GET /health`
- `GET /extract?video_id=VIDEO_ID`
- `GET /api/extract?video_id=VIDEO_ID`

The function downloads the current standalone `yt-dlp` release into the instance's ephemeral `/tmp` directory on a cold start, then runs it as a subprocess. Audio is streamed through the function after the URL is resolved.

## Deploy

1. Create an application at [Deno Deploy](https://console.deno.com/).
2. Connect this repository and set the application root to `serverless/deno`.
3. Set the entrypoint to `main.ts`.
4. Add these environment variables in the Deno Deploy dashboard:

   - `PUBLIC_FRONTEND_ORIGIN=https://ecoliving-tips.github.io`
   - `PUBLIC_ACCESS=true`
   - `API_KEY` if private API-key access is needed
   - `YOUTUBE_COOKIE` or `YT_COOKIES_B64` only if YouTube requires authenticated cookies
   - `REQUESTS_PER_MINUTE=3`
   - `MAX_AUDIO_BITRATE=96000`
   - `MAX_AUDIO_BYTES=52428800`

The first request after a cold start may take longer while `yt-dlp` is downloaded. Do not commit cookie values to the repository.

## Local check

```sh
deno check main.ts
deno task dev
```