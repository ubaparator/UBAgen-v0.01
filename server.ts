/**
 * UBAgen — server.ts
 * Express full-stack server running Vite in development and serving /api/stats and /health.
 */

import express from 'express';
import { createServer as createViteServer } from 'vite';
import { execFile, spawn, ChildProcess } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { timingSafeEqual } from 'crypto';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let pythonWorkerProcess: ChildProcess | null = null;
let statsCache: { expiresAt: number; data: unknown } | null = null;
let lastBotRestartAt = 0;
let isRestartingBot = false;
let signalHandlersRegistered = false;

function stopPythonWorker() {
  if (pythonWorkerProcess && !pythonWorkerProcess.killed) {
    try {
      pythonWorkerProcess.kill('SIGTERM');
    } catch {}
  }
  pythonWorkerProcess = null;
}

export function verifyAdminToken(authorizationHeader: string | undefined, expectedToken: string): boolean {
  const cleanExpected = (expectedToken || '').trim();
  if (!cleanExpected || !authorizationHeader) {
    return false;
  }
  const match = authorizationHeader.trim().match(/^Bearer\s+(.+)$/i);
  const suppliedToken = match?.[1]?.trim() || '';
  if (!suppliedToken) {
    return false;
  }
  const suppliedBuf = Buffer.from(suppliedToken, 'utf8');
  const expectedBuf = Buffer.from(cleanExpected, 'utf8');
  if (suppliedBuf.length !== expectedBuf.length) {
    timingSafeEqual(expectedBuf, expectedBuf);
    return false;
  }
  return timingSafeEqual(suppliedBuf, expectedBuf);
}

export function resetServerRestartStateForTests() {
  lastBotRestartAt = 0;
  isRestartingBot = false;
  statsCache = null;
}

function ensurePythonWorker() {
  if (pythonWorkerProcess) return;

  try {
    pythonWorkerProcess = spawn('python3', ['-B', 'main.py'], {
      env: {
        ...process.env,
        PYTHONDONTWRITEBYTECODE: '1',
        UBAGEN_HTTP_PORT: '5050',
      },
      stdio: ['ignore', 'inherit', 'inherit'],
    });

    pythonWorkerProcess.on('error', (err) => {
      console.warn('Python worker notice:', err.message);
      pythonWorkerProcess = null;
    });

    pythonWorkerProcess.on('exit', () => {
      pythonWorkerProcess = null;
      setTimeout(() => {
        ensurePythonWorker();
      }, 3000);
    });

    if (!signalHandlersRegistered) {
      signalHandlersRegistered = true;
      process.on('exit', stopPythonWorker);
      process.on('SIGINT', () => {
        stopPythonWorker();
        process.exit(0);
      });
      process.on('SIGTERM', () => {
        stopPythonWorker();
        process.exit(0);
      });
    }
  } catch (err) {
    console.warn('Could not launch Python worker:', err);
  }
}

function degradedStats(reason: string) {
  return {
    status: 'degraded',
    timestamp: new Date().toISOString(),
    epoch_time: Date.now() / 1000,
    diagnostic_message: reason,
    bot: { status: 'unavailable', connected: false, is_ready: false,
      http_alive: true, gateway_connected: false, bot_ready: false,
      diagnostic_message: 'Bot statistics are temporarily unavailable.', latency_ms: null,
      user: 'Unavailable', guild_count: 0, uptime_seconds: null, uptime_str: 'Unavailable',
      sync_status: null, client_id: null, invite_url: null,
      reconnect_attempts: 0, wake_in_progress: false, wake_attempts: 0,
      last_wake_time: null, last_wake_reason: null,
      interactions_endpoint_configured: false,
      interactions_endpoint_path: '/api/discord/interactions',
      wake_endpoint_path: '/api/discord/wake' },
    compute: { cpu_usage_pct: null, logical_cores: null, physical_cores: null,
      os_name: 'Unavailable', python_version: 'Unavailable', python_impl: 'Unavailable',
      system_uptime_sec: null, system_uptime_str: 'Unavailable',
      backend_uptime_sec: null, backend_uptime_str: 'Unavailable' },
    memory: { system_total_bytes: null, system_total_str: 'Unavailable',
      system_used_bytes: null, system_used_str: 'Unavailable',
      system_available_bytes: null, system_available_str: 'Unavailable',
      system_free_bytes: null, system_free_str: 'Unavailable', system_percent: null,
      container_limit: { is_limited: false, memory_limited: false, cpu_limited: false,
        limit_bytes: null, limit_str: 'Unavailable', usage_bytes: null, usage_str: null,
        cpu_quota_cores: null, cpu_quota_str: 'Unavailable', type: 'Unavailable',
        description: 'Container metrics unavailable' },
      process_rss_bytes: null, process_rss_str: 'Unavailable', process_percent: null },
    disk: { total_bytes: null, total_str: 'Unavailable', used_bytes: null,
      used_str: 'Unavailable', free_bytes: null, free_str: 'Unavailable', percent: null,
      mount_point: '/', fs_type: 'Unavailable', writable: false,
      is_persistent: false, persistence_verified: false,
      storage_mode: 'unavailable', decision_branch: 'UNAVAILABLE', summary: reason },
    storage: null,
    binaries: { ffmpeg: { installed: false, version: 'Unavailable', path: null },
      ffprobe: { installed: false, version: 'Unavailable', path: null },
      aria2: { installed: false, version: 'Unavailable', path: null },
      rclone: { installed: false, version: 'Unavailable', path: null } },
    env_vars: [],
  };
}

export interface CreateApiAppOptions {
  cooldownMs?: number;
  getWorkerProcess?: () => { killed?: boolean; kill: (signal?: NodeJS.Signals) => boolean } | null;
  fetchImpl?: typeof fetch;
}

export function createApiApp(options: CreateApiAppOptions = {}) {
  const app = express();
  const cooldownMs = options.cooldownMs ?? 15000;
  const getWorker = options.getWorkerProcess ?? (() => pythonWorkerProcess);
  const fetchFn = options.fetchImpl ?? fetch;

  app.use(
    express.json({
      verify: (req: any, _res, buf) => {
        req.rawBody = Buffer.from(buf);
      },
    })
  );

  // Enable CORS
  app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization');
    res.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    if (req.method === 'OPTIONS') {
      return res.sendStatus(200);
    }
    next();
  });

  // POST /api/admin/restart-bot: authenticated restart of the Discord bot.
  // Configure UBAGEN_ADMIN_TOKEN in the host's server-side secrets; never put it in frontend code.
  app.post('/api/admin/restart-bot', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');

    // Strictly require UBAGEN_ADMIN_TOKEN; never fall back to HOST or other env vars.
    const expectedToken = (process.env.UBAGEN_ADMIN_TOKEN || '').trim();
    if (!expectedToken) {
      return res.status(503).json({
        error: 'Bot restart is disabled: UBAGEN_ADMIN_TOKEN is not configured on the server.',
      });
    }

    const authorization = req.header('authorization');
    if (!verifyAdminToken(authorization, expectedToken)) {
      res.setHeader('WWW-Authenticate', 'Bearer');
      return res.status(401).json({
        error: 'Unauthorized. The admin token is missing or invalid.',
      });
    }

    const now = Date.now();
    if (isRestartingBot) {
      const retrySec = Math.max(1, Math.ceil(cooldownMs / 1000));
      res.setHeader('Retry-After', String(retrySec));
      return res.status(429).json({
        error: `A bot restart is already in progress. Wait ${retrySec} seconds before trying again.`,
        retry_after: retrySec,
      });
    }

    if (lastBotRestartAt > 0 && now - lastBotRestartAt < cooldownMs) {
      const retrySec = Math.max(1, Math.ceil((cooldownMs - (now - lastBotRestartAt)) / 1000));
      res.setHeader('Retry-After', String(retrySec));
      return res.status(429).json({
        error: `A bot restart was requested recently. Wait ${retrySec} seconds before trying again.`,
        retry_after: retrySec,
      });
    }

    isRestartingBot = true;
    try {
      // 1. Prefer delegating to the running Python FastAPI companion so the Discord bot
      //    restarts cleanly in-process without stopping the FastAPI HTTP server.
      const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
      try {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        let upstreamRes: Response;
        try {
          upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/admin/restart-bot`, {
            method: 'POST',
            signal: controller.signal,
            headers: {
              Accept: 'application/json',
              Authorization: `Bearer ${expectedToken}`,
            },
          });
        } finally {
          clearTimeout(timeout);
        }

        const contentType = upstreamRes.headers.get('content-type') || '';
        const payload = contentType.includes('application/json')
          ? await upstreamRes.json().catch(() => ({}))
          : {};

        if (upstreamRes.status === 202 || upstreamRes.ok) {
          lastBotRestartAt = Date.now();
          statsCache = null;
          return res.status(202).json({
            status: payload.status || 'restarting',
            message: payload.message || 'Bot restart requested. The worker should reconnect shortly.',
          });
        }

        if (upstreamRes.status === 429 || upstreamRes.status === 503 || upstreamRes.status === 401) {
          const retryAfter = upstreamRes.headers.get('retry-after');
          if (retryAfter) {
            res.setHeader('Retry-After', retryAfter);
          }
          return res.status(upstreamRes.status).json(
            payload && payload.error
              ? payload
              : { error: `Bot restart rejected by backend (HTTP ${upstreamRes.status}).` }
          );
        }
      } catch {
        // Companion HTTP endpoint not reachable; fall back to local worker management below
      }

      // 2. Fallback: verify DISCORD_BOT_TOKEN and signal local pythonWorkerProcess
      const botToken = (process.env.DISCORD_BOT_TOKEN || '').trim();
      const isPlaceholder =
        !botToken ||
        botToken === 'your_discord_bot_token_here' ||
        botToken === 'MY_DISCORD_BOT_TOKEN' ||
        botToken === 'CHANGE_ME';
      if (isPlaceholder) {
        return res.status(503).json({
          error: 'Cannot restart Discord bot: DISCORD_BOT_TOKEN is not configured on the server.',
        });
      }

      const worker = getWorker();
      if (!worker || worker.killed) {
        return res.status(503).json({
          error: 'The bot worker is not currently available to restart.',
        });
      }

      const sent = worker.kill('SIGTERM');
      if (!sent) {
        return res.status(500).json({
          error: 'Could not send the restart signal to the bot worker.',
        });
      }

      lastBotRestartAt = Date.now();
      statsCache = null;
      return res.status(202).json({
        status: 'restarting',
        message: 'Bot restart requested. The worker should reconnect shortly.',
      });
    } finally {
      isRestartingBot = false;
    }
  });

  // Reject disallowed HTTP methods on /api/admin/restart-bot
  app.all('/api/admin/restart-bot', (req, res) => {
    res.setHeader('Allow', 'POST, OPTIONS');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(405).json({
      error: 'Method not allowed. Use POST /api/admin/restart-bot.',
    });
  });

  // POST /api/discord/wake: Authenticated wake-on-command endpoint
  app.post('/api/discord/wake', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const expectedToken = (process.env.UBAGEN_ADMIN_TOKEN || '').trim();
    if (!expectedToken) {
      return res.status(503).json({
        error: 'Bot wake endpoint is disabled: UBAGEN_ADMIN_TOKEN is not configured on the server.',
      });
    }
    const authorization = req.header('authorization');
    if (!verifyAdminToken(authorization, expectedToken)) {
      res.setHeader('WWW-Authenticate', 'Bearer');
      return res.status(401).json({
        error: 'Unauthorized. The admin token is missing or invalid.',
      });
    }

    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3500);
      let upstreamRes: Response;
      try {
        upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/discord/wake`, {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            Authorization: `Bearer ${expectedToken}`,
          },
          body: JSON.stringify(req.body || {}),
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timeout);
      }
      const payload = (await upstreamRes.json().catch(() => null)) as Record<string, any> | null;
      if (upstreamRes.status === 429 && payload?.retry_after) {
        res.setHeader('Retry-After', String(payload.retry_after));
      }
      statsCache = null;
      return res.status(upstreamRes.status).json(
        payload || { status: upstreamRes.ok ? 'waking' : 'error' }
      );
    } catch {
      const botToken = (process.env.DISCORD_BOT_TOKEN || '').trim();
      const isPlaceholder =
        !botToken ||
        botToken === 'your_discord_bot_token_here' ||
        botToken === 'MY_DISCORD_BOT_TOKEN' ||
        botToken === 'CHANGE_ME';
      if (isPlaceholder) {
        return res.status(503).json({
          error: 'Cannot wake Discord bot: DISCORD_BOT_TOKEN is not configured on the server.',
        });
      }
      if (!options.getWorkerProcess) {
        ensurePythonWorker();
      }
      statsCache = null;
      return res.status(202).json({
        status: 'waking',
        woke: true,
        ready: false,
        message: 'Wake-on-command accepted. Discord bot worker is reconnecting to Gateway.',
      });
    }
  });

  app.all('/api/discord/wake', (_req, res) => {
    res.setHeader('Allow', 'POST, OPTIONS');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(405).json({
      error: 'Method not allowed. Use POST /api/discord/wake.',
    });
  });

  // POST /api/discord/interactions: Discord Signed HTTP Interactions & Wake-on-Command Endpoint
  app.post(['/api/discord/interactions', '/discord/interactions'], async (req: any, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const sig = req.header('x-signature-ed25519') || '';
    const ts = req.header('x-signature-timestamp') || '';
    const rawBuf: Buffer = Buffer.isBuffer(req.rawBody)
      ? req.rawBody
      : Buffer.from(typeof req.body === 'object' ? JSON.stringify(req.body) : String(req.body || ''), 'utf8');

    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2800);
      let upstreamRes: Response;
      try {
        upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/discord/interactions`, {
          method: 'POST',
          headers: {
            Accept: 'application/json',
            'Content-Type': 'application/json',
            'X-Signature-Ed25519': sig,
            'X-Signature-Timestamp': ts,
          },
          body: new Uint8Array(rawBuf),
          signal: controller.signal,
        });
      } finally {
        clearTimeout(timeout);
      }
      const payload = await upstreamRes.json().catch(() => ({ error: 'Invalid response' }));
      return res.status(upstreamRes.status).json(payload);
    } catch {
      // Fallback: execute Python discord_interactions handler directly
      const b64Body = rawBuf.toString('base64');
      execFile(
        'python3',
        [
          '-B',
          '-c',
          'import asyncio, base64, json, sys; from discord_interactions import handle_discord_interaction_request; raw = base64.b64decode(sys.argv[1]); sig = sys.argv[2]; ts = sys.argv[3]; code, out = asyncio.run(handle_discord_interaction_request(raw_body=raw, signature_header=sig, timestamp_header=ts, app=None)); print(json.dumps({"code": code, "payload": out}))',
          b64Body,
          sig,
          ts,
        ],
        { timeout: 2800, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
        (error, stdout) => {
          if (!error && stdout) {
            try {
              const parsed = JSON.parse(stdout);
              if (parsed.code === 200) {
                ensurePythonWorker();
              }
              return res.status(parsed.code || 500).json(parsed.payload || {});
            } catch {}
          }
          return res.status(500).json({ error: 'Discord interaction handler unavailable.' });
        }
      );
    }
  });

  app.all(['/api/discord/interactions', '/discord/interactions'], (_req, res) => {
    res.setHeader('Allow', 'POST, OPTIONS');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(405).json({
      error: 'Method not allowed. Use POST /api/discord/interactions.',
    });
  });

  // GET /api/oauth/google/status: Non-sensitive Google Drive OAuth 2.0 configuration status
  app.get('/api/oauth/google/status', async (_req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1500);
      try {
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/oauth/google/status`, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (upstreamRes.ok) {
          return res.json(await upstreamRes.json());
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      ['-B', '-c', 'import json; from google_drive_oauth import get_drive_oauth_config_status; print(json.dumps(get_drive_oauth_config_status()))'],
      { timeout: 3000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(200).json({ configured: false });
      }
    );
  });

  // GET /api/oauth/google/callback: Google Drive OAuth 2.0 Authorization Code Callback
  app.get(['/api/oauth/google/callback', '/oauth/google/callback'], async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const state = typeof req.query.state === 'string' ? req.query.state : '';
    const code = typeof req.query.code === 'string' ? req.query.code : '';
    const error = typeof req.query.error === 'string' ? req.query.error : '';
    const errorDescription = typeof req.query.error_description === 'string' ? req.query.error_description : '';
    const format = typeof req.query.format === 'string' ? req.query.format : '';

    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    const qs = new URLSearchParams();
    if (state) qs.set('state', state);
    if (code) qs.set('code', code);
    if (error) qs.set('error', error);
    if (errorDescription) qs.set('error_description', errorDescription);
    if (format) qs.set('format', format);

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const upstreamRes = await fetchFn(
          `${standaloneBackendUrl}/api/oauth/google/callback${qs.toString() ? `?${qs.toString()}` : ''}`,
          {
            method: 'GET',
            signal: controller.signal,
            headers: {
              Accept: req.header('accept') || 'text/html,application/json',
            },
          }
        );
        const contentType = upstreamRes.headers.get('content-type') || '';
        const textBody = await upstreamRes.text();
        res.status(upstreamRes.status);
        if (contentType) {
          res.setHeader('Content-Type', contentType);
        }
        return res.send(textBody);
      } finally {
        clearTimeout(timeout);
      }
    } catch {
      execFile(
        'python3',
        [
          '-B',
          '-c',
          'import json, sys; from google_drive_oauth import handle_google_oauth_callback_request; st, js, ht = handle_google_oauth_callback_request(state=sys.argv[1] or None, code=sys.argv[2] or None, error=sys.argv[3] or None, error_description=sys.argv[4] or None); print(json.dumps({"status_code": st, "json": js, "html": ht}))',
          state,
          code,
          error,
          errorDescription,
        ],
        { timeout: 10000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
        (err, stdout) => {
          if (!err && stdout) {
            try {
              const parsed = JSON.parse(stdout);
              const acceptHdr = String(req.header('accept') || '').toLowerCase();
              const wantsJson =
                format.toLowerCase() === 'json' ||
                (acceptHdr.includes('application/json') && !acceptHdr.includes('text/html'));
              if (wantsJson) {
                return res.status(parsed.status_code || 400).json(parsed.json || {});
              }
              res.setHeader('Content-Type', 'text/html; charset=utf-8');
              return res.status(parsed.status_code || 400).send(parsed.html || '');
            } catch {}
          }
          return res.status(500).json({ status: 'error', error: 'oauth_callback_unavailable' });
        }
      );
    }
  });

  app.all(['/api/oauth/google/callback', '/oauth/google/callback'], (_req, res) => {
    res.setHeader('Allow', 'GET, OPTIONS');
    res.setHeader('Cache-Control', 'no-store');
    return res.status(405).json({
      error: 'Method not allowed. Use GET /api/oauth/google/callback.',
    });
  });

  // GET /api/stats: Fetches live hardware metrics from backend
  app.get(['/api/stats', '/stats'], async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
    res.setHeader('Pragma', 'no-cache');

    const bypassCache = req.query.fresh === '1' || req.query.fresh === 'true';

    // Share a short-lived snapshot between dashboard polls to avoid repeated subprocess work.
    if (!bypassCache && statsCache && Date.now() < statsCache.expiresAt) {
      return res.json(statsCache.data);
    }

    // 1. If an upstream Python service is running on BACKEND_URL or http://127.0.0.1:5050, query it
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1200);
      let upstreamRes: Response;
      try {
        upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/stats`, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
      } finally {
        clearTimeout(timeout);
      }

      const contentType = upstreamRes.headers.get('content-type') || '';
      if (upstreamRes.ok && contentType.includes('application/json')) {
        const data = await upstreamRes.json();
        statsCache = { expiresAt: Date.now() + 3000, data };
        return res.json(data);
      }
    } catch {
      // Standalone backend not responding on 5050; fall back to direct stats.py execution
    }

    // 2. Direct measurement via Python on the local machine
    const scriptPath = path.join(__dirname, 'stats.py');
    execFile(
      'python3',
      ['-B', scriptPath, '--json'],
      { timeout: 3500, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout, stderr) => {
      if (!error && stdout) {
        try {
          const stats = JSON.parse(stdout);
          statsCache = { expiresAt: Date.now() + 3000, data: stats };
          return res.json(stats);
        } catch (parseError) {
          console.error('Failed to parse stats.py output:', parseError, stdout);
        }
      }

      const reason = 'Backend statistics collection is temporarily unavailable.';
      console.error('stats.py execution error:', error?.message || stderr || 'Unknown error');
      // Return a valid dashboard payload instead of HTTP 503; the next poll retries.
      const fallback = degradedStats(reason);
      statsCache = { expiresAt: Date.now() + 1000, data: fallback };
      return res.status(200).json(fallback);
    });
  });

  // GET /health: Health probe
  app.get('/health', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');

    // Attempt upstream check first
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1000);
      const upstreamRes = await fetchFn(`${standaloneBackendUrl}/health`, {
        signal: controller.signal,
        headers: { Accept: 'application/json' },
      });
      clearTimeout(timeout);
      const contentType = upstreamRes.headers.get('content-type') || '';
      if (upstreamRes.ok && contentType.includes('application/json')) {
        const data = await upstreamRes.json();
        return res.json(data);
      }
    } catch {}

    res.json({
      status: 'healthy',
      service: 'ubagen-backend',
      http: 'alive',
      http_alive: true,
      discord_gateway_connected: false,
      discord_bot_ready: false,
      timestamp: new Date().toISOString(),
    });
  });

  // GET /api/storage: Storage infrastructure audit and stored output files
  app.get('/api/storage', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1500);
      try {
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/storage`, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (upstreamRes.ok) {
          const data = await upstreamRes.json();
          return res.json(data);
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      [
        '-B',
        '-c',
        'import json; from storage_manager import get_storage_manager; from job_store import get_job_store; m = get_storage_manager(); s = get_job_store(); s.cleanup_expired_jobs(); print(json.dumps({"status":"online","storage":m.inspect(),"outputs":m.list_output_files(),"jobs":[j.to_public_dict() for j in s.list_jobs(limit=25)]}))',
      ],
      { timeout: 3000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(200).json({ status: 'degraded', storage: null, outputs: [], jobs: [] });
      }
    );
  });

  // GET /api/jobs: List persistent video encode jobs
  app.get('/api/jobs', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    const qs = new URLSearchParams();
    if (typeof req.query.user_id === 'string') qs.set('user_id', req.query.user_id);
    if (typeof req.query.status === 'string') qs.set('status', req.query.status);
    if (typeof req.query.limit === 'string') qs.set('limit', req.query.limit);
    const querySuffix = qs.toString() ? `?${qs.toString()}` : '';

    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1500);
      try {
        const headers: Record<string, string> = { Accept: 'application/json' };
        const xUser = req.header('x-user-id');
        if (xUser) headers['X-User-Id'] = xUser;
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/jobs${querySuffix}`, {
          signal: controller.signal,
          headers,
        });
        if (upstreamRes.ok) {
          const data = await upstreamRes.json();
          return res.json(data);
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    const reqUser = (req.header('x-user-id') || (typeof req.query.user_id === 'string' ? req.query.user_id : '') || '').trim();
    execFile(
      'python3',
      [
        '-B',
        '-c',
        'import json, sys; from job_store import get_job_store; s = get_job_store(); s.cleanup_expired_jobs(); u = sys.argv[1] or None; jobs = s.list_jobs(user_id=u, limit=50); print(json.dumps({"status":"online","backend":s.get_backend_status(),"count":len(jobs),"jobs":[j.to_public_dict(requester_user_id=u) for j in jobs]}))',
        reqUser,
      ],
      { timeout: 3000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(200).json({ status: 'online', count: 0, jobs: [] });
      }
    );
  });

  // POST /api/jobs/cleanup-expired: Server-side 7200s retention cleanup trigger
  app.post('/api/jobs/cleanup-expired', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 3000);
      try {
        const headers: Record<string, string> = { Accept: 'application/json' };
        const auth = req.header('authorization');
        if (auth) headers.Authorization = auth;
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/jobs/cleanup-expired`, {
          method: 'POST',
          signal: controller.signal,
          headers,
        });
        const data = await upstreamRes.json().catch(() => ({}));
        return res.status(upstreamRes.status).json(data);
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      [
        '-B',
        '-c',
        'import json; from job_store import get_job_store; s = get_job_store(); c = s.cleanup_expired_jobs(); r = s.recover_stale_running_jobs(); print(json.dumps({"status":"ok","cleanup":c,"recovered_crashed_jobs":[j.job_id for j in r]}))',
      ],
      { timeout: 4000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(500).json({ error: 'Cleanup execution failed.' });
      }
    );
  });

  // GET /api/jobs/:jobId/download: User-authorized 7200s GCS video output download
  app.get('/api/jobs/:jobId/download', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const jobId = (req.params.jobId || '').trim();
    const userId = (
      (typeof req.query.user_id === 'string' ? req.query.user_id : '') ||
      req.header('x-user-id') ||
      ''
    ).trim();
    const token = (
      (typeof req.query.token === 'string' ? req.query.token : '') ||
      (req.header('authorization') || '').replace(/^Bearer\s+/i, '') ||
      ''
    ).trim();

    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    const qs = new URLSearchParams();
    if (userId) qs.set('user_id', userId);
    if (token) qs.set('token', token);
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const upstreamRes = await fetchFn(
          `${standaloneBackendUrl}/api/jobs/${encodeURIComponent(jobId)}/download?${qs.toString()}`,
          { signal: controller.signal }
        );
        const contentType = upstreamRes.headers.get('content-type') || 'application/json';
        if (contentType.includes('application/json')) {
          const payload = await upstreamRes.json().catch(() => ({}));
          return res.status(upstreamRes.status).json(payload);
        }
        const buf = Buffer.from(await upstreamRes.arrayBuffer());
        const disp = upstreamRes.headers.get('content-disposition');
        if (disp) res.setHeader('Content-Disposition', disp);
        res.setHeader('Content-Type', contentType);
        return res.status(upstreamRes.status).send(buf);
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    return res.status(503).json({ error: 'Backend download service temporarily unavailable.' });
  });

  // GET /api/logs: Redacted real-time backend logs
  app.get('/api/logs', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const limit = Math.max(1, Math.min(Number(req.query.limit) || 100, 250));
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1200);
      try {
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}/api/logs?limit=${limit}`, {
          signal: controller.signal,
          headers: { Accept: 'application/json' },
        });
        if (upstreamRes.ok) {
          const data = await upstreamRes.json();
          return res.json(data);
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    return res.json({ status: 'online', count: 0, logs: [] });
  });

  // GET /api/diagnostics/log: Comprehensive redacted diagnostic report (/log equivalent)
  app.get('/api/diagnostics/log', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const liveTests = req.query.live_tests === '1' || req.query.live_tests === 'true';
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 5000);
      try {
        const upstreamRes = await fetchFn(
          `${standaloneBackendUrl}/api/diagnostics/log?live_tests=${liveTests ? 'true' : 'false'}`,
          { signal: controller.signal, headers: { Accept: 'application/json' } }
        );
        if (upstreamRes.ok) {
          return res.json(await upstreamRes.json());
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      [
        '-B',
        '-c',
        'import json, sys; from rclone_manager import collect_log_diagnostics, format_log_diagnostics_report; d = collect_log_diagnostics(run_live_remote_tests=(sys.argv[1]=="1")); print(json.dumps({"status":"online","diagnostics":d,"formatted_report":format_log_diagnostics_report(d)}))',
        liveTests ? '1' : '0',
      ],
      { timeout: 6000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(200).json({ status: 'degraded', diagnostics: null, formatted_report: 'Tanılama verisi alınamadı.' });
      }
    );
  });

  // GET /api/rclone/status: Multi-account MEGA & Google Drive Rclone status
  app.get('/api/rclone/status', async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    const refresh = req.query.refresh === '1' || req.query.refresh === 'true';
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 4000);
      try {
        const adminToken = process.env.UBAGEN_ADMIN_TOKEN?.trim();
        const authHeader = req.headers.authorization || (adminToken ? `Bearer ${adminToken}` : undefined);
        const headers: Record<string, string> = { Accept: 'application/json' };
        if (authHeader) {
          headers.Authorization = authHeader;
        }
        const upstreamRes = await fetchFn(
          `${standaloneBackendUrl}/api/rclone/status?refresh=${refresh ? 'true' : 'false'}`,
          { signal: controller.signal, headers }
        );
        if (upstreamRes.ok) {
          return res.json(await upstreamRes.json());
        }
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      [
        '-B',
        '-c',
        'import json, sys; from rclone_manager import get_rclone_manager; m = get_rclone_manager(); print(json.dumps(m.get_status_summary(refresh_storage=(sys.argv[1]=="1"))))',
        refresh ? '1' : '0',
      ],
      { timeout: 6000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        if (!error && stdout) {
          try {
            return res.json(JSON.parse(stdout));
          } catch {}
        }
        return res.status(200).json({
          rclone_binary: { installed: false, version: 'Unavailable', path: null },
          accounts: [],
          storage_summary: { total_accounts: 0, connected_accounts: 0, enabled_accounts: 0, total_bytes: 0, used_bytes: 0, free_bytes: 0 },
          pending_deletions: [],
          pending_deletions_count: 0,
        });
      }
    );
  });

  // Helper to proxy or execute Rclone POST mutations
  const proxyOrExecRclonePost = async (
    req: express.Request,
    res: express.Response,
    upstreamPath: string,
    pythonSnippet: string,
    pyArgs: string[],
    successStatus = 200
  ) => {
    res.setHeader('Cache-Control', 'no-store');
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const adminToken = process.env.UBAGEN_ADMIN_TOKEN?.trim();
        const authHeader = req.headers.authorization || (adminToken ? `Bearer ${adminToken}` : undefined);
        const headers: Record<string, string> = {
          Accept: 'application/json',
          'Content-Type': 'application/json',
        };
        if (authHeader) {
          headers.Authorization = authHeader;
        }
        const upstreamRes = await fetchFn(`${standaloneBackendUrl}${upstreamPath}`, {
          method: 'POST',
          signal: controller.signal,
          headers,
          body: JSON.stringify(req.body || {}),
        });
        const payload = await upstreamRes.json().catch(() => ({}));
        statsCache = null;
        return res.status(upstreamRes.status).json(payload);
      } finally {
        clearTimeout(timeout);
      }
    } catch {}

    execFile(
      'python3',
      ['-B', '-c', pythonSnippet, ...pyArgs],
      { timeout: 12000, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } },
      (error, stdout) => {
        statsCache = null;
        if (!error && stdout) {
          try {
            const parsed = JSON.parse(stdout);
            const code = parsed._http_status || (parsed.ok ? successStatus : 400);
            delete parsed._http_status;
            return res.status(code).json(parsed);
          } catch {}
        }
        return res.status(500).json({ ok: false, error: 'Rclone işlemi yürütülemedi.' });
      }
    );
  };

  app.post('/api/rclone/accounts/mega', (req, res) => {
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      '/api/rclone/accounts/mega',
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[1]); ok, out = get_rclone_manager().add_mega_account(display_name=str(b.get("display_name") or ""), email=str(b.get("email") or ""), password=str(b.get("password") or ""), remote_name=str(b.get("remote_name") or "").strip() or None, root_folder=str(b.get("root_folder") or "UBAgen_Outputs").strip(), enabled=bool(b.get("enabled", True)), skip_verify=bool(b.get("skip_verify", False))); out["_http_status"] = 201 if ok else 400; print(json.dumps(out))',
      [bodyJson],
      201
    );
  });

  app.post('/api/rclone/accounts/gdrive/start', (req, res) => {
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      '/api/rclone/accounts/gdrive/start',
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[1]); ok, out = get_rclone_manager().start_gdrive_auth_session(display_name=str(b.get("display_name") or "Google Drive"), remote_name=str(b.get("remote_name") or "").strip() or None, scope=str(b.get("scope") or "drive.file").strip(), root_folder=str(b.get("root_folder") or "UBAgen_Outputs").strip()); out["_http_status"] = 200 if ok else 400; print(json.dumps(out))',
      [bodyJson],
      200
    );
  });

  app.post('/api/rclone/accounts/gdrive/complete', (req, res) => {
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      '/api/rclone/accounts/gdrive/complete',
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[1]); ok, out = get_rclone_manager().complete_gdrive_auth(session_id=str(b.get("session_id") or "").strip() or None, remote_name=str(b.get("remote_name") or "").strip() or None, display_name=str(b.get("display_name") or "").strip() or None, code_or_token=str(b.get("code_or_token") or b.get("token") or b.get("code") or ""), root_folder=str(b.get("root_folder") or "UBAgen_Outputs").strip(), scope=str(b.get("scope") or "drive.file").strip(), skip_verify=bool(b.get("skip_verify", False))); out["_http_status"] = 200 if ok else 400; print(json.dumps(out))',
      [bodyJson],
      200
    );
  });

  app.post('/api/rclone/accounts/:remoteName/test', (req, res) => {
    const remoteName = String(req.params.remoteName || '').trim();
    return proxyOrExecRclonePost(
      req,
      res,
      `/api/rclone/accounts/${encodeURIComponent(remoteName)}/test`,
      'import json, sys; from rclone_manager import get_rclone_manager; ok, out = get_rclone_manager().test_account_connection(sys.argv[1]); out["_http_status"] = 200 if ok else 400; print(json.dumps(out))',
      [remoteName],
      200
    );
  });

  app.post('/api/rclone/accounts/:remoteName/reconnect', (req, res) => {
    const remoteName = String(req.params.remoteName || '').trim();
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      `/api/rclone/accounts/${encodeURIComponent(remoteName)}/reconnect`,
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[2]); ok, out = get_rclone_manager().reconnect_account(sys.argv[1], password=str(b.get("password") or "").strip() or None, code_or_token=str(b.get("code_or_token") or b.get("token") or "").strip() or None, email=str(b.get("email") or "").strip() or None, skip_verify=bool(b.get("skip_verify", False))); out["_http_status"] = 200 if ok else 400; print(json.dumps(out))',
      [remoteName, bodyJson],
      200
    );
  });

  app.post('/api/rclone/accounts/:remoteName/disconnect', (req, res) => {
    const remoteName = String(req.params.remoteName || '').trim();
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      `/api/rclone/accounts/${encodeURIComponent(remoteName)}/disconnect`,
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[2]); ok, out = get_rclone_manager().disconnect_account(sys.argv[1], remove_record=bool(b.get("remove_record", False))); out["_http_status"] = 200 if ok else 404; print(json.dumps(out))',
      [remoteName, bodyJson],
      200
    );
  });

  app.post('/api/rclone/accounts/:remoteName/toggle', (req, res) => {
    const remoteName = String(req.params.remoteName || '').trim();
    const bodyJson = JSON.stringify(req.body || {});
    return proxyOrExecRclonePost(
      req,
      res,
      `/api/rclone/accounts/${encodeURIComponent(remoteName)}/toggle`,
      'import json, sys; from rclone_manager import get_rclone_manager; b = json.loads(sys.argv[2]); ok, out = get_rclone_manager().set_account_enabled(sys.argv[1], bool(b.get("enabled", True))); out["_http_status"] = 200 if ok else 404; print(json.dumps(out))',
      [remoteName, bodyJson],
      200
    );
  });

  app.post('/api/rclone/pending-deletions/process', (req, res) => {
    return proxyOrExecRclonePost(
      req,
      res,
      '/api/rclone/pending-deletions/process',
      'import json; from rclone_manager import get_rclone_manager; out = get_rclone_manager().process_pending_deletions(); out["_http_status"] = 200; print(json.dumps(out))',
      [],
      200
    );
  });

  // GET /api/outputs/:filename: Serve stored video or subtitle output file with Range support
  app.get('/api/outputs/:filename', (req, res) => {
    const rawName = (req.params.filename || '').trim();
    if (!rawName || rawName.includes('/') || rawName.includes('\\') || rawName.includes('..') || rawName.startsWith('.')) {
      return res.status(404).json({ error: 'Requested output file was not found or has expired.' });
    }
    const baseOutDir = path.resolve(
      process.env.UBAGEN_OUTPUT_DIR ||
        path.join(process.env.UBAGEN_STORAGE_DIR || __dirname, 'video_outputs')
    );
    const resolvedFile = path.resolve(baseOutDir, rawName);
    if (path.dirname(resolvedFile) !== baseOutDir || !fs.existsSync(resolvedFile) || !fs.statSync(resolvedFile).isFile()) {
      return res.status(404).json({ error: 'Requested output file was not found or has expired.' });
    }
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Content-Disposition', `attachment; filename="${path.basename(resolvedFile)}"`);
    return res.sendFile(resolvedFile);
  });

  return app;
}

async function startServer() {
  const isProduction = process.env.NODE_ENV === 'production';
  const nginxPort = (process.env.NGINX_PORT || '').trim();
  const rawPort = (process.env.PORT || '8080').trim();
  const defaultAppPort = (process.env.DEFAULT_APP_PORT || '3000').trim();
  const PORT =
    !isProduction || (nginxPort && nginxPort === rawPort)
      ? parseInt(defaultAppPort || '3000', 10)
      : parseInt(rawPort || '8080', 10);

  // Launch background Python Discord & FastAPI companion worker
  ensurePythonWorker();

  const app = createApiApp();

  // Mount Vite middlewares in development or serve dist in production
  if (!isProduction) {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    app.use(express.static(path.join(__dirname, 'dist')));
    app.get('*', (req, res) => {
      res.sendFile(path.join(__dirname, 'dist', 'index.html'));
    });
  }

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`UBAgen System Monitor running on http://0.0.0.0:${PORT}`);
  });
}

const isMainModule =
  Boolean(process.argv[1]) && path.resolve(process.argv[1]) === path.resolve(__filename);
if (isMainModule) {
  startServer();
}
