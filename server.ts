/**
 * UBAgen — server.ts
 * Express full-stack server running Vite in development and serving /api/stats and /health.
 */

import express from 'express';
import { createServer as createViteServer } from 'vite';
import { execFile, spawn, ChildProcess } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

let pythonWorkerProcess: ChildProcess | null = null;
let statsCache: { expiresAt: number; data: unknown } | null = null;

function ensurePythonWorker() {
  if (pythonWorkerProcess) return;

  try {
    pythonWorkerProcess = spawn('python3', ['main.py'], {
      env: {
        ...process.env,
        UBAGEN_HTTP_PORT: '5050',
        PORT: '5050',
      },
      stdio: 'ignore',
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

    process.on('exit', () => {
      if (pythonWorkerProcess) {
        try {
          pythonWorkerProcess.kill();
        } catch {}
      }
    });
  } catch (err) {
    console.warn('Could not launch Python worker:', err);
  }
}

function degradedStats(reason: string) {
  return {
    status: 'degraded',
    timestamp: new Date().toISOString(),
    diagnostic_message: reason,
    bot: { status: 'unavailable', connected: false, is_ready: false,
      diagnostic_message: 'Bot statistics are temporarily unavailable.', latency_ms: null,
      user: 'Unavailable', guild_count: 0, uptime_seconds: null, uptime_str: 'Unavailable',
      sync_status: null, client_id: null, invite_url: null },
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
      used_str: 'Unavailable', free_bytes: null, free_str: 'Unavailable', percent: null },
    binaries: { ffmpeg: { installed: false, version: 'Unavailable', path: null },
      aria2: { installed: false, version: 'Unavailable', path: null } },
    env_vars: [],
  };
}

async function startServer() {
  const app = express();
  const isProduction = process.env.NODE_ENV === 'production';
  const PORT = isProduction ? parseInt(process.env.PORT || '8080', 10) : 3000;

  // Launch background Python Discord & FastAPI companion worker
  ensurePythonWorker();

  app.use(express.json());

  // Enable CORS
  app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept');
    res.header('Access-Control-Allow-Methods', 'GET, OPTIONS');
    if (req.method === 'OPTIONS') {
      return res.sendStatus(200);
    }
    next();
  });

  // GET /api/stats: Fetches live hardware metrics from backend
  app.get(['/api/stats', '/stats'], async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
    res.setHeader('Pragma', 'no-cache');

    // Share a short-lived snapshot between dashboard polls to avoid repeated subprocess work.
    if (statsCache && Date.now() < statsCache.expiresAt) {
      return res.json(statsCache.data);
    }

    // 1. If an upstream Python service is running on BACKEND_URL or http://127.0.0.1:5050, query it
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:5050';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1200);
      let upstreamRes: Response;
      try {
        upstreamRes = await fetch(`${standaloneBackendUrl}/api/stats`, {
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
    execFile('python3', [scriptPath, '--json'], { timeout: 3500 }, (error, stdout, stderr) => {
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
      const upstreamRes = await fetch(`${standaloneBackendUrl}/health`, {
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
      timestamp: new Date().toISOString(),
    });
  });

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

startServer();
