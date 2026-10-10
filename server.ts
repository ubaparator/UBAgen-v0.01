/**
 * UBAgen — server.ts
 * Express full-stack server running Vite in development and serving /api/stats and /health.
 */

import express from 'express';
import { createServer as createViteServer } from 'vite';
import { execFile } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

async function startServer() {
  const app = express();
  const isProduction = process.env.NODE_ENV === 'production';
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

  // GET /api/stats: Fetches live hardware metrics from backend
  app.get(['/api/stats', '/stats'], async (req, res) => {
    res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0');
    res.setHeader('Pragma', 'no-cache');

    // 1. If an upstream Python service is running on BACKEND_URL or http://127.0.0.1:8080, query it
    const standaloneBackendUrl = process.env.BACKEND_URL || 'http://127.0.0.1:8080';
    try {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1200);
      const upstreamRes = await fetch(`${standaloneBackendUrl}/api/stats`, {
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (upstreamRes.ok) {
        const data = await upstreamRes.json();
        return res.json(data);
      }
    } catch {
      // Standalone backend not running on port 8080; fall back to direct stats.py execution
    }

    // 2. Direct measurement via Python on the local machine
    const scriptPath = path.join(__dirname, 'stats.py');
    execFile('python3', [scriptPath, '--json'], { timeout: 3500 }, (error, stdout, stderr) => {
      if (!error && stdout) {
        try {
          const stats = JSON.parse(stdout);
          return res.json(stats);
        } catch (parseError) {
          console.error('Failed to parse stats.py output:', parseError, stdout);
        }
      }

      console.error('stats.py execution error:', error?.message || stderr);
      return res.status(503).json({
        status: 'error',
        error: 'Backend statistics collection failed',
        details: error?.message || stderr || 'Unknown error',
      });
    });
  });

  // GET /health: Health probe
  app.get('/health', (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
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
