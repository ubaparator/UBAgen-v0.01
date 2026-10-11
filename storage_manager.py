"""
UBAgen — storage_manager.py
Real runtime storage infrastructure inspector, decision tree evaluator,
and isolated workspace/output lifecycle manager.

Distinguishes between:
- Google AI Studio Run (container overlayfs on /app/applet & /tmp)
- Published Cloud Run (ephemeral in-memory/scratch overlay unless GCS FUSE / Bucket is configured)
- Docker / VPS deployments (ephemeral container overlay unless a host volume or persistent disk is mounted)

Implements the Storage Decision Tree:
- Separates temporary torrent downloads (`download_temp`), temporary encode files (`encode_temp`),
  and final video/subtitle outputs (`video_outputs`).
- Never fabricates persistence guarantees from raw root disk size alone.
- Enforces configurable storage quotas, minimum free space reserves, TTL retention cleanup,
  and guaranteed `finally` cleanup for temporary workspaces.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Optional
import uuid

logger = logging.getLogger("ubagen.storage")

# Default limits and directory names
DEFAULT_MAX_STORAGE_GB = 200.0
DEFAULT_MIN_FREE_RESERVE_GB = 2.0
DEFAULT_RETENTION_HOURS = 24.0
DISCORD_DEFAULT_UPLOAD_LIMIT_BYTES = 8 * 1024 * 1024  # 8 MiB standard Discord attachment limit

EPHEMERAL_FS_TYPES = {
    "overlay",
    "overlayfs",
    "tmpfs",
    "ramfs",
    "aufs",
    "devtmpfs",
    "squashfs",
}

PERSISTENT_DISK_FS_TYPES = {
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "zfs",
    "ntfs",
    "apfs",
    "nfs",
    "nfs4",
    "cifs",
    "smb3",
    "fuse.gcsfuse",
    "gcsfuse",
    "fuse.s3fs",
    "fuse.rclone",
}


def format_storage_bytes(num_bytes: int | float | None) -> str:
    """Format byte count into human-readable binary units (B, KB, MB, GB, TB)."""
    if num_bytes is None or num_bytes < 0:
        return "Unavailable"
    val = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(val) < 1024.0:
            return f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} PB"


def detect_deployment_environment() -> dict[str, Any]:
    """
    Distinguish between Google AI Studio Run, Published Google Cloud Run,
    Docker container, and standalone Linux/VPS host.
    """
    k_service = os.getenv("K_SERVICE", "").strip()
    k_revision = os.getenv("K_REVISION", "").strip()
    control_plane_port = os.getenv("CONTROL_PLANE_PORT", "").strip()
    app_url = os.getenv("APP_URL", "").strip()
    pwd = os.getcwd()
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

    if k_service and (control_plane_port or pwd.startswith("/app/applet") or "ais-dev-" in app_url):
        return {
            "runtime_type": "google_ai_studio_run",
            "label": "Google AI Studio Run (Build Preview Container)",
            "is_cloud_run": True,
            "is_ai_studio_preview": True,
            "is_container": True,
            "service_name": k_service,
            "revision": k_revision or None,
            "public_base_url": os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip() or app_url or None,
        }

    if k_service:
        return {
            "runtime_type": "google_cloud_run",
            "label": "Google Cloud Run (Published Serverless Container)",
            "is_cloud_run": True,
            "is_ai_studio_preview": False,
            "is_container": True,
            "service_name": k_service,
            "revision": k_revision or None,
            "public_base_url": os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip() or app_url or None,
        }

    if in_docker:
        return {
            "runtime_type": "docker_container",
            "label": "Docker Container Runtime",
            "is_cloud_run": False,
            "is_ai_studio_preview": False,
            "is_container": True,
            "service_name": None,
            "revision": None,
            "public_base_url": os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip() or app_url or None,
        }

    return {
        "runtime_type": "host_or_vps",
        "label": "Direct Host / VPS Runtime",
        "is_cloud_run": False,
        "is_ai_studio_preview": False,
        "is_container": False,
        "service_name": None,
        "revision": None,
        "public_base_url": os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip() or app_url or None,
    }


def parse_proc_mounts() -> list[dict[str, str]]:
    """
    Parse /proc/mounts (with fallback to `mount` command) to inspect real filesystem
    mount points, fs types (overlay, tmpfs, ext4, fuse.gcsfuse, etc.), and options.
    """
    mounts: list[dict[str, str]] = []
    proc_mounts = Path("/proc/mounts")
    if proc_mounts.exists():
        try:
            for line in proc_mounts.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    mounts.append({
                        "device": parts[0],
                        "mount_point": parts[1].replace("\\040", " "),
                        "fs_type": parts[2],
                        "options": parts[3],
                    })
            if mounts:
                return mounts
        except Exception as exc:
            logger.debug("Could not read /proc/mounts: %s", exc)

    try:
        proc = subprocess.run(
            ["mount"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        for line in (proc.stdout or "").splitlines():
            # Format: <device> on <mount_point> type <fs_type> (<options>)
            m = re.match(r"^(\S+)\s+on\s+(.+?)\s+type\s+(\S+)\s+\(([^)]*)\)", line.strip())
            if m:
                mounts.append({
                    "device": m.group(1),
                    "mount_point": m.group(2),
                    "fs_type": m.group(3),
                    "options": m.group(4),
                })
    except Exception as exc:
        logger.debug("Could not run mount fallback: %s", exc)

    return mounts


def find_mount_for_path(target_path: Path, mounts: Optional[list[dict[str, str]]] = None) -> dict[str, str]:
    """Find the most specific mount entry covering `target_path`."""
    all_mounts = mounts if mounts is not None else parse_proc_mounts()
    try:
        resolved = str(target_path.resolve())
    except Exception:
        resolved = os.path.abspath(str(target_path))

    best_match: dict[str, str] = {
        "device": "unknown",
        "mount_point": "/",
        "fs_type": "unknown",
        "options": "rw",
    }
    best_len = -1
    for entry in all_mounts:
        mp = entry.get("mount_point", "/")
        if resolved == mp or resolved.startswith(mp.rstrip("/") + "/") or mp == "/":
            if len(mp) > best_len:
                best_match = entry
                best_len = len(mp)
    return best_match


def verify_directory_writable(directory: Path) -> tuple[bool, Optional[str]]:
    """
    Ensure `directory` exists and perform a real write+delete probe to confirm
    actual filesystem write permissions.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe_file = directory / f".ubagen_write_probe_{uuid.uuid4().hex[:8]}"
        probe_file.write_bytes(b"ok")
        probe_file.unlink(missing_ok=True)
        return True, None
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def inspect_path_metrics(target_path: Path, mounts: Optional[list[dict[str, str]]] = None) -> dict[str, Any]:
    """Measure statvfs, shutil.disk_usage, mount info, and write access for a given path."""
    writable, write_err = verify_directory_writable(target_path)
    probe_target = target_path if target_path.exists() else Path("/")

    total_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    free_bytes: Optional[int] = None
    avail_bytes: Optional[int] = None
    statvfs_bsize: Optional[int] = None

    try:
        st = os.statvfs(str(probe_target))
        statvfs_bsize = int(st.f_frsize)
        total_bytes = int(st.f_blocks * st.f_frsize)
        avail_bytes = int(st.f_bavail * st.f_frsize)
        free_bytes = int(st.f_bfree * st.f_frsize)
        used_bytes = max(0, total_bytes - avail_bytes)
    except Exception:
        with suppress(Exception):
            du = shutil.disk_usage(str(probe_target))
            total_bytes = int(du.total)
            used_bytes = int(du.used)
            free_bytes = int(du.free)
            avail_bytes = int(du.free)

    mount_info = find_mount_for_path(probe_target, mounts)
    fs_type = mount_info.get("fs_type", "unknown").lower()
    is_ephemeral_fs = fs_type in EPHEMERAL_FS_TYPES

    pct = round((used_bytes / total_bytes) * 100.0, 1) if (total_bytes and used_bytes is not None) else None

    return {
        "path": str(target_path),
        "resolved_path": str(probe_target.resolve()) if probe_target.exists() else str(target_path),
        "writable": writable,
        "write_error": write_err,
        "mount_point": mount_info.get("mount_point", "/"),
        "device": mount_info.get("device", "unknown"),
        "fs_type": fs_type,
        "mount_options": mount_info.get("options", ""),
        "is_ephemeral_fs": is_ephemeral_fs,
        "statvfs_fragment_size": statvfs_bsize,
        "total_bytes": total_bytes,
        "total_str": format_storage_bytes(total_bytes),
        "used_bytes": used_bytes,
        "used_str": format_storage_bytes(used_bytes),
        "free_bytes": avail_bytes,
        "free_str": format_storage_bytes(avail_bytes),
        "raw_free_bytes": free_bytes,
        "percent": pct,
    }


def detect_external_cloud_storage() -> dict[str, Any]:
    """Check whether external cloud/object storage environment variables are configured."""
    gcs_bucket = os.getenv("UBAGEN_GCS_BUCKET", "").strip() or os.getenv("GCS_BUCKET_NAME", "").strip()
    s3_bucket = os.getenv("UBAGEN_S3_BUCKET", "").strip() or os.getenv("S3_BUCKET_NAME", "").strip()
    r2_bucket = os.getenv("UBAGEN_R2_BUCKET", "").strip() or os.getenv("R2_BUCKET_NAME", "").strip()
    gdrive_creds = (
        os.getenv("GOOGLE_DRIVE_CREDENTIALS", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        or os.getenv("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    )
    gdrive_files = any(
        os.path.isfile(f)
        for f in ("credentials.json", "service_account.json", "google_drive_credentials.json")
    )

    providers: list[str] = []
    if gcs_bucket:
        providers.append(f"GCS ({gcs_bucket})")
    if s3_bucket:
        providers.append(f"S3 ({s3_bucket})")
    if r2_bucket:
        providers.append(f"Cloudflare R2 ({r2_bucket})")
    if gdrive_creds or gdrive_files:
        providers.append("Google Drive Credentials")

    rclone_accounts: list[dict[str, Any]] = []
    rclone_connected_remotes: list[str] = []
    with suppress(Exception):
        from rclone_manager import get_rclone_manager

        mgr = get_rclone_manager()
        for acc in mgr.list_accounts():
            acc_dict = acc.to_public_dict() if hasattr(acc, "to_public_dict") else dict(acc)
            rclone_accounts.append(acc_dict)
            if acc_dict.get("is_active") and acc_dict.get("status") == "connected":
                prov = acc_dict.get("provider", "remote")
                rname = acc_dict.get("remote_name", "unknown")
                rclone_connected_remotes.append(f"rclone:{prov} ({rname})")
                providers.append(f"rclone:{prov} ({rname})")

    return {
        "configured": bool(providers),
        "providers": providers,
        "gcs_bucket": gcs_bucket or None,
        "s3_bucket": s3_bucket or None,
        "r2_bucket": r2_bucket or None,
        "google_drive_configured": bool(gdrive_creds or gdrive_files or any(a.get("provider") == "drive" and a.get("status") == "connected" for a in rclone_accounts)),
        "rclone_connected_remotes": rclone_connected_remotes,
        "rclone_accounts": rclone_accounts,
    }


class StorageManager:
    """
    Manages UBAgen storage directories, enforces the Storage Decision Tree,
    separates temporary torrent/encode directories from final video outputs,
    and performs quota & retention cleanup.
    """

    def __init__(
        self,
        base_storage_dir: Optional[Path | str] = None,
        temp_dir: Optional[Path | str] = None,
        output_dir: Optional[Path | str] = None,
        max_quota_gb: Optional[float] = None,
        min_free_reserve_gb: Optional[float] = None,
        retention_hours: Optional[float] = None,
    ) -> None:
        env_storage = os.getenv("UBAGEN_STORAGE_DIR", "").strip()
        env_temp = os.getenv("UBAGEN_TEMP_DIR", "").strip()
        env_output = os.getenv("UBAGEN_OUTPUT_DIR", "").strip()

        self.base_dir = Path(base_storage_dir or env_storage or ".").resolve()
        self.download_temp_dir = Path(temp_dir or env_temp or (self.base_dir / "download_temp")).resolve()
        self.encode_temp_dir = (self.download_temp_dir / "encode_temp").resolve()
        self.video_outputs_dir = Path(output_dir or env_output or (self.base_dir / "video_outputs")).resolve()

        raw_quota_gb = max_quota_gb if max_quota_gb is not None else float(
            os.getenv("UBAGEN_STORAGE_MAX_GB", str(DEFAULT_MAX_STORAGE_GB)) or DEFAULT_MAX_STORAGE_GB
        )
        raw_reserve_gb = min_free_reserve_gb if min_free_reserve_gb is not None else float(
            os.getenv("UBAGEN_MIN_FREE_RESERVE_GB", str(DEFAULT_MIN_FREE_RESERVE_GB)) or DEFAULT_MIN_FREE_RESERVE_GB
        )
        raw_retention_h = retention_hours if retention_hours is not None else float(
            os.getenv("UBAGEN_OUTPUT_RETENTION_HOURS", str(DEFAULT_RETENTION_HOURS)) or DEFAULT_RETENTION_HOURS
        )

        self.configured_quota_bytes = int(max(1.0, raw_quota_gb) * (1024**3))
        self.min_free_reserve_bytes = int(max(0.25, raw_reserve_gb) * (1024**3))
        self.retention_seconds = max(60.0, raw_retention_h * 3600.0)

        self._ensure_directories_with_fallback()

    def _ensure_directories_with_fallback(self) -> None:
        """
        Ensure download_temp, encode_temp, and video_outputs exist and are writable.
        If the configured/working directory is read-only (e.g. restricted container rootfs),
        gracefully fall back to /tmp/ubagen_storage so runtime operations never crash.
        """
        ok_temp, _ = verify_directory_writable(self.download_temp_dir)
        ok_enc, _ = verify_directory_writable(self.encode_temp_dir)
        ok_out, _ = verify_directory_writable(self.video_outputs_dir)

        if not (ok_temp and ok_enc and ok_out):
            fallback_root = Path(tempfile.gettempdir()) / "ubagen_storage"
            logger.warning(
                "Primary storage directories (%s) not writable; falling back to %s",
                self.base_dir,
                fallback_root,
            )
            self.base_dir = fallback_root.resolve()
            self.download_temp_dir = (self.base_dir / "download_temp").resolve()
            self.encode_temp_dir = (self.download_temp_dir / "encode_temp").resolve()
            self.video_outputs_dir = (self.base_dir / "video_outputs").resolve()
            verify_directory_writable(self.download_temp_dir)
            verify_directory_writable(self.encode_temp_dir)
            verify_directory_writable(self.video_outputs_dir)

    def get_directory_usage_bytes(self, directory: Path) -> tuple[int, int]:
        """Return (total_bytes, file_count) inside `directory`."""
        if not directory.exists():
            return 0, 0
        total = 0
        count = 0
        for p in directory.rglob("*"):
            if p.is_file():
                with suppress(OSError):
                    total += p.stat().st_size
                    count += 1
        return total, count

    def inspect(self) -> dict[str, Any]:
        """
        Execute full storage infrastructure audit and evaluate the Storage Decision Tree:
        - Branch A (`persistent_disk`): Verified writable persistent filesystem mount
        - Branch B (`cloud_object_storage`): Configured external bucket / cloud storage
        - Branch C (`ephemeral_local`): Writable container overlay/tmpfs; isolated temp & output
          directories active, aggressive cleanup enforced, no false persistence claims.
        """
        mounts = parse_proc_mounts()
        env_info = detect_deployment_environment()
        cloud_info = detect_external_cloud_storage()

        root_metrics = inspect_path_metrics(Path("/"), mounts)
        output_metrics = inspect_path_metrics(self.video_outputs_dir, mounts)
        temp_metrics = inspect_path_metrics(self.download_temp_dir, mounts)
        tmp_sys_metrics = inspect_path_metrics(Path("/tmp"), mounts)

        outputs_used_bytes, outputs_file_count = self.get_directory_usage_bytes(self.video_outputs_dir)
        temp_used_bytes, temp_file_count = self.get_directory_usage_bytes(self.download_temp_dir)

        fs_type = output_metrics["fs_type"]
        mount_point = output_metrics["mount_point"]
        explicit_persistent_flag = os.getenv("UBAGEN_PERSISTENT_VOLUME_VERIFIED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        )

        # Determine if output_dir is on a genuinely persistent disk mount
        is_persistent_disk = bool(
            output_metrics["writable"]
            and (
                fs_type in PERSISTENT_DISK_FS_TYPES
                or explicit_persistent_flag
                or (not output_metrics["is_ephemeral_fs"] and mount_point not in ("/", "/tmp", "/dev", "/dev/shm"))
            )
        )

        avail_bytes = output_metrics["free_bytes"] or 0
        safe_usable_bytes = max(0, avail_bytes - self.min_free_reserve_bytes)
        effective_budget_bytes = min(self.configured_quota_bytes, safe_usable_bytes)

        if is_persistent_disk:
            decision_branch = "A_PERSISTENT_DISK"
            storage_mode = "persistent_disk"
            persistence_verified = True
            summary = (
                f"Kalıcı sunucu diski doğrulandı ({fs_type} @ {mount_point}). "
                f"Video çıktıları '{self.video_outputs_dir}' dizininde, geçici torrent/encode "
                f"dosyaları '{self.download_temp_dir}' dizininde ayrıştırıldı."
            )
        elif cloud_info["configured"]:
            decision_branch = "B_EXTERNAL_CLOUD_STORAGE"
            storage_mode = "cloud_hybrid"
            persistence_verified = True
            summary = (
                f"Yerel worker dosya sistemi geçici ({fs_type} @ {mount_point}), kalıcı video çıktıları "
                f"Google Cloud Storage / Harici Bulut ({', '.join(cloud_info['providers'])}) üzerinde "
                f"7200 saniye (2 saat) saklama süresiyle tutulur. Geçici worker dosyaları işlem sonunda silinir."
            )
        else:
            decision_branch = "B_EPHEMERAL_CONTAINER_STORAGE"
            storage_mode = "ephemeral_container_fs"
            persistence_verified = False
            summary = (
                f"Dosya sistemi '{fs_type}' (mount: {mount_point}) konteyner katmanıdır ve GEÇİCİDİR (ephemeral). "
                f"Toplam görünür alan ({output_metrics['total_str']}) kalıcı 200 GB disk garantisi vermez; "
                f"konteyner yeniden başlatıldığında veya Cloud Run yeniden dağıtıldığında sıfırlanır. "
                f"Kalıcı çıktılar için Google Cloud Storage (UBAGEN_GCS_BUCKET) veya Volume Mount yapılandırılmalıdır."
            )

        worker_temp_root = Path(
            os.getenv("UBAGEN_WORKER_TEMP_DIR", "").strip()
            or (self.download_temp_dir / "worker_jobs")
        ).resolve()

        job_backend_info: dict[str, Any] = {}
        cloud_run_arch: dict[str, Any] = {}
        with suppress(Exception):
            from job_store import detect_cloud_run_environment_details, get_job_store

            job_backend_info = get_job_store().get_backend_status()
            cloud_run_arch = detect_cloud_run_environment_details()

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "environment": env_info,
            "decision_branch": decision_branch,
            "storage_mode": storage_mode,
            "is_persistent": is_persistent_disk,
            "persistence_verified": persistence_verified,
            "200gb_persistent_guaranteed": bool(
                is_persistent_disk and safe_usable_bytes >= int(200 * (1024**3))
            ),
            "summary": summary,
            "directories": {
                "base_dir": str(self.base_dir),
                "video_outputs_dir": str(self.video_outputs_dir),
                "download_temp_dir": str(self.download_temp_dir),
                "encode_temp_dir": str(self.encode_temp_dir),
                "worker_scratch_dir": str(worker_temp_root),
            },
            "separation": {
                "persistent_output_storage": {
                    "provider": "Google Cloud Storage (GCS)" if cloud_info.get("gcs_bucket") else (
                        "Persistent Disk Volume" if is_persistent_disk else "Unconfigured (Local Ephemeral Fallback)"
                    ),
                    "gcs_bucket": cloud_info.get("gcs_bucket"),
                    "local_outputs_dir": str(self.video_outputs_dir),
                    "is_persistent": bool(cloud_info.get("gcs_bucket") or is_persistent_disk),
                    "retention_seconds": int(cloud_run_arch.get("retention_seconds") or 7200),
                    "deletion_policy": "Server-side active GCS object deletion & link revocation after 7200s (2 hours)",
                },
                "ephemeral_worker_scratch_disk": {
                    "worker_scratch_dir": str(worker_temp_root),
                    "download_temp_dir": str(self.download_temp_dir),
                    "encode_temp_dir": str(self.encode_temp_dir),
                    "fs_type": temp_metrics["fs_type"],
                    "mount_point": temp_metrics["mount_point"],
                    "is_ephemeral": True,
                    "free_str": temp_metrics["free_str"],
                    "cleanup_policy": "Isolated per-job directory (worker_<job_id>) wiped in finally block and startup",
                },
                "job_state_database": job_backend_info,
                "cloud_run_architecture": cloud_run_arch,
            },
            "usage": {
                "video_outputs_bytes": outputs_used_bytes,
                "video_outputs_str": format_storage_bytes(outputs_used_bytes),
                "video_outputs_count": outputs_file_count,
                "temp_workspace_bytes": temp_used_bytes,
                "temp_workspace_str": format_storage_bytes(temp_used_bytes),
                "temp_workspace_files": temp_file_count,
                "configured_quota_bytes": self.configured_quota_bytes,
                "configured_quota_str": format_storage_bytes(self.configured_quota_bytes),
                "safe_usable_bytes": safe_usable_bytes,
                "safe_usable_str": format_storage_bytes(safe_usable_bytes),
                "effective_budget_bytes": effective_budget_bytes,
                "effective_budget_str": format_storage_bytes(effective_budget_bytes),
                "min_free_reserve_bytes": self.min_free_reserve_bytes,
                "min_free_reserve_str": format_storage_bytes(self.min_free_reserve_bytes),
                "retention_hours": round(self.retention_seconds / 3600.0, 1),
                "encode_output_retention_seconds": int(cloud_run_arch.get("retention_seconds") or 7200),
            },
            "mounts": {
                "root": root_metrics,
                "video_outputs": output_metrics,
                "download_temp": temp_metrics,
                "system_tmp": tmp_sys_metrics,
            },
            "cloud_storage": cloud_info,
        }

    def create_download_job_dir(self, prefix: str = "job_") -> Path:
        """Create an isolated per-job torrent download directory inside `download_temp`."""
        self.download_temp_dir.mkdir(parents=True, exist_ok=True)
        job_dir = tempfile.mkdtemp(prefix=f"{prefix}{uuid.uuid4().hex[:8]}_", dir=str(self.download_temp_dir))
        return Path(job_dir)

    def create_encode_job_dir(self, prefix: str = "encode_") -> Path:
        """Create an isolated per-job FFmpeg encode directory inside `encode_temp`."""
        self.encode_temp_dir.mkdir(parents=True, exist_ok=True)
        job_dir = tempfile.mkdtemp(prefix=f"{prefix}{uuid.uuid4().hex[:8]}_", dir=str(self.encode_temp_dir))
        return Path(job_dir)

    def cleanup_stale_temp_dirs(self) -> int:
        """Remove leftover `job_*`, `encode_*`, and `worker_*` directories inside `download_temp`."""
        removed = 0
        worker_jobs_dir = self.download_temp_dir / "worker_jobs"
        for folder, prefix in (
            (self.download_temp_dir, "job_"),
            (self.encode_temp_dir, "encode_"),
            (worker_jobs_dir, "worker_"),
        ):
            if not folder.exists():
                continue
            for child in folder.iterdir():
                if child.is_dir() and child.name.startswith(prefix):
                    with suppress(Exception):
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
        return removed

    def cleanup_outputs(self, required_free_bytes: int = 0) -> dict[str, int]:
        """
        Clean up expired files in `video_outputs` (older than `retention_seconds`)
        and evict oldest files (FIFO) if output usage exceeds `effective_budget_bytes`
        or if `required_free_bytes` is needed for a new job.
        """
        if not self.video_outputs_dir.exists():
            return {"removed_files": 0, "freed_bytes": 0}

        now = time.time()
        removed_files = 0
        freed_bytes = 0

        files: list[tuple[float, int, Path]] = []
        for p in self.video_outputs_dir.iterdir():
            if p.is_file() and not p.name.startswith("."):
                try:
                    st = p.stat()
                    files.append((st.st_mtime, st.st_size, p))
                except OSError:
                    continue

        # 1. Remove expired outputs exceeding TTL retention
        remaining: list[tuple[float, int, Path]] = []
        for mtime, size, p in files:
            if (now - mtime) > self.retention_seconds:
                with suppress(OSError):
                    p.unlink(missing_ok=True)
                    removed_files += 1
                    freed_bytes += size
            else:
                remaining.append((mtime, size, p))

        # 2. Enforce quota budget and required_free_bytes (oldest first)
        remaining.sort(key=lambda item: item[0])
        current_outputs_bytes = sum(item[1] for item in remaining)
        metrics = inspect_path_metrics(self.video_outputs_dir)
        avail_bytes = metrics.get("free_bytes") or 0
        safe_usable = max(0, avail_bytes - self.min_free_reserve_bytes)

        for mtime, size, p in remaining:
            over_quota = (current_outputs_bytes + required_free_bytes) > self.configured_quota_bytes
            low_disk = safe_usable < required_free_bytes
            if not over_quota and not low_disk:
                break
            with suppress(OSError):
                p.unlink(missing_ok=True)
                removed_files += 1
                freed_bytes += size
                current_outputs_bytes = max(0, current_outputs_bytes - size)
                safe_usable += size

        return {"removed_files": removed_files, "freed_bytes": freed_bytes}

    def check_space_for_job(self, estimated_required_bytes: int = 256 * 1024 * 1024) -> tuple[bool, str]:
        """
        Verify sufficient writable space exists before starting a torrent download or FFmpeg job.
        Runs automatic retention/quota cleanup first.
        """
        self.cleanup_outputs(required_free_bytes=estimated_required_bytes)
        metrics = inspect_path_metrics(self.download_temp_dir)
        if not metrics["writable"]:
            return False, f"Depolama dizini yazılabilir değil ({metrics['write_error'] or 'Permission denied'})."

        avail_bytes = metrics.get("free_bytes")
        if avail_bytes is None:
            return True, "OK"

        safe_usable = max(0, avail_bytes - self.min_free_reserve_bytes)
        if safe_usable < estimated_required_bytes:
            return (
                False,
                f"Yetersiz kullanılabilir depolama alanı: Gerekli ~{format_storage_bytes(estimated_required_bytes)}, "
                f"güvenli kullanılabilir alan {format_storage_bytes(safe_usable)} "
                f"(toplam boş: {format_storage_bytes(avail_bytes)}).",
            )
        return True, "OK"

    def store_output_file(self, source_path: Path, preferred_filename: Optional[str] = None) -> dict[str, Any]:
        """
        Move or copy a completed output file (video or subtitle) into `video_outputs_dir`
        after running quota cleanup, and return metadata + relative download path.
        """
        if not source_path.is_file():
            raise FileNotFoundError(f"Output source file not found: {source_path}")

        file_size = source_path.stat().st_size
        self.cleanup_outputs(required_free_bytes=file_size)
        self.video_outputs_dir.mkdir(parents=True, exist_ok=True)

        raw_name = preferred_filename or source_path.name
        safe_name = re.sub(r"[^\w.\-]+", "_", raw_name).strip("._") or f"output_{uuid.uuid4().hex[:8]}"
        dest_path = self.video_outputs_dir / safe_name
        if dest_path.exists():
            stem = dest_path.stem[:60]
            suffix = dest_path.suffix
            safe_name = f"{stem}_{uuid.uuid4().hex[:6]}{suffix}"
            dest_path = self.video_outputs_dir / safe_name

        shutil.copy2(source_path, dest_path)
        inspection = self.inspect()
        base_url = (inspection["environment"].get("public_base_url") or "").rstrip("/")
        rel_url = f"/api/outputs/{safe_name}"
        full_url = f"{base_url}{rel_url}" if base_url else rel_url

        return {
            "filename": safe_name,
            "path": str(dest_path),
            "size_bytes": file_size,
            "size_str": format_storage_bytes(file_size),
            "relative_url": rel_url,
            "download_url": full_url,
            "is_persistent": inspection["is_persistent"],
            "storage_mode": inspection["storage_mode"],
            "retention_hours": inspection["usage"]["retention_hours"],
        }

    def list_output_files(self) -> list[dict[str, Any]]:
        """List stored files in `video_outputs_dir` sorted newest-first."""
        if not self.video_outputs_dir.exists():
            return []
        items: list[dict[str, Any]] = []
        base_url = (detect_deployment_environment().get("public_base_url") or "").rstrip("/")
        for p in self.video_outputs_dir.iterdir():
            if p.is_file() and not p.name.startswith("."):
                try:
                    st = p.stat()
                    rel_url = f"/api/outputs/{p.name}"
                    items.append({
                        "filename": p.name,
                        "size_bytes": st.st_size,
                        "size_str": format_storage_bytes(st.st_size),
                        "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                        "age_seconds": max(0, int(time.time() - st.st_mtime)),
                        "relative_url": rel_url,
                        "download_url": f"{base_url}{rel_url}" if base_url else rel_url,
                    })
                except OSError:
                    continue
        items.sort(key=lambda x: x["created_at"], reverse=True)
        return items

    def resolve_output_file(self, filename: str) -> Optional[Path]:
        """Safely resolve a requested filename inside `video_outputs_dir` without path traversal."""
        clean = (filename or "").strip()
        if not clean or "/" in clean or "\\" in clean or ".." in clean or clean.startswith("."):
            return None
        candidate = (self.video_outputs_dir / clean).resolve()
        if candidate.parent != self.video_outputs_dir.resolve():
            return None
        if not candidate.is_file():
            return None
        return candidate


_DEFAULT_STORAGE_MANAGER: Optional[StorageManager] = None


def get_storage_manager() -> StorageManager:
    """Return singleton StorageManager instance."""
    global _DEFAULT_STORAGE_MANAGER
    if _DEFAULT_STORAGE_MANAGER is None:
        _DEFAULT_STORAGE_MANAGER = StorageManager()
    return _DEFAULT_STORAGE_MANAGER
