import { Innertube } from "youtubei.js";

const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const DEFAULT_MAX_AUDIO_BITRATE = 96_000;
const DEFAULT_MAX_AUDIO_BYTES = 50 * 1024 * 1024;

let youtubeClientPromise;

export default {
  async fetch(request, env) {
    const startedAt = Date.now();
    const requestId = request.headers.get("cf-ray") || crypto.randomUUID();
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    const url = new URL(request.url);
    if ((url.pathname === "/" || url.pathname === "/health") && request.method === "GET") {
      return json({ service: "Swaram YT Extract Serverless", status: "ok" });
    }

    if (url.pathname !== "/extract" || request.method !== "GET") {
      return json({ error: "Not found" }, 404);
    }

    if (!env.API_KEY || request.headers.get("X-API-Key") !== env.API_KEY) {
      console.warn("[extract] unauthorized", { requestId });
      return json({ error: "Invalid API key" }, 401);
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
        return json({ error: "No direct audio stream is available" }, 404);
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
        return json({ error: "Audio stream could not be fetched" }, 502);
      }

      const headers = new Headers(corsHeaders());
      headers.set("Content-Type", audioFormat.mime_type || "audio/mp4");
      headers.set("Content-Disposition", `attachment; filename="${videoId}.${extensionFor(audioFormat.mime_type)}"`);
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
      return json({ error: "YouTube extraction failed" }, 502);
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

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "X-API-Key, Content-Type",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
  };
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(), "Content-Type": "application/json" },
  });
}
