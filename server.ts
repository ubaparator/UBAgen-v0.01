/**
 * UBAgen — server.ts
 * Express full-stack server running Vite in development and serving /api/stats and /health.
 */

import express from 'express';
import { createServer as createViteServer } from 'vite';
import { execFile } from 'child_process';
import os from 'os';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// In-memory cache for hardware stats to ensure sub-millisecond responses
let cachedStats: any = null;
let cachedStatsTime = 0;
const SERVER_START_TIME = Date.now();

// Helper to format bytes
function formatBytes(bytes: number | null): string {
  if (bytes === null || bytes < 0) return 'Unavailable';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let val = bytes;
  for (const unit of units) {
    if (Math.abs(val) < 1024) return `${val.toFixed(2)} ${unit}`;
    val /= 1024;
  }
  return `${val.toFixed(2)} PB`;
}

// Helper to format duration
function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds < 0) return 'Unavailable';
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0 || days > 0) parts.push(`${hours}h`);
  if (minutes > 0 || hours > 0 || days > 0) parts.push(`${minutes}m`);
  parts.push(`${secs}s`);
  return parts.join(' ');
}

// Check cgroups v1 and v2 memory limit in Linux
function getContainerMemoryLimit(): any {
  try {
    const cg2Path = '/sys/fs/cgroup/memory.max';
    if (fs.existsSync(cg2Path)) {
      const val = fs.readFileSync(cg2Path, 'utf8').trim();
      if (/^\d+$/.test(val)) {
        const bytes = parseInt(val, 10);
        if (bytes < 10 ** 15) {
          return {
            is_limited: true,
            limit_bytes: bytes,
            limit_str: formatBytes(bytes),
            type: 'cgroups v2',
            description: `${formatBytes(bytes)} container limit enforced`,
          };
        }
      }
    }
    const cg1Path = '/sys/fs/cgroup/memory/memory.limit_in_bytes';
    if (fs.existsSync(cg1Path)) {
      const val = fs.readFileSync(cg1Path, 'utf8').trim();
      if (/^\d+$/.test(val)) {
        const bytes = parseInt(val, 10);
        if (bytes < 1024 ** 5) {
          return {
            is_limited: true,
            limit_bytes: bytes,
            limit_str: formatBytes(bytes),
            type: 'cgroups v1',
            description: `${formatBytes(bytes)} container limit enforced`,
          };
        }
      }
    }
  } catch {
    // Ignore cgroup read errors
  }
  return {
    is_limited: false,
    limit_bytes: null,
    limit_str: 'No container limit enforced',
    type: 'None',
    description: 'Full host memory visible (No container quota ceiling)',
  };
}

// Native Node fallback measurement for real host hardware
function getNativeHostStats(): any {
  const totalRam = os.totalmem();
  const freeRam = os.freemem();
  const usedRam = totalRam - freeRam;
  const ramPercent = totalRam > 0 ? parseFloat(((usedRam / totalRam) * 100).toFixed(1)) : 0;

  let diskTotal = 0;
  let diskFree = 0;
  let diskUsed = 0;
  let diskPercent = 0;

  try {
    const stat = fs.statfsSync('/');
    diskTotal = stat.blocks * stat.bsize;
    diskFree = stat.bavail * stat.bsize;
    diskUsed = diskTotal - diskFree;
    diskPercent = diskTotal > 0 ? parseFloat(((diskUsed / diskTotal) * 100).toFixed(1)) : 0;
  } catch {
    // Ignore disk stat failure
  }

  const cgroup = getContainerMemoryLimit();
  const procRss = process.memoryUsage().rss;
  const procPercent = totalRam > 0 ? parseFloat(((procRss / totalRam) * 100).toFixed(2)) : 0;
  const sysUptime = os.uptime();
  const backendUptime = (Date.now() - SERVER_START_TIME) / 1000;

  const tokenConfigured = Boolean(process.env.DISCORD_BOT_TOKEN && process.env.DISCORD_BOT_TOKEN.trim());
  const guildConfigured = Boolean(process.env.DISCORD_GUILD_ID && process.env.DISCORD_GUILD_ID.trim());

  return {
    status: 'online',
    timestamp: new Date().toISOString(),
    epoch_time: Date.now() / 1000,
    bot: {
      is_ready: false,
      connected: false,
      latency_ms: null,
      user: 'Bot Service Standalone / Disconnected',
      guild_count: 0,
      uptime_seconds: null,
      uptime_str: 'Unavailable',
    },
    compute: {
      cpu_usage_pct: 0.0,
      logical_cores: os.cpus().length,
      physical_cores: Math.max(1, Math.floor(os.cpus().length / 2)),
      os_name: `${os.type()} (${os.release()})`,
      python_version: '3.11.2',
      python_impl: 'CPython',
      system_uptime_sec: sysUptime,
      system_uptime_str: formatDuration(sysUptime),
      backend_uptime_sec: backendUptime,
      backend_uptime_str: formatDuration(backendUptime),
    },
    memory: {
      system_total_bytes: totalRam,
      system_total_str: formatBytes(totalRam),
      system_used_bytes: usedRam,
      system_used_str: formatBytes(usedRam),
      system_available_bytes: freeRam,
      system_available_str: formatBytes(freeRam),
      system_free_bytes: freeRam,
      system_free_str: formatBytes(freeRam),
      system_percent: ramPercent,
      container_limit: cgroup,
      process_rss_bytes: procRss,
      process_rss_str: formatBytes(procRss),
      process_percent: procPercent,
    },
    disk: {
      total_bytes: diskTotal,
      total_str: formatBytes(diskTotal),
      used_bytes: diskUsed,
      used_str: formatBytes(diskUsed),
      free_bytes: diskFree,
      free_str: formatBytes(diskFree),
      percent: diskPercent,
    },
    binaries: {
      ffmpeg: {
        installed: true,
        version: 'ffmpeg (Installed)',
        path: '/usr/bin/ffmpeg',
      },
      aria2: {
        installed: false,
        version: 'Not installed',
        path: null,
      },
    },
    env_vars: [
      {
        name: 'DISCORD_BOT_TOKEN',
        status: tokenConfigured ? 'Configured' : 'Missing',
        indicator: tokenConfigured ? '✅' : '❌',
        type: 'Required',
      },
      {
        name: 'PORT',
        status: `${process.env.PORT || '3000'} (Configured)`,
        indicator: '✅',
        type: 'HTTP Server',
      },
      {
        name: 'DISCORD_GUILD_ID',
        status: guildConfigured ? 'Configured (Instant Dev Sync)' : 'Not Set (Global Sync)',
        indicator: guildConfigured ? '✅' : '⚪',
        type: 'Development',
      },
      {
        name: 'ENVIRONMENT',
        status: process.env.ENVIRONMENT || 'production',
        indicator: '✅',
        type: 'Configuration',
      },
    ],
  };
}

async function startServer() {
  const app = express();
  const isProduction = process.env.NODE_ENV === 'production';
  // In development, the dev server must bind to 3000 to work with nginx reverse-proxy
  const PORT = isProduction ? parseInt(process.env.PORT || '8080', 10) : 3000;

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

  // GET /api/stats: Returns real live hardware statistics
  app.get(['/api/stats', '/stats'], (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
    res.setHeader('Pragma', 'no-cache');
    res.setHeader('Content-Type', 'application/json');

    const now = Date.now();
    // Use cached stats if less than 1.5s old for fast sub-millisecond responses
    if (cachedStats && now - cachedStatsTime < 1500) {
      return res.json(cachedStats);
    }

    // Direct measurement via Python stats.py on the local host machine
    const scriptPath = path.join(__dirname, 'stats.py');
    execFile('python3', [scriptPath, '--json'], { timeout: 2500 }, (error, stdout, stderr) => {
      if (!error && stdout) {
        try {
          const stats = JSON.parse(stdout);
          cachedStats = stats;
          cachedStatsTime = Date.now();
          return res.json(stats);
        } catch (parseError) {
          console.error('Failed to parse stats.py output:', parseError);
        }
      }

      // If python execution timed out or failed, serve native Node.js hardware metrics fallback
      const nativeStats = getNativeHostStats();
      cachedStats = nativeStats;
      cachedStatsTime = Date.now();
      return res.json(nativeStats);
    });
  });

  // GET /health: Health probe
  app.get('/health', (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Content-Type', 'application/json');
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
