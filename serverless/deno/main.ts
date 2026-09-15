const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const DEFAULT_MAX_AUDIO_BITRATE = 96_000;
const DEFAULT_MAX_AUDIO_BYTES = 50 * 1024 * 1024;
const DEFAULT_REQUESTS_PER_MINUTE = 3;
const rateBuckets = new Map<string, { startedAt: number; count: number }>();

let ytDlpPathPromise: Promise<string> | undefined;

Deno.serve(async (request) => {
  return handleRequest(request);
});

async function handleRequest(request: Request): Promise<Response> {
  const startedAt = Date.now();
  const requestId = request.headers.get("x-request-id") || crypto.randomUUID();
  const env = Deno.env;

  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders(request) });
  }

  const url = new URL(request.url);
  if (url.pathname === "/health" && request.method === "GET") {
    return json({ service: "Swaram YT Extract Deno", status: "ok" }, 200, request);
  }
  if (url.pathname !== "/api/extract" && url.pathname !== "/extract") {
    return json({ error: "Not found" }, 404, request);
  }
  if (request.method !== "GET") {
    return json({ error: "Method not allowed" }, 405, request);
  }

  const hasValidApiKey = Boolean(env.get("API_KEY")) && request.headers.get("x-api-key") === env.get("API_KEY");
  const isPublicBrowserRequest = env.get("PUBLIC_ACCESS") !== "false" && isAllowedOrigin(request);
  if (!hasValidApiKey && !isPublicBrowserRequest) {
    return json({ error: "Authentication required" }, 401, request);
  }

  const clientIp = request.headers.get("x-forwarded-for")?.split(",")[0].trim() || "unknown";
  if (!allowRequest(clientIp)) {
    return json({ error: "Too many requests" }, 429, request, { "Retry-After": "60" });
  }

  const videoId = url.searchParams.get("video_id") || "";
  if (!VIDEO_ID_RE.test(videoId)) {
    return json({ error: "Invalid video_id - must be 11 alphanumeric chars" }, 400, request);
  }

  const maxBitrate = positiveInteger(env.get("MAX_AUDIO_BITRATE"), DEFAULT_MAX_AUDIO_BITRATE);
  const maxBytes = positiveInteger(env.get("MAX_AUDIO_BYTES"), DEFAULT_MAX_AUDIO_BYTES);
  const cookieHeader = cookieFromEnvironment();

  try {
    console.log("[extract] yt-dlp lookup started", { requestId, videoId });
    const ytDlpPath = await getYtDlpPath();

    const audioUrl = await resolveAudioUrl(ytDlpPath, videoId, cookieHeader, maxBitrate);
    const audioResponse = await fetch(audioUrl);
    if (!audioResponse.ok || !audioResponse.body) {
      return json({ error: "Audio stream could not be fetched" }, 502, request);
    }

    const contentType = audioResponse.headers.get("content-type") || "audio/mp4";
    const contentLength = Number(audioResponse.headers.get("content-length") || 0);
    if (contentLength > maxBytes) {
      return json({ error: "Audio stream exceeds the configured size limit" }, 413, request);
    }

    const headers = new Headers(corsHeaders(request));
    headers.set("Content-Type", contentType);
    headers.set("Content-Disposition", `attachment; filename="${videoId}.${extensionFor(contentType)}"`);
    headers.set("Cache-Control", "no-store");
    headers.set("X-Content-Type-Options", "nosniff");
    if (contentLength > 0) headers.set("Content-Length", String(contentLength));

    console.log("[extract] success", { requestId, videoId, elapsedMs: Date.now() - startedAt });
    return new Response(audioResponse.body, { status: 200, headers });
  } catch (error) {
    console.error("[extract] yt-dlp failed", {
      requestId,
      videoId,
      error: error instanceof Error ? error.message : String(error),
      elapsedMs: Date.now() - startedAt,
    });
    return json({ error: "YouTube extraction failed" }, 502, request);
  }
}

async function getYtDlpPath(): Promise<string> {
  if (!ytDlpPathPromise) ytDlpPathPromise = downloadYtDlp();
  return ytDlpPathPromise;
}

async function downloadYtDlp(): Promise<string> {
  const architecture = Deno.build.arch === "aarch64" ? "linux_aarch64" : "linux";
  const path = `/tmp/yt-dlp-latest-${architecture}`;
  try {
    const stat = await Deno.stat(path);
    if (stat.isFile) return path;
  } catch {
    // The ephemeral file is expected to be absent on a cold start.
  }

  const downloadUrl = `https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_${architecture}`;
  const response = await fetch(downloadUrl);
  if (!response.ok || !response.body) throw new Error(`yt-dlp download failed: HTTP ${response.status}`);
  await Deno.writeFile(path, new Uint8Array(await response.arrayBuffer()));
  await Deno.chmod(path, 0o755);
  return path;
}

async function resolveAudioUrl(path: string, videoId: string, cookieHeader: string | undefined, maxBitrate: number): Promise<string> {
  const args = [
    "--ignore-config",
    "--no-playlist",
    "--no-warnings",
    "--get-url",
    "--format", `bestaudio[abr<=${Math.round(maxBitrate / 1000)}]/bestaudio`,
  ];
  if (cookieHeader) args.push("--add-header", `Cookie:${cookieHeader}`);
  const command = new Deno.Command(path, {
    args: [...args, `https://www.youtube.com/watch?v=${videoId}`],
    stdout: "piped",
    stderr: "piped",
  });
  const output = await command.output();
  if (!output.success) {
    const detail = new TextDecoder().decode(output.stderr).trim().slice(-500);
    throw new Error(`yt-dlp exited with code ${output.code}${detail ? `: ${detail}` : ""}`);
  }
  const urls = new TextDecoder().decode(output.stdout).split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
  const audioUrl = urls.at(-1);
  if (!audioUrl || !audioUrl.startsWith("http")) throw new Error("yt-dlp returned no audio URL");
  return audioUrl;
}

function cookieFromEnvironment(): string | undefined {
  const rawCookie = Deno.env.get("YOUTUBE_COOKIE");
  if (rawCookie) return rawCookie;
  const encodedCookies = Deno.env.get("YT_COOKIES_B64");
  if (!encodedCookies) return undefined;

  try {
    const bytes = Uint8Array.from(atob(encodedCookies), (character) => character.charCodeAt(0));
    const netscapeCookies = new TextDecoder().decode(bytes);
    const cookies = new Map<string, string>();
    for (const line of netscapeCookies.split(/\r?\n/)) {
      if (!line || line.startsWith("#")) continue;
      const fields = line.split("\t");
      if (fields.length >= 7 && isGoogleOrYouTubeCookieDomain(fields[0]) && fields[5] && fields[6] !== undefined) {
        cookies.set(fields[5], fields[6]);
      }
    }
    return [...cookies.entries()].map(([name, value]) => `${name}=${value}`).join("; ") || undefined;
  } catch {
    console.warn("[extract] invalid YT_COOKIES_B64 value");
    return undefined;
  }
}

function isGoogleOrYouTubeCookieDomain(domain: string): boolean {
  const normalized = domain.toLowerCase().replace(/^\./, "");
  return normalized === "google.com" || normalized.endsWith(".google.com") || normalized === "youtube.com" || normalized.endsWith(".youtube.com");
}

function positiveInteger(value: string | null | undefined, fallback: number): number {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function extensionFor(contentType: string): string {
  if (contentType.includes("webm")) return "webm";
  if (contentType.includes("ogg")) return "opus";
  return "m4a";
}

function isAllowedOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  const configuredOrigin = (Deno.env.get("PUBLIC_FRONTEND_ORIGIN") || "").replace(/\/$/, "");
  return Boolean(origin && configuredOrigin && origin === configuredOrigin);
}

function corsHeaders(request: Request): Record<string, string> {
  const origin = request.headers.get("origin");
  const headers: Record<string, string> = {
    "Access-Control-Allow-Headers": "X-API-Key, Content-Type",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    Vary: "Origin",
  };
  if (isAllowedOrigin(request)) {
    headers["Access-Control-Allow-Origin"] = origin!;
    headers["Access-Control-Max-Age"] = "600";
  }
  return headers;
}

function allowRequest(clientIp: string): boolean {
  const limit = positiveInteger(Deno.env.get("REQUESTS_PER_MINUTE"), DEFAULT_REQUESTS_PER_MINUTE);
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

function json(body: unknown, status: number, request: Request, extraHeaders: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders(request), "Content-Type": "application/json", "Cache-Control": "no-store", ...extraHeaders },
  });
}