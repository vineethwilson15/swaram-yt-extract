import { Innertube } from "youtubei.js";

const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const DEFAULT_MAX_AUDIO_BITRATE = 96_000;
const DEFAULT_MAX_AUDIO_BYTES = 50 * 1024 * 1024;
const DEFAULT_REQUESTS_PER_MINUTE = 3;
const rateBuckets = new Map();

let youtubeClientPromise;

export default {
  async fetch(request, env) {
    const startedAt = Date.now();
    const requestId = request.headers.get("cf-ray") || crypto.randomUUID();
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(request, env) });
    }

    const url = new URL(request.url);
    if ((url.pathname === "/" || url.pathname === "/health") && request.method === "GET") {
      return json({ service: "Swaram YT Extract Serverless", status: "ok" }, 200, request, env);
    }

    if (url.pathname !== "/extract" || request.method !== "GET") {
      return json({ error: "Not found" }, 404);
    }

    const hasValidApiKey = Boolean(env.API_KEY) && request.headers.get("X-API-Key") === env.API_KEY;
    const isPublicBrowserRequest = env.PUBLIC_ACCESS === "true" && isAllowedOrigin(request, env);
    if (!hasValidApiKey && !isPublicBrowserRequest) {
      console.warn("[extract] unauthorized", { requestId });
      return json({ error: "Authentication required" }, 401, request, env);
    }

    const clientIp = request.headers.get("CF-Connecting-IP") || "unknown";
    if (!allowRequest(clientIp, env)) {
      console.warn("[extract] rate limited", { requestId, clientIp });
      return json({ error: "Too many requests" }, 429, request, env, {
        "Retry-After": "60",
      });
    }

    const videoId = url.searchParams.get("video_id") || "";
    if (!VIDEO_ID_RE.test(videoId)) {
      console.warn("[extract] invalid video id", { requestId });
      return json({ error: "Invalid video_id - must be 11 alphanumeric chars" }, 400);
    }

    const maxBitrate = positiveInteger(env.MAX_AUDIO_BITRATE, DEFAULT_MAX_AUDIO_BITRATE);
    const maxBytes = positiveInteger(env.MAX_AUDIO_BYTES, DEFAULT_MAX_AUDIO_BYTES);

    try {
      console.log("[extract] youtube lookup started", { requestId, videoId });
      const youtube = await getYoutubeClient();
      const info = await youtube.getBasicInfo(videoId);
      const audioFormat = selectAudioFormat(info.streaming_data?.adaptive_formats, maxBitrate, maxBytes);

      if (!audioFormat?.url) {
        console.warn("[extract] no audio format", {
          requestId,
          videoId,
          formatCount: info.streaming_data?.adaptive_formats?.length || 0,
          elapsedMs: Date.now() - startedAt,
        });
        return json({ error: "No direct audio stream is available" }, 404, request, env);
      }

      console.log("[extract] audio format selected", {
        requestId,
        videoId,
        mimeType: audioFormat.mime_type || "",
        bitrate: audioFormat.bitrate || audioFormat.average_bitrate || 0,
        contentLength: audioFormat.content_length || "unknown",
        elapsedMs: Date.now() - startedAt,
      });

      const audioResponse = await fetch(audioFormat.url);
      if (!audioResponse.ok || !audioResponse.body) {
        console.warn("[extract] audio stream failed", {
          requestId,
          videoId,
          status: audioResponse.status,
          elapsedMs: Date.now() - startedAt,
        });
        return json({ error: "Audio stream could not be fetched" }, 502, request, env);
      }

      const headers = new Headers(corsHeaders(request, env));
      headers.set("Content-Type", audioFormat.mime_type || "audio/mp4");
      headers.set("Content-Disposition", `attachment; filename="${videoId}.${extensionFor(audioFormat.mime_type)}"`);
      headers.set("Cache-Control", "no-store");
      headers.set("X-Content-Type-Options", "nosniff");
      if (audioFormat.content_length) {
        headers.set("Content-Length", String(audioFormat.content_length));
      }

      console.log("[extract] success", {
        requestId,
        videoId,
        status: 200,
        elapsedMs: Date.now() - startedAt,
      });

      return new Response(audioResponse.body, { status: 200, headers });
    } catch (error) {
      console.error("[extract] YouTube extraction failed", {
        requestId,
        videoId,
        errorType: error?.constructor?.name || "UnknownError",
        error: error instanceof Error ? error.message : String(error),
        elapsedMs: Date.now() - startedAt,
      });
      return json({ error: "YouTube extraction failed" }, 502, request, env);
    }
  },
};

async function getYoutubeClient() {
  if (!youtubeClientPromise) {
    youtubeClientPromise = Innertube.create();
  }
  return youtubeClientPromise;
}

function selectAudioFormat(formats, maxBitrate, maxBytes) {
  if (!Array.isArray(formats)) return null;

  const audioOnly = formats.filter((format) => {
    const bitrate = Number(format.bitrate || format.average_bitrate || 0);
    const size = Number(format.content_length || 0);
    return format.url && format.has_audio && !format.has_video && bitrate > 0 && (!size || size <= maxBytes);
  });
  if (audioOnly.length === 0) return null;

  const compact = audioOnly.filter((format) =>
    Number(format.bitrate || format.average_bitrate || 0) <= maxBitrate
  );
  const candidates = compact.length > 0 ? compact : audioOnly;

  return [...candidates].sort((left, right) =>
    Number(right.bitrate || right.average_bitrate || 0) - Number(left.bitrate || left.average_bitrate || 0)
  )[0];
}

function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function extensionFor(mimeType = "") {
  if (mimeType.includes("webm")) return "webm";
  if (mimeType.includes("ogg")) return "opus";
  return "m4a";
}

function isAllowedOrigin(request, env) {
  const origin = request.headers.get("Origin");
  const configuredOrigin = (env.PUBLIC_FRONTEND_ORIGIN || "").replace(/\/$/, "");
  return Boolean(origin && configuredOrigin && origin === configuredOrigin);
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  const headers = {
    "Access-Control-Allow-Headers": "X-API-Key, Content-Type",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Vary": "Origin",
  };
  if (isAllowedOrigin(request, env)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Max-Age"] = "600";
  }
  return headers;
}

function allowRequest(clientIp, env) {
  const limit = positiveInteger(env.REQUESTS_PER_MINUTE, DEFAULT_REQUESTS_PER_MINUTE);
  const now = Date.now();
  const bucket = rateBuckets.get(clientIp);
  if (!bucket || now - bucket.startedAt >= 60_000) {
    rateBuckets.set(clientIp, { startedAt: now, count: 1 });
    return true;
  }
  if (bucket.count >= limit) return false;
  bucket.count += 1;
  return true;
}

function json(body, status = 200, request = null, env = {}, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      ...corsHeaders(request || new Request("https://worker.invalid"), env),
      "Content-Type": "application/json",
      "Cache-Control": "no-store",
      "X-Content-Type-Options": "nosniff",
      ...extraHeaders,
    },
  });
}
