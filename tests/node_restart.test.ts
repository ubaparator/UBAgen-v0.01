/**
 * End-to-end TypeScript tests for:
 * 1. Frontend URL resolution (resolveRestartBotUrl) for relative and full backend URLs
 * 2. Frontend restart request handling (executeBotRestartRequest)
 * 3. Node/Express server.ts (POST /api/admin/restart-bot, auth, rate-limit, method check, companion delegation & fallback)
 */

import assert from 'node:assert/strict';
import http from 'node:http';
import { createApiApp, resetServerRestartStateForTests, verifyAdminToken } from '../server.ts';
import { resolveRestartBotUrl, executeBotRestartRequest } from '../src/restartBot.ts';

async function runTests() {
  // 1. Frontend URL resolution tests
  assert.equal(resolveRestartBotUrl('/api/stats'), '/api/admin/restart-bot');
  assert.equal(resolveRestartBotUrl('/stats'), '/api/admin/restart-bot');
  assert.equal(resolveRestartBotUrl(''), '/api/admin/restart-bot');
  assert.equal(resolveRestartBotUrl('   '), '/api/admin/restart-bot');
  assert.equal(
    resolveRestartBotUrl('https://my-ubagen.onrender.com/api/stats'),
    'https://my-ubagen.onrender.com/api/admin/restart-bot'
  );
  assert.equal(
    resolveRestartBotUrl('https://my-ubagen.onrender.com/api/stats/'),
    'https://my-ubagen.onrender.com/api/admin/restart-bot'
  );
  assert.equal(
    resolveRestartBotUrl('http://127.0.0.1:8080/stats?foo=1#bar'),
    'http://127.0.0.1:8080/api/admin/restart-bot'
  );
  assert.equal(
    resolveRestartBotUrl('https://ubagen.example.com'),
    'https://ubagen.example.com/api/admin/restart-bot'
  );
  assert.equal(
    resolveRestartBotUrl('https://ubagen.example.com/subpath/api/stats'),
    'https://ubagen.example.com/subpath/api/admin/restart-bot'
  );
  assert.equal(
    resolveRestartBotUrl('/subpath/api/stats'),
    '/subpath/api/admin/restart-bot'
  );
  assert.throws(() => resolveRestartBotUrl('http://[::1'));
  assert.throws(() => resolveRestartBotUrl('ftp://example.com/api/stats'));

  // 2. Frontend executeBotRestartRequest tests
  let capturedUrl = '';
  let capturedAuth = '';
  const okResult = await executeBotRestartRequest({
    endpointUrl: 'https://my-ubagen.onrender.com/api/stats',
    adminToken: 'secret-token-abc',
    fetchImpl: async (input, init) => {
      capturedUrl = String(input);
      capturedAuth = String((init?.headers as Record<string, string>)?.Authorization || '');
      return new Response(JSON.stringify({ status: 'restarting', message: 'Restarting now' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  assert.equal(okResult.ok, true);
  assert.equal(okResult.status, 202);
  assert.equal(capturedUrl, 'https://my-ubagen.onrender.com/api/admin/restart-bot');
  assert.equal(capturedAuth, 'Bearer secret-token-abc');

  const relativeResult = await executeBotRestartRequest({
    endpointUrl: '/api/stats',
    adminToken: 'secret-token-rel',
    fetchImpl: async (input) => {
      capturedUrl = String(input);
      return new Response(JSON.stringify({ status: 'restarting' }), {
        status: 202,
        headers: { 'Content-Type': 'application/json' },
      });
    },
  });
  assert.equal(relativeResult.ok, true);
  assert.equal(capturedUrl, '/api/admin/restart-bot');

  const nonJsonFail = await executeBotRestartRequest({
    endpointUrl: '/api/stats',
    adminToken: 'wrong-token',
    fetchImpl: async () => new Response('Gateway Error Plaintext', { status: 401 }),
  });
  assert.equal(nonJsonFail.ok, false);
  assert.equal(nonJsonFail.status, 401);

  const emptyTokenResult = await executeBotRestartRequest({
    endpointUrl: '/api/stats',
    adminToken: '   ',
  });
  assert.equal(emptyTokenResult.ok, false);
  assert.equal(emptyTokenResult.status, 0);

  // 3. Node/Express server.ts endpoint tests
  assert.equal(verifyAdminToken('Bearer my-secret', 'my-secret'), true);
  assert.equal(verifyAdminToken('Bearer wrong', 'my-secret'), false);
  assert.equal(verifyAdminToken('', 'my-secret'), false);

  let workerKilled = 0;
  let companionCalled = false;
  let simulateCompanionAlive = false;

  const app = createApiApp({
    cooldownMs: 5000,
    getWorkerProcess: () => ({
      killed: false,
      kill: () => {
        workerKilled += 1;
        return true;
      },
    }),
    fetchImpl: async (input, init) => {
      const url = String(input);
      if (url.endsWith('/api/stats')) {
        return new Response(JSON.stringify({ status: 'online', source: 'upstream' }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.endsWith('/api/admin/restart-bot') && simulateCompanionAlive) {
        companionCalled = true;
        const auth = String((init?.headers as Record<string, string>)?.Authorization || '');
        assert.equal(auth, 'Bearer node-admin-secret');
        return new Response(
          JSON.stringify({ status: 'restarting', message: 'In-process companion restart' }),
          { status: 202, headers: { 'content-type': 'application/json' } }
        );
      }
      throw new Error('Upstream companion not running in fallback test');
    },
  });

  const server = http.createServer(app);
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const addr = server.address() as { port: number };
  const baseUrl = `http://127.0.0.1:${addr.port}`;

  try {
    // 3a. Unconfigured UBAGEN_ADMIN_TOKEN -> 503 (never uses HOST)
    resetServerRestartStateForTests();
    process.env.UBAGEN_ADMIN_TOKEN = '';
    process.env.HOST = '0.0.0.0';
    process.env.DISCORD_BOT_TOKEN = 'MTIzNDU2Nzg5MDEyMzQ1Njc4.Ga.bc';
    const r503 = await fetch(`${baseUrl}/api/admin/restart-bot`, {
      method: 'POST',
      headers: { Authorization: 'Bearer 0.0.0.0' },
    });
    assert.equal(r503.status, 503);

    // 3b. Missing or wrong token -> 401
    process.env.UBAGEN_ADMIN_TOKEN = 'node-admin-secret';
    const r401Missing = await fetch(`${baseUrl}/api/admin/restart-bot`, { method: 'POST' });
    assert.equal(r401Missing.status, 401);
    const r401Wrong = await fetch(`${baseUrl}/api/admin/restart-bot`, {
      method: 'POST',
      headers: { Authorization: 'Bearer bad-token' },
    });
    assert.equal(r401Wrong.status, 401);

    // 3c. Disallowed method GET/DELETE -> 405
    const r405 = await fetch(`${baseUrl}/api/admin/restart-bot`, { method: 'GET' });
    assert.equal(r405.status, 405);

    // 3d. Valid token with Python FastAPI companion alive -> delegates in-process without killing worker
    simulateCompanionAlive = true;
    const r202Companion = await fetch(`${baseUrl}/api/admin/restart-bot`, {
      method: 'POST',
      headers: { Authorization: 'Bearer node-admin-secret' },
    });
    assert.equal(r202Companion.status, 202);
    assert.equal(companionCalled, true);
    assert.equal(workerKilled, 0);

    // 3e. Immediate repeat request -> 429
    const r429 = await fetch(`${baseUrl}/api/admin/restart-bot`, {
      method: 'POST',
      headers: { Authorization: 'Bearer node-admin-secret' },
    });
    assert.equal(r429.status, 429);

    // 3f. Valid token fallback when companion is unreachable -> signals worker
    resetServerRestartStateForTests();
    simulateCompanionAlive = false;
    const r202Fallback = await fetch(`${baseUrl}/api/admin/restart-bot`, {
      method: 'POST',
      headers: { Authorization: 'Bearer node-admin-secret' },
    });
    assert.equal(r202Fallback.status, 202);
    assert.equal(workerKilled, 1);

    // 3g. /api/stats and /health remain accessible
    const rStats = await fetch(`${baseUrl}/api/stats`);
    assert.equal(rStats.status, 200);
    const rHealth = await fetch(`${baseUrl}/health`);
    assert.equal(rHealth.status, 200);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
