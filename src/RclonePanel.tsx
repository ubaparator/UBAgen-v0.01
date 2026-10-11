import React, { useState, useEffect, useCallback } from 'react';
import {
  HardDrive,
  Cloud,
  CheckCircle2,
  AlertTriangle,
  XCircle,
  RefreshCw,
  Plus,
  Link2,
  Unlink,
  Play,
  ShieldCheck,
  Clock,
  Terminal,
  ExternalLink,
  Copy,
  Check,
  Power,
  FileWarning,
  Layers,
} from 'lucide-react';
import { resolveApiUrl } from './restartBot';

export interface RcloneAccount {
  remote_name: string;
  provider: 'gdrive' | 'mega' | string;
  display_name: string;
  account_email: string | null;
  status: 'connected' | 'pending_auth' | 'disconnected' | 'error' | string;
  enabled: boolean;
  root_folder: string;
  storage_total_bytes: number | null;
  storage_used_bytes: number | null;
  storage_free_bytes: number | null;
  storage_total_str: string;
  storage_used_str: string;
  storage_free_str: string;
  last_verified_at: number | null;
  last_error: string | null;
  created_at: number;
  updated_at: number;
  has_credentials: boolean;
}

export interface RclonePendingDeletion {
  deletion_id: string;
  job_id: string | null;
  remote_name: string;
  provider: string;
  remote_path: string;
  created_at: number;
  due_at: number;
  due_in_seconds: number;
  is_due: boolean;
  status: 'pending' | 'completed' | 'failed' | string;
  attempts: number;
  last_error: string | null;
  completed_at: number | null;
}

export interface RcloneStatusSummary {
  rclone_binary: {
    installed: boolean;
    version: string;
    path: string | null;
    error?: string | null;
  };
  config_path: string;
  db_path: string;
  accounts: RcloneAccount[];
  storage_summary: {
    total_accounts: number;
    connected_accounts: number;
    enabled_accounts: number;
    mega_accounts: number;
    gdrive_accounts: number;
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    total_str: string;
    used_str: string;
    free_str: string;
  };
  pending_deletions: RclonePendingDeletion[];
  pending_deletions_count: number;
  due_deletions_count: number;
}

export interface DiagnosticsLogResponse {
  status: string;
  formatted_report: string;
  diagnostics: {
    timestamp: string;
    rclone: {
      installed: boolean;
      version: string;
      path: string | null;
      config_path: string;
      accounts_total: number;
      connected_count: number;
      pending_deletions_count: number;
    };
    discord: {
      status: string;
      connected: boolean;
      is_ready: boolean;
      registered_slash_commands: string[];
      duplicate_slash_commands: string[];
      removed_drive_commands_present: string[];
    };
    recent_errors: Array<{
      timestamp: string;
      level: string;
      logger: string;
      message: string;
      traceback?: string | null;
    }>;
  } | null;
}

interface RclonePanelProps {
  onStorageChanged?: () => void;
}

export const RclonePanel: React.FC<RclonePanelProps> = ({ onStorageChanged }) => {
  const [statusData, setStatusData] = useState<RcloneStatusSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshingQuota, setRefreshingQuota] = useState<boolean>(false);
  const [banner, setBanner] = useState<{ type: 'success' | 'error' | 'info'; message: string } | null>(null);

  // Google Drive built-in rclone OAuth form state
  const [gdriveDisplayName, setGdriveDisplayName] = useState<string>('');
  const [gdriveRemoteName, setGdriveRemoteName] = useState<string>('');
  const [gdriveRootFolder, setGdriveRootFolder] = useState<string>('UBAgen_Outputs');
  const [gdriveScope, setGdriveScope] = useState<string>('drive.file');
  const [gdriveSession, setGdriveSession] = useState<{
    session_id?: string;
    remote_name: string;
    display_name: string;
    auth_url?: string | null;
    mode?: string;
    instructions?: string;
    authorize_command?: string;
  } | null>(null);
  const [gdriveCodeOrToken, setGdriveCodeOrToken] = useState<string>('');
  const [gdriveBusy, setGdriveBusy] = useState<boolean>(false);
  const [copiedUrl, setCopiedUrl] = useState<boolean>(false);

  // MEGA account form state
  const [megaDisplayName, setMegaDisplayName] = useState<string>('');
  const [megaEmail, setMegaEmail] = useState<string>('');
  const [megaPassword, setMegaPassword] = useState<string>('');
  const [megaRemoteName, setMegaRemoteName] = useState<string>('');
  const [megaRootFolder, setMegaRootFolder] = useState<string>('UBAgen_Outputs');
  const [megaBusy, setMegaBusy] = useState<boolean>(false);

  // Per-account action state
  const [accountActionBusy, setAccountActionBusy] = useState<Record<string, string>>({});
  const [reconnectModalRemote, setReconnectModalRemote] = useState<RcloneAccount | null>(null);
  const [reconnectCredential, setReconnectCredential] = useState<string>('');

  // Diagnostics (/log) state
  const [diagData, setDiagData] = useState<DiagnosticsLogResponse | null>(null);
  const [diagLoading, setDiagLoading] = useState<boolean>(false);
  const [showDiagPanel, setShowDiagPanel] = useState<boolean>(false);

  const fetchRcloneStatus = useCallback(async (refreshQuota = false) => {
    if (refreshQuota) {
      setRefreshingQuota(true);
    }
    try {
      const url = resolveApiUrl(`/api/rclone/status${refreshQuota ? '?refresh=1' : ''}`);
      const res = await fetch(url, { headers: { Accept: 'application/json' } });
      if (res.ok) {
        const data = (await res.json()) as RcloneStatusSummary;
        setStatusData(data);
      }
    } catch (err: any) {
      console.warn('Rclone status fetch warning:', err);
    } finally {
      setLoading(false);
      setRefreshingQuota(false);
    }
  }, []);

  const fetchDiagnosticsLog = useCallback(async (liveTests = false) => {
    setDiagLoading(true);
    try {
      const url = resolveApiUrl(`/api/diagnostics/log${liveTests ? '?live_tests=1' : ''}`);
      const res = await fetch(url, { headers: { Accept: 'application/json' } });
      if (res.ok) {
        const data = (await res.json()) as DiagnosticsLogResponse;
        setDiagData(data);
      }
    } catch (err) {
      console.warn('Diagnostics log fetch warning:', err);
    } finally {
      setDiagLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchRcloneStatus(false);
    const interval = setInterval(() => fetchRcloneStatus(false), 12000);
    return () => clearInterval(interval);
  }, [fetchRcloneStatus]);

  const handleStartGdriveOAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    setBanner(null);
    setGdriveBusy(true);
    try {
      const res = await fetch(resolveApiUrl('/api/rclone/accounts/gdrive/start'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          display_name: gdriveDisplayName.trim() || 'Google Drive Hesabı',
          remote_name: gdriveRemoteName.trim() || undefined,
          root_folder: gdriveRootFolder.trim() || 'UBAgen_Outputs',
          scope: gdriveScope,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        setBanner({
          type: 'error',
          message: payload.error || 'Google Drive Rclone OAuth oturumu başlatılamadı.',
        });
      } else {
        setGdriveSession(payload);
        setBanner({
          type: 'info',
          message:
            payload.instructions ||
            'Google yetkilendirme bağlantısını açıp izin verin, ardından doğrulama kodunu aşağıya yapıştırın.',
        });
        await fetchRcloneStatus(false);
      }
    } catch (err: any) {
      setBanner({
        type: 'error',
        message: `Google Drive OAuth başlatma hatası: ${err?.message || 'Bağlantı hatası'}`,
      });
    } finally {
      setGdriveBusy(false);
    }
  };

  const handleCompleteGdriveOAuth = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!gdriveCodeOrToken.trim()) {
      setBanner({ type: 'error', message: 'Lütfen Google doğrulama kodunu veya rclone token JSON değerini girin.' });
      return;
    }
    setBanner(null);
    setGdriveBusy(true);
    try {
      const res = await fetch(resolveApiUrl('/api/rclone/accounts/gdrive/complete'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          session_id: gdriveSession?.session_id,
          remote_name: gdriveSession?.remote_name || gdriveRemoteName.trim() || undefined,
          display_name: gdriveSession?.display_name || gdriveDisplayName.trim() || 'Google Drive Hesabı',
          code_or_token: gdriveCodeOrToken.trim(),
          root_folder: gdriveRootFolder.trim() || 'UBAgen_Outputs',
          scope: gdriveScope,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        setBanner({
          type: 'error',
          message: payload.error || 'Google Drive yetkilendirmesi doğrulanamadı.',
        });
      } else {
        setBanner({
          type: 'success',
          message: payload.message || 'Google Drive hesabı Rclone üzerinden başarıyla bağlandı ve doğrulandı.',
        });
        setGdriveCodeOrToken('');
        setGdriveSession(null);
        setGdriveDisplayName('');
        setGdriveRemoteName('');
        await fetchRcloneStatus(true);
        onStorageChanged?.();
      }
    } catch (err: any) {
      setBanner({
        type: 'error',
        message: `Google Drive doğrulama hatası: ${err?.message || 'Bağlantı hatası'}`,
      });
    } finally {
      setGdriveBusy(false);
    }
  };

  const handleAddMegaAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!megaEmail.trim() || !megaPassword) {
      setBanner({ type: 'error', message: 'MEGA hesabı için e-posta ve parola zorunludur.' });
      return;
    }
    setBanner(null);
    setMegaBusy(true);
    try {
      const res = await fetch(resolveApiUrl('/api/rclone/accounts/mega'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          display_name: megaDisplayName.trim() || `MEGA (${megaEmail.trim()})`,
          email: megaEmail.trim(),
          password: megaPassword,
          remote_name: megaRemoteName.trim() || undefined,
          root_folder: megaRootFolder.trim() || 'UBAgen_Outputs',
          enabled: true,
        }),
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        setBanner({
          type: 'error',
          message: payload.error || 'MEGA hesabı doğrulanamadı.',
        });
      } else {
        setBanner({
          type: 'success',
          message: payload.message || 'MEGA hesabı Rclone üzerinden başarıyla eklendi ve bağlantı testi geçti.',
        });
        setMegaDisplayName('');
        setMegaEmail('');
        setMegaPassword('');
        setMegaRemoteName('');
        await fetchRcloneStatus(true);
        onStorageChanged?.();
      }
    } catch (err: any) {
      setBanner({
        type: 'error',
        message: `MEGA hesap ekleme hatası: ${err?.message || 'Bağlantı hatası'}`,
      });
    } finally {
      setMegaBusy(false);
    }
  };

  const handleTestAccount = async (remoteName: string) => {
    setBanner(null);
    setAccountActionBusy((prev) => ({ ...prev, [remoteName]: 'test' }));
    try {
      const res = await fetch(resolveApiUrl(`/api/rclone/accounts/${encodeURIComponent(remoteName)}/test`), {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok || payload.ok === false) {
        setBanner({
          type: 'error',
          message: `[${remoteName}] Bağlantı testi başarısız: ${payload.error || 'Uzak depolama erişilemez.'}`,
        });
      } else {
        setBanner({
          type: 'success',
          message: payload.message || `[${remoteName}] Bağlantı testi başarılı ve kota bilgileri güncellendi.`,
        });
      }
      await fetchRcloneStatus(false);
      onStorageChanged?.();
    } catch (err: any) {
      setBanner({ type: 'error', message: `Bağlantı testi hatası: ${err?.message || 'Bilinmeyen hata'}` });
    } finally {
      setAccountActionBusy((prev) => {
        const next = { ...prev };
        delete next[remoteName];
        return next;
      });
    }
  };

  const handleToggleAccount = async (acc: RcloneAccount) => {
    const remoteName = acc.remote_name;
    setBanner(null);
    setAccountActionBusy((prev) => ({ ...prev, [remoteName]: 'toggle' }));
    try {
      const res = await fetch(resolveApiUrl(`/api/rclone/accounts/${encodeURIComponent(remoteName)}/toggle`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ enabled: !acc.enabled }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && payload.ok !== false) {
        setBanner({
          type: 'info',
          message: `[${acc.display_name}] ${!acc.enabled ? 'aktif edildi' : 'pasif duruma alındı'}.`,
        });
        await fetchRcloneStatus(false);
        onStorageChanged?.();
      } else {
        setBanner({ type: 'error', message: payload.error || 'Hesap durumu güncellenemedi.' });
      }
    } finally {
      setAccountActionBusy((prev) => {
        const next = { ...prev };
        delete next[remoteName];
        return next;
      });
    }
  };

  const handleDisconnectAccount = async (acc: RcloneAccount) => {
    const remoteName = acc.remote_name;
    setBanner(null);
    setAccountActionBusy((prev) => ({ ...prev, [remoteName]: 'disconnect' }));
    try {
      const res = await fetch(resolveApiUrl(`/api/rclone/accounts/${encodeURIComponent(remoteName)}/disconnect`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ remove_record: false }),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && payload.ok !== false) {
        setBanner({
          type: 'info',
          message:
            payload.message ||
            `[${acc.display_name}] bağlantısı güvenli şekilde kesildi. Buluttaki dosyalar silinmedi ve ${payload.preserved_pending_deletions ?? 0} bekleyen silme görevi korundu.`,
        });
        await fetchRcloneStatus(false);
        onStorageChanged?.();
      } else {
        setBanner({ type: 'error', message: payload.error || 'Bağlantı kesilemedi.' });
      }
    } finally {
      setAccountActionBusy((prev) => {
        const next = { ...prev };
        delete next[remoteName];
        return next;
      });
    }
  };

  const handleReconnectSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!reconnectModalRemote) return;
    const remoteName = reconnectModalRemote.remote_name;
    setBanner(null);
    setAccountActionBusy((prev) => ({ ...prev, [remoteName]: 'reconnect' }));
    try {
      const bodyPayload: Record<string, any> = {};
      if (reconnectModalRemote.provider === 'mega') {
        if (reconnectCredential.trim()) bodyPayload.password = reconnectCredential;
      } else {
        if (reconnectCredential.trim()) bodyPayload.code_or_token = reconnectCredential.trim();
      }
      const res = await fetch(resolveApiUrl(`/api/rclone/accounts/${encodeURIComponent(remoteName)}/reconnect`), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(bodyPayload),
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok && payload.ok !== false) {
        setBanner({
          type: 'success',
          message: payload.message || `[${reconnectModalRemote.display_name}] yeniden bağlandı.`,
        });
        setReconnectModalRemote(null);
        setReconnectCredential('');
        await fetchRcloneStatus(true);
        onStorageChanged?.();
      } else {
        setBanner({
          type: 'error',
          message: payload.error || `[${reconnectModalRemote.display_name}] yeniden bağlanamadı.`,
        });
      }
    } finally {
      setAccountActionBusy((prev) => {
        const next = { ...prev };
        delete next[remoteName];
        return next;
      });
    }
  };

  const handleProcessPendingDeletions = async () => {
    setBanner(null);
    try {
      const res = await fetch(resolveApiUrl('/api/rclone/pending-deletions/process'), {
        method: 'POST',
        headers: { Accept: 'application/json' },
      });
      const payload = await res.json().catch(() => ({}));
      if (res.ok) {
        setBanner({
          type: 'info',
          message: `Kalıcı silme kuyruğu işlendi: ${payload.deleted ?? 0} süresi dolan dosya silindi, ${payload.remaining_pending ?? 0} görev sırada bekliyor.`,
        });
        await fetchRcloneStatus(false);
      }
    } catch (err: any) {
      setBanner({ type: 'error', message: `Silme kuyruğu işlenemedi: ${err?.message || 'Hata'}` });
    }
  };

  const copyAuthUrl = (url: string) => {
    navigator.clipboard?.writeText(url);
    setCopiedUrl(true);
    setTimeout(() => setCopiedUrl(false), 2000);
  };

  const rcloneBin = statusData?.rclone_binary;
  const summary = statusData?.storage_summary;
  const accounts = statusData?.accounts || [];
  const pendingDeletions = statusData?.pending_deletions || [];

  return (
    <div className="space-y-6">
      {/* Top Rclone Engine Summary Bar */}
      <div className="bg-[#131822] border border-slate-800/80 rounded-xl p-5 shadow-lg">
        <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-4">
          <div className="flex items-start gap-3.5">
            <div className="p-2.5 rounded-lg bg-indigo-500/10 border border-indigo-500/20 text-indigo-400">
              <Cloud className="w-6 h-6" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="text-lg font-bold text-white tracking-tight">
                  Rclone Çoklu Bulut Depolama Yönetimi (MEGA & Google Drive)
                </h2>
                {rcloneBin?.installed ? (
                  <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-md text-xs font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    <CheckCircle2 className="w-3.5 h-3.5" />
                    {rcloneBin.version}
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-md text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/20">
                    <AlertTriangle className="w-3.5 h-3.5" />
                    Rclone Binary Bekleniyor
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-400 mt-1">
                Google Drive bağlantıları rclone&apos;un yerleşik OAuth istemcisiyle (özel Google Cloud projesi veya manuel{' '}
                <code className="text-slate-300">client_id</code> / <code className="text-slate-300">client_secret</code>{' '}
                gerektirmeden) ve MEGA hesapları <code className="text-slate-300">rclone obscure</code> ile yönetilir.
                Hesap bağlantısını kesmek buluttaki dosyaları silmez; 7200 sn silme görevleri kalıcı olarak korunur.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2.5">
            <button
              type="button"
              onClick={() => {
                const next = !showDiagPanel;
                setShowDiagPanel(next);
                if (next && !diagData) {
                  fetchDiagnosticsLog(false);
                }
              }}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition-colors cursor-pointer"
            >
              <Terminal className="w-3.5 h-3.5 text-indigo-400" />
              {showDiagPanel ? 'Tanılama (/log) Kapat' : 'Tanılama Raporu (/log)'}
            </button>

            <button
              type="button"
              onClick={() => fetchRcloneStatus(true)}
              disabled={refreshingQuota}
              className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-semibold bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 transition-colors disabled:opacity-50 cursor-pointer"
            >
              <RefreshCw className={`w-3.5 h-3.5 ${refreshingQuota ? 'animate-spin' : ''}`} />
              Kotaları & Durumu Yenile
            </button>
          </div>
        </div>

        {/* Summary Metrics Row */}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mt-4 pt-4 border-t border-slate-800/80">
          <div className="bg-slate-900/60 border border-slate-800/70 rounded-lg p-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400">Bağlı Hesaplar</div>
            <div className="text-lg font-bold text-white mt-0.5">
              {summary?.connected_accounts ?? 0}{' '}
              <span className="text-xs font-normal text-slate-400">/ {summary?.total_accounts ?? 0} kayıtlı</span>
            </div>
            <div className="text-[11px] text-slate-500 mt-0.5">
              GDrive: {summary?.gdrive_accounts ?? 0} • MEGA: {summary?.mega_accounts ?? 0}
            </div>
          </div>

          <div className="bg-slate-900/60 border border-slate-800/70 rounded-lg p-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400">Toplam Bulut Kotası</div>
            <div className="text-lg font-bold text-white mt-0.5">{summary?.total_str || '0.00 B'}</div>
            <div className="text-[11px] text-emerald-400 mt-0.5">Boş: {summary?.free_str || '0.00 B'}</div>
          </div>

          <div className="bg-slate-900/60 border border-slate-800/70 rounded-lg p-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400">Kullanılan Alan</div>
            <div className="text-lg font-bold text-white mt-0.5">{summary?.used_str || '0.00 B'}</div>
            <div className="text-[11px] text-slate-500 mt-0.5">
              Aktif Yükleme Hedefi: {summary?.enabled_accounts ?? 0} hesap
            </div>
          </div>

          <div className="bg-slate-900/60 border border-slate-800/70 rounded-lg p-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400">Kalıcı Silme Kuyruğu</div>
            <div className="text-lg font-bold text-white mt-0.5">
              {statusData?.pending_deletions_count ?? 0}{' '}
              <span className="text-xs font-normal text-slate-400">bekleyen görev</span>
            </div>
            <div className="text-[11px] text-amber-400 mt-0.5">
              Süresi dolan: {statusData?.due_deletions_count ?? 0} (7200 sn saklama)
            </div>
          </div>
        </div>
      </div>

      {/* Status Alert Banner */}
      {banner && (
        <div
          className={`p-4 rounded-xl border flex items-start justify-between gap-3 ${
            banner.type === 'success'
              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-200'
              : banner.type === 'error'
              ? 'bg-rose-500/10 border-rose-500/30 text-rose-200'
              : 'bg-indigo-500/10 border-indigo-500/30 text-indigo-200'
          }`}
        >
          <div className="flex items-start gap-2.5 text-xs leading-relaxed">
            {banner.type === 'success' ? (
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
            ) : banner.type === 'error' ? (
              <XCircle className="w-4 h-4 text-rose-400 shrink-0 mt-0.5" />
            ) : (
              <ShieldCheck className="w-4 h-4 text-indigo-400 shrink-0 mt-0.5" />
            )}
            <span>{banner.message}</span>
          </div>
          <button
            type="button"
            onClick={() => setBanner(null)}
            className="text-xs text-slate-400 hover:text-white cursor-pointer"
          >
            Kapat
          </button>
        </div>
      )}

      {/* /log System Diagnostics Drawer */}
      {showDiagPanel && (
        <div className="bg-[#131822] border border-indigo-500/30 rounded-xl p-5 shadow-lg space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              <Terminal className="w-5 h-5 text-indigo-400" />
              <h3 className="text-sm font-bold text-white">
                Discord <code className="text-indigo-300">/log</code> Yönetici Tanılama Raporu (Canlı Önizleme)
              </h3>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => fetchDiagnosticsLog(false)}
                disabled={diagLoading}
                className="px-2.5 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 cursor-pointer"
              >
                Hızlı Yenile
              </button>
              <button
                type="button"
                onClick={() => fetchDiagnosticsLog(true)}
                disabled={diagLoading}
                className="px-2.5 py-1.5 rounded-lg text-xs font-medium bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 cursor-pointer"
              >
                {diagLoading ? 'Test Ediliyor...' : 'Canlı Remote Testiyle Çalıştır'}
              </button>
            </div>
          </div>

          {diagData?.diagnostics?.discord && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3 text-xs">
              <div className="bg-slate-900/70 border border-slate-800 rounded-lg p-3">
                <div className="text-slate-400 font-medium">Tekil Slash Komutları</div>
                <div className="text-emerald-400 font-mono mt-1">
                  {diagData.diagnostics.discord.registered_slash_commands
                    .map((c) => `/${c}`)
                    .join(', ') || 'Yok'}
                </div>
              </div>
              <div className="bg-slate-900/70 border border-slate-800 rounded-lg p-3">
                <div className="text-slate-400 font-medium">Yinelenen (Duplicate) Komut</div>
                <div className="text-white font-semibold mt-1">
                  {diagData.diagnostics.discord.duplicate_slash_commands.length === 0
                    ? '0 (Tekil Doğrulandı)'
                    : diagData.diagnostics.discord.duplicate_slash_commands.join(', ')}
                </div>
              </div>
              <div className="bg-slate-900/70 border border-slate-800 rounded-lg p-3">
                <div className="text-slate-400 font-medium">Eski Drive Komutları (/drive_*)</div>
                <div className="text-emerald-400 font-semibold mt-1">
                  {diagData.diagnostics.discord.removed_drive_commands_present.length === 0
                    ? 'Tamamen Kaldırıldı'
                    : diagData.diagnostics.discord.removed_drive_commands_present.join(', ')}
                </div>
              </div>
            </div>
          )}

          <pre className="bg-slate-950/90 border border-slate-800 rounded-lg p-4 text-xs text-slate-300 font-mono overflow-x-auto max-h-96 whitespace-pre-wrap">
            {diagLoading && !diagData
              ? 'Tanılama raporu hazırlanıyor...'
              : diagData?.formatted_report || 'Tanılama raporu yüklenemedi.'}
          </pre>
        </div>
      )}

      {/* Add Account Forms: Google Drive (Built-in Rclone OAuth) & MEGA */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Google Drive Built-in Rclone OAuth Card */}
        <div className="bg-[#131822] border border-slate-800/80 rounded-xl p-5 shadow-lg flex flex-col justify-between space-y-4">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded-lg bg-blue-500/10 border border-blue-500/20 text-blue-400">
                  <HardDrive className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-white">Google Drive Hesabı Ekle (Rclone Varsayılan OAuth)</h3>
                  <p className="text-[11px] text-slate-400">
                    Google Cloud projesi veya özel client_id / client_secret gerektirmez
                  </p>
                </div>
              </div>
              <span className="text-[11px] px-2 py-0.5 rounded bg-blue-500/10 text-blue-300 border border-blue-500/20">
                Çoklu Hesap Destekli
              </span>
            </div>

            <form onSubmit={handleStartGdriveOAuth} className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    Hesap Etiketi (Görünen Ad)
                  </label>
                  <input
                    type="text"
                    value={gdriveDisplayName}
                    onChange={(e) => setGdriveDisplayName(e.target.value)}
                    placeholder="Örn: Ana Google Drive #1"
                    className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    Hedef Klasör & Yetki Kapsamı
                  </label>
                  <div className="flex gap-2">
                    <input
                      type="text"
                      value={gdriveRootFolder}
                      onChange={(e) => setGdriveRootFolder(e.target.value)}
                      placeholder="UBAgen_Outputs"
                      className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-blue-500"
                    />
                    <select
                      value={gdriveScope}
                      onChange={(e) => setGdriveScope(e.target.value)}
                      className="px-2.5 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-slate-200 focus:outline-none focus:border-blue-500"
                    >
                      <option value="drive.file">drive.file</option>
                      <option value="drive">drive (Tam)</option>
                    </select>
                  </div>
                </div>
              </div>

              <button
                type="submit"
                disabled={gdriveBusy}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-xs font-semibold bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50 cursor-pointer"
              >
                <Link2 className="w-4 h-4" />
                {gdriveBusy ? 'Rclone OAuth Başlatılıyor...' : '1. Rclone Google Drive Yetkilendirmesini Başlat'}
              </button>
            </form>

            {gdriveSession && (
              <div className="bg-slate-900/90 border border-blue-500/30 rounded-lg p-3.5 space-y-2.5">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-semibold text-blue-300">
                    Aktif Oturum: <code>{gdriveSession.remote_name}</code>
                  </span>
                  <span className="text-[11px] text-slate-400">{gdriveSession.mode}</span>
                </div>

                {gdriveSession.auth_url && (
                  <div className="space-y-1.5">
                    <div className="text-[11px] text-slate-300">
                      Aşağıdaki Rclone yerleşik OAuth bağlantısını açıp Google hesabınızla onay verin:
                    </div>
                    <div className="flex items-center gap-2">
                      <a
                        href={gdriveSession.auth_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-semibold bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 border border-blue-500/30"
                      >
                        <ExternalLink className="w-3.5 h-3.5" />
                        Google Yetkilendirme Sayfasını Aç
                      </a>
                      <button
                        type="button"
                        onClick={() => copyAuthUrl(gdriveSession.auth_url!)}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 cursor-pointer"
                      >
                        {copiedUrl ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                        {copiedUrl ? 'Kopyalandı' : 'URL Kopyala'}
                      </button>
                    </div>
                  </div>
                )}

                <div className="text-[11px] text-slate-400 leading-relaxed">
                  Not: Sunucu ortamı uzak (headless) bir konteynerde çalışıyorsa ve Google yönlendirmesi{' '}
                  <code>127.0.0.1:53682</code> adresine dönerse, tarayıcı adres çubuğundaki <code>code=...</code>{' '}
                  parametresini (veya <code>rclone authorize &quot;drive&quot;</code> çıktısındaki JSON token&apos;ı)
                  aşağıya yapıştırarak yetkilendirmeyi tamamlayabilirsiniz.
                </div>
              </div>
            )}

            <form onSubmit={handleCompleteGdriveOAuth} className="space-y-2.5 pt-2 border-t border-slate-800/80">
              <label className="block text-[11px] font-medium text-slate-300">
                2. Google Doğrulama Kodu veya Rclone OAuth Token JSON
              </label>
              <div className="flex flex-col sm:flex-row gap-2">
                <input
                  type="text"
                  value={gdriveCodeOrToken}
                  onChange={(e) => setGdriveCodeOrToken(e.target.value)}
                  placeholder='Doğrulama kodu (4/0A...) veya {"access_token":"...","refresh_token":"..."}'
                  className="flex-1 px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 font-mono focus:outline-none focus:border-blue-500"
                />
                <button
                  type="submit"
                  disabled={gdriveBusy}
                  className="inline-flex items-center justify-center gap-1.5 px-4 py-2 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white transition-colors disabled:opacity-50 shrink-0 cursor-pointer"
                >
                  <CheckCircle2 className="w-3.5 h-3.5" />
                  Doğrula & Kaydet
                </button>
              </div>
            </form>
          </div>
        </div>

        {/* MEGA Account Addition Card */}
        <div className="bg-[#131822] border border-slate-800/80 rounded-xl p-5 shadow-lg flex flex-col justify-between space-y-4">
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <div className="p-2 rounded-lg bg-rose-500/10 border border-rose-500/20 text-rose-400">
                  <Plus className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-sm font-bold text-white">MEGA Hesabı Ekle (Rclone Obscure Kimlik Doğrulama)</h3>
                  <p className="text-[11px] text-slate-400">
                    Parola sunucu tarafında <code>rclone obscure</code> ile şifrelenir ve düz metin olarak saklanmaz
                  </p>
                </div>
              </div>
              <span className="text-[11px] px-2 py-0.5 rounded bg-rose-500/10 text-rose-300 border border-rose-500/20">
                Çoklu Hesap Destekli
              </span>
            </div>

            <form onSubmit={handleAddMegaAccount} className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    Hesap Etiketi (Görünen Ad)
                  </label>
                  <input
                    type="text"
                    value={megaDisplayName}
                    onChange={(e) => setMegaDisplayName(e.target.value)}
                    placeholder="Örn: MEGA Depolama #1"
                    className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-rose-500"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    MEGA E-posta Adresi
                  </label>
                  <input
                    type="email"
                    required
                    value={megaEmail}
                    onChange={(e) => setMegaEmail(e.target.value)}
                    placeholder="hesap@ornek.com"
                    className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-rose-500"
                  />
                </div>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    MEGA Parolası
                  </label>
                  <input
                    type="password"
                    required
                    value={megaPassword}
                    onChange={(e) => setMegaPassword(e.target.value)}
                    placeholder="••••••••••••"
                    className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-rose-500"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-slate-300 mb-1">
                    Hedef Klasör
                  </label>
                  <input
                    type="text"
                    value={megaRootFolder}
                    onChange={(e) => setMegaRootFolder(e.target.value)}
                    placeholder="UBAgen_Outputs"
                    className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-700/80 text-xs text-white placeholder-slate-500 focus:outline-none focus:border-rose-500"
                  />
                </div>
              </div>

              <button
                type="submit"
                disabled={megaBusy}
                className="w-full inline-flex items-center justify-center gap-2 px-4 py-2.5 rounded-lg text-xs font-semibold bg-rose-600 hover:bg-rose-500 text-white transition-colors disabled:opacity-50 cursor-pointer"
              >
                <Plus className="w-4 h-4" />
                {megaBusy ? 'MEGA Bağlantısı Doğrulanıyor...' : 'MEGA Hesabını Ekle & Bağlantıyı Test Et'}
              </button>
            </form>
          </div>

          <div className="text-[11px] text-slate-400 bg-slate-900/50 border border-slate-800/80 rounded-lg p-3">
            <strong className="text-slate-300">Çoklu Hesap & Kota Dağıtımı:</strong> Birden fazla MEGA ve Google Drive
            hesabı ekleyebilirsiniz. Encode edilen videolar en fazla boş alana sahip aktif hesaba yüklenir ve 7200
            saniye (2 saat) sonunda otomatik olarak kalıcı silme kuyruğu üzerinden temizlenir.
          </div>
        </div>
      </div>

      {/* Configured Accounts Table */}
      <div className="bg-[#131822] border border-slate-800/80 rounded-xl p-5 shadow-lg space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Layers className="w-5 h-5 text-indigo-400" />
            <h3 className="text-sm font-bold text-white">
              Kayıtlı MEGA ve Google Drive Hesapları ({accounts.length})
            </h3>
          </div>
          <span className="text-xs text-slate-400">
            Güvenli bağlantı kesme işlemi buluttaki mevcut dosyaları silmez
          </span>
        </div>

        {loading ? (
          <div className="text-xs text-slate-400 py-6 text-center">Hesap listesi yükleniyor...</div>
        ) : accounts.length === 0 ? (
          <div className="bg-slate-900/50 border border-slate-800/80 rounded-lg p-6 text-center space-y-1.5">
            <div className="text-sm font-semibold text-slate-300">Henüz bağlı bir MEGA veya Google Drive hesabı yok</div>
            <p className="text-xs text-slate-400">
              Yukarıdaki panellerden bir veya birden fazla Google Drive ya da MEGA hesabı ekleyerek Rclone depolama
              sistemini etkinleştirebilirsiniz.
            </p>
          </div>
        ) : (
          <div className="space-y-3">
            {accounts.map((acc) => {
              const busyState = accountActionBusy[acc.remote_name];
              const pctUsed =
                acc.storage_total_bytes && acc.storage_total_bytes > 0 && acc.storage_used_bytes != null
                  ? Math.min(100, Math.round((acc.storage_used_bytes / acc.storage_total_bytes) * 100))
                  : null;

              return (
                <div
                  key={acc.remote_name}
                  className="bg-slate-900/70 border border-slate-800 rounded-xl p-4 flex flex-col lg:flex-row lg:items-center justify-between gap-4"
                >
                  <div className="space-y-1.5 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <span
                        className={`px-2 py-0.5 rounded text-[11px] font-bold uppercase ${
                          acc.provider === 'mega'
                            ? 'bg-rose-500/15 text-rose-300 border border-rose-500/30'
                            : 'bg-blue-500/15 text-blue-300 border border-blue-500/30'
                        }`}
                      >
                        {acc.provider === 'mega' ? 'MEGA' : 'Google Drive'}
                      </span>
                      <span className="text-sm font-bold text-white">{acc.display_name}</span>
                      <code className="text-xs text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800">
                        {acc.remote_name}:/{acc.root_folder}
                      </code>
                      {acc.account_email && (
                        <span className="text-xs text-slate-400">({acc.account_email})</span>
                      )}

                      {/* Status badge */}
                      {acc.status === 'connected' ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                          <CheckCircle2 className="w-3 h-3" /> Bağlı
                        </span>
                      ) : acc.status === 'pending_auth' ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-amber-500/10 text-amber-300 border border-amber-500/20">
                          <Clock className="w-3 h-3" /> OAuth Onayı Bekleniyor
                        </span>
                      ) : acc.status === 'disconnected' ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-slate-700/50 text-slate-300 border border-slate-600">
                          <Unlink className="w-3 h-3" /> Bağlantı Kesildi (Dosyalar Korundu)
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20">
                          <AlertTriangle className="w-3 h-3" /> Bağlantı Hatası
                        </span>
                      )}

                      {!acc.enabled && (
                        <span className="px-2 py-0.5 rounded text-[11px] bg-slate-800 text-slate-400 border border-slate-700">
                          Yüklemeye Kapalı
                        </span>
                      )}
                    </div>

                    {/* Quota bar */}
                    <div className="flex flex-wrap items-center gap-4 text-xs text-slate-400 pt-1">
                      <div>
                        Kullanılan: <strong className="text-slate-200">{acc.storage_used_str}</strong> /{' '}
                        <strong className="text-slate-200">{acc.storage_total_str}</strong> (Boş:{' '}
                        <strong className="text-emerald-400">{acc.storage_free_str}</strong>)
                      </div>
                      {pctUsed !== null && (
                        <div className="w-36 bg-slate-800 h-2 rounded-full overflow-hidden">
                          <div
                            className="bg-indigo-500 h-full rounded-full"
                            style={{ width: `${pctUsed}%` }}
                          />
                        </div>
                      )}
                      {acc.last_verified_at && (
                        <span className="text-[11px] text-slate-500">
                          Son doğrulama: {new Date(acc.last_verified_at * 1000).toLocaleTimeString()}
                        </span>
                      )}
                    </div>

                    {acc.last_error && (
                      <div className="text-xs text-rose-400 flex items-center gap-1.5 pt-1">
                        <FileWarning className="w-3.5 h-3.5 shrink-0" />
                        <span>{acc.last_error}</span>
                      </div>
                    )}
                  </div>

                  {/* Account Action Buttons */}
                  <div className="flex flex-wrap items-center gap-2 shrink-0">
                    <button
                      type="button"
                      onClick={() => handleTestAccount(acc.remote_name)}
                      disabled={Boolean(busyState)}
                      className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700 transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <Play className="w-3.5 h-3.5 text-emerald-400" />
                      {busyState === 'test' ? 'Test Ediliyor...' : 'Bağlantıyı Test Et'}
                    </button>

                    <button
                      type="button"
                      onClick={() => {
                        setReconnectModalRemote(acc);
                        setReconnectCredential('');
                      }}
                      disabled={Boolean(busyState)}
                      className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-indigo-600/20 hover:bg-indigo-600/30 text-indigo-300 border border-indigo-500/30 transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <RefreshCw className="w-3.5 h-3.5" />
                      Yeniden Bağlan
                    </button>

                    <button
                      type="button"
                      onClick={() => handleToggleAccount(acc)}
                      disabled={Boolean(busyState)}
                      className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 transition-colors disabled:opacity-50 cursor-pointer"
                    >
                      <Power className="w-3.5 h-3.5" />
                      {acc.enabled ? 'Pasif Yap' : 'Aktif Et'}
                    </button>

                    {acc.status !== 'disconnected' && (
                      <button
                        type="button"
                        onClick={() => handleDisconnectAccount(acc)}
                        disabled={Boolean(busyState)}
                        className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs font-medium bg-rose-500/15 hover:bg-rose-500/25 text-rose-300 border border-rose-500/30 transition-colors disabled:opacity-50 cursor-pointer"
                      >
                        <Unlink className="w-3.5 h-3.5" />
                        {busyState === 'disconnect' ? 'Kesiliyor...' : 'Güvenli Bağlantıyı Kes'}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {/* Inline Reconnect Form when an account is selected for reconnection */}
        {reconnectModalRemote && (
          <form
            onSubmit={handleReconnectSubmit}
            className="bg-slate-900 border border-indigo-500/40 rounded-xl p-4 space-y-3"
          >
            <div className="flex items-center justify-between">
              <div className="text-xs font-bold text-indigo-300">
                Yeniden Bağlan: {reconnectModalRemote.display_name} ({reconnectModalRemote.remote_name})
              </div>
              <button
                type="button"
                onClick={() => setReconnectModalRemote(null)}
                className="text-xs text-slate-400 hover:text-white cursor-pointer"
              >
                İptal
              </button>
            </div>
            <p className="text-[11px] text-slate-400">
              {reconnectModalRemote.provider === 'mega'
                ? 'Mevcut kimlik bilgileriyle yeniden test etmek için boş bırakabilir veya yeni MEGA parolasını girebilirsiniz:'
                : 'Mevcut OAuth token ile yeniden test etmek için boş bırakabilir veya yeni rclone doğrulama kodu / token JSON yapıştırabilirsiniz:'}
            </p>
            <div className="flex flex-col sm:flex-row gap-2">
              <input
                type={reconnectModalRemote.provider === 'mega' ? 'password' : 'text'}
                value={reconnectCredential}
                onChange={(e) => setReconnectCredential(e.target.value)}
                placeholder={
                  reconnectModalRemote.provider === 'mega'
                    ? 'İsteğe bağlı yeni MEGA parolası'
                    : 'İsteğe bağlı yeni Google doğrulama kodu veya token JSON'
                }
                className="flex-1 px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-xs text-white"
              />
              <button
                type="submit"
                className="px-4 py-2 rounded-lg text-xs font-semibold bg-indigo-600 hover:bg-indigo-500 text-white cursor-pointer"
              >
                Yeniden Bağlanmayı Tamamla
              </button>
            </div>
          </form>
        )}
      </div>

      {/* Persistent Pending Deletions Table */}
      <div className="bg-[#131822] border border-slate-800/80 rounded-xl p-5 shadow-lg space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Clock className="w-4 h-4 text-amber-400" />
            <h3 className="text-sm font-bold text-white">
              Kalıcı Dosya Silme Görevleri (7200 Saniye Otomatik Temizleme Kuyruğu)
            </h3>
          </div>
          <button
            type="button"
            onClick={handleProcessPendingDeletions}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-amber-500/15 hover:bg-amber-500/25 text-amber-300 border border-amber-500/30 transition-colors cursor-pointer"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Süresi Dolanları Şimdi Temizle
          </button>
        </div>

        <p className="text-xs text-slate-400">
          Bir hesabın bağlantısı kesilse bile silme görevleri SQLite veritabanında kalıcı olarak tutulur; hesap yeniden
          bağlandığında süresi dolmuş dosyalar otomatik olarak temizlenir.
        </p>

        {pendingDeletions.length === 0 ? (
          <div className="text-xs text-slate-500 py-3">Şu anda bekleyen bulut dosya silme görevi bulunmuyor.</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left border-collapse text-xs">
              <thead>
                <tr className="border-b border-slate-800 text-slate-400">
                  <th className="py-2 pr-3">Remote</th>
                  <th className="py-2 pr-3">Dosya Yolu</th>
                  <th className="py-2 pr-3">İş Kimliği</th>
                  <th className="py-2 pr-3">Durum</th>
                  <th className="py-2 pr-3">Kalan Süre</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/60 text-slate-300">
                {pendingDeletions.slice(0, 15).map((item) => (
                  <tr key={item.deletion_id}>
                    <td className="py-2 pr-3 font-mono text-indigo-300">{item.remote_name}</td>
                    <td className="py-2 pr-3 font-mono text-slate-300">{item.remote_path}</td>
                    <td className="py-2 pr-3 font-mono text-slate-400">{item.job_id || '—'}</td>
                    <td className="py-2 pr-3">
                      <span
                        className={`px-2 py-0.5 rounded text-[11px] ${
                          item.status === 'completed'
                            ? 'bg-emerald-500/10 text-emerald-400'
                            : item.status === 'failed'
                            ? 'bg-rose-500/10 text-rose-400'
                            : 'bg-amber-500/10 text-amber-300'
                        }`}
                      >
                        {item.status}
                      </span>
                    </td>
                    <td className="py-2 pr-3 font-mono">
                      {item.status === 'completed'
                        ? 'Silindi'
                        : item.is_due
                        ? 'Süresi Doldu (Silinmeye Hazır)'
                        : `${Math.ceil(item.due_in_seconds / 60)} dk`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
};
