/**
 * UBAgen — src/restartBot.ts
 * URL resolution and authenticated HTTP request helpers for POST /api/admin/restart-bot.
 */

export const RESTART_BOT_PATH = '/api/admin/restart-bot';

export interface BotRestartResult {
  ok: boolean;
  status: number;
  statusCodeLabel: string;
  targetUrl: string;
  message: string;
  retryAfterSeconds?: number;
}

/**
 * Resolves the target URL for POST /api/admin/restart-bot from the configured stats endpoint URL.
 * - Preserves same-origin behavior for relative URLs (e.g. "/api/stats", "/stats").
 * - Resolves "/api/admin/restart-bot" on the same backend host when a full URL is configured,
 *   stripping any trailing "/api/stats" or "/stats" so "/api/stats/api/admin/restart-bot" is never formed.
 */
export function resolveRestartBotUrl(endpointUrl: string): string {
  const raw = (endpointUrl || '').trim();
  if (!raw || raw === '/api/stats' || raw === '/stats' || raw === 'api/stats' || raw === 'stats') {
    return RESTART_BOT_PATH;
  }

  // Relative path starting with "/" (excluding protocol-relative "//")
  if (raw.startsWith('/') && !raw.startsWith('//')) {
    const cleanPath = raw
      .replace(/[?#].*$/, '')
      .replace(/\/+(?:api\/)?stats\/?$/i, '')
      .replace(/\/+$/, '');
    return `${cleanPath}${RESTART_BOT_PATH}`;
  }

  // Full HTTP(S) URL
  if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
    let parsed: URL;
    try {
      parsed = new URL(raw);
    } catch {
      throw new Error('Backend Endpoint URL geçersiz. Lütfen bağlantı ayarlarını kontrol edin.');
    }
    if (!parsed.hostname || (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')) {
      throw new Error('Backend Endpoint URL geçersiz. Lütfen bağlantı ayarlarını kontrol edin.');
    }

    const basePath = parsed.pathname
      .replace(/\/+(?:api\/)?stats\/?$/i, '')
      .replace(/\/+$/, '');
    parsed.pathname = `${basePath}${RESTART_BOT_PATH}`;
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  }

  // Reject malformed URLs or unsupported schemes
  if (raw.includes('://') || raw.startsWith('//') || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    throw new Error('Backend Endpoint URL geçersiz. Lütfen bağlantı ayarlarını kontrol edin.');
  }

  // Relative path without leading slash (e.g. "custom/api/stats")
  const normalized = raw
    .replace(/[?#].*$/, '')
    .replace(/^\/+/, '')
    .replace(/\/+(?:api\/)?stats\/?$/i, '')
    .replace(/\/+$/, '');
  return normalized ? `/${normalized}${RESTART_BOT_PATH}` : RESTART_BOT_PATH;
}

/**
 * Sends an authenticated POST /api/admin/restart-bot request and safely parses HTTP status and JSON body.
 * Never logs or persists the admin token.
 */
export async function executeBotRestartRequest(options: {
  endpointUrl: string;
  adminToken: string;
  fetchImpl?: typeof fetch;
}): Promise<BotRestartResult> {
  const token = (options.adminToken || '').trim();
  if (!token) {
    return {
      ok: false,
      status: 0,
      statusCodeLabel: 'Eksik Token',
      targetUrl: RESTART_BOT_PATH,
      message: 'Devam etmek için sunucuda tanımlı UBAGEN_ADMIN_TOKEN değerini girmelisiniz.',
    };
  }

  let targetUrl: string;
  try {
    targetUrl = resolveRestartBotUrl(options.endpointUrl);
  } catch (err) {
    return {
      ok: false,
      status: 0,
      statusCodeLabel: 'URL Hatası',
      targetUrl: options.endpointUrl || RESTART_BOT_PATH,
      message: err instanceof Error ? err.message : 'Backend Endpoint URL geçersiz.',
    };
  }

  const fetchFn = options.fetchImpl || fetch;
  let response: Response;
  try {
    response = await fetchFn(targetUrl, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
      },
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : 'Bağlantı kurulamadı';
    return {
      ok: false,
      status: 0,
      statusCodeLabel: 'Ağ Hatası',
      targetUrl,
      message: `Backend sunucusuna ulaşılamadı (${detail}). Lütfen sunucunun çalıştığından emin olun.`,
    };
  }

  let payload: Record<string, unknown> = {};
  try {
    const rawText = await response.text();
    if (rawText && rawText.trim()) {
      const parsed = JSON.parse(rawText);
      if (parsed && typeof parsed === 'object') {
        payload = parsed as Record<string, unknown>;
      }
    }
  } catch {
    // Non-JSON response body handled gracefully
  }

  const serverMessage =
    typeof payload.message === 'string' && payload.message.trim()
      ? payload.message.trim()
      : typeof payload.error === 'string' && payload.error.trim()
      ? payload.error.trim()
      : '';

  const retryAfterHeader = response.headers?.get?.('Retry-After');
  const retryAfterSeconds =
    typeof payload.retry_after === 'number'
      ? payload.retry_after
      : retryAfterHeader && /^\d+$/.test(retryAfterHeader)
      ? parseInt(retryAfterHeader, 10)
      : undefined;

  if (response.ok) {
    return {
      ok: true,
      status: response.status,
      statusCodeLabel: `HTTP ${response.status}`,
      targetUrl,
      message:
        serverMessage ||
        'Yeniden başlatma isteği başarıyla alındı. Discord botu birkaç saniye içinde tekrar bağlanacaktır.',
    };
  }

  let statusExplanation = `Bot yeniden başlatılamadı (HTTP ${response.status}).`;
  if (response.status === 401) {
    statusExplanation = 'Yetkisiz istek (HTTP 401): Girilen UBAGEN_ADMIN_TOKEN hatalı veya geçersiz.';
  } else if (response.status === 405) {
    statusExplanation = 'İzin verilmeyen HTTP metodu (HTTP 405): Yalnızca POST isteği desteklenir.';
  } else if (response.status === 429) {
    statusExplanation = retryAfterSeconds
      ? `Çok sık yeniden başlatma isteği (HTTP 429): Lütfen ${retryAfterSeconds} saniye bekleyip tekrar deneyin.`
      : 'Çok sık yeniden başlatma isteği (HTTP 429): Lütfen biraz bekleyip tekrar deneyin.';
  } else if (response.status === 503) {
    statusExplanation =
      'Servis kullanılamıyor (HTTP 503): Sunucuda UBAGEN_ADMIN_TOKEN veya DISCORD_BOT_TOKEN yapılandırılmamış.';
  }

  return {
    ok: false,
    status: response.status,
    statusCodeLabel: `HTTP ${response.status}`,
    targetUrl,
    message: serverMessage ? `${statusExplanation} (${serverMessage})` : statusExplanation,
    retryAfterSeconds,
  };
}

/**
 * Resolve an API path for legacy panels that use same-origin backend routing.
 * Absolute HTTP(S) URLs are preserved; relative paths are normalized to root-relative paths.
 */
export function resolveApiUrl(path: string): string {
  const raw = (path || '').trim();
  if (raw.startsWith('http://') || raw.startsWith('https://')) return raw;
  if (raw.startsWith('//')) {
    throw new Error('Protocol-relative API URLs are not supported.');
  }
  return raw.startsWith('/') ? raw : `/${raw}`;
}
