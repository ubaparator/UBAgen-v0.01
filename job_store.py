"""
UBAgen — job_store.py
Persistent Job State Store & Google Cloud Storage (GCS) Output Lifecycle Manager.

Satisfies production Cloud Run & Cloud Run Job requirements:
1. Never stores job state exclusively in Cloud Run instance memory:
   - Primary backend: Google Cloud Firestore (`google-cloud-firestore` SDK or Firestore v1 REST API via ADC).
   - Fallback backend (when Firestore is not configured in local/CI environments): Multi-process-safe
     transactional SQLite database in WAL mode (`ubagen_jobs.sqlite3`).
2. Tracks complete per-job telemetry:
   - `job_id`, `user_id`, `stage`, `status`, `percent`, `processed_frames`, `total_frames`,
     `fps`, `eta_seconds`, `codec`, `error`, `created_at`, `completed_at`, `expires_at` (7200s TTL).
3. Enforces Google Cloud Storage verification & 7200-second (2-hour) retention:
   - Never exposes a completed download URL until `gcs_upload_verified` is True.
   - Enforces user ownership (`user_id` + HMAC/token) on downloads (blocks cross-user access with 403).
   - Actively deletes expired objects from GCS after 7200 seconds via server-side scheduled/background tasks
     (does not treat signed URL expiration alone as file deletion).
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger("ubagen.jobs")

DEFAULT_RETENTION_SECONDS = 7200  # 2 hours (7200 seconds) strict output retention
DEFAULT_CLOUD_RUN_JOB_NAME = "ubagen-encoder-worker"
SUPPORTED_CODECS: dict[str, str] = {
    "h264": "libx264",
    "libx264": "libx264",
    "x264": "libx264",
    "avc": "libx264",
    "h265": "libx265",
    "libx265": "libx265",
    "x265": "libx265",
    "hevc": "libx265",
}

VALID_STAGES = {
    "queued",
    "downloading",
    "preparing_subtitles",
    "processing_intro",
    "encoding",
    "uploading",
    "completed",
    "failed",
    "cancelled",
    "expired",
}


class FirestoreUnavailableError(RuntimeError):
    """Raised when Firestore is enabled (`UBAGEN_USE_FIRESTORE=true`) but unreachable or unauthorized."""


class JobNotFoundError(KeyError):
    """Raised when a requested `job_id` does not exist in the authoritative job store."""


def compute_job_idempotency_key(
    *,
    user_id: str,
    magnet: str,
    codec: str = "h264",
    subtitle_lang: str = "tur",
    subtitle_source: Optional[str] = None,
    intro_source: Optional[str] = None,
) -> str:
    """
    Compute a deterministic SHA-256 idempotency key for an encode request so identical
    in-flight requests from the same user are deduplicated instead of launched multiple times.
    """
    clean_user = (user_id or "").strip()
    clean_magnet = (magnet or "").strip()
    btih_match = re.search(r"xt=urn:btih:([A-Za-z0-9]{32,40})", clean_magnet, flags=re.IGNORECASE)
    magnet_canonical = btih_match.group(1).lower() if btih_match else clean_magnet.lower()
    _, canonical_codec, _ = normalize_video_codec(codec)
    clean_lang = (subtitle_lang or "tur").strip().lower()
    sub_part = Path(str(subtitle_source or "")).name if subtitle_source else ""
    intro_part = Path(str(intro_source or "")).name if intro_source else ""
    raw = f"{clean_user}|{magnet_canonical}|{canonical_codec}|{clean_lang}|{sub_part}|{intro_part}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]



def normalize_video_codec(raw_codec: Optional[str]) -> tuple[bool, str, str]:
    """
    Validate and normalize requested video codec.
    Returns `(is_valid, canonical_codec_name, ffmpeg_encoder_name)`.
    - `'h264'` -> `('h264', 'libx264')`
    - `'h265'` -> `('h265', 'libx265')`
    """
    cleaned = (raw_codec or "h264").strip().lower()
    if not cleaned:
        cleaned = "h264"
    encoder = SUPPORTED_CODECS.get(cleaned)
    if not encoder:
        return (
            False,
            cleaned,
            "Geçersiz video codec seçimi. Yalnızca `h264` (`libx264`) veya `h265` (`libx265`) desteklenmektedir.",
        )
    canonical = "h265" if encoder == "libx265" else "h264"
    return True, canonical, encoder


def _iso_utc(epoch_sec: Optional[float]) -> Optional[str]:
    if epoch_sec is None:
        return None
    try:
        return datetime.fromtimestamp(float(epoch_sec), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _get_gcp_metadata(path: str, timeout: float = 1.5) -> Optional[str]:
    """Query Google Cloud Metadata Server when running on Cloud Run."""
    req = urllib.request.Request(
        f"http://metadata.google.internal/computeMetadata/v1/{path}",
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return resp.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return None
    return None


def get_gcp_access_token() -> Optional[str]:
    """
    Obtain an OAuth2 access token from Application Default Credentials or the
    Cloud Run Metadata Server without embedding any keys in source code.
    """
    # 1. Try google.auth if installed
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if not creds.valid:
            creds.refresh(google.auth.transport.requests.Request())
        if creds.token:
            return str(creds.token)
    except Exception:
        pass

    # 2. Try Cloud Run / GCE Metadata Server
    raw = _get_gcp_metadata("instance/service-accounts/default/token")
    if raw:
        with suppress(Exception):
            data = json.loads(raw)
            token = data.get("access_token")
            if token:
                return str(token)
    return None


def _discover_job_name_from_yaml() -> Optional[str]:
    """Read canonical Cloud Run Job name from `cloud-run-encoder-job.yaml` in the repository."""
    candidates = [
        Path(__file__).resolve().parent / "cloud-run-encoder-job.yaml",
        Path("cloud-run-encoder-job.yaml").resolve(),
    ]
    for yaml_path in candidates:
        if yaml_path.is_file():
            with suppress(Exception):
                content = yaml_path.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"metadata:\s*\n(?:\s*#.*\n)*\s*name:\s*([A-Za-z0-9_-]+)", content)
                if m and m.group(1).strip():
                    return m.group(1).strip()
    return None


def discover_cloud_run_job_name(project_id: Optional[str] = None, region: Optional[str] = None) -> str:
    """
    Determine the Cloud Run Job worker name from:
    1. Explicit `UBAGEN_CLOUD_RUN_JOB_NAME` or `CLOUD_RUN_JOB` environment variable
    2. `cloud-run-encoder-job.yaml` (`metadata.name`)
    3. Canonical default `ubagen-encoder-worker`
    """
    explicit = os.getenv("UBAGEN_CLOUD_RUN_JOB_NAME", "").strip() or os.getenv("CLOUD_RUN_JOB", "").strip()
    if explicit:
        return explicit
    if os.getenv("UBAGEN_DISABLE_JOB_AUTODISCOVERY", "").strip().lower() in ("1", "true", "yes"):
        return ""
    from_yaml = _discover_job_name_from_yaml()
    if from_yaml:
        return from_yaml
    return DEFAULT_CLOUD_RUN_JOB_NAME


def detect_cloud_run_environment_details() -> dict[str, Any]:
    """
    Inspect current Cloud Run service settings, service account, region, project ID,
    and configured Cloud Run Job / Firestore / GCS settings without fabricating access.
    """
    k_service = os.getenv("K_SERVICE", "").strip() or None
    k_revision = os.getenv("K_REVISION", "").strip() or None
    k_config = os.getenv("K_CONFIGURATION", "").strip() or None
    cloud_run_job = os.getenv("CLOUD_RUN_JOB", "").strip() or None
    on_gcp_runtime = bool(k_service or cloud_run_job)

    project_id = (
        os.getenv("UBAGEN_GCP_PROJECT_ID", "").strip()
        or os.getenv("UBAGEN_FIRESTORE_PROJECT", "").strip()
        or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.getenv("GCLOUD_PROJECT", "").strip()
        or (_get_gcp_metadata("project/project-id") if on_gcp_runtime else None)
    )
    region_meta = _get_gcp_metadata("instance/region") if on_gcp_runtime else None
    region_from_meta = region_meta.split("/")[-1] if region_meta and "/" in region_meta else region_meta
    region = (
        os.getenv("UBAGEN_GCP_REGION", "").strip()
        or os.getenv("UBAGEN_CLOUD_RUN_REGION", "").strip()
        or os.getenv("CLOUD_RUN_REGION", "").strip()
        or region_from_meta
        or "europe-west2"
    )
    service_account = (
        os.getenv("UBAGEN_SERVICE_ACCOUNT_EMAIL", "").strip()
        or (_get_gcp_metadata("instance/service-accounts/default/email") if on_gcp_runtime else None)
        or os.getenv("AUTHORIZED_SERVICE_ACCOUNT_EMAIL", "").strip()
        or None
    )

    public_base_url = (
        os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip()
        or os.getenv("APP_URL", "").strip()
        or os.getenv("CLOUD_RUN_SERVICE_URL", "").strip()
        or os.getenv("SHARED_APP_URL", "").strip()
        or None
    )
    cloud_run_job_name = discover_cloud_run_job_name(project_id=project_id, region=region) or None
    gcs_bucket = os.getenv("UBAGEN_GCS_BUCKET", "").strip() or os.getenv("GCS_BUCKET_NAME", "").strip() or None
    firestore_collection = os.getenv("UBAGEN_FIRESTORE_COLLECTION", "ubagen_encode_jobs").strip() or "ubagen_encode_jobs"
    firestore_db = os.getenv("UBAGEN_FIRESTORE_DATABASE", "(default)").strip() or "(default)"
    firestore_enabled = os.getenv("UBAGEN_USE_FIRESTORE", "").strip().lower() in ("1", "true", "yes")
    worker_temp_dir = (
        os.getenv("UBAGEN_WORKER_TEMP_DIR", "").strip()
        or ("/tmp/ubagen_worker_scratch" if on_gcp_runtime else None)
    )

    return {
        "k_service": k_service,
        "k_revision": k_revision,
        "k_configuration": k_config,
        "cloud_run_job": cloud_run_job,
        "project_id": project_id,
        "region": region,
        "service_account_email": service_account,
        "public_base_url": public_base_url,
        "cloud_run_job_name": cloud_run_job_name,
        "cloud_run_job_configured": bool(cloud_run_job_name and project_id),
        "gcs_bucket": gcs_bucket,
        "gcs_configured": bool(gcs_bucket),
        "recommended_gcs_bucket": f"{project_id}-ubagen-media" if project_id else "ubagen-media",
        "firestore_enabled": firestore_enabled,
        "firestore_database": firestore_db,
        "firestore_collection": firestore_collection,
        "worker_temp_dir": worker_temp_dir,
        "retention_seconds": get_configured_retention_seconds(),
        "secrets_present": {
            "DISCORD_BOT_TOKEN": bool(os.getenv("DISCORD_BOT_TOKEN", "").strip()),
            "UBAGEN_ADMIN_TOKEN": bool(os.getenv("UBAGEN_ADMIN_TOKEN", "").strip()),
        },
        "recommended_job_profile": {
            "cpu": "4",
            "memory": "8Gi",
            "ephemeral_storage_note": "Cloud Run Gen2 /tmp uses in-memory tmpfs backed by container memory (8Gi recommended for <=1.5GB source + encode output).",
            "task_timeout_seconds": 3600,
            "parallelism": 1,
            "tasks": 1,
            "max_retries": 1,
        },
    }


def discover_and_apply_runtime_config() -> dict[str, Any]:
    """
    Auto-discover non-secret configuration values from the Cloud Run / container environment
    and populate missing `UBAGEN_*` variables without overwriting explicitly configured values
    and without ever fabricating or logging secret values.
    """
    details = detect_cloud_run_environment_details()
    applied: dict[str, str] = {}

    def _apply_if_missing(env_key: str, candidate: Optional[str]) -> None:
        if not os.getenv(env_key, "").strip() and candidate and str(candidate).strip():
            val = str(candidate).strip()
            os.environ[env_key] = val
            applied[env_key] = val

    _apply_if_missing("UBAGEN_GCP_PROJECT_ID", details.get("project_id"))
    _apply_if_missing("UBAGEN_GCP_REGION", details.get("region"))
    _apply_if_missing("UBAGEN_SERVICE_ACCOUNT_EMAIL", details.get("service_account_email"))
    _apply_if_missing("UBAGEN_PUBLIC_BASE_URL", details.get("public_base_url"))
    _apply_if_missing("UBAGEN_CLOUD_RUN_JOB_NAME", details.get("cloud_run_job_name"))
    _apply_if_missing("UBAGEN_FIRESTORE_COLLECTION", details.get("firestore_collection"))
    _apply_if_missing("UBAGEN_FIRESTORE_DATABASE", details.get("firestore_database"))
    _apply_if_missing("UBAGEN_ENCODE_RETENTION_SECONDS", str(details.get("retention_seconds") or DEFAULT_RETENTION_SECONDS))
    if details.get("worker_temp_dir"):
        _apply_if_missing("UBAGEN_WORKER_TEMP_DIR", details.get("worker_temp_dir"))

    return {
        "discovered": detect_cloud_run_environment_details(),
        "auto_applied_keys": sorted(applied.keys()),
        "auto_applied_values": applied,
    }


def get_configured_retention_seconds() -> int:
    """Return configured encode output retention in seconds (default: 7200s = 2 hours)."""
    for env_key in ("UBAGEN_ENCODE_RETENTION_SECONDS", "UBAGEN_OUTPUT_TTL_SECONDS"):
        raw = os.getenv(env_key, "").strip()
        if raw:
            with suppress(ValueError):
                val = int(float(raw))
                if val > 0:
                    return val
    return DEFAULT_RETENTION_SECONDS


@dataclass
class EncodeJob:
    """Persistent state model for an encode job."""

    job_id: str
    user_id: str
    magnet: str
    codec: str = "h264"  # 'h264' or 'h265'
    video_encoder: str = "libx264"  # 'libx264' or 'libx265'
    subtitle_lang: str = "tur"
    subtitle_source: Optional[str] = None  # optional external .ass/.srt path or URL
    subtitle_format: Optional[str] = None  # 'ass' or 'srt'
    intro_source: Optional[str] = None  # optional intro video path or URL
    has_intro: bool = False
    stage: str = "queued"
    status: str = "queued"  # queued, running, completed, failed, expired
    percent: float = 0.0
    processed_frames: int = 0
    total_frames: Optional[int] = None
    fps: float = 0.0
    eta_seconds: Optional[int] = None
    error: Optional[str] = None
    user_name: Optional[str] = None
    guild_id: Optional[str] = None
    channel_id: Optional[str] = None
    message_id: Optional[str] = None
    application_id: Optional[str] = None
    interaction_token: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    expires_at: Optional[float] = None
    retention_seconds: int = DEFAULT_RETENTION_SECONDS
    gcs_bucket: Optional[str] = None
    gcs_object_name: Optional[str] = None
    gcs_uri: Optional[str] = None
    gcs_upload_verified: bool = False
    output_filename: Optional[str] = None
    output_size_bytes: Optional[int] = None
    download_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    download_url: Optional[str] = None
    deleted_from_gcs: bool = False
    deleted_at: Optional[float] = None
    worker_execution_name: Optional[str] = None
    staged_gcs_objects: list[str] = field(default_factory=list)
    command_source: str = "encode"
    idempotency_key: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 1
    cancelled_at: Optional[float] = None
    cancelled_by: Optional[str] = None


    def is_expired(self, now: Optional[float] = None) -> bool:
        cur = now if now is not None else time.time()
        if self.deleted_from_gcs or self.status == "expired" or self.stage == "expired":
            return True
        if self.expires_at is not None and cur >= self.expires_at:
            return True
        return False

    def can_user_access(self, requester_user_id: Optional[str], supplied_token: Optional[str] = None) -> bool:
        """
        Enforce strict per-user access control:
        - `requester_user_id` must match `self.user_id`.
        - If `supplied_token` is provided (or required), it must match `self.download_token` in constant time.
        """
        clean_req_user = (requester_user_id or "").strip()
        clean_job_user = (self.user_id or "").strip()
        if not clean_req_user or not clean_job_user:
            return False
        if not hmac.compare_digest(clean_req_user.encode("utf-8"), clean_job_user.encode("utf-8")):
            return False
        if supplied_token is not None:
            clean_tok = supplied_token.strip()
            if not clean_tok or not self.download_token:
                return False
            if not hmac.compare_digest(clean_tok.encode("utf-8"), self.download_token.encode("utf-8")):
                return False
        return True

    def is_downloadable(self, now: Optional[float] = None) -> bool:
        """
        Return True ONLY if the GCS object was verified uploaded, the job is completed,
        and the 7200-second retention window has not expired.
        """
        if self.status != "completed" or self.stage != "completed":
            return False
        if not self.gcs_upload_verified:
            return False
        if self.deleted_from_gcs:
            return False
        if not self.gcs_object_name and not self.output_filename:
            return False
        if self.is_expired(now):
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EncodeJob":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def to_public_dict(
        self,
        requester_user_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Return safe dictionary representation for API/dashboard responses.
        Never exposes `interaction_token`, raw `magnet` credentials, or `download_token`
        to unauthorized users, and never exposes `download_url` unless `is_downloadable()` is True
        and `requester_user_id` matches `self.user_id`.
        """
        cur = now if now is not None else time.time()
        expired = self.is_expired(cur)
        effective_status = "expired" if (expired and self.status == "completed") else self.status
        effective_stage = "expired" if (expired and self.stage == "completed") else self.stage
        remaining_sec = (
            max(0, int(round(self.expires_at - cur)))
            if (self.expires_at is not None and not expired)
            else (0 if self.expires_at is not None else None)
        )

        authorized = bool(
            requester_user_id
            and hmac.compare_digest(str(requester_user_id).strip(), str(self.user_id).strip())
        )
        effective_download_url = (
            self.download_url
            if (authorized and self.is_downloadable(cur))
            else None
        )

        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "codec": self.codec,
            "video_encoder": self.video_encoder,
            "subtitle_lang": self.subtitle_lang,
            "subtitle_format": self.subtitle_format,
            "has_intro": self.has_intro,
            "stage": effective_stage,
            "status": effective_status,
            "percent": round(float(self.percent or 0.0), 1),
            "processed_frames": int(self.processed_frames or 0),
            "total_frames": int(self.total_frames) if self.total_frames is not None else None,
            "fps": round(float(self.fps or 0.0), 2),
            "eta_seconds": int(self.eta_seconds) if self.eta_seconds is not None else None,
            "error": self.error,
            "created_at": self.created_at,
            "created_at_iso": _iso_utc(self.created_at),
            "updated_at": self.updated_at,
            "updated_at_iso": _iso_utc(self.updated_at),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "completed_at_iso": _iso_utc(self.completed_at),
            "expires_at": self.expires_at,
            "expires_at_iso": _iso_utc(self.expires_at),
            "expires_in_seconds": remaining_sec,
            "retention_seconds": self.retention_seconds,
            "gcs_bucket": self.gcs_bucket,
            "gcs_object_name": self.gcs_object_name,
            "gcs_uri": self.gcs_uri,
            "gcs_upload_verified": self.gcs_upload_verified,
            "output_filename": self.output_filename,
            "output_size_bytes": self.output_size_bytes,
            "download_ready": self.is_downloadable(cur),
            "download_url": effective_download_url,
            "deleted_from_gcs": self.deleted_from_gcs,
            "deleted_at": self.deleted_at,
            "deleted_at_iso": _iso_utc(self.deleted_at),
            "worker_execution_name": self.worker_execution_name,
            "command_source": self.command_source,
            "idempotency_key": self.idempotency_key,
            "retry_count": int(self.retry_count or 0),
            "max_retries": int(self.max_retries or 1),
            "cancelled_at": self.cancelled_at,
            "cancelled_at_iso": _iso_utc(self.cancelled_at),
        }


# ------------------------------------------------------------------------------
# Google Cloud Storage Backend (SDK + REST API + Verified Local Staging for Tests)
# ------------------------------------------------------------------------------
class GCSOutputStorage:
    """
    Handles uploading, verifying, streaming/signing, and actively deleting encoded video outputs
    in Google Cloud Storage (`gs://<UBAGEN_GCS_BUCKET>/...`).
    Never embeds credentials in code; uses Application Default Credentials (ADC) or Metadata Server.
    """

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        local_mirror_dir: Optional[Path | str] = None,
    ) -> None:
        self.bucket_name = (
            bucket_name
            if bucket_name is not None
            else (os.getenv("UBAGEN_GCS_BUCKET", "").strip() or os.getenv("GCS_BUCKET_NAME", "").strip())
        )
        env_mirror = os.getenv("UBAGEN_GCS_LOCAL_MIRROR_DIR", "").strip()
        self.local_mirror_dir: Optional[Path] = (
            Path(local_mirror_dir).resolve()
            if local_mirror_dir is not None
            else (Path(env_mirror).resolve() if env_mirror else None)
        )
        if self.local_mirror_dir is not None:
            self.local_mirror_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_configured(self) -> bool:
        if self.bucket_name:
            return True
        with suppress(Exception):
            from rclone_manager import get_rclone_manager

            if get_rclone_manager().select_active_remote() is not None:
                return True
        return False

    def _mirror_object_path(self, bucket: str, object_name: str) -> Optional[Path]:
        if self.local_mirror_dir is None:
            return None
        safe_rel = Path(object_name.lstrip("/"))
        if ".." in safe_rel.parts:
            raise ValueError("Invalid GCS object name")
        target = (self.local_mirror_dir / bucket / safe_rel).resolve()
        return target

    def upload_file(
        self,
        local_path: Path,
        object_name: str,
        content_type: str = "video/mp4",
        metadata: Optional[dict[str, str]] = None,
        *,
        retention_seconds: Optional[int] = None,
        job_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Upload `local_path` via Rclone (MEGA / Google Drive) or local mirror in tests,
        and strictly verify that the uploaded object exists and has the exact same non-zero byte size
        before returning `verified=True`.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Yüklenecek yerel çıktı dosyası bulunamadı: {local_path}")

        expected_size = local_path.stat().st_size
        if expected_size <= 0:
            raise RuntimeError("Encode çıktı dosyası boş (0 byte); yükleme iptal edildi.")

        clean_object = object_name.lstrip("/")
        bucket = (self.bucket_name or "").strip()

        # 1. If local mirror is active (used for deterministic integration testing)
        if self.local_mirror_dir is not None and bucket:
            mirror_target = self._mirror_object_path(bucket, clean_object)
            if mirror_target is not None:
                mirror_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, mirror_target)
                verified = self.verify_object(bucket, clean_object, expected_size=expected_size)
                if not verified:
                    raise RuntimeError("Nesne doğrulaması (boyut/varlık kontrolü) başarısız oldu.")
                return {
                    "uploaded": True,
                    "verified": True,
                    "bucket": bucket,
                    "object_name": clean_object,
                    "gcs_uri": f"gs://{bucket}/{clean_object}",
                    "size_bytes": expected_size,
                    "backend": "gcs_local_mirror",
                }

        # 2. Primary Runtime Backend: Rclone (MEGA / Google Drive)
        with suppress(ImportError):
            from rclone_manager import get_rclone_manager

            rclone_mgr = get_rclone_manager()
            active_remote = rclone_mgr.select_active_remote(preferred_remote=bucket or None)
            if active_remote is not None:
                job_id_meta = job_id or ((metadata or {}).get("job_id") if isinstance(metadata, dict) else None)
                effective_retention = (
                    retention_seconds if retention_seconds is not None else get_configured_retention_seconds()
                )
                return rclone_mgr.upload_file(
                    local_path,
                    clean_object,
                    preferred_remote=active_remote.remote_name,
                    retention_seconds=effective_retention,
                    job_id=job_id_meta,
                )

        if not bucket:
            raise RuntimeError(
                "Rclone depolama hesabı (MEGA / Google Drive) veya (`UBAGEN_GCS_BUCKET`) yapılandırılmamış. "
                "Kalıcı çıktı yüklemesi tamamlanamadı."
            )

        # 3. Try google-cloud-storage Python SDK if installed
        sdk_tried = False
        try:
            from google.cloud import storage as gcs_sdk  # type: ignore

            sdk_tried = True
            client = gcs_sdk.Client()
            bkt = client.bucket(bucket)
            blob = bkt.blob(clean_object)
            if metadata:
                blob.metadata = metadata
            blob.upload_from_filename(str(local_path), content_type=content_type, timeout=600)
            blob.reload()
            remote_size = int(blob.size or 0)
            if remote_size != expected_size:
                raise RuntimeError(
                    f"GCS boyut uyuşmazlığı: yerel={expected_size} B, uzak={remote_size} B"
                )
            return {
                "uploaded": True,
                "verified": True,
                "bucket": bucket,
                "object_name": clean_object,
                "gcs_uri": f"gs://{bucket}/{clean_object}",
                "size_bytes": remote_size,
                "backend": "google_cloud_storage_sdk",
            }
        except ImportError:
            pass
        except Exception as exc:
            if sdk_tried:
                logger.warning("google-cloud-storage SDK upload error (%s): %s", type(exc).__name__, exc)
                raise RuntimeError(f"GCS yükleme hatası: {exc}") from exc

        # 4. Direct REST fallback
        token = get_gcp_access_token()
        if not token:
            raise RuntimeError(
                "Aktif bir rclone (MEGA / Google Drive) hesabı bulunamadı ve kimlik doğrulama token'ı alınamadı."
            )

        encoded_name = urllib.parse.quote(clean_object, safe="")
        upload_url = (
            f"https://storage.googleapis.com/upload/storage/v1/b/"
            f"{urllib.parse.quote(bucket, safe='')}/o?uploadType=media&name={encoded_name}"
        )
        with open(local_path, "rb") as f:
            payload = f.read()

        req = urllib.request.Request(
            upload_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": content_type,
                "Content-Length": str(expected_size),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"GCS HTTP {exc.code} yükleme hatası: {err_body}") from exc
        except Exception as exc:
            raise RuntimeError(f"GCS bağlantı hatası: {exc}") from exc

        remote_size = int(resp_data.get("size") or 0)
        if remote_size != expected_size or not self.verify_object(bucket, clean_object, expected_size=expected_size):
            raise RuntimeError("GCS nesnesi yüklendikten sonra doğrulanamadı.")

        return {
            "uploaded": True,
            "verified": True,
            "bucket": bucket,
            "object_name": clean_object,
            "gcs_uri": f"gs://{bucket}/{clean_object}",
            "size_bytes": remote_size,
            "backend": "google_cloud_storage_rest",
        }

    def verify_object(self, bucket: str, object_name: str, expected_size: Optional[int] = None) -> bool:
        """Verify that remote object exists and matches `expected_size`."""
        clean_object = object_name.lstrip("/")
        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            if not mirror_target.is_file():
                return False
            actual_size = mirror_target.stat().st_size
            if actual_size <= 0:
                return False
            if expected_size is not None and actual_size != expected_size:
                return False
            return True

        with suppress(Exception):
            from rclone_manager import get_rclone_manager

            rclone_mgr = get_rclone_manager()
            if rclone_mgr.get_account(bucket) is not None or rclone_mgr.select_active_remote(bucket) is not None:
                return rclone_mgr.verify_object(bucket, clean_object, expected_size=expected_size)

        try:
            from google.cloud import storage as gcs_sdk  # type: ignore

            client = gcs_sdk.Client()
            blob = client.bucket(bucket).blob(clean_object)
            if not blob.exists():
                return False
            blob.reload()
            actual_size = int(blob.size or 0)
            if actual_size <= 0:
                return False
            if expected_size is not None and actual_size != expected_size:
                return False
            return True
        except ImportError:
            pass
        except Exception:
            return False

        token = get_gcp_access_token()
        if not token:
            return False
        encoded_name = urllib.parse.quote(clean_object, safe="")
        meta_url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o/{encoded_name}"
        req = urllib.request.Request(meta_url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    return False
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
                actual_size = int(data.get("size") or 0)
                if actual_size <= 0:
                    return False
                if expected_size is not None and actual_size != expected_size:
                    return False
                return True
        except Exception:
            return False

    def delete_object(self, bucket: str, object_name: str) -> bool:
        """
        Actively delete remote object via rclone (or local mirror in tests).
        If the rclone remote is disconnected, preserves the deletion task in `rclone_pending_deletions`.
        """
        clean_object = (object_name or "").lstrip("/")
        if not bucket or not clean_object:
            return False

        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            if mirror_target.exists():
                mirror_target.unlink(missing_ok=True)
            return not mirror_target.exists()

        with suppress(Exception):
            from rclone_manager import get_rclone_manager

            rclone_mgr = get_rclone_manager()
            if rclone_mgr.get_account(bucket) is not None:
                return rclone_mgr.delete_object(bucket, clean_object)

        try:
            from google.cloud import storage as gcs_sdk  # type: ignore

            client = gcs_sdk.Client()
            blob = client.bucket(bucket).blob(clean_object)
            if blob.exists():
                blob.delete()
            return True
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("GCS SDK delete error for gs://%s/%s: %s", bucket, clean_object, exc)
            return False

        token = get_gcp_access_token()
        if not token:
            return False
        encoded_name = urllib.parse.quote(clean_object, safe="")
        del_url = f"https://storage.googleapis.com/storage/v1/b/{urllib.parse.quote(bucket, safe='')}/o/{encoded_name}"
        req = urllib.request.Request(del_url, method="DELETE", headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status in (200, 204)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return True
            logger.warning("GCS REST delete HTTP %s for gs://%s/%s", exc.code, bucket, clean_object)
            return False
        except Exception as exc:
            logger.warning("GCS REST delete error for gs://%s/%s: %s", bucket, clean_object, exc)
            return False

    def open_download_stream_or_path(self, bucket: str, object_name: str) -> tuple[Optional[Path], Optional[bytes]]:
        """
        Retrieve either a local verified mirror Path or download bytes via rclone
        after authorization and expiration checks have passed.
        """
        clean_object = object_name.lstrip("/")
        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            if mirror_target.is_file():
                return mirror_target, None
            return None, None

        with suppress(Exception):
            from rclone_manager import get_rclone_manager

            rclone_mgr = get_rclone_manager()
            if rclone_mgr.get_account(bucket) is not None or rclone_mgr.select_active_remote(bucket) is not None:
                with tempfile.NamedTemporaryFile(prefix="ubagen_rclone_dl_", delete=False) as tmp_f:
                    tmp_path = Path(tmp_f.name)
                try:
                    rclone_mgr.download_to_path(bucket, clean_object, tmp_path)
                    data = tmp_path.read_bytes()
                    return None, data
                finally:
                    with suppress(Exception):
                        tmp_path.unlink(missing_ok=True)

        try:
            from google.cloud import storage as gcs_sdk  # type: ignore

            client = gcs_sdk.Client()
            blob = client.bucket(bucket).blob(clean_object)
            if not blob.exists():
                return None, None
            return None, blob.download_as_bytes(timeout=300)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning("GCS SDK download error: %s", exc)
            return None, None

        token = get_gcp_access_token()
        if not token:
            return None, None
        encoded_name = urllib.parse.quote(clean_object, safe="")
        dl_url = (
            f"https://storage.googleapis.com/storage/v1/b/"
            f"{urllib.parse.quote(bucket, safe='')}/o/{encoded_name}?alt=media"
        )
        req = urllib.request.Request(dl_url, headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                if resp.status == 200:
                    return None, resp.read()
        except Exception as exc:
            logger.warning("GCS REST download error: %s", exc)
        return None, None

    def upload_staging_bytes(
        self,
        data: bytes,
        *,
        user_id: str,
        job_id: str,
        kind: str,
        original_filename: str,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """
        Upload user-supplied attachment bytes (e.g. `.ass`/`.srt` subtitle or intro clip from `/encodeal`)
        to a unique, user- and job-scoped staging object in GCS (`staging/{user_id}/{job_id}/...`)
        and verify its existence and byte size.
        """
        if not data or len(data) <= 0:
            raise ValueError("Staging dosyası boş (0 byte); yükleme reddedildi.")

        safe_user = re.sub(r"[^A-Za-z0-9._-]+", "_", (user_id or "").strip())[:64]
        safe_job = re.sub(r"[^A-Za-z0-9._-]+", "_", (job_id or "").strip())[:64]
        safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "_", (kind or "input").strip())[:24]
        raw_base = Path(str(original_filename or "file").replace("\\", "/")).name
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_base).strip("._")[:80] or "attachment"
        if not safe_user or not safe_job:
            raise ValueError("Staging yüklemesi için geçerli user_id ve job_id gereklidir.")

        object_name = f"staging/{safe_user}/{safe_job}/{safe_kind}_{uuid.uuid4().hex[:8]}_{safe_name}"

        with tempfile.NamedTemporaryFile(prefix=f"ubagen_stage_{safe_kind}_", delete=False) as tmp_f:
            tmp_f.write(data)
            tmp_path = Path(tmp_f.name)

        try:
            res = self.upload_file(
                tmp_path,
                object_name,
                content_type=content_type,
                metadata={
                    "user_id": safe_user,
                    "job_id": safe_job,
                    "staging_kind": safe_kind,
                    "original_filename": safe_name,
                },
            )
            if not res.get("verified") or int(res.get("size_bytes") or 0) != len(data):
                raise RuntimeError("GCS staging nesnesi doğrulanamadı.")
            return res
        finally:
            with suppress(Exception):
                tmp_path.unlink(missing_ok=True)

    def download_to_path(self, bucket: str, object_name: str, dest_path: Path) -> Path:
        """
        Download a GCS object (`gs://{bucket}/{object_name}`) into `dest_path` for worker processing
        and verify that the downloaded file is non-empty.
        """
        clean_bucket = (bucket or self.bucket_name or "").strip()
        clean_object = (object_name or "").lstrip("/")
        if not clean_bucket or not clean_object:
            raise ValueError("GCS indirme işlemi için bucket ve object_name zorunludur.")

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        local_mirror, raw_bytes = self.open_download_stream_or_path(clean_bucket, clean_object)
        if local_mirror is not None and local_mirror.is_file():
            shutil.copy2(local_mirror, dest_path)
        elif raw_bytes is not None:
            dest_path.write_bytes(raw_bytes)
        else:
            raise FileNotFoundError(
                f"GCS staging/kaynak nesnesi okunamadı veya bulunamadı: gs://{clean_bucket}/{clean_object}"
            )

        if not dest_path.is_file() or dest_path.stat().st_size <= 0:
            raise RuntimeError(f"GCS nesnesi indirildi ancak boş: gs://{clean_bucket}/{clean_object}")
        return dest_path

    def cleanup_job_staging_objects(self, job: EncodeJob) -> int:
        """
        Delete all per-job staging objects (`staging/{user_id}/{job_id}/...`) from GCS.
        Called automatically when a job completes, fails, or expires.
        """
        bucket = (job.gcs_bucket or self.bucket_name or "").strip()
        if not bucket:
            return 0

        candidates: set[str] = set()
        for obj in job.staged_gcs_objects or []:
            if obj and str(obj).strip():
                candidates.add(str(obj).strip().lstrip("/"))

        for src in (job.subtitle_source, job.intro_source):
            if src and str(src).strip().startswith("gs://"):
                without_scheme = str(src).strip()[5:]
                if "/" in without_scheme:
                    src_bucket, src_obj = without_scheme.split("/", 1)
                    if src_bucket == bucket and src_obj.startswith("staging/"):
                        candidates.add(src_obj.lstrip("/"))

        deleted = 0
        for obj_name in sorted(candidates):
            with suppress(Exception):
                if self.delete_object(bucket, obj_name):
                    deleted += 1
        return deleted



# ------------------------------------------------------------------------------
# Persistent Job Store (Firestore Primary + SQLite WAL Persistent Fallback)
# ------------------------------------------------------------------------------
class PersistentJobStore:
    """
    Stores and queries `EncodeJob` records outside of Cloud Run instance memory.
    Uses Google Cloud Firestore when enabled/configured, and a multi-process-safe
    SQLite WAL database on disk (`ubagen_jobs.sqlite3`) so restarts never lose state.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        gcs_storage: Optional[GCSOutputStorage] = None,
        use_firestore: Optional[bool] = None,
    ) -> None:
        env_db = os.getenv("UBAGEN_JOBS_DB_PATH", "").strip()
        env_storage_dir = os.getenv("UBAGEN_STORAGE_DIR", "").strip() or "."
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        elif env_db:
            self.db_path = Path(env_db).resolve()
        else:
            base = Path(env_storage_dir).resolve()
            self.db_path = (base / "ubagen_jobs.sqlite3").resolve()

        self._lock = threading.Lock()
        self._ensure_sqlite_db()

        self.gcs = gcs_storage or GCSOutputStorage()
        self.firestore_collection = os.getenv("UBAGEN_FIRESTORE_COLLECTION", "ubagen_encode_jobs").strip() or "ubagen_encode_jobs"
        self.firestore_database = os.getenv("UBAGEN_FIRESTORE_DATABASE", "(default)").strip() or "(default)"
        self.firestore_project = (
            os.getenv("UBAGEN_FIRESTORE_PROJECT", "").strip()
            or os.getenv("UBAGEN_GCP_PROJECT_ID", "").strip()
            or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            or os.getenv("GCLOUD_PROJECT", "").strip()
        )
        self._use_firestore = (
            use_firestore
            if use_firestore is not None
            else (os.getenv("UBAGEN_USE_FIRESTORE", "").strip().lower() in ("1", "true", "yes"))
        )
        self._firestore_client: Any = None
        self._firestore_init_error: Optional[str] = None
        if self._use_firestore:
            self._init_firestore_client()

    def _init_firestore_client(self) -> None:
        if not self.firestore_project:
            self._firestore_init_error = (
                "Firestore etkin (`UBAGEN_USE_FIRESTORE=true`) ancak `UBAGEN_GCP_PROJECT_ID` tanımlı değil."
            )
            logger.error(self._firestore_init_error)
            self._firestore_client = None
            return
        try:
            from google.cloud import firestore  # type: ignore

            kwargs: dict[str, Any] = {"project": self.firestore_project}
            if self.firestore_database and self.firestore_database != "(default)":
                kwargs["database"] = self.firestore_database
            self._firestore_client = firestore.Client(**kwargs)
            self._firestore_init_error = None
        except Exception as exc:
            self._firestore_init_error = f"Firestore istemcisi başlatılamadı: {type(exc).__name__}: {exc}"
            logger.error(self._firestore_init_error)
            self._firestore_client = None

    def _require_firestore_client(self) -> Any:
        """
        Ensure Firestore client is available when `use_firestore=True`.
        Never silently falls back to local SQLite when Firestore is configured,
        preventing split-brain state where the Cloud Run Job worker cannot find jobs.
        """
        if self._firestore_client is None:
            self._init_firestore_client()
        if self._firestore_client is None:
            msg = (
                self._firestore_init_error
                or "Firestore bağlantısı kurulamadı. Yerel SQLite'a sessizce geçilmedi."
            )
            raise FirestoreUnavailableError(msg)
        return self._firestore_client


    def _ensure_sqlite_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect_sqlite() as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS encode_jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL,
                        deleted_from_gcs INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON encode_jobs(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON encode_jobs(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_expires ON encode_jobs(expires_at, deleted_from_gcs);")
        except Exception as exc:
            fallback_dir = Path(tempfile.gettempdir()) / "ubagen_storage"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            self.db_path = (fallback_dir / "ubagen_jobs.sqlite3").resolve()
            logger.warning("Primary jobs DB path not writable (%s); falling back to %s", exc, self.db_path)
            with self._connect_sqlite() as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS encode_jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        expires_at REAL,
                        deleted_from_gcs INTEGER NOT NULL DEFAULT 0,
                        payload_json TEXT NOT NULL
                    )
                    """
                )

    def _connect_sqlite(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_backend_status(self) -> dict[str, Any]:
        """Return diagnostic information about the active job persistence backend."""
        if self._use_firestore and self._firestore_client is not None:
            return {
                "backend_type": "google_cloud_firestore",
                "in_memory_only": False,
                "persistent_across_instances": True,
                "firestore_project": self.firestore_project,
                "firestore_database": self.firestore_database,
                "firestore_collection": self.firestore_collection,
                "sqlite_mirror_path": str(self.db_path),
            }
        return {
            "backend_type": "sqlite_wal_persistent",
            "in_memory_only": False,
            "persistent_across_restarts": True,
            "persistent_across_instances": bool(self._use_firestore),
            "firestore_configured": bool(self._use_firestore and self.firestore_project),
            "firestore_collection": self.firestore_collection,
            "sqlite_db_path": str(self.db_path),
            "setup_note": (
                "Cloud Run çoklu örnek ve Cloud Run Job paylaşımı için Firestore önerilir: "
                "UBAGEN_USE_FIRESTORE=true ve UBAGEN_GCP_PROJECT_ID değişkenlerini ayarlayın."
                if not self._use_firestore
                else "Firestore etkin."
            ),
        }

    def save_job(self, job: EncodeJob) -> EncodeJob:
        """
        Persist `job` to the authoritative store:
        - When `use_firestore=True`: writes to Google Cloud Firestore and raises `FirestoreUnavailableError`
          on failure (never silently falling back to local SQLite).
        - When `use_firestore=False`: writes to local transactional SQLite WAL database.
        """
        job.updated_at = time.time()
        payload_dict = job.to_dict()
        payload_json = json.dumps(payload_dict, ensure_ascii=False)

        if self._use_firestore:
            client = self._require_firestore_client()
            try:
                client.collection(self.firestore_collection).document(job.job_id).set(payload_dict)
            except Exception as exc:
                err_msg = (
                    f"Firestore iş kaydı yazılamadı (`{self.firestore_collection}/{job.job_id}`): "
                    f"{type(exc).__name__}"
                )
                logger.error("%s: %s", err_msg, exc)
                raise FirestoreUnavailableError(err_msg) from exc

        with self._lock:
            with self._connect_sqlite() as conn:
                conn.execute(
                    """
                    INSERT INTO encode_jobs (
                        job_id, user_id, status, stage, created_at, updated_at,
                        expires_at, deleted_from_gcs, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        user_id=excluded.user_id,
                        status=excluded.status,
                        stage=excluded.stage,
                        updated_at=excluded.updated_at,
                        expires_at=excluded.expires_at,
                        deleted_from_gcs=excluded.deleted_from_gcs,
                        payload_json=excluded.payload_json
                    """,
                    (
                        job.job_id,
                        job.user_id,
                        job.status,
                        job.stage,
                        job.created_at,
                        job.updated_at,
                        job.expires_at,
                        1 if job.deleted_from_gcs else 0,
                        payload_json,
                    ),
                )

        return job

    def create_job(
        self,
        user_id: str,
        magnet: str,
        codec: str = "h264",
        subtitle_lang: str = "tur",
        subtitle_source: Optional[str] = None,
        intro_source: Optional[str] = None,
        user_name: Optional[str] = None,
        guild_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        message_id: Optional[str] = None,
        application_id: Optional[str] = None,
        interaction_token: Optional[str] = None,
        retention_seconds: Optional[int] = None,
        staged_gcs_objects: Optional[list[str]] = None,
        command_source: str = "encode",
    ) -> EncodeJob:
        """Validate inputs and create a new persistent `EncodeJob` in `queued` state."""
        clean_user = (user_id or "").strip()
        if not clean_user:
            raise ValueError("Kullanıcı kimliği (user_id) zorunludur.")

        ok_codec, canonical_codec, video_encoder = normalize_video_codec(codec)
        if not ok_codec:
            raise ValueError(video_encoder)

        now = time.time()
        job_id = f"job_{int(now)}_{uuid.uuid4().hex[:8]}"
        ret_sec = retention_seconds if retention_seconds is not None else get_configured_retention_seconds()
        clean_lang = (subtitle_lang or "tur").strip().lower()
        idem_key = compute_job_idempotency_key(
            user_id=clean_user,
            magnet=magnet.strip(),
            codec=canonical_codec,
            subtitle_lang=clean_lang,
            subtitle_source=subtitle_source,
            intro_source=intro_source,
        )

        job = EncodeJob(
            job_id=job_id,
            user_id=clean_user,
            user_name=user_name,
            guild_id=guild_id,
            channel_id=channel_id,
            message_id=message_id,
            application_id=application_id,
            interaction_token=interaction_token,
            magnet=magnet.strip(),
            codec=canonical_codec,
            video_encoder=video_encoder,
            subtitle_lang=clean_lang,
            subtitle_source=subtitle_source,
            intro_source=intro_source,
            has_intro=bool(intro_source and str(intro_source).strip()),
            stage="queued",
            status="queued",
            percent=0.0,
            processed_frames=0,
            total_frames=None,
            fps=0.0,
            eta_seconds=None,
            error=None,
            created_at=now,
            updated_at=now,
            retention_seconds=ret_sec,
            staged_gcs_objects=list(staged_gcs_objects or []),
            command_source=command_source,
            idempotency_key=idem_key,
        )
        return self.save_job(job)

    def find_active_duplicate_job(
        self,
        *,
        user_id: str,
        magnet: str,
        codec: str = "h264",
        subtitle_lang: str = "tur",
        subtitle_source: Optional[str] = None,
        intro_source: Optional[str] = None,
        max_age_seconds: float = 3600.0,
    ) -> Optional[EncodeJob]:
        """
        Check if `user_id` already has an active (`queued` or `running`) encode job
        with the exact same parameters so the same job is not launched multiple times.
        """
        clean_user = (user_id or "").strip()
        if not clean_user:
            return None
        target_key = compute_job_idempotency_key(
            user_id=clean_user,
            magnet=magnet,
            codec=codec,
            subtitle_lang=subtitle_lang,
            subtitle_source=subtitle_source,
            intro_source=intro_source,
        )
        now = time.time()
        user_jobs = self.list_jobs(user_id=clean_user, limit=30)
        for existing in user_jobs:
            if existing.status not in ("queued", "running"):
                continue
            if (now - float(existing.updated_at or existing.created_at or 0.0)) > max_age_seconds:
                continue
            existing_key = existing.idempotency_key or compute_job_idempotency_key(
                user_id=existing.user_id,
                magnet=existing.magnet,
                codec=existing.codec,
                subtitle_lang=existing.subtitle_lang,
                subtitle_source=existing.subtitle_source,
                intro_source=existing.intro_source,
            )
            if existing_key == target_key:
                return existing
        return None

    def acquire_dispatch_claim(
        self,
        job_id: str,
        *,
        allow_retry: bool = False,
    ) -> tuple[bool, Optional[EncodeJob], str]:
        """
        Prevent the same `job_id` from being dispatched multiple times concurrently.
        Returns `(can_dispatch, job, reason)`.
        """
        job = self.get_job(job_id)
        if job is None:
            return False, None, "Job bulunamadı."
        if job.status in ("completed", "cancelled", "expired"):
            return False, job, f"İş zaten sonlanmış durumda (`{job.status}`); tekrar başlatılamaz."
        if job.worker_execution_name and job.status in ("queued", "running") and not allow_retry:
            return (
                False,
                job,
                f"İş (`{job.job_id}`) zaten aktif bir worker üzerinde çalışıyor (`{job.worker_execution_name}`).",
            )
        if allow_retry and job.status == "failed":
            if int(job.retry_count or 0) >= int(job.max_retries or 1):
                return False, job, f"Maksimum yeniden deneme sınırına ({job.max_retries}) ulaşıldı."
            job.retry_count = int(job.retry_count or 0) + 1
            job.status = "queued"
            job.stage = "queued"
            job.percent = 0.0
            job.processed_frames = 0
            job.fps = 0.0
            job.eta_seconds = None
            job.error = None
            job.worker_execution_name = None
            job = self.save_job(job)
        return True, job, "OK"

    def prepare_job_for_worker_execution(
        self,
        job_id: str,
        *,
        is_task_retry: bool = False,
    ) -> tuple[bool, EncodeJob, str]:
        """
        Validate and prepare `job_id` before starting heavy torrent/FFmpeg execution:
        - Raises `JobNotFoundError` if `job_id` does not exist.
        - Refuses execution if the job is already `completed` or `cancelled`.
        - Safely resets transient progress counters if recovering from a previous attempt (`retry_count += 1`).
        """
        clean_id = (job_id or "").strip()
        if not clean_id:
            raise JobNotFoundError("Job ID boş olamaz.")
        job = self.get_job(clean_id)
        if job is None:
            raise JobNotFoundError(f"Encode işi bulunamadı: {clean_id}")

        if job.status == "completed" and job.gcs_upload_verified:
            return False, job, "already_completed"
        if job.status == "cancelled" or job.stage == "cancelled":
            return False, job, "cancelled"
        if job.status == "expired" or job.stage == "expired":
            return False, job, "expired"

        # If recovering from a previous failed attempt or Cloud Run task retry, increment retry_count cleanly
        if job.status == "failed" or is_task_retry or (job.started_at is not None and job.processed_frames > 0):
            job.retry_count = int(job.retry_count or 0) + 1
            job.percent = 0.0
            job.processed_frames = 0
            job.fps = 0.0
            job.eta_seconds = None
            job.error = None

        job.started_at = time.time()
        job.status = "running"
        job.stage = "downloading"
        job = self.save_job(job)
        return True, job, "ready"

    def cancel_job(
        self,
        job_id: str,
        *,
        requester_user_id: Optional[str] = None,
        is_admin: bool = False,
        reason: Optional[str] = None,
    ) -> tuple[bool, Optional[EncodeJob], str]:
        """
        Cancel a queued or running job, verifying user ownership (unless `is_admin=True`),
        and clean up any temporary GCS staging objects.
        """
        job = self.get_job(job_id)
        if job is None:
            return False, None, "Encode işi bulunamadı."
        if not is_admin:
            clean_req = (requester_user_id or "").strip()
            if not clean_req or not hmac.compare_digest(clean_req.encode("utf-8"), job.user_id.encode("utf-8")):
                return False, job, "Bu encode işini yalnızca işi başlatan kullanıcı veya yönetici iptal edebilir."
        if job.status in ("completed", "expired"):
            return False, job, f"Tamamlanmış veya süresi dolmuş iş (`{job.status}`) iptal edilemez."
        if job.status == "cancelled":
            return True, job, "İş zaten iptal edilmiş."

        now = time.time()
        job.stage = "cancelled"
        job.status = "cancelled"
        job.eta_seconds = 0
        job.download_url = None
        job.gcs_upload_verified = False
        job.cancelled_at = now
        job.cancelled_by = (requester_user_id or "admin").strip()
        job.error = (reason or "İşlem kullanıcı tarafından iptal edildi.").strip()
        with suppress(Exception):
            self.gcs.cleanup_job_staging_objects(job)
        saved = self.save_job(job)
        return True, saved, "İşlem iptal edildi."

    def is_job_cancelled(self, job_id: str) -> bool:
        """Return True if `job_id` has been marked as `cancelled`."""
        with suppress(Exception):
            job = self.get_job(job_id)
            if job is not None and (job.status == "cancelled" or job.stage == "cancelled"):
                return True
        return False

    def get_job(self, job_id: str) -> Optional[EncodeJob]:
        """
        Load a job by `job_id` from persistent storage.
        When `use_firestore=True`, reads exclusively from Firestore (raising `FirestoreUnavailableError`
        on connection/auth failure and returning `None` if the document does not exist in Firestore).
        """
        clean_id = (job_id or "").strip()
        if not clean_id:
            return None

        if self._use_firestore:
            client = self._require_firestore_client()
            try:
                doc = client.collection(self.firestore_collection).document(clean_id).get()
                if not doc.exists:
                    logger.warning(
                        "Job document '%s' not found in Firestore collection '%s' (project=%s, db=%s).",
                        clean_id,
                        self.firestore_collection,
                        self.firestore_project,
                        self.firestore_database,
                    )
                    return None
                data = doc.to_dict()
                if isinstance(data, dict):
                    return EncodeJob.from_dict(data)
                return None
            except FirestoreUnavailableError:
                raise
            except Exception as exc:
                err_msg = (
                    f"Firestore iş kaydı okunamadı (`{self.firestore_collection}/{clean_id}`): "
                    f"{type(exc).__name__}"
                )
                logger.error("%s: %s", err_msg, exc)
                raise FirestoreUnavailableError(err_msg) from exc

        with self._connect_sqlite() as conn:
            row = conn.execute(
                "SELECT payload_json FROM encode_jobs WHERE job_id = ?",
                (clean_id,),
            ).fetchone()
            if not row:
                return None
            return EncodeJob.from_dict(json.loads(row["payload_json"]))


    def update_job_progress(
        self,
        job_id: str,
        *,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        percent: Optional[float] = None,
        processed_frames: Optional[int] = None,
        total_frames: Optional[int] = None,
        fps: Optional[float] = None,
        eta_seconds: Optional[int] = None,
        subtitle_format: Optional[str] = None,
        error: Optional[str] = None,
        worker_execution_name: Optional[str] = None,
    ) -> Optional[EncodeJob]:
        """Atomically update job stage, frame telemetry, FPS, ETA, or error state."""
        job = self.get_job(job_id)
        if job is None:
            return None

        now = time.time()
        if stage is not None:
            job.stage = stage
        if status is not None:
            job.status = status
        elif stage in ("downloading", "preparing_subtitles", "processing_intro", "encoding", "uploading"):
            job.status = "running"
            if job.started_at is None:
                job.started_at = now
        elif stage == "failed":
            job.status = "failed"

        if percent is not None:
            job.percent = max(0.0, min(100.0, float(percent)))
        if processed_frames is not None:
            job.processed_frames = max(0, int(processed_frames))
        if total_frames is not None:
            job.total_frames = max(0, int(total_frames))
        if fps is not None:
            job.fps = max(0.0, float(fps))
        if eta_seconds is not None:
            job.eta_seconds = max(0, int(eta_seconds))
        elif stage in ("completed", "failed", "expired"):
            job.eta_seconds = 0
        if subtitle_format is not None:
            job.subtitle_format = subtitle_format
        if error is not None:
            job.error = error
        if worker_execution_name is not None:
            job.worker_execution_name = worker_execution_name

        return self.save_job(job)

    def build_download_url(self, job: EncodeJob, public_base_url: Optional[str] = None) -> str:
        """
        Build the user-authorized download URL for a completed, GCS-verified job.
        """
        base = (
            public_base_url
            or os.getenv("UBAGEN_PUBLIC_BASE_URL", "").strip()
            or os.getenv("APP_URL", "").strip()
        ).rstrip("/")
        query = urllib.parse.urlencode({
            "user_id": job.user_id,
            "token": job.download_token,
        })
        rel = f"/api/jobs/{job.job_id}/download?{query}"
        return f"{base}{rel}" if base else rel

    def mark_job_completed(
        self,
        job_id: str,
        *,
        gcs_bucket: str,
        gcs_object_name: str,
        output_filename: str,
        output_size_bytes: int,
        gcs_upload_verified: bool,
        completed_at: Optional[float] = None,
        public_base_url: Optional[str] = None,
    ) -> EncodeJob:
        """
        Mark a job as completed ONLY if `gcs_upload_verified` is True and `output_size_bytes > 0`.
        Sets `expires_at = completed_at + retention_seconds` (default 7200s = 2 hours).
        If GCS upload was not verified, marks the job as `failed` and refuses to generate a download URL.
        """
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")

        if not gcs_upload_verified or output_size_bytes <= 0 or not gcs_bucket or not gcs_object_name:
            job.stage = "failed"
            job.status = "failed"
            job.gcs_upload_verified = False
            job.download_url = None
            job.error = (
                "GCS yükleme doğrulaması başarısız oldu; çıktı nesnesi doğrulanmadan indirme bağlantısı üretilmedi."
            )
            return self.save_job(job)

        fin_time = completed_at if completed_at is not None else time.time()
        retention = int(job.retention_seconds or DEFAULT_RETENTION_SECONDS)
        job.stage = "completed"
        job.status = "completed"
        job.percent = 100.0
        if job.total_frames is not None and job.processed_frames < job.total_frames:
            job.processed_frames = job.total_frames
        job.eta_seconds = 0
        job.error = None
        job.completed_at = fin_time
        job.expires_at = fin_time + retention
        job.gcs_bucket = gcs_bucket
        job.gcs_object_name = gcs_object_name
        job.gcs_uri = f"gs://{gcs_bucket}/{gcs_object_name.lstrip('/')}"
        job.gcs_upload_verified = True
        job.output_filename = output_filename
        job.output_size_bytes = int(output_size_bytes)
        job.deleted_from_gcs = False
        job.download_url = self.build_download_url(job, public_base_url=public_base_url)
        with suppress(Exception):
            self.gcs.cleanup_job_staging_objects(job)
        return self.save_job(job)

    def mark_job_failed(self, job_id: str, error_message: str) -> Optional[EncodeJob]:
        """Transition a job to `failed` state, clean up any GCS staging files, and clear download link."""
        job = self.get_job(job_id)
        if job is None:
            return None
        job.stage = "failed"
        job.status = "failed"
        job.eta_seconds = 0
        job.gcs_upload_verified = False
        job.download_url = None
        job.error = error_message
        with suppress(Exception):
            self.gcs.cleanup_job_staging_objects(job)
        return self.save_job(job)


    def _list_all_firestore_jobs(self, max_docs: int = 500) -> list[EncodeJob]:
        client = self._require_firestore_client()
        try:
            docs = client.collection(self.firestore_collection).limit(max_docs).stream()
            items: list[EncodeJob] = []
            for doc in docs:
                data = doc.to_dict()
                if isinstance(data, dict) and data.get("job_id"):
                    items.append(EncodeJob.from_dict(data))
            items.sort(key=lambda j: float(j.created_at or 0.0), reverse=True)
            return items
        except FirestoreUnavailableError:
            raise
        except Exception as exc:
            err_msg = f"Firestore iş listesi okunamadı (`{self.firestore_collection}`): {type(exc).__name__}"
            logger.error("%s: %s", err_msg, exc)
            raise FirestoreUnavailableError(err_msg) from exc

    def list_jobs(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[EncodeJob]:
        """List jobs ordered newest-first."""
        safe_limit = max(1, min(int(limit or 50), 200))
        if self._use_firestore:
            all_jobs = self._list_all_firestore_jobs(max_docs=max(200, safe_limit * 4))
            filtered: list[EncodeJob] = []
            for j in all_jobs:
                if user_id and j.user_id != user_id.strip():
                    continue
                if status and j.status != status.strip():
                    continue
                filtered.append(j)
                if len(filtered) >= safe_limit:
                    break
            return filtered

        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id.strip())
        if status:
            clauses.append("status = ?")
            params.append(status.strip())

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT payload_json FROM encode_jobs {where_sql} ORDER BY created_at DESC LIMIT ?"
        params.append(safe_limit)

        with self._connect_sqlite() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [EncodeJob.from_dict(json.loads(r["payload_json"])) for r in rows]

    def cleanup_expired_jobs(self, now: Optional[float] = None) -> dict[str, Any]:
        """
        Server-side 7200-second retention enforcement:
        Finds all jobs whose `expires_at <= now`, deletes their video object from Google Cloud Storage,
        revokes download access (`download_url = None`), and updates their status to `expired`.
        """
        cur = now if now is not None else time.time()
        candidates: list[EncodeJob] = []
        if self._use_firestore:
            for j in self._list_all_firestore_jobs(max_docs=500):
                if j.expires_at is not None and j.expires_at <= cur and not j.deleted_from_gcs:
                    candidates.append(j)
        else:
            with self._connect_sqlite() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM encode_jobs
                    WHERE expires_at IS NOT NULL AND expires_at <= ? AND deleted_from_gcs = 0
                    """,
                    (cur,),
                ).fetchall()
            candidates = [EncodeJob.from_dict(json.loads(r["payload_json"])) for r in rows]

        expired_count = 0
        gcs_deleted_count = 0
        purged_job_ids: list[str] = []

        for job in candidates:
            deleted_ok = True
            if job.gcs_bucket and job.gcs_object_name:
                deleted_ok = self.gcs.delete_object(job.gcs_bucket, job.gcs_object_name)
                if deleted_ok:
                    gcs_deleted_count += 1
            with suppress(Exception):
                self.gcs.cleanup_job_staging_objects(job)

            job.stage = "expired"
            job.status = "expired"
            job.download_url = None
            job.deleted_from_gcs = bool(deleted_ok)
            job.deleted_at = cur
            self.save_job(job)
            expired_count += 1
            purged_job_ids.append(job.job_id)

        rclone_pending_cleanup: Optional[dict[str, Any]] = None
        if self.gcs.local_mirror_dir is None:
            with suppress(Exception):
                from rclone_manager import get_rclone_manager

                rclone_pending_cleanup = get_rclone_manager().process_pending_deletions(now=cur)
                gcs_deleted_count += int(rclone_pending_cleanup.get("deleted") or 0)

        return {
            "checked_at": _iso_utc(cur),
            "expired_jobs_processed": expired_count,
            "gcs_objects_deleted": gcs_deleted_count,
            "job_ids": purged_job_ids,
            "rclone_pending_cleanup": rclone_pending_cleanup,
        }

    def recover_stale_running_jobs(
        self,
        now: Optional[float] = None,
        stale_timeout_seconds: float = 900.0,
    ) -> list[EncodeJob]:
        """
        Detect jobs stuck in `running` state because a worker crashed (e.g. OOM / SIGKILL)
        and mark them as `failed` with a clear diagnostic error.
        """
        cur = now if now is not None else time.time()
        cutoff = cur - float(stale_timeout_seconds)
        candidates: list[EncodeJob] = []
        if self._use_firestore:
            for j in self._list_all_firestore_jobs(max_docs=500):
                if j.status == "running" and float(j.updated_at or 0.0) <= cutoff:
                    candidates.append(j)
        else:
            with self._connect_sqlite() as conn:
                rows = conn.execute(
                    """
                    SELECT payload_json FROM encode_jobs
                    WHERE status = 'running' AND updated_at <= ?
                    """,
                    (cutoff,),
                ).fetchall()
            candidates = [EncodeJob.from_dict(json.loads(r["payload_json"])) for r in rows]

        recovered: list[EncodeJob] = []
        for job in candidates:
            job.stage = "failed"
            job.status = "failed"
            job.eta_seconds = 0
            job.download_url = None
            job.gcs_upload_verified = False
            job.error = (
                f"Worker beklenmedik şekilde sonlandı veya zaman aşımına uğradı "
                f"({int(stale_timeout_seconds)}sn boyunca ilerleme sinyali alınamadı)."
            )
            with suppress(Exception):
                self.gcs.cleanup_job_staging_objects(job)
            self.save_job(job)
            recovered.append(job)
        return recovered


_DEFAULT_JOB_STORE: Optional[PersistentJobStore] = None


def get_job_store() -> PersistentJobStore:
    """Return singleton `PersistentJobStore` instance."""
    global _DEFAULT_JOB_STORE
    if _DEFAULT_JOB_STORE is None:
        _DEFAULT_JOB_STORE = PersistentJobStore()
    return _DEFAULT_JOB_STORE


def reset_job_store_for_tests(store: Optional[PersistentJobStore] = None) -> None:
    """Override or reset singleton `PersistentJobStore` during automated tests."""
    global _DEFAULT_JOB_STORE
    _DEFAULT_JOB_STORE = store
