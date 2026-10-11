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
  ExternalLink,
  Copy,
  Check,
  Share2,
  X,
} from 'lucide-react';
import { executeBotRestartRequest, resolveRestartBotUrl } from './restartBot';
import RcloneManagement from './RcloneManagement';

export { executeBotRestartRequest, resolveRestartBotUrl };

// Official Discord Clyde controller SVG logo
function DiscordLogo({ className = 'w-4 h-4' }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M20.317 4.37a19.791 19.791 0 0 0-4.885-1.515.074.074 0 0 0-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 0 0-5.487 0 12.64 12.64 0 0 0-.617-1.25.077.077 0 0 0-.079-.037A19.736 19.736 0 0 0 3.677 4.37a.07.07 0 0 0-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 0 0 .031.057 19.9 19.9 0 0 0 5.993 3.03.078.078 0 0 0 .084-.028c.462-.63.874-1.295 1.226-1.994.021-.041.001-.09-.041-.106a13.107 13.107 0 0 1-1.872-.892.077.077 0 0 1-.008-.128 10.2 10.2 0 0 0 .372-.292.074.074 0 0 1 .077-.01c3.929 1.793 8.18 1.793 12.061 0a.074.074 0 0 1 .078.01c.12.098.246.198.373.292a.077.077 0 0 1-.006.127 12.299 12.299 0 0 1-1.873.893.077.077 0 0 0-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 0 0 .084.028 19.839 19.839 0 0 0 6.002-3.03.077.077 0 0 0 .032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 0 0-.031-.028zM8.02 15.33c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.956-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.956 2.418-2.157 2.418zm7.975 0c-1.183 0-2.157-1.085-2.157-2.419 0-1.333.955-2.419 2.157-2.419 1.21 0 2.176 1.096 2.157 2.42 0 1.333-.946 2.418-2.157 2.418z" />
    </svg>
  );
}

interface StatsResponse {
  status: string;
  timestamp: string;
  epoch_time: number;
  bot: {
    status?: string;
    is_ready: boolean;
    connected: boolean;
    http_alive?: boolean;
    gateway_connected?: boolean;
    bot_ready?: boolean;
    latency_ms: number | null;
    user: string;
    guild_count: number;
    uptime_seconds: number | null;
    uptime_str: string;
    diagnostic_message?: string;
    sync_status?: string | null;
    client_id?: string | null;
    invite_url?: string | null;
    reconnect_attempts?: number;
    wake_in_progress?: boolean;
    wake_attempts?: number;
    last_wake_time?: number | null;
    last_wake_reason?: string | null;
    interactions_endpoint_configured?: boolean;
    interactions_endpoint_path?: string;
    wake_endpoint_path?: string;
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
    mount_point?: string;
    fs_type?: string;
    writable?: boolean;
    is_persistent?: boolean;
    persistence_verified?: boolean;
    storage_mode?: string;
    decision_branch?: string;
    summary?: string;
    video_outputs_dir?: string;
    download_temp_dir?: string;
    encode_temp_dir?: string;
    video_outputs_str?: string;
    video_outputs_count?: number;
    temp_workspace_str?: string;
    effective_budget_str?: string;
  };
  storage?: {
    separation?: {
      persistent_output_storage?: {
        provider?: string;
        gcs_bucket?: string | null;
        local_outputs_dir?: string;
        is_persistent?: boolean;
        retention_seconds?: number;
        deletion_policy?: string;
      };
      ephemeral_worker_scratch_disk?: {
        worker_scratch_dir?: string;
        download_temp_dir?: string;
        encode_temp_dir?: string;
        fs_type?: string;
        mount_point?: string;
        is_ephemeral?: boolean;
        free_str?: string;
        cleanup_policy?: string;
      };
      job_state_database?: {
        backend_type?: string;
        in_memory_only?: boolean;
        firestore_collection?: string;
        sqlite_db_path?: string;
        setup_note?: string;
      };
      cloud_run_architecture?: {
        k_service?: string | null;
        k_revision?: string | null;
        project_id?: string | null;
        region?: string | null;
        service_account_email?: string | null;
        cloud_run_job_name?: string | null;
        cloud_run_job_configured?: boolean;
        gcs_bucket?: string | null;
        retention_seconds?: number;
      };
    };
  } | null;
  binaries: {
    ffmpeg: {
      installed: boolean;
      version: string;
      path: string | null;
    };
    ffprobe?: {
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

interface EncodeJobItem {
  job_id: string;
  user_id: string;
  stage: string;
  status: string;
  percent: number;
  processed_frames: number;
  total_frames: number | null;
  fps: number;
  eta_seconds: number | null;
  codec: string;
  video_encoder: string;
  subtitle_lang: string;
  has_intro: boolean;
  error: string | null;
  expires_at_iso: string | null;
  expires_in_seconds: number | null;
  retention_seconds: number;
  gcs_upload_verified: boolean;
  deleted_from_gcs: boolean;
}

export default function App() {
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [encodeJobs, setEncodeJobs] = useState<EncodeJobItem[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [restartingBot, setRestartingBot] = useState<boolean>(false);
  const [showRestartModal, setShowRestartModal] = useState<boolean>(false);
  const [restartAdminTokenInput, setRestartAdminTokenInput] = useState<string>('');
  const [restartFeedback, setRestartFeedback] = useState<{
    type: 'success' | 'error' | 'cancelled';
    title: string;
    message: string;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isPaused, setIsPaused] = useState<boolean>(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [endpointUrl, setEndpointUrl] = useState<string>(() => {
    return localStorage.getItem('ubagen_endpoint_url') || '/api/stats';
  });
  const [showConfig, setShowConfig] = useState<boolean>(false);
  const [customUrlInput, setCustomUrlInput] = useState<string>(endpointUrl);
  const [showInviteModal, setShowInviteModal] = useState<boolean>(false);
  const [copiedLink, setCopiedLink] = useState<boolean>(false);
  const [customClientId, setCustomClientId] = useState<string>(() => {
    return localStorage.getItem('ubagen_custom_client_id') || '';
  });
  const [permissionPreset, setPermissionPreset] = useState<'recommended' | 'admin' | 'basic'>('recommended');

  const timerRef = useRef<NodeJS.Timeout | null>(null);

  // Derive active Discord client ID & invite URL
  const effectiveClientId =
    stats?.bot?.client_id ||
    customClientId.trim() ||
    '';

  const getInviteUrlWithPerms = (preset: 'recommended' | 'admin' | 'basic') => {
    if (!effectiveClientId) return null;
    const permCode = preset === 'admin' ? 8 : preset === 'basic' ? 0 : 2147485696;
    return `https://discord.com/oauth2/authorize?client_id=${effectiveClientId}&permissions=${permCode}&scope=bot%20applications.commands`;
  };

  const activeInviteUrl =
    permissionPreset === 'recommended' && stats?.bot?.invite_url
      ? stats.bot.invite_url
      : getInviteUrlWithPerms(permissionPreset);

  const handleCopyInviteLink = async () => {
    if (!activeInviteUrl) return;
    try {
      await navigator.clipboard.writeText(activeInviteUrl);
      setCopiedLink(true);
      setTimeout(() => setCopiedLink(false), 2200);
    } catch {
      const textArea = document.createElement('textarea');
      textArea.value = activeInviteUrl;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand('copy');
      document.body.removeChild(textArea);
      setCopiedLink(true);
      setTimeout(() => setCopiedLink(false), 2200);
    }
  };

  // Fetch telemetry from real backend /api/stats
  const fetchStats = useCallback(
    async (isManual: boolean = false) => {
      if (isManual) setRefreshing(true);
      try {
        // Try the configured endpoint first, then same-origin aliases. This also
        // recovers users who have an obsolete custom endpoint saved in localStorage.
        const candidates = [...new Set([endpointUrl, '/api/stats', '/stats'])];
        let lastFailure: Error | null = null;
        for (const candidate of candidates) {
          try {
            const requestUrl = isManual
              ? `${candidate}${candidate.includes('?') ? '&' : '?'}fresh=1`
              : candidate;
            const response = await fetch(requestUrl, {
              headers: { Accept: 'application/json' },
              cache: 'no-store',
            });
            if (!response.ok) {
              throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const data: StatsResponse = await response.json();
            if (!data || typeof data !== 'object' || !data.status) {
              throw new Error('Invalid statistics response from backend.');
            }
            setStats(data);
            setError(null);
            setLastUpdated(new Date());
            fetch('/api/jobs?limit=10', { headers: { Accept: 'application/json' }, cache: 'no-store' })
              .then((r) => (r.ok ? r.json() : null))
              .then((jData) => {
                if (jData && Array.isArray(jData.jobs)) {
                  setEncodeJobs(jData.jobs);
                }
              })
              .catch(() => {});
            return;
          } catch (candidateError: any) {
            lastFailure = candidateError instanceof Error
              ? candidateError
              : new Error('Statistics endpoint unavailable.');
          }
        }

        // Preserve the last good snapshot instead of blanking the dashboard.
        setError((current) => current || (
          lastFailure?.message ||
          'Unable to connect to UBAgen backend. Please verify the backend is running.'
        ));
      } finally {
        setLoading(false);
        if (isManual) setRefreshing(false);
      }
    },
    [endpointUrl]
  );

  // Polling loop: every 5 seconds to reduce background resource usage
  useEffect(() => {
    fetchStats(false);

    if (timerRef.current) clearInterval(timerRef.current);

    if (!isPaused) {
      timerRef.current = setInterval(() => {
        fetchStats(false);
      }, 5000);
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

  const resolvedRestartEndpointPreview = (() => {
    try {
      return resolveRestartBotUrl(endpointUrl);
    } catch {
      return '/api/admin/restart-bot (Geçersiz Backend URL)';
    }
  })();

  const handleRestartBot = () => {
    if (restartingBot) return;
    setRestartAdminTokenInput('');
    setShowRestartModal(true);
  };

  const handleCancelRestartBot = () => {
    if (restartingBot) return;
    setShowRestartModal(false);
    setRestartAdminTokenInput('');
    setRestartFeedback({
      type: 'cancelled',
      title: 'Yeniden Başlatma İptal Edildi',
      message: 'İşlem kullanıcı tarafından iptal edildi; bota herhangi bir yeniden başlatma isteği gönderilmedi.',
    });
  };

  const handleConfirmRestartBot = async (e: React.FormEvent) => {
    e.preventDefault();
    if (restartingBot) return;

    const cleanToken = restartAdminTokenInput.trim();
    if (!cleanToken) {
      setRestartFeedback({
        type: 'error',
        title: 'Doğrulama Hatası',
        message: 'Devam etmek için sunucuda tanımlı UBAGEN_ADMIN_TOKEN değerini girmelisiniz.',
      });
      return;
    }

    setRestartingBot(true);
    try {
      const result = await executeBotRestartRequest({
        endpointUrl,
        adminToken: cleanToken,
      });

      if (result.ok) {
        setRestartAdminTokenInput('');
        setShowRestartModal(false);
        setRestartFeedback({
          type: 'success',
          title: `Bot Yeniden Başlatma İsteği Alındı (${result.statusCodeLabel})`,
          message: result.message,
        });
        // Immediately query bot status and poll again after reconnect window
        await fetchStats(true);
        window.setTimeout(() => {
          fetchStats(true);
        }, 2500);
        window.setTimeout(() => {
          fetchStats(true);
        }, 5500);
      } else {
        setRestartFeedback({
          type: 'error',
          title: `Bot Yeniden Başlatılamadı (${result.statusCodeLabel})`,
          message: result.message,
        });
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Bilinmeyen hata';
      setRestartFeedback({
        type: 'error',
        title: 'Bot Yeniden Başlatılamadı',
        message,
      });
    } finally {
      setRestartingBot(false);
    }
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
          {/* Rclone storage management */}
          <button
            type="button"
            onClick={() => document.getElementById('rclone-storage-management')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
            className="px-3 py-1.5 text-xs font-semibold rounded-lg bg-emerald-500/10 text-emerald-200 border border-emerald-500/30 hover:bg-emerald-500/20 transition-all flex items-center gap-1.5 cursor-pointer"
            title="MEGA ve Google Drive hesaplarını ve depolama kotalarını yönet"
          >
            <HardDrive className="w-3.5 h-3.5" />
            <span>Rclone Yönetimi</span>
          </button>

          {/* Pause / Resume Button */}
          <button
            onClick={handleTogglePause}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-all flex items-center gap-1.5 cursor-pointer shadow-sm ${
              isPaused
                ? 'bg-amber-500/10 text-amber-300 border-amber-500/30 hover:bg-amber-500/20'
                : 'bg-slate-800/80 text-slate-300 border-slate-700/60 hover:bg-slate-800 hover:text-white'
            }`}
            title={isPaused ? 'Resume 5-second live refresh' : 'Pause live auto-refresh'}
          >
            {isPaused ? (
              <>
                <Play className="w-3.5 h-3.5 fill-current" />
                <span>Resume (5s)</span>
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

          {/* Secure Bot Restart Button */}
          <button
            type="button"
            onClick={handleRestartBot}
            disabled={restartingBot}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-rose-600/15 text-rose-300 border border-rose-500/30 hover:bg-rose-600/25 transition-all flex items-center gap-1.5 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            title="Restart the Discord bot worker (requires UBAGEN_ADMIN_TOKEN)"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${restartingBot ? 'animate-spin' : ''}`} />
            <span>{restartingBot ? 'Restarting Bot…' : 'Restart Bot'}</span>
          </button>

          {/* Connection Settings Toggle */}
          <button
            onClick={() => setShowConfig(!showConfig)}
            className="p-2 text-xs font-medium rounded-lg bg-slate-800 text-slate-400 hover:text-white hover:bg-slate-700 transition-colors border border-slate-700/60 cursor-pointer"
            title="Configure Backend Connection Endpoint"
          >
            <Settings className="w-4 h-4" />
          </button>

          <div className="h-5 w-px bg-slate-800/80 mx-0.5 hidden sm:block" />

          {/* Discord Bot Sunucuya Ekle (Add Bot to Server - Discord Purple Theme) */}
          <div className="flex items-center gap-1">
            <a
              href={activeInviteUrl || '#'}
              target={activeInviteUrl ? '_blank' : undefined}
              rel={activeInviteUrl ? 'noopener noreferrer' : undefined}
              onClick={(e) => {
                if (!activeInviteUrl) {
                  e.preventDefault();
                  setShowInviteModal(true);
                }
              }}
              className="px-3.5 py-1.5 text-xs sm:text-sm font-semibold rounded-lg bg-[#5865F2] hover:bg-[#4752C4] active:bg-[#3c45a5] active:scale-95 text-white shadow-md shadow-[#5865F2]/25 hover:shadow-lg hover:shadow-[#5865F2]/40 border border-[#7983F5]/40 transition-all flex items-center gap-2 cursor-pointer select-none group"
              title="UBAgen Discord botunu kendi sunucunuza ekleyin (OAuth2)"
            >
              <DiscordLogo className="w-4 h-4 shrink-0 transition-transform group-hover:scale-110" />
              <span>Sunucuya Ekle</span>
              <ExternalLink className="w-3.5 h-3.5 opacity-80 group-hover:opacity-100 transition-opacity" />
            </a>

            {/* Invite Modal / Options Trigger */}
            <button
              onClick={() => setShowInviteModal(true)}
              className="p-1.5 sm:p-2 text-xs font-medium rounded-lg bg-[#5865F2]/15 hover:bg-[#5865F2]/25 text-[#949cf7] hover:text-white border border-[#5865F2]/30 transition-colors cursor-pointer"
              title="Davet linki seçenekleri ve yetki ayarları"
            >
              <Share2 className="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </header>

      <RcloneManagement inline />

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

        {/* Bot Restart Status Feedback Banner */}
        {restartFeedback && (
          <div
            role="status"
            className={`rounded-2xl p-4 flex items-start justify-between gap-4 shadow-lg border ${
              restartFeedback.type === 'success'
                ? 'bg-emerald-950/40 border-emerald-500/40 text-emerald-100'
                : restartFeedback.type === 'cancelled'
                ? 'bg-slate-900/90 border-slate-700/80 text-slate-200'
                : 'bg-rose-950/50 border-rose-500/40 text-rose-100'
            }`}
          >
            <div className="flex items-start gap-3">
              <div
                className={`p-2 rounded-xl shrink-0 mt-0.5 ${
                  restartFeedback.type === 'success'
                    ? 'bg-emerald-500/20 text-emerald-400'
                    : restartFeedback.type === 'cancelled'
                    ? 'bg-slate-800 text-slate-400'
                    : 'bg-rose-500/20 text-rose-400'
                }`}
              >
                {restartFeedback.type === 'success' ? (
                  <CheckCircle2 className="w-5 h-5" />
                ) : restartFeedback.type === 'cancelled' ? (
                  <HelpCircle className="w-5 h-5" />
                ) : (
                  <AlertTriangle className="w-5 h-5" />
                )}
              </div>
              <div>
                <h3 className="text-sm font-bold">{restartFeedback.title}</h3>
                <p className="text-xs opacity-90 mt-0.5">{restartFeedback.message}</p>
              </div>
            </div>
            <button
              type="button"
              onClick={() => setRestartFeedback(null)}
              className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800/60 transition-colors cursor-pointer"
              aria-label="Bildirimi kapat"
            >
              <X className="w-4 h-4" />
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
                {isPaused ? 'Paused' : 'Every 5 seconds'}
              </strong>
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            {/* 1. HTTP Alive State */}
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border bg-emerald-500/10 border-emerald-500/30 text-emerald-300 font-mono text-[11px]">
              <span className="w-1.5 h-1.5 rounded-sm bg-emerald-400" />
              HTTP Alive: <strong>{error ? 'DEGRADED' : 'ONLINE'}</strong>
            </span>

            {/* 2. Discord Gateway Connection State */}
            <span
              className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border font-mono text-[11px] ${
                stats?.bot?.connected
                  ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
                  : stats?.bot?.status === 'connecting' || stats?.bot?.wake_in_progress
                  ? 'bg-sky-500/10 border-sky-500/30 text-sky-300'
                  : 'bg-rose-500/10 border-rose-500/30 text-rose-300'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-sm ${
                  stats?.bot?.connected
                    ? 'bg-emerald-400'
                    : stats?.bot?.status === 'connecting' || stats?.bot?.wake_in_progress
                    ? 'bg-sky-400 animate-pulse'
                    : 'bg-rose-400'
                }`}
              />
              Discord Gateway:{' '}
              <strong>
                {stats?.bot?.connected
                  ? 'CONNECTED'
                  : stats?.bot?.status === 'connecting' || stats?.bot?.wake_in_progress
                  ? 'CONNECTING'
                  : 'DISCONNECTED'}
              </strong>
            </span>

            {/* 3. Bot Ready State (Never shown online unless is_ready === true) */}
            <span
              className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-md border font-mono text-[11px] ${
                stats?.bot?.is_ready
                  ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
                  : stats?.bot?.connected || stats?.bot?.status === 'connecting' || stats?.bot?.wake_in_progress
                  ? 'bg-amber-500/10 border-amber-500/30 text-amber-300'
                  : 'bg-slate-800 border-slate-700 text-slate-300'
              }`}
            >
              {stats?.bot?.is_ready ? (
                <>
                  <CheckCircle2 className="w-3 h-3 text-emerald-400" />
                  Bot Ready: <strong>READY ({stats.bot.latency_ms !== null ? `${stats.bot.latency_ms} ms` : 'OK'})</strong>
                </>
              ) : stats?.bot?.connected || stats?.bot?.status === 'connecting' || stats?.bot?.wake_in_progress ? (
                <>
                  <RefreshCw className="w-3 h-3 text-amber-400 animate-spin" />
                  Bot Ready: <strong>{stats?.bot?.wake_in_progress ? 'WAKING' : 'HANDSHAKING'}</strong>
                </>
              ) : stats?.bot?.status === 'invalid_token' ? (
                <>
                  <XCircle className="w-3 h-3 text-rose-400" />
                  Bot Ready: <strong>AUTH FAILED</strong>
                </>
              ) : stats?.bot?.status === 'not_configured' ? (
                <>
                  <HelpCircle className="w-3 h-3 text-slate-400" />
                  Bot Ready: <strong>STANDBY (WEB MODE)</strong>
                </>
              ) : (
                <>
                  <XCircle className="w-3 h-3 text-rose-400" />
                  Bot Ready: <strong>NOT READY</strong>
                </>
              )}
            </span>
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

                <div className="mt-3 text-xs space-y-1 text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Persistence:</span>
                    <span
                      className={`font-mono font-semibold ${
                        stats.disk.is_persistent ? 'text-emerald-400' : 'text-amber-400'
                      }`}
                    >
                      {stats.disk.is_persistent
                        ? 'Persistent Disk'
                        : `Ephemeral (${stats.disk.fs_type || 'overlay'})`}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Outputs / Temp:</span>
                    <span className="font-mono text-slate-200">
                      {stats.disk.video_outputs_str || '0.00 B'} / {stats.disk.temp_workspace_str || '0.00 B'}
                    </span>
                  </div>
                </div>
              </div>

              <div className="border-t border-slate-800/80 pt-3 mt-4 text-xs flex items-center justify-between text-slate-400">
                <span>Mount &amp; FS:</span>
                <span className="text-slate-200 font-mono font-medium">
                  {stats.disk.mount_point || '/'} ({stats.disk.fs_type || 'root'})
                </span>
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
                      stats.bot.is_ready
                        ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30'
                        : stats.bot.connected || stats.bot.status === 'connecting' || stats.bot.wake_in_progress
                        ? 'bg-sky-500/10 text-sky-400 border-sky-500/30'
                        : stats.bot.status === 'invalid_token'
                        ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                        : stats.bot.status === 'network_error'
                        ? 'bg-amber-500/10 text-amber-400 border-amber-500/30'
                        : stats.bot.status === 'not_configured'
                        ? 'bg-slate-800 text-slate-400 border-slate-700'
                        : 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                    }`}
                  >
                    <span
                      className={`w-1.5 h-1.5 rounded-sm ${
                        stats.bot.is_ready
                          ? 'bg-emerald-400'
                          : stats.bot.connected || stats.bot.status === 'connecting' || stats.bot.wake_in_progress
                          ? 'bg-sky-400 animate-pulse'
                          : stats.bot.status === 'network_error'
                          ? 'bg-amber-400'
                          : stats.bot.status === 'not_configured'
                          ? 'bg-slate-400'
                          : 'bg-rose-400'
                      }`}
                    />
                    {stats.bot.is_ready
                      ? 'Ready'
                      : stats.bot.wake_in_progress
                      ? 'Waking'
                      : stats.bot.connected
                      ? 'Handshaking'
                      : stats.bot.status === 'invalid_token'
                      ? 'Auth Failed'
                      : stats.bot.status === 'connecting'
                      ? 'Connecting'
                      : stats.bot.status === 'network_error'
                      ? 'Network Error'
                      : stats.bot.status === 'not_configured'
                      ? 'Not Configured'
                      : 'Offline'}
                  </span>
                </div>

                <div className="text-lg font-bold font-mono text-white mt-1 truncate">
                  {stats.bot.user}
                </div>
                <p className="text-xs text-slate-400 mt-1">
                  {stats.bot.is_ready
                    ? `Ready across ${stats.bot.guild_count} Discord server(s)`
                    : stats.bot.connected
                    ? 'Gateway WebSocket open — waiting for guild & command readiness'
                    : stats.bot.status === 'invalid_token'
                    ? 'DISCORD_BOT_TOKEN rejected by Discord Gateway'
                    : stats.bot.status === 'not_configured'
                    ? 'Running in Web Monitor mode (token not provided)'
                    : 'Wake-on-Command ready or reconnecting to Discord Gateway'}
                </p>

                {/* Separate Tri-State Breakdown: HTTP Alive vs Gateway vs Bot Ready */}
                <div className="mt-3 grid grid-cols-3 gap-1.5 text-[11px] font-mono">
                  <div className="p-1.5 rounded-lg bg-slate-950/80 border border-slate-800/80 flex flex-col">
                    <span className="text-[10px] text-slate-400 uppercase">HTTP Alive</span>
                    <span className="text-emerald-400 font-semibold mt-0.5">ONLINE</span>
                  </div>
                  <div className="p-1.5 rounded-lg bg-slate-950/80 border border-slate-800/80 flex flex-col">
                    <span className="text-[10px] text-slate-400 uppercase">Gateway WS</span>
                    <span
                      className={`font-semibold mt-0.5 ${
                        stats.bot.connected ? 'text-emerald-400' : 'text-rose-400'
                      }`}
                    >
                      {stats.bot.connected ? 'CONNECTED' : 'CLOSED'}
                    </span>
                  </div>
                  <div className="p-1.5 rounded-lg bg-slate-950/80 border border-slate-800/80 flex flex-col">
                    <span className="text-[10px] text-slate-400 uppercase">Bot Ready</span>
                    <span
                      className={`font-semibold mt-0.5 ${
                        stats.bot.is_ready
                          ? 'text-emerald-400'
                          : stats.bot.connected || stats.bot.wake_in_progress
                          ? 'text-amber-400'
                          : 'text-rose-400'
                      }`}
                    >
                      {stats.bot.is_ready ? 'READY' : stats.bot.connected ? 'SYNCING' : 'OFFLINE'}
                    </span>
                  </div>
                </div>

                {/* Safe Diagnostic Message Box */}
                {stats.bot.diagnostic_message && (
                  <div className="mt-2.5 p-2 rounded-xl bg-slate-950/70 border border-slate-800/80 text-[11px] text-slate-300">
                    <span className="font-semibold text-slate-400 block text-[10px] uppercase tracking-wider mb-0.5">
                      Diagnostic Status
                    </span>
                    <span className="leading-relaxed block">
                      {stats.bot.diagnostic_message}
                    </span>
                  </div>
                )}

                <div className="mt-3 text-xs space-y-1.5 text-slate-300">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Gateway Latency:</span>
                    <span className="font-mono text-indigo-300 font-semibold">
                      {stats.bot.is_ready && stats.bot.latency_ms !== null
                        ? `${stats.bot.latency_ms} ms`
                        : 'Offline'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Wake-on-Command:</span>
                    <span
                      className={`font-mono text-[11px] font-semibold ${
                        stats.bot.interactions_endpoint_configured
                          ? 'text-emerald-400'
                          : 'text-sky-300'
                      }`}
                      title="POST /api/discord/interactions (Ed25519) & POST /api/discord/wake"
                    >
                      {stats.bot.interactions_endpoint_configured
                        ? `Ed25519 Active (${stats.bot.wake_attempts ?? 0} wakes)`
                        : `HTTP /api/discord/wake (${stats.bot.wake_attempts ?? 0} wakes)`}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Slash Commands:</span>
                    <span className="font-mono text-emerald-400 text-[11px]">
                      /encode, /encodeal, /is_durumu, /discord, /stats
                    </span>
                  </div>
                  {stats.bot.sync_status && (
                    <div className="flex justify-between text-[11px] pt-0.5 border-t border-slate-800/60">
                      <span className="text-slate-400">Sync:</span>
                      <span className="font-mono text-slate-300 truncate max-w-[180px]" title={stats.bot.sync_status}>
                        {stats.bot.sync_status}
                      </span>
                    </div>
                  )}
                </div>

                {/* Direct Server Invite Button in Card */}
                <div className="mt-3 pt-2.5 border-t border-slate-800/80 flex items-center justify-between gap-2">
                  <span className="text-[11px] text-slate-400">Sunucu Bağlantısı:</span>
                  <a
                    href={activeInviteUrl || '#'}
                    target={activeInviteUrl ? '_blank' : undefined}
                    rel={activeInviteUrl ? 'noopener noreferrer' : undefined}
                    onClick={(e) => {
                      if (!activeInviteUrl) {
                        e.preventDefault();
                        setShowInviteModal(true);
                      }
                    }}
                    className="inline-flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-md bg-[#5865F2] hover:bg-[#4752C4] active:bg-[#3c45a5] text-white shadow-sm transition-all cursor-pointer"
                    title="Botu kendi Discord sunucunuza ekleyin"
                  >
                    <DiscordLogo className="w-3.5 h-3.5" />
                    <span>Sunucuya Ekle</span>
                    <ExternalLink className="w-3 h-3 opacity-80" />
                  </a>
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

        {/* Storage Infrastructure & Persistence Decision Tree Section */}
        {stats && (
          <div className="bg-slate-900/60 border border-slate-800/80 rounded-2xl p-5 shadow-lg space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h3 className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-2">
                <HardDrive className="w-4 h-4 text-indigo-400" />
                Storage Infrastructure &amp; Persistence Audit
              </h3>
              <span
                className={`text-xs font-mono font-semibold ${
                  stats.disk.is_persistent ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {stats.disk.decision_branch || 'B_EPHEMERAL_CONTAINER_STORAGE'} &bull;{' '}
                {stats.disk.writable !== false ? 'Writable' : 'Read-Only'}
              </span>
            </div>

            {stats.disk.summary && (
              <p className="text-xs text-slate-300 leading-relaxed bg-slate-950/70 border border-slate-800 rounded-xl p-3">
                {stats.disk.summary}
              </p>
            )}

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
              <div className="bg-slate-950/70 border border-slate-800 rounded-xl p-3">
                <span className="text-slate-400 block font-semibold">
                  1. Persistent Video Outputs (GCS • 7200s TTL)
                </span>
                <span
                  className="font-mono text-emerald-300 block truncate mt-0.5"
                  title={
                    stats.storage?.separation?.persistent_output_storage?.gcs_bucket
                      ? `gs://${stats.storage.separation.persistent_output_storage.gcs_bucket}`
                      : stats.disk.video_outputs_dir || 'video_outputs'
                  }
                >
                  {stats.storage?.separation?.persistent_output_storage?.gcs_bucket
                    ? `gs://${stats.storage.separation.persistent_output_storage.gcs_bucket}`
                    : stats.disk.video_outputs_dir || 'video_outputs (GCS Unconfigured)'}
                </span>
                <span className="text-[11px] text-slate-400 mt-1 block">
                  Retention: {stats.storage?.separation?.persistent_output_storage?.retention_seconds ?? 7200}s (2h) &bull; Active GCS Deletion
                </span>
              </div>

              <div className="bg-slate-950/70 border border-slate-800 rounded-xl p-3">
                <span className="text-slate-400 block font-semibold">
                  2. Ephemeral Worker Scratch Disk
                </span>
                <span
                  className="font-mono text-amber-300 block truncate mt-0.5"
                  title={
                    stats.storage?.separation?.ephemeral_worker_scratch_disk?.worker_scratch_dir ||
                    stats.disk.download_temp_dir ||
                    'download_temp/worker_jobs'
                  }
                >
                  {stats.storage?.separation?.ephemeral_worker_scratch_disk?.worker_scratch_dir ||
                    stats.disk.download_temp_dir ||
                    'download_temp/worker_jobs'}
                </span>
                <span className="text-[11px] text-slate-400 mt-1 block">
                  Temp Usage: {stats.disk.temp_workspace_str || '0.00 B'} (Isolated per-job &bull; Wiped in finally)
                </span>
              </div>

              <div className="bg-slate-950/70 border border-slate-800 rounded-xl p-3">
                <span className="text-slate-400 block font-semibold">
                  3. Job State Database (Non-Memory)
                </span>
                <span className="font-mono text-indigo-300 block truncate mt-0.5">
                  {stats.storage?.separation?.job_state_database?.backend_type || 'sqlite_wal_persistent'}
                </span>
                <span className="text-[11px] text-slate-400 mt-1 block truncate">
                  Collection: {stats.storage?.separation?.job_state_database?.firestore_collection || 'ubagen_encode_jobs'}
                </span>
              </div>

              <div className="bg-slate-950/70 border border-slate-800 rounded-xl p-3">
                <span className="text-slate-400 block font-semibold">
                  4. Cloud Run Job Worker Profile
                </span>
                <span className="font-mono text-slate-200 block truncate mt-0.5">
                  {stats.storage?.separation?.cloud_run_architecture?.cloud_run_job_name ||
                    'ubagen-encoder-worker (4 vCPU / 8 GiB)'}
                </span>
                <span className="text-[11px] text-slate-400 mt-1 block">
                  Timeout: 3600s &bull; Parallelism: 1 &bull; Max Retries: 1
                </span>
              </div>
            </div>

            {encodeJobs.length > 0 && (
              <div className="pt-2">
                <h4 className="text-xs font-semibold uppercase tracking-wider text-slate-400 mb-2">
                  Recent Cloud Encoder Jobs (H.264 / H.265 Telemetry)
                </h4>
                <div className="overflow-x-auto rounded-xl border border-slate-800 bg-slate-950/70">
                  <table className="w-full text-left text-xs">
                    <thead className="border-b border-slate-800 text-slate-400 bg-slate-900/60">
                      <tr>
                        <th className="px-3 py-2">Job ID</th>
                        <th className="px-3 py-2">User ID</th>
                        <th className="px-3 py-2">Codec</th>
                        <th className="px-3 py-2">Stage</th>
                        <th className="px-3 py-2">Progress</th>
                        <th className="px-3 py-2">Frames</th>
                        <th className="px-3 py-2">FPS / ETA</th>
                        <th className="px-3 py-2">7200s TTL</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/70 font-mono">
                      {encodeJobs.map((j) => (
                        <tr key={j.job_id} className="text-slate-200">
                          <td className="px-3 py-2 truncate max-w-[140px]" title={j.job_id}>
                            {j.job_id}
                          </td>
                          <td className="px-3 py-2">{j.user_id}</td>
                          <td className="px-3 py-2 uppercase">
                            {j.codec} ({j.video_encoder})
                          </td>
                          <td className="px-3 py-2">{j.stage}</td>
                          <td className="px-3 py-2">{j.percent.toFixed(1)}%</td>
                          <td className="px-3 py-2">
                            {j.processed_frames}
                            {j.total_frames ? ` / ${j.total_frames}` : ''}
                          </td>
                          <td className="px-3 py-2">
                            {j.fps.toFixed(1)} FPS &bull;{' '}
                            {j.eta_seconds !== null ? `${j.eta_seconds}s` : '-'}
                          </td>
                          <td className="px-3 py-2">
                            {j.deleted_from_gcs || j.status === 'expired'
                              ? 'Deleted from GCS'
                              : j.expires_in_seconds !== null
                              ? `${j.expires_in_seconds}s left`
                              : `${j.retention_seconds}s policy`}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
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

      {/* Discord Bot Restart Confirmation & Auth Modal */}
      {showRestartModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-in fade-in duration-150"
          onClick={handleCancelRestartBot}
        >
          <div
            className="w-full max-w-md bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl overflow-hidden animate-in zoom-in-95 duration-150"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="bg-rose-600/20 border-b border-rose-500/30 px-6 py-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-lg bg-rose-500/20 border border-rose-500/30 flex items-center justify-center text-rose-400">
                  <RefreshCw className={`w-5 h-5 ${restartingBot ? 'animate-spin' : ''}`} />
                </div>
                <div>
                  <h3 className="font-bold text-base text-white leading-tight">
                    Discord Botunu Yeniden Başlat
                  </h3>
                  <p className="text-xs text-rose-200/80 mt-0.5">
                    Onay ve Yönetici Doğrulaması
                  </p>
                </div>
              </div>
              <button
                type="button"
                onClick={handleCancelRestartBot}
                disabled={restartingBot}
                className="p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-colors cursor-pointer disabled:opacity-50"
                aria-label="İptal"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <form onSubmit={handleConfirmRestartBot} className="p-6 space-y-4">
              <p className="text-xs text-slate-300 leading-relaxed">
                Discord botunu yeniden başlatmak istediğinizden emin misiniz? Mevcut Discord Gateway
                bağlantısı güvenli şekilde kapatılıp tekrar bağlanacaktır. Web izleme sunucusu
                kesintisiz çalışmaya devam eder.
              </p>

              <div className="p-3 rounded-xl bg-slate-950/70 border border-slate-800 text-[11px] text-slate-400 space-y-1">
                <span className="font-semibold text-slate-300 block">Hedef Endpoint:</span>
                <code className="font-mono text-indigo-300 break-all block">
                  POST {resolvedRestartEndpointPreview}
                </code>
              </div>

              <div>
                <label
                  htmlFor="ubagen-admin-token-input"
                  className="block text-xs font-semibold text-slate-200 mb-1.5"
                >
                  Sunucu Yönetici Token&apos;ı (<code>UBAGEN_ADMIN_TOKEN</code>):
                </label>
                <input
                  id="ubagen-admin-token-input"
                  type="password"
                  autoComplete="off"
                  value={restartAdminTokenInput}
                  onChange={(e) => setRestartAdminTokenInput(e.target.value)}
                  disabled={restartingBot}
                  placeholder="Sunucuda tanımlı UBAGEN_ADMIN_TOKEN değerini girin"
                  className="w-full bg-slate-950 border border-slate-700 rounded-xl px-3.5 py-2 text-xs text-slate-100 font-mono focus:outline-none focus:border-rose-500 disabled:opacity-50"
                  autoFocus
                />
                <span className="text-[11px] text-slate-500 mt-1 block">
                  Token yalnızca <code>Authorization: Bearer</code> başlığı ile gönderilir, tarayıcıda saklanmaz.
                </span>
              </div>

              {restartFeedback && restartFeedback.type === 'error' && (
                <div className="p-3 rounded-xl bg-rose-950/60 border border-rose-500/40 text-xs text-rose-200">
                  <strong className="block font-semibold text-rose-100">
                    {restartFeedback.title}
                  </strong>
                  <span className="mt-0.5 block">{restartFeedback.message}</span>
                </div>
              )}

              <div className="pt-2 flex items-center justify-end gap-2.5">
                <button
                  type="button"
                  onClick={handleCancelRestartBot}
                  disabled={restartingBot}
                  className="px-4 py-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-semibold transition-colors cursor-pointer disabled:opacity-50"
                >
                  İptal
                </button>
                <button
                  type="submit"
                  disabled={restartingBot}
                  className="px-4 py-2 rounded-xl bg-rose-600 hover:bg-rose-500 text-white text-xs font-semibold shadow-lg shadow-rose-600/25 transition-all flex items-center gap-2 cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${restartingBot ? 'animate-spin' : ''}`} />
                  <span>{restartingBot ? 'Yeniden Başlatılıyor…' : 'Onayla ve Yeniden Başlat'}</span>
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Discord Bot Server Invite Modal */}
      {showInviteModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-sm animate-in fade-in duration-150"
          onClick={() => setShowInviteModal(false)}
        >
          <div
            className="w-full max-w-lg bg-slate-900 border border-slate-800 rounded-2xl shadow-2xl overflow-hidden animate-in zoom-in-95 duration-150"
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="bg-[#5865F2] px-6 py-4 flex items-center justify-between text-white shadow-md">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-lg bg-white/10 flex items-center justify-center backdrop-blur-sm">
                  <DiscordLogo className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="font-bold text-base leading-tight">
                    Discord Botunu Sunucunuza Ekleyin
                  </h3>
                  <p className="text-xs text-white/80 mt-0.5">
                    OAuth2 Sunucu Yetkilendirmesi &amp; Komut Senkronizasyonu
                  </p>
                </div>
              </div>
              <button
                onClick={() => setShowInviteModal(false)}
                className="p-1.5 rounded-lg text-white/80 hover:text-white hover:bg-white/10 transition-colors cursor-pointer"
                aria-label="Kapat"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-6 space-y-5">
              {/* Bot Info & Status */}
              <div className="p-3.5 rounded-xl bg-slate-950/70 border border-slate-800/80 flex items-center justify-between gap-3">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-full bg-[#5865F2]/20 border border-[#5865F2]/40 flex items-center justify-center text-[#5865F2] font-bold">
                    <Bot className="w-5 h-5" />
                  </div>
                  <div>
                    <span className="text-sm font-bold text-white block">
                      {stats?.bot?.user && stats.bot.user !== 'Disconnected'
                        ? stats.bot.user
                        : 'UBAgen Bot'}
                    </span>
                    <span className="text-xs text-slate-400 block font-mono">
                      Client ID: {effectiveClientId || 'Algılanamadı'}
                    </span>
                  </div>
                </div>

                {stats?.bot?.connected ? (
                  <span className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
                    Gateway Online
                  </span>
                ) : (
                  <span className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-semibold rounded-full bg-slate-800 text-slate-400 border border-slate-700">
                    Beklemede
                  </span>
                )}
              </div>

              {/* Permission Presets */}
              <div>
                <label className="text-xs font-semibold text-slate-300 block mb-2">
                  Yetki Seviyesi Seçin:
                </label>
                <div className="grid grid-cols-3 gap-2">
                  <button
                    type="button"
                    onClick={() => setPermissionPreset('recommended')}
                    className={`p-2.5 rounded-xl text-left border transition-all cursor-pointer ${
                      permissionPreset === 'recommended'
                        ? 'bg-[#5865F2]/15 border-[#5865F2] text-white shadow-sm'
                        : 'bg-slate-950/50 border-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    <div className="font-semibold text-xs flex items-center gap-1">
                      <span>Önerilen</span>
                      {permissionPreset === 'recommended' && (
                        <Check className="w-3 h-3 text-[#5865F2]" />
                      )}
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5 leading-snug">
                      Slash Komutları + Mesaj
                    </div>
                  </button>

                  <button
                    type="button"
                    onClick={() => setPermissionPreset('admin')}
                    className={`p-2.5 rounded-xl text-left border transition-all cursor-pointer ${
                      permissionPreset === 'admin'
                        ? 'bg-[#5865F2]/15 border-[#5865F2] text-white shadow-sm'
                        : 'bg-slate-950/50 border-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    <div className="font-semibold text-xs flex items-center gap-1">
                      <span>Yönetici</span>
                      {permissionPreset === 'admin' && (
                        <Check className="w-3 h-3 text-[#5865F2]" />
                      )}
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5 leading-snug">
                      Administrator (Tam yetki)
                    </div>
                  </button>

                  <button
                    type="button"
                    onClick={() => setPermissionPreset('basic')}
                    className={`p-2.5 rounded-xl text-left border transition-all cursor-pointer ${
                      permissionPreset === 'basic'
                        ? 'bg-[#5865F2]/15 border-[#5865F2] text-white shadow-sm'
                        : 'bg-slate-950/50 border-slate-800 text-slate-400 hover:text-slate-200'
                    }`}
                  >
                    <div className="font-semibold text-xs flex items-center gap-1">
                      <span>Temel</span>
                      {permissionPreset === 'basic' && (
                        <Check className="w-3 h-3 text-[#5865F2]" />
                      )}
                    </div>
                    <div className="text-[10px] text-slate-400 mt-0.5 leading-snug">
                      Yetkisiz / Basic Scope
                    </div>
                  </button>
                </div>
              </div>

              {/* Direct Invite URL Input & Copy */}
              <div>
                <label className="text-xs font-semibold text-slate-300 block mb-1.5">
                  Resmi Discord Davet Bağlantısı (OAuth2):
                </label>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    readOnly
                    value={activeInviteUrl || ''}
                    className="flex-1 bg-slate-950 border border-slate-800 rounded-xl px-3 py-2 text-xs font-mono text-slate-300 focus:outline-none focus:border-[#5865F2] select-all"
                  />
                  <button
                    type="button"
                    onClick={handleCopyInviteLink}
                    className={`px-3 py-2 text-xs font-medium rounded-xl border transition-all flex items-center gap-1.5 cursor-pointer shrink-0 ${
                      copiedLink
                        ? 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40'
                        : 'bg-slate-800 hover:bg-slate-700 text-slate-200 border-slate-700/80'
                    }`}
                  >
                    {copiedLink ? (
                      <>
                        <Check className="w-3.5 h-3.5 text-emerald-400" />
                        <span>Kopyalandı!</span>
                      </>
                    ) : (
                      <>
                        <Copy className="w-3.5 h-3.5" />
                        <span>Kopyala</span>
                      </>
                    )}
                  </button>
                </div>
              </div>

              {/* Action Buttons */}
              <div className="pt-2 flex flex-col sm:flex-row items-center gap-3">
                <a
                  href={activeInviteUrl || '#'}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="w-full sm:flex-1 py-2.5 px-4 rounded-xl bg-[#5865F2] hover:bg-[#4752C4] active:bg-[#3c45a5] text-white font-semibold text-sm shadow-lg shadow-[#5865F2]/25 border border-[#7983F5]/40 transition-all flex items-center justify-center gap-2 cursor-pointer text-center"
                >
                  <DiscordLogo className="w-4 h-4" />
                  <span>Discord'da Aç ve Sunucuya Ekle</span>
                  <ExternalLink className="w-4 h-4 opacity-80" />
                </a>
                <button
                  type="button"
                  onClick={() => setShowInviteModal(false)}
                  className="w-full sm:w-auto py-2.5 px-4 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-sm font-medium transition-colors cursor-pointer"
                >
                  Kapat
                </button>
              </div>

              {/* Instructions Guide */}
              <div className="p-3.5 rounded-xl bg-slate-950/40 border border-slate-800/60 text-xs text-slate-400 space-y-1.5">
                <span className="font-semibold text-slate-300 block mb-1">
                  💡 Nasıl Kullanılır:
                </span>
                <p>
                  1. <strong>"Discord'da Aç ve Sunucuya Ekle"</strong> butonuna tıklayın.
                </p>
                <p>
                  2. Açılan Discord yetkilendirme penceresinde yönetici olduğunuz sunucuyu seçip <strong>"Yetkilendir"</strong>e basın.
                </p>
                <p>
                  3. Sunucunuzda herhangi bir metin kanalında <code className="text-indigo-300 bg-slate-800 px-1 py-0.5 rounded font-mono">/stats</code> yazarak canlı sistem raporunu anında görün.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
