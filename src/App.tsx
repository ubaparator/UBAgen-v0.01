/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Activity,
  Cpu,
  HardDrive,
  Server,
  Bot,
  Play,
  Pause,
  RefreshCw,
  AlertTriangle,
  Clock,
  CheckCircle2,
  XCircle,
  HelpCircle,
  Layers,
  Settings,
  Shield,
  FileCode,
  Terminal,
  Zap,
} from 'lucide-react';

interface StatsResponse {
  status: string;
  timestamp: string;
  epoch_time: number;
  bot: {
    is_ready: boolean;
    connected: boolean;
    latency_ms: number | null;
    user: string;
    guild_count: number;
    uptime_seconds: number | null;
    uptime_str: string;
  };
  compute: {
    cpu_usage_pct: number | null;
    logical_cores: number | null;
    physical_cores: number | null;
    os_name: string;
    python_version: string;
    python_impl: string;
    system_uptime_sec: number | null;
    system_uptime_str: string;
    backend_uptime_sec: number | null;
    backend_uptime_str: string;
  };
  memory: {
    system_total_bytes: number | null;
    system_total_str: string;
    system_used_bytes: number | null;
    system_used_str: string;
    system_available_bytes: number | null;
    system_available_str: string;
    system_free_bytes: number | null;
    system_free_str: string;
    system_percent: number | null;
    container_limit: {
      is_limited: boolean;
      limit_bytes: number | null;
      limit_str: string;
      usage_bytes: number | null;
      usage_str: string | null;
      type: string;
      description: string;
    };
    process_rss_bytes: number | null;
    process_rss_str: string;
    process_percent: number | null;
  };
  disk: {
    total_bytes: number | null;
    total_str: string;
    used_bytes: number | null;
    used_str: string;
    free_bytes: number | null;
    free_str: string;
    percent: number | null;
  };
  binaries: {
    ffmpeg: {
      installed: boolean;
      version: string;
      path: string | null;
    };
    aria2: {
      installed: boolean;
      version: string;
      path: string | null;
    };
  };
  env_vars: Array<{
    name: string;
    status: string;
    indicator: string;
    type: string;
  }>;
}

export default function App() {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [isPaused, setIsPaused] = useState<boolean>(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [endpointUrl, setEndpointUrl] = useState<string>(() => {
    return localStorage.getItem('ubagen_endpoint_url') || '/api/stats';
  });
  const [showConfig, setShowConfig] = useState<boolean>(false);
  const [customUrlInput, setCustomUrlInput] = useState<string>(endpointUrl);

  const timerRef = useRef<NodeJS.Timeout | null>(null);

  // Fetch telemetry from real backend /api/stats
  const fetchStats = useCallback(
    async (isManual: boolean = false) => {
      if (isManual) setRefreshing(true);
      try {
        const response = await fetch(endpointUrl, {
          headers: {
            Accept: 'application/json',
          },
          cache: 'no-store',
        });

        const contentType = response.headers.get('content-type') || '';
        const rawText = await response.text();

        // Guard against HTML warmup pages, proxy 502/504 pages, or non-JSON responses
        if (!contentType.includes('application/json') || rawText.trim().startsWith('<')) {
          console.warn('Backend returned HTML (warming up). Retrying in next polling cycle...');
          setError('Backend is initializing or warming up... Connecting in 2s');
          return;
        }

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data: StatsResponse = JSON.parse(rawText);
        setStats(data);
        setError(null);
        setLastUpdated(new Date());
      } catch (err: any) {
        console.error('Failed to fetch backend statistics:', err);
        setError(
          err?.message ||
            'Unable to connect to UBAgen backend. Please verify the backend is running.'
        );
      } finally {
        setLoading(false);
        if (isManual) setRefreshing(false);
      }
    },
    [endpointUrl]
  );

  // Polling loop: every 2 seconds
  useEffect(() => {
    fetchStats(false);

    if (timerRef.current) clearInterval(timerRef.current);

    if (!isPaused) {
      timerRef.current = setInterval(() => {
        fetchStats(false);
      }, 2000);
    }

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [fetchStats, isPaused]);

  const handleTogglePause = () => {
    setIsPaused((prev) => !prev);
  };

  const handleManualRefresh = () => {
    fetchStats(true);
  };

  const handleSaveEndpoint = (e: React.FormEvent) => {
    e.preventDefault();
    const cleanUrl = customUrlInput.trim() || '/api/stats';
    setEndpointUrl(cleanUrl);
    localStorage.setItem('ubagen_endpoint_url', cleanUrl);
    setShowConfig(false);
    setLoading(true);
  };

  const handleResetEndpoint = () => {
    setCustomUrlInput('/api/stats');
    setEndpointUrl('/api/stats');
    localStorage.removeItem('ubagen_endpoint_url');
    setShowConfig(false);
    setLoading(true);
  };

  // Helper for status colors
  const getUsageColor = (pct: number | null) => {
    if (pct === null) return 'text-slate-400 bg-slate-800';
    if (pct < 60) return 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30';
    if (pct < 85) return 'text-amber-400 bg-amber-500/10 border-amber-500/30';
    return 'text-rose-400 bg-rose-500/10 border-rose-500/30';
  };

  const getProgressColor = (pct: number | null) => {
    if (pct === null) return 'bg-slate-700';
    if (pct < 60) return 'bg-emerald-500';
    if (pct < 85) return 'bg-amber-500';
    return 'bg-rose-500';
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-indigo-500 selection:text-white">
      {/* Top Header */}
      <header className="border-b border-slate-800/80 bg-slate-900/90 backdrop-blur sticky top-0 z-30 px-4 sm:px-6 py-3.5 flex flex-wrap items-center justify-between gap-4 shadow-xl">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-600 to-indigo-800 flex items-center justify-center shadow-lg shadow-indigo-600/30 text-white font-bold">
            <Activity className="w-5 h-5 text-indigo-100" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="text-lg font-bold tracking-tight text-white">
                UBAgen — System Monitor
              </h1>
              {stats?.status === 'online' ? (
                <span className="flex items-center gap-1.5 px-2 py-0.5 text-xs font-semibold rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                  <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                  Live Host
                </span>
              ) : (
                <span className="flex items-center gap-1.5 px-2 py-0.5 text-xs font-semibold rounded-full bg-rose-500/15 text-rose-400 border border-rose-500/30">
                  <span className="w-2 h-2 rounded-full bg-rose-400" />
                  Offline
                </span>
              )}
            </div>
            <p className="text-xs text-slate-400 flex items-center gap-2 mt-0.5">
              <span>Cloud Host &amp; Container Hardware Telemetry</span>
              <span className="text-slate-600">&bull;</span>
              <span className="font-mono text-slate-300">
                {endpointUrl === '/api/stats' ? 'Local / Same-Origin' : endpointUrl}
              </span>
            </p>
          </div>
        </div>

        {/* Header Actions & Controls */}
        <div className="flex items-center flex-wrap gap-2.5">
          {/* Pause / Resume Button */}
          <button
            onClick={handleTogglePause}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-all flex items-center gap-1.5 cursor-pointer shadow-sm ${
              isPaused
                ? 'bg-amber-500/10 text-amber-300 border-amber-500/30 hover:bg-amber-500/20'
                : 'bg-slate-800/80 text-slate-300 border-slate-700/60 hover:bg-slate-800 hover:text-white'
            }`}
            title={isPaused ? 'Resume 2-second live refresh' : 'Pause live auto-refresh'}
          >
            {isPaused ? (
              <>
                <Play className="w-3.5 h-3.5 fill-current" />
                <span>Resume (2s)</span>
              </>
            ) : (
              <>
                <Pause className="w-3.5 h-3.5 fill-current" />
                <span>Pause Auto-Refresh</span>
              </>
            )}
          </button>

          {/* Manual Refresh Button */}
          <button
            onClick={handleManualRefresh}
            disabled={refreshing}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 hover:bg-indigo-500 active:scale-95 text-white border border-indigo-500/40 transition-all flex items-center gap-1.5 cursor-pointer shadow-md shadow-indigo-600/20 disabled:opacity-50"
            title="Trigger immediate backend measurement"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>{refreshing ? 'Reading...' : 'Refresh'}</span>
          </button>

          {/* Connection Settings Toggle */}
          <button
            onClick={() => setShowConfig(!showConfig)}
            className="p-2 text-xs font-medium rounded-lg bg-slate-800 text-slate-400 hover:text-white hover:bg-slate-700 transition-colors border border-slate-700/60 cursor-pointer"
            title="Configure Backend Connection Endpoint"
          >
            <Settings className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* Endpoint Configuration Bar (Collapsible) */}
      {showConfig && (
        <div className="bg-slate-900 border-b border-slate-800 px-6 py-4 animate-in slide-in-from-top-2 duration-150">
          <form
            onSubmit={handleSaveEndpoint}
            className="max-w-4xl mx-auto flex flex-col sm:flex-row items-center gap-3"
          >
            <div className="flex-1 w-full">
              <label className="block text-xs font-medium text-slate-300 mb-1">
                Backend Endpoint URL (Default: <code>/api/stats</code>)
              </label>
              <input
                type="text"
                value={customUrlInput}
                onChange={(e) => setCustomUrlInput(e.target.value)}
                placeholder="e.g. /api/stats or https://my-ubagen.onrender.com/api/stats"
                className="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-1.5 text-xs text-slate-100 font-mono focus:outline-none focus:border-indigo-500"
              />
            </div>
            <div className="flex items-center gap-2 self-end sm:self-auto mt-2 sm:mt-5">
              <button
                type="submit"
                className="px-3 py-1.5 text-xs font-medium rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white cursor-pointer"
              >
                Apply URL
              </button>
              <button
                type="button"
                onClick={handleResetEndpoint}
                className="px-3 py-1.5 text-xs font-medium rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 cursor-pointer"
              >
                Reset Default
              </button>
            </div>
          </form>
        </div>
      )}

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto p-4 sm:p-6 flex flex-col gap-6">
        {/* Error Banner */}
        {error && (
          <div className="bg-rose-950/50 border border-rose-500/40 rounded-2xl p-4 flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 shadow-lg text-rose-200">
            <div className="flex items-start gap-3">
              <div className="p-2 rounded-xl bg-rose-500/20 text-rose-400 shrink-0 mt-0.5 sm:mt-0">
                <AlertTriangle className="w-5 h-5" />
              </div>
              <div>
                <h3 className="text-sm font-bold text-rose-100">
                  Backend Unreachable &bull; Unable to collect statistics
                </h3>
                <p className="text-xs text-rose-300/90 mt-0.5">{error}</p>
                <p className="text-[11px] text-rose-400/80 mt-1">
                  Ensure the UBAgen service is running. If deployed across origins, verify CORS is
                  enabled.
                </p>
              </div>
            </div>
            <button
              onClick={() => fetchStats(true)}
              className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-rose-600 hover:bg-rose-500 text-white shrink-0 self-end sm:self-auto cursor-pointer"
            >
              Retry Connection
            </button>
          </div>
        )}

        {/* Live Status Sub-Header Bar */}
        <div className="flex flex-wrap items-center justify-between gap-3 text-xs bg-slate-900/60 border border-slate-800/80 rounded-xl px-4 py-2.5">
          <div className="flex items-center gap-3">
            <span className="flex items-center gap-1.5 text-slate-400">
              <Clock className="w-3.5 h-3.5 text-slate-500" />
              Last Update:
              <strong className="text-slate-200 font-mono">
                {lastUpdated ? lastUpdated.toLocaleTimeString() : 'Pending...'}
              </strong>
            </span>
            <span className="text-slate-700">|</span>
            <span className="flex items-center gap-1.5 text-slate-400">
              Auto-Refresh:
              <strong
                className={
                  isPaused ? 'text-amber-400 font-semibold' : 'text-emerald-400 font-semibold'
                }
              >
                {isPaused ? 'Paused' : 'Every 2 seconds'}
              </strong>
            </span>
          </div>

          <div className="flex items-center gap-2">
            <span className="text-slate-400">Discord Gateway:</span>
            {stats?.bot?.connected ? (
              <span className="flex items-center gap-1 text-emerald-400 font-semibold">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Connected ({stats.bot.latency_ms ?? 0} ms)
              </span>
            ) : (
              <span className="flex items-center gap-1 text-rose-400 font-semibold">
                <XCircle className="w-3.5 h-3.5" />
                Offline
              </span>
            )}
          </div>
        </div>

        {/* Loading Skeleton */}
        {loading && !stats && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 animate-pulse">
            {[...Array(8)].map((_, i) => (
              <div
                key={i}
                className="h-44 bg-slate-900/70 border border-slate-800/80 rounded-2xl p-5"
              />
            ))}
          </div>
        )}

        {/* Active Telemetry Grid */}
        {stats && (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* CARD 1: CPU USAGE */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Cpu className="w-4 h-4 text-indigo-400" />
                    CPU Compute
                  </span>
                  <span
                    className={`px-2 py-0.5 text-xs font-semibold rounded-md border ${getUsageColor(
                      stats.compute.cpu_usage_pct
                    )}`}
                  >
                    {stats.compute.cpu_usage_pct !== null
                      ? `${stats.compute.cpu_usage_pct.toFixed(1)}%`
                      : 'Unavailable'}
                  </span>
                </div>

                <div className="text-3xl font-bold font-mono tracking-tight text-white mt-1">
                  {stats.compute.cpu_usage_pct !== null
                    ? `${stats.compute.cpu_usage_pct.toFixed(1)}%`
                    : 'Unavailable'}
                </div>
                <p className="text-xs text-slate-400 mt-1">Real-time processor load</p>

                {/* Meter Bar */}
                <div className="w-full h-2.5 bg-slate-800 rounded-full overflow-hidden mt-4">
                  <div
                    className={`h-full transition-all duration-500 ${getProgressColor(
                      stats.compute.cpu_usage_pct
                    )}`}
                    style={{ width: `${Math.min(stats.compute.cpu_usage_pct || 0, 100)}%` }}
                  />
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Cores:</span>
                <span className="text-slate-200 font-mono font-medium">
                  {stats.compute.logical_cores ?? 'Unavailable'} Logical (
                  {stats.compute.physical_cores ?? 'Unavailable'} Physical)
                </span>
              </div>
            </div>

            {/* CARD 2: SYSTEM RAM */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Server className="w-4 h-4 text-sky-400" />
                    System RAM (Host)
                  </span>
                  <span
                    className={`px-2 py-0.5 text-xs font-semibold rounded-md border ${getUsageColor(
                      stats.memory.system_percent
                    )}`}
                  >
                    {stats.memory.system_percent !== null
                      ? `${stats.memory.system_percent.toFixed(1)}%`
                      : 'Unavailable'}
                  </span>
                </div>

                <div className="text-2xl font-bold font-mono text-white mt-1">
                  {stats.memory.system_used_str}{' '}
                  <span className="text-slate-500 text-lg font-normal">
                    / {stats.memory.system_total_str}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Available: <strong className="text-slate-200">{stats.memory.system_available_str}</strong>
                </p>

                {/* Meter Bar */}
                <div className="w-full h-2.5 bg-slate-800 rounded-full overflow-hidden mt-4">
                  <div
                    className={`h-full transition-all duration-500 ${getProgressColor(
                      stats.memory.system_percent
                    )}`}
                    style={{ width: `${Math.min(stats.memory.system_percent || 0, 100)}%` }}
                  />
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Allocation:</span>
                <span className="text-slate-200 font-mono font-medium">Host Node Visible</span>
              </div>
            </div>

            {/* CARD 3: UBAGEN PROCESS RAM */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Zap className="w-4 h-4 text-amber-400" />
                    UBAgen Process RAM
                  </span>
                  <span className="px-2 py-0.5 text-xs font-semibold rounded-md border border-amber-500/30 bg-amber-500/10 text-amber-400">
                    RSS Memory
                  </span>
                </div>

                <div className="text-3xl font-bold font-mono text-white mt-1">
                  {stats.memory.process_rss_str}
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Exact memory used by UBAgen process
                </p>

                {/* Progress bar relative to system */}
                <div className="w-full h-2.5 bg-slate-800 rounded-full overflow-hidden mt-4">
                  <div
                    className="h-full bg-amber-400 transition-all duration-500"
                    style={{ width: `${Math.min((stats.memory.process_percent || 1) * 3, 100)}%` }}
                  />
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Host RAM Share:</span>
                <span className="text-slate-200 font-mono font-medium">
                  {stats.memory.process_percent !== null
                    ? `${stats.memory.process_percent}% of system`
                    : 'Unavailable'}
                </span>
              </div>
            </div>

            {/* CARD 4: CONTAINER QUOTA LIMIT */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Shield className="w-4 h-4 text-emerald-400" />
                    Container Memory Limit
                  </span>
                  <span
                    className={`px-2 py-0.5 text-[11px] font-semibold rounded-md border ${
                      stats.memory.container_limit.is_limited
                        ? 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                        : 'bg-slate-800 text-slate-400 border-slate-700'
                    }`}
                  >
                    {stats.memory.container_limit.type}
                  </span>
                </div>

                <div className="text-2xl font-bold font-mono text-white mt-1 truncate">
                  {stats.memory.container_limit.limit_str}
                </div>
                <p className="text-xs text-slate-400 mt-1 line-clamp-2">
                  {stats.memory.container_limit.description}
                </p>

                {stats.memory.container_limit.usage_str && (
                  <div className="text-xs text-slate-300 font-mono mt-2">
                    Current Container Usage: <strong>{stats.memory.container_limit.usage_str}</strong>
                  </div>
                )}
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>OOM Status:</span>
                <span
                  className={
                    stats.memory.container_limit.is_limited
                      ? 'text-amber-300 font-medium'
                      : 'text-emerald-400 font-medium'
                  }
                >
                  {stats.memory.container_limit.is_limited
                    ? 'Quota Monitored'
                    : 'No Cgroup Ceiling'}
                </span>
              </div>
            </div>

            {/* CARD 5: DISK STORAGE */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <HardDrive className="w-4 h-4 text-indigo-400" />
                    Storage (Root Volume)
                  </span>
                  <span
                    className={`px-2 py-0.5 text-xs font-semibold rounded-md border ${getUsageColor(
                      stats.disk.percent
                    )}`}
                  >
                    {stats.disk.percent !== null ? `${stats.disk.percent.toFixed(1)}%` : 'Unavailable'}
                  </span>
                </div>

                <div className="text-2xl font-bold font-mono text-white mt-1">
                  {stats.disk.used_str}{' '}
                  <span className="text-slate-500 text-lg font-normal">
                    / {stats.disk.total_str}
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  Free Space: <strong className="text-slate-200">{stats.disk.free_str}</strong>
                </p>

                {/* Meter Bar */}
                <div className="w-full h-2.5 bg-slate-800 rounded-full overflow-hidden mt-4">
                  <div
                    className={`h-full transition-all duration-500 ${getProgressColor(
                      stats.disk.percent
                    )}`}
                    style={{ width: `${Math.min(stats.disk.percent || 0, 100)}%` }}
                  />
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Mount Path:</span>
                <span className="text-slate-200 font-mono font-medium">/ (Root filesystem)</span>
              </div>
            </div>

            {/* CARD 6: DISCORD BOT CONNECTION */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Bot className="w-4 h-4 text-[#5865F2]" />
                    Discord Bot Gateway
                  </span>
                  <span
                    className={`px-2 py-0.5 text-xs font-semibold rounded-md border flex items-center gap-1 ${
                      stats.bot.connected
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                        : 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                    }`}
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-full ${
                        stats.bot.connected ? 'bg-emerald-400' : 'bg-rose-400'
                      }`}
                    />
                    {stats.bot.connected ? 'Connected' : 'Disconnected'}
                  </span>
                </div>

                <div className="text-lg font-bold font-mono text-white mt-1 truncate">
                  {stats.bot.user}
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  {stats.bot.connected
                    ? `Connected to ${stats.bot.guild_count} Discord server(s)`
                    : 'Set DISCORD_BOT_TOKEN in hosting env to connect'}
                </p>

                <div className="mt-3 text-xs space-y-1 text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Gateway Latency:</span>
                    <span className="font-mono text-indigo-300">
                      {stats.bot.latency_ms !== null ? `${stats.bot.latency_ms} ms` : 'Offline'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Slash Command:</span>
                    <span className="font-mono text-emerald-400">/stats Active</span>
                  </div>
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Bot Uptime:</span>
                <span className="text-slate-200 font-mono font-medium">
                  {stats.bot.uptime_str}
                </span>
              </div>
            </div>

            {/* CARD 7: SYSTEM & BACKEND UPTIME */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Clock className="w-4 h-4 text-purple-400" />
                    Uptime &amp; Environment
                  </span>
                  <span className="px-2 py-0.5 text-xs font-semibold rounded-md border border-purple-500/30 bg-purple-500/10 text-purple-400">
                    Host &bull; App
                  </span>
                </div>

                <div className="text-2xl font-bold font-mono text-white mt-1">
                  {stats.compute.backend_uptime_str}
                </div>
                <p className="text-xs text-slate-400 mt-1">UBAgen backend process uptime</p>

                <div className="mt-3 text-xs space-y-1.5 text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Host OS:</span>
                    <span className="font-medium text-slate-200 truncate max-w-[180px]">
                      {stats.compute.os_name}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Python:</span>
                    <span className="font-mono text-slate-200">
                      v{stats.compute.python_version} ({stats.compute.python_impl})
                    </span>
                  </div>
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>System Uptime:</span>
                <span className="text-slate-200 font-mono font-medium">
                  {stats.compute.system_uptime_str}
                </span>
              </div>
            </div>

            {/* CARD 8: TOOLCHAIN & BINARIES */}
            <div className="bg-slate-900 border border-slate-800/80 rounded-2xl p-5 flex flex-col justify-between shadow-lg">
              <div>
                <div className="flex items-center justify-between mb-3">
                  <span className="text-xs font-semibold text-slate-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Terminal className="w-4 h-4 text-cyan-400" />
                    Installed Toolchain
                  </span>
                  <span className="px-2 py-0.5 text-xs font-semibold rounded-md border border-cyan-500/30 bg-cyan-500/10 text-cyan-400">
                    Binaries
                  </span>
                </div>

                <div className="space-y-3 mt-2 text-xs">
                  {/* FFmpeg */}
                  <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-bold text-white flex items-center gap-1.5">
                        {stats.binaries.ffmpeg.installed ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                        ) : (
                          <XCircle className="w-3.5 h-3.5 text-slate-500" />
                        )}
                        FFmpeg
                      </span>
                      <span
                        className={`text-[10px] font-semibold px-1.5 py-0.2 rounded ${
                          stats.binaries.ffmpeg.installed
                            ? 'bg-emerald-500/20 text-emerald-400'
                            : 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        {stats.binaries.ffmpeg.installed ? 'Installed' : 'Missing'}
                      </span>
                    </div>
                    <p className="text-[11px] text-slate-400 font-mono truncate">
                      {stats.binaries.ffmpeg.version}
                    </p>
                  </div>

                  {/* aria2 */}
                  <div className="bg-slate-950/60 p-2.5 rounded-xl border border-slate-800/80">
                    <div className="flex items-center justify-between mb-1">
                      <span className="font-bold text-white flex items-center gap-1.5">
                        {stats.binaries.aria2.installed ? (
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                        ) : (
                          <XCircle className="w-3.5 h-3.5 text-slate-500" />
                        )}
                        aria2
                      </span>
                      <span
                        className={`text-[10px] font-semibold px-1.5 py-0.2 rounded ${
                          stats.binaries.aria2.installed
                            ? 'bg-emerald-500/20 text-emerald-400'
                            : 'bg-slate-800 text-slate-400'
                        }`}
                      >
                        {stats.binaries.aria2.installed ? 'Installed' : 'Missing'}
                      </span>
                    </div>
                    <p className="text-[11px] text-slate-400 font-mono truncate">
                      {stats.binaries.aria2.version}
                    </p>
                  </div>
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Docker Image:</span>
                <span className="text-slate-200 font-mono font-medium">Bundled in Dockerfile</span>
              </div>
            </div>
          </div>
        )}

        {/* Environment Variables Audit Section */}
        {stats && (
          <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5 shadow-lg">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-3 flex items-center gap-2">
              <Shield className="w-4 h-4 text-indigo-400" />
              Hosting Environment Configuration Audit (No Secrets Leaked)
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
              {stats.env_vars.map((item, index) => (
                <div
                  key={index}
                  className="bg-slate-950/70 border border-slate-800 rounded-xl p-3 flex items-center justify-between gap-2"
                >
                  <div className="min-w-0">
                    <span className="text-xs font-mono font-bold text-slate-200 block truncate">
                      {item.name}
                    </span>
                    <span className="text-[11px] text-slate-400 mt-0.5 block truncate">
                      {item.status}
                    </span>
                  </div>
                  <span className="text-sm shrink-0">{item.indicator}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="border-t border-slate-900 bg-slate-950 px-6 py-4 text-center text-xs text-slate-500 flex flex-col sm:flex-row items-center justify-between gap-2">
        <span>UBAgen System Monitor &bull; Real-time Host Hardware Telemetry</span>
        <span className="font-mono text-slate-600">GET /api/stats &bull; GET /health</span>
      </footer>
    </div>
  );
}
