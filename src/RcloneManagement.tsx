import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  CheckCircle2,
  Cloud,
  ExternalLink,
  HardDrive,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
  Unplug,
  X,
} from 'lucide-react';

type RcloneAccount = {
  remote_name: string;
  provider: 'drive' | 'mega' | string;
  provider_label?: string;
  display_name?: string;
  account_identity?: string | null;
  remote_base_path?: string;
  status: string;
  is_active: boolean;
  last_test_ok?: boolean | null;
  last_tested_at_iso?: string | null;
  last_error?: string | null;
  quota?: { total?: number | null; used?: number | null; free?: number | null } | null;
  pending_deletions_count?: number;
};

type PendingDeletion = {
  deletion_id?: string;
  remote_name?: string;
  remote_object_path?: string;
  due_at?: number;
  status?: string;
  attempts?: number;
};

type RcloneStatus = {
  binary?: { installed?: boolean; executable?: boolean; version?: string; error?: string | null };
  accounts?: RcloneAccount[];
  accounts_total?: number;
  active_accounts_count?: number;
  pending_deletions_count?: number;
  pending_deletions?: PendingDeletion[];
  requires_google_cloud_project?: boolean;
};

type Props = { onClose: () => void };

const panelClass = 'rounded-xl border border-slate-800 bg-slate-900/80';
const inputClass = 'w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/20 placeholder:text-slate-600';
const buttonClass = 'inline-flex items-center justify-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50';

async function requestJson(url: string, init?: RequestInit, adminToken?: string): Promise<any> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(adminToken ? { Authorization: `Bearer ${adminToken}` } : {}), ...init?.headers },
    cache: 'no-store',
  });
  const raw = await response.text();
  let data: any = {};
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { error: raw || 'Sunucudan geçersiz yanıt geldi.' }; }
  if (!response.ok || data?.ok === false || data?.status === 'error') {
    throw new Error(data?.error || data?.message || data?.detail || `İstek başarısız (HTTP ${response.status}).`);
  }
  return data;
}

function formatBytes(value?: number | null): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return 'Bilinmiyor';
  const bytes = Math.max(0, Number(value));
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let i = 0; i < units.length - 1 && amount >= 1024; i++) {
    amount /= 1024;
    unit = units[i + 1];
  }
  return `${amount.toFixed(amount >= 100 ? 0 : 1)} ${unit}`;
}

function getUsage(account: RcloneAccount): number | null {
  const total = Number(account.quota?.total);
  const used = Number(account.quota?.used);
  if (!Number.isFinite(total) || !Number.isFinite(used) || total <= 0) return null;
  return Math.max(0, Math.min(100, (used / total) * 100));
}

function statusLabel(status: string): string {
  switch (status) {
    case 'connected': return 'Bağlı';
    case 'auth_pending': return 'Yetkilendirme bekliyor';
    case 'disconnected': return 'Bağlantı kesildi';
    case 'error': return 'Hata';
    default: return status || 'Bilinmiyor';
  }
}

export default function RcloneManagement({ onClose }: Props) {
  const [status, setStatus] = useState<RcloneStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyKey, setBusyKey] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [providerTab, setProviderTab] = useState<'mega' | 'drive'>('mega');
  const [megaName, setMegaName] = useState('mega_account');
  const [megaDisplayName, setMegaDisplayName] = useState('');
  const [megaEmail, setMegaEmail] = useState('');
  const [megaPassword, setMegaPassword] = useState('');
  const [driveName, setDriveName] = useState('gdrive_account');
  const [driveDisplayName, setDriveDisplayName] = useState('');
  const [driveSession, setDriveSession] = useState<{ session_id?: string; google_auth_url?: string | null; instructions_tr?: string; rclone_cli_command?: string; remote_name?: string } | null>(null);
  const [driveCodeOrToken, setDriveCodeOrToken] = useState('');
  const [showMegaForm, setShowMegaForm] = useState(false);
  const [showDriveForm, setShowDriveForm] = useState(false);
  const [adminToken, setAdminToken] = useState('');
  const [adminTokenInput, setAdminTokenInput] = useState('');
  const [showAuthForm, setShowAuthForm] = useState(true);

  const accounts = status?.accounts || [];
  const sortedAccounts = useMemo(
    () => [...accounts].sort((a, b) => a.provider.localeCompare(b.provider) || a.remote_name.localeCompare(b.remote_name)),
    [accounts],
  );

  const refresh = useCallback(async (showSpinner = true) => {
    if (!adminToken) {
      setLoading(false);
      setShowAuthForm(true);
      setError('Rclone yönetimi için önce sunucu yönetici tokenı ile yetkilendir.');
      return;
    }
    if (showSpinner) setLoading(true);
    setError('');
    try {
      const data = await requestJson('/api/rclone/status?refresh=true', undefined, adminToken);
      setStatus(data as RcloneStatus);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Rclone durumu alınamadı.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const mutate = async (key: string, url: string, body?: unknown, successMessage?: string) => {
    setBusyKey(key);
    setError('');
    setNotice('');
    try {
      await requestJson(url, {
        method: 'POST',
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }, adminToken);
      setNotice(successMessage || 'İşlem tamamlandı.');
      await refresh(false);
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'İşlem tamamlanamadı.');
      return false;
    } finally {
      setBusyKey('');
    }
  };

  const submitMega = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!megaName.trim() || !megaEmail.trim() || !megaPassword) {
      setError('MEGA hesap adı, e-posta ve parola alanlarını doldur.');
      return;
    }
    const ok = await mutate('add-mega', '/api/rclone/accounts/mega', {
      remote_name: megaName.trim(),
      display_name: megaDisplayName.trim() || megaName.trim(),
      email: megaEmail.trim(),
      user: megaEmail.trim(),
      password: megaPassword,
      remote_base_path: 'ubagen_outputs',
      root_folder: 'ubagen_outputs',
    }, 'MEGA hesabı ekleme isteği gönderildi.');
    if (ok) {
      setMegaPassword('');
      setShowMegaForm(false);
    }
  };

  const startDriveAuth = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!driveName.trim()) {
      setError('Google Drive için bir hesap/remote adı gir.');
      return;
    }
    setBusyKey('start-drive');
    setError('');
    setNotice('');
    try {
      const data = await requestJson('/api/rclone/accounts/gdrive/start', {
        method: 'POST',
        body: JSON.stringify({
          remote_name: driveName.trim(),
          display_name: driveDisplayName.trim() || driveName.trim(),
          root_folder: 'ubagen_outputs',
          remote_base_path: 'ubagen_outputs',
          scope: 'drive.file',
        }),
      }, adminToken);
      setDriveSession(data);
      setDriveCodeOrToken('');
      setNotice('Yetkilendirme oturumu başlatıldı. Google hesabını onayla, ardından yönlendirme adresini/kodunu veya rclone token JSON çıktısını aşağıya gir.');
      if (data?.google_auth_url) window.open(data.google_auth_url, '_blank', 'noopener,noreferrer');
      await refresh(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Google Drive yetkilendirmesi başlatılamadı.');
    } finally {
      setBusyKey('');
    }
  };

  const completeDriveAuth = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!driveCodeOrToken.trim()) {
      setError('Google yönlendirme URL’sini, kodu veya rclone token JSON çıktısını yapıştır.');
      return;
    }
    const remoteName = driveSession?.remote_name || driveName.trim();
    const ok = await mutate('complete-drive', '/api/rclone/accounts/gdrive/complete', {
      remote_name: remoteName,
      session_id: driveSession?.session_id,
      display_name: driveDisplayName.trim() || remoteName,
      code_or_token: driveCodeOrToken.trim(),
      root_folder: 'ubagen_outputs',
      scope: 'drive.file',
    }, 'Google Drive yetkilendirme isteği tamamlandı.');
    if (ok) {
      setDriveCodeOrToken('');
      setDriveSession(null);
      setShowDriveForm(false);
    }
  };

  const testAccount = (remoteName: string) =>
    mutate(`test:${remoteName}`, `/api/rclone/accounts/${encodeURIComponent(remoteName)}/test`, {}, `${remoteName} bağlantı testi tamamlandı.`);

  const toggleAccount = (account: RcloneAccount) =>
    mutate(
      `toggle:${account.remote_name}`,
      `/api/rclone/accounts/${encodeURIComponent(account.remote_name)}/toggle`,
      { enabled: !account.is_active },
      account.is_active ? 'Hesap yüklemeler için devre dışı bırakıldı.' : 'Hesap yeniden etkinleştirildi.',
    );

  const disconnectAccount = async (account: RcloneAccount) => {
    const confirmed = window.confirm(
      `${account.display_name || account.remote_name} bağlantısı kesilsin mi? Buluttaki dosyalar silinmez; bekleyen silme işleri korunur.`,
    );
    if (!confirmed) return;
    await mutate(
      `disconnect:${account.remote_name}`,
      `/api/rclone/accounts/${encodeURIComponent(account.remote_name)}/disconnect`,
      { remove_record: false },
      'Hesap bağlantısı kesildi. Buluttaki dosyalar silinmedi.',
    );
  };

  const processDeletions = () =>
    mutate('process-deletions', '/api/rclone/pending-deletions/process', {}, 'Vadesi gelen silme işleri işlendi.');

  return (
    <div className="fixed inset-0 z-[80] flex items-start justify-center overflow-y-auto bg-black/75 p-3 backdrop-blur-sm sm:p-6" role="dialog" aria-modal="true" aria-labelledby="rclone-panel-title">
      <div className="my-auto w-full max-w-6xl overflow-hidden rounded-2xl border border-slate-700 bg-slate-950 shadow-2xl shadow-black/50">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 bg-slate-900/90 px-4 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-500/15 text-indigo-300"><HardDrive className="h-5 w-5" /></div>
            <div>
              <h2 id="rclone-panel-title" className="text-lg font-bold text-white sm:text-xl">Rclone Depolama Yönetimi</h2>
              <p className="text-xs text-slate-400">Google Drive ve MEGA hesapları · gerçek kota · güvenli bağlantı yönetimi</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => { setShowAuthForm(v => !v); setError(''); }} className={`${buttonClass} ${adminToken ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200' : 'border-amber-500/30 bg-amber-500/10 text-amber-200'} hover:opacity-90`}>
              <ShieldCheck className="h-4 w-4" /> {adminToken ? 'Yönetici yetkisi açık' : 'Yönetici ile yetkilendir'}
            </button>
            <button type="button" onClick={() => void refresh()} disabled={loading || !adminToken} className={`${buttonClass} border-slate-700 bg-slate-800 text-slate-200 hover:bg-slate-700`}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> Yenile
            </button>
            <button type="button" onClick={onClose} className="rounded-lg p-2 text-slate-400 transition hover:bg-slate-800 hover:text-white" aria-label="Rclone yönetimini kapat"><X className="h-5 w-5" /></button>
          </div>
        </div>

        <div className="max-h-[calc(100vh-90px)] space-y-5 overflow-y-auto p-4 sm:p-6">
          {showAuthForm && <form onSubmit={async event => {
            event.preventDefault();
            const candidate = adminTokenInput.trim();
            if (!candidate) { setError('Sunucuda tanımlı UBAGEN_ADMIN_TOKEN değerini gir.'); return; }
            setBusyKey('admin-auth');
            setError('');
            setNotice('');
            try {
              const data = await requestJson('/api/rclone/status?refresh=false', undefined, candidate);
              setAdminToken(candidate);
              setStatus(data as RcloneStatus);
              setAdminTokenInput('');
              setShowAuthForm(false);
              setNotice('Yönetici yetkisi doğrulandı. Token yalnızca bu sayfa açıkken bellekte tutulur.');
            } catch (err) {
              setError(err instanceof Error ? err.message : 'Yönetici yetkilendirmesi başarısız.');
            } finally { setBusyKey(''); }
          }} className="rounded-xl border border-amber-500/25 bg-amber-500/5 p-4 sm:p-5">
            <h3 className="font-semibold text-white">Yönetici hesabıyla yetkilendir</h3>
            <p className="mt-1 text-xs leading-5 text-slate-400">Sunucuda <code>UBAGEN_ADMIN_TOKEN</code> olarak tanımlı yönetici anahtarını gir. Bu anahtar tarayıcı depolamasına kaydedilmez ve yalnızca korumalı rclone API isteklerinde gönderilir.</p>
            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
              <input type="password" autoComplete="current-password" className={inputClass} value={adminTokenInput} onChange={e => setAdminTokenInput(e.target.value)} placeholder="Sunucu yönetici tokenı" required />
              <button type="submit" disabled={busyKey === 'admin-auth'} className={`${buttonClass} shrink-0 border-emerald-500/30 bg-emerald-600 text-white hover:bg-emerald-500`}>{busyKey === 'admin-auth' ? <Loader2 className="h-4 w-4 animate-spin" /> : <ShieldCheck className="h-4 w-4" />} Yetkilendir</button>
            </div>
            {adminToken && <button type="button" onClick={() => { setAdminToken(''); setStatus(null); setShowAuthForm(true); setNotice('Yönetici oturumu kapatıldı.'); }} className="mt-3 text-xs text-rose-300 underline underline-offset-4">Yetkiyi kaldır</button>}
          </form>}
          {error && <div role="alert" className="flex items-start gap-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" /><span>{error}</span></div>}
          {notice && <div role="status" className="flex items-start gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-200"><CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" /><span>{notice}</span></div>}

          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <div className={`${panelClass} p-4`}><p className="text-xs text-slate-400">Tanımlı hesap</p><p className="mt-1 text-2xl font-bold text-white">{status?.accounts_total ?? accounts.length}</p></div>
            <div className={`${panelClass} p-4`}><p className="text-xs text-slate-400">Aktif ve bağlı</p><p className="mt-1 text-2xl font-bold text-emerald-300">{status?.active_accounts_count ?? accounts.filter(a => a.is_active && a.status === 'connected').length}</p></div>
            <div className={`${panelClass} p-4`}><p className="text-xs text-slate-400">Bekleyen silme</p><p className="mt-1 text-2xl font-bold text-amber-300">{status?.pending_deletions_count ?? 0}</p></div>
            <div className={`${panelClass} p-4`}>
              <p className="text-xs text-slate-400">Rclone motoru</p>
              <p className={`mt-1 text-sm font-semibold ${status?.binary?.installed && status?.binary?.executable ? 'text-emerald-300' : 'text-rose-300'}`}>
                {loading ? 'Kontrol ediliyor…' : status?.binary?.installed && status?.binary?.executable ? 'Hazır' : 'Kullanılamıyor'}
              </p>
              <p className="mt-1 truncate text-[11px] text-slate-500">{status?.binary?.version || status?.binary?.error || 'Durum bilgisi alınamadı'}</p>
            </div>
          </div>

          {status?.binary?.error && !status?.binary?.executable && <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">Rclone backend ortamında çalışır durumda değil: {status.binary.error}</p>}

          <section className={`${panelClass} overflow-hidden`}>
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-4 py-4 sm:px-5">
              <div><h3 className="font-semibold text-white">Hesaplar</h3><p className="mt-1 text-xs text-slate-400">Kota, bağlantı durumu ve yükleme için etkinlik ayarları</p></div>
              <div className="flex gap-2">
                <button type="button" onClick={() => { setProviderTab('mega'); setShowMegaForm(v => !v); setShowDriveForm(false); setError(''); }} className={`${buttonClass} border-emerald-500/30 bg-emerald-500/10 text-emerald-200 hover:bg-emerald-500/20`}><Plus className="h-4 w-4" /> MEGA ekle</button>
                <button type="button" onClick={() => { setProviderTab('drive'); setShowDriveForm(v => !v); setShowMegaForm(false); setError(''); }} className={`${buttonClass} border-indigo-500/30 bg-indigo-500/10 text-indigo-200 hover:bg-indigo-500/20`}><Plus className="h-4 w-4" /> Google yöneticisini bağla</button>
              </div>
            </div>

            {showMegaForm && <form onSubmit={submitMega} className="grid gap-3 border-b border-slate-800 bg-slate-900/40 p-4 sm:grid-cols-2 sm:p-5">
              <div className="sm:col-span-2"><h4 className="text-sm font-semibold text-white">MEGA hesabı bağla</h4><p className="mt-1 text-xs text-slate-400">Parola yalnızca backend isteğine gönderilir; arayüzde kalıcı olarak saklanmaz.</p></div>
              <label className="space-y-1 text-xs text-slate-300">Remote adı<input className={inputClass} value={megaName} onChange={e => setMegaName(e.target.value)} placeholder="mega_account" autoComplete="off" required /></label>
              <label className="space-y-1 text-xs text-slate-300">Görünen ad (isteğe bağlı)<input className={inputClass} value={megaDisplayName} onChange={e => setMegaDisplayName(e.target.value)} placeholder="MEGA hesabım" /></label>
              <label className="space-y-1 text-xs text-slate-300">MEGA e-posta<input className={inputClass} type="email" value={megaEmail} onChange={e => setMegaEmail(e.target.value)} autoComplete="username" required /></label>
              <label className="space-y-1 text-xs text-slate-300">MEGA parola<input className={inputClass} type="password" value={megaPassword} onChange={e => setMegaPassword(e.target.value)} autoComplete="new-password" required /></label>
              <div className="flex flex-wrap gap-2 sm:col-span-2"><button type="submit" disabled={!!busyKey} className={`${buttonClass} border-emerald-500/40 bg-emerald-600 text-white hover:bg-emerald-500`}>{busyKey === 'add-mega' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />} Hesabı bağla</button><button type="button" onClick={() => { setShowMegaForm(false); setMegaPassword(''); }} className={`${buttonClass} border-slate-700 text-slate-300 hover:bg-slate-800`}>İptal</button></div>
            </form>}

            {showDriveForm && <div className="space-y-4 border-b border-slate-800 bg-slate-900/40 p-4 sm:p-5">
              <form onSubmit={startDriveAuth} className="grid gap-3 sm:grid-cols-2">
                <div className="sm:col-span-2"><h4 className="text-sm font-semibold text-white">Google Drive hesabı bağla</h4><p className="mt-1 text-xs leading-5 text-slate-400">Rclone’un varsayılan OAuth akışı kullanılır; bu arayüz Google Cloud projesi veya özel OAuth anahtarı istemez. Sağlayıcı kısıtlamaları nedeniyle yetkilendirme tamamlanamayabilir.</p></div>
                <label className="space-y-1 text-xs text-slate-300">Remote adı<input className={inputClass} value={driveName} onChange={e => setDriveName(e.target.value)} placeholder="gdrive_account" autoComplete="off" required /></label>
                <label className="space-y-1 text-xs text-slate-300">Görünen ad (isteğe bağlı)<input className={inputClass} value={driveDisplayName} onChange={e => setDriveDisplayName(e.target.value)} placeholder="Google Drive hesabım" /></label>
                <div className="flex flex-wrap gap-2 sm:col-span-2"><button type="submit" disabled={!!busyKey} className={`${buttonClass} border-indigo-500/40 bg-indigo-600 text-white hover:bg-indigo-500`}>{busyKey === 'start-drive' ? <Loader2 className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />} Yönetici Google hesabıyla yetkilendir</button><button type="button" onClick={() => { setShowDriveForm(false); setDriveSession(null); setDriveCodeOrToken(''); }} className={`${buttonClass} border-slate-700 text-slate-300 hover:bg-slate-800`}>İptal</button></div>
              </form>
              {driveSession && <form onSubmit={completeDriveAuth} className="space-y-3 rounded-xl border border-indigo-500/25 bg-slate-950/80 p-4">
                <div className="flex items-start gap-2"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-indigo-300" /><div><p className="text-sm font-semibold text-white">Yetkilendirmeyi tamamla</p><p className="mt-1 whitespace-pre-line text-xs leading-5 text-slate-400">{driveSession.instructions_tr || 'Google yetkilendirmesini tamamladıktan sonra yönlendirme URL’sini, kodu veya rclone token JSON çıktısını aşağıya yapıştır.'}</p></div></div>
                {driveSession.google_auth_url && <a href={driveSession.google_auth_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-2 text-sm text-indigo-300 underline decoration-indigo-500/40 underline-offset-4 hover:text-indigo-200">Google yetkilendirme sayfasını aç <ExternalLink className="h-3.5 w-3.5" /></a>}
                <textarea className={`${inputClass} min-h-28 resize-y font-mono text-xs`} value={driveCodeOrToken} onChange={e => setDriveCodeOrToken(e.target.value)} placeholder="http://127.0.0.1:53682/?state=...&code=... veya rclone token JSON" autoComplete="off" spellCheck={false} />
                <p className="text-[11px] leading-4 text-amber-200/80">Token çıktısını kimseyle paylaşma. Bu alan yalnızca tamamla isteğinde kullanılır; otomatik olarak localStorage’a yazılmaz.</p>
                <button type="submit" disabled={!!busyKey} className={`${buttonClass} border-indigo-500/40 bg-indigo-600 text-white hover:bg-indigo-500`}>{busyKey === 'complete-drive' ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />} Yetkilendirmeyi tamamla</button>
              </form>}
            </div>}

            {loading && !status ? <div className="flex items-center justify-center gap-2 p-12 text-sm text-slate-400"><Loader2 className="h-4 w-4 animate-spin" /> Rclone hesapları yükleniyor…</div> : sortedAccounts.length === 0 ? <div className="p-10 text-center"><Cloud className="mx-auto h-8 w-8 text-slate-600" /><p className="mt-3 font-medium text-slate-300">Henüz hesap eklenmemiş</p><p className="mt-1 text-sm text-slate-500">MEGA veya Google Drive ekleyerek başlayabilirsin.</p></div> : <div className="grid gap-3 p-4 lg:grid-cols-2 sm:p-5">
              {sortedAccounts.map(account => {
                const usage = getUsage(account);
                const connected = account.status === 'connected' && account.is_active;
                return <article key={account.remote_name} className="rounded-xl border border-slate-800 bg-slate-950/80 p-4">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 items-start gap-3">
                      <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${account.provider === 'drive' ? 'bg-indigo-500/15 text-indigo-300' : 'bg-emerald-500/15 text-emerald-300'}`}><HardDrive className="h-5 w-5" /></div>
                      <div className="min-w-0"><h4 className="truncate font-semibold text-white">{account.display_name || account.remote_name}</h4><p className="mt-0.5 truncate font-mono text-[11px] text-slate-500">{account.remote_name} · {account.provider_label || (account.provider === 'drive' ? 'Google Drive' : 'MEGA')}</p>{account.account_identity && <p className="mt-1 truncate text-xs text-slate-400">{account.account_identity}</p>}</div>
                    </div>
                    <span className={`shrink-0 rounded-full border px-2 py-1 text-[11px] font-medium ${connected ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : account.status === 'error' ? 'border-rose-500/30 bg-rose-500/10 text-rose-300' : 'border-amber-500/30 bg-amber-500/10 text-amber-200'}`}>{statusLabel(account.status)}{!account.is_active && account.status !== 'disconnected' ? ' · pasif' : ''}</span>
                  </div>
                  <div className="mt-4 rounded-lg bg-slate-900 p-3">
                    <div className="flex items-center justify-between gap-3 text-xs"><span className="text-slate-400">Kullanılan alan</span><span className="font-medium text-slate-200">{usage === null ? 'Kota bilgisi alınmadı' : `${usage.toFixed(1)}% · ${formatBytes(account.quota?.used)} / ${formatBytes(account.quota?.total)}`}</span></div>
                    <div className="mt-2 h-2 overflow-hidden rounded-full bg-slate-800"><div className={`h-full rounded-full transition-all ${usage === null ? 'bg-slate-700' : usage >= 90 ? 'bg-rose-500' : usage >= 75 ? 'bg-amber-500' : 'bg-emerald-500'}`} style={{ width: `${usage ?? 0}%` }} /></div>
                    <div className="mt-2 flex justify-between gap-3 text-[11px] text-slate-500"><span>Boş: {formatBytes(account.quota?.free)}</span><span>Bekleyen silme: {account.pending_deletions_count ?? 0}</span></div>
                  </div>
                  {account.last_error && <p className="mt-3 rounded-lg border border-rose-500/20 bg-rose-500/5 p-2 text-xs text-rose-200">{account.last_error}</p>}
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" onClick={() => void testAccount(account.remote_name)} disabled={!!busyKey || !account.is_active || account.status === 'auth_pending'} className={`${buttonClass} border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800`}>{busyKey === `test:${account.remote_name}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />} Bağlantıyı test et</button>
                    <button type="button" onClick={() => void toggleAccount(account)} disabled={!!busyKey || account.status === 'disconnected' || account.status === 'auth_pending'} className={`${buttonClass} border-amber-500/25 bg-amber-500/5 text-amber-200 hover:bg-amber-500/10`}>{busyKey === `toggle:${account.remote_name}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}{account.is_active ? 'Yüklemelerde devre dışı bırak' : 'Yüklemeler için etkinleştir'}</button>
                    <button type="button" onClick={() => void disconnectAccount(account)} disabled={!!busyKey || account.status === 'disconnected'} className={`${buttonClass} border-rose-500/25 bg-rose-500/5 text-rose-200 hover:bg-rose-500/10`}>{busyKey === `disconnect:${account.remote_name}` ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Unplug className="h-3.5 w-3.5" />} Bağlantıyı kes</button>
                  </div>
                </article>;
              })}
            </div>}
          </section>

          <section className={`${panelClass} p-4 sm:p-5`}>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div><h3 className="font-semibold text-white">Zamanlanmış silme işleri</h3><p className="mt-1 text-xs text-slate-400">Bağlantısı kesilen hesapların işleri korunur; hesap tekrar bağlanınca işlenebilir.</p></div>
              <button type="button" onClick={() => void processDeletions()} disabled={!!busyKey || (status?.pending_deletions_count ?? 0) === 0} className={`${buttonClass} border-amber-500/30 bg-amber-500/10 text-amber-200 hover:bg-amber-500/20`}>{busyKey === 'process-deletions' ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />} Vadesi gelenleri işle</button>
            </div>
            {(status?.pending_deletions || []).length === 0 ? <p className="mt-4 text-sm text-slate-500">Bekleyen silme işi görünmüyor.</p> : <div className="mt-4 space-y-2">{(status?.pending_deletions || []).map((item, index) => <div key={item.deletion_id || `${item.remote_name}-${index}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-800 bg-slate-950/70 p-3 text-xs"><div className="min-w-0"><p className="truncate font-mono text-slate-200">{item.remote_name || 'Bilinmeyen hesap'}:{item.remote_object_path || 'nesne yolu yok'}</p><p className="mt-1 text-slate-500">Deneme: {item.attempts ?? 0} · Durum: {item.status || 'pending'}</p></div><span className="shrink-0 text-slate-400">{item.due_at ? new Date(item.due_at * 1000).toLocaleString() : 'Zaman bilinmiyor'}</span></div>)}</div>}
          </section>

          <p className="flex items-start gap-2 text-[11px] leading-5 text-slate-500"><ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" /> Bu sayfa token veya parola değerlerini görüntülemez. Hesap bağlantısını kesmek uzaktaki dosyaları silmez. Kota bilgisi yalnızca backend’in gerçek rclone sorgusu başarılı olduğunda gösterilir.</p>
        </div>
      </div>
    </div>
  );
}
