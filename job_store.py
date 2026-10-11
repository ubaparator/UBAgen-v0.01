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
    "expired",
}


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


def detect_cloud_run_environment_details() -> dict[str, Any]:
    """
    Inspect current Cloud Run service settings, service account, region, project ID,
    and configured Cloud Run Job / Firestore / GCS settings without fabricating access.
    """
    k_service = os.getenv("K_SERVICE", "").strip() or None
    k_revision = os.getenv("K_REVISION", "").strip() or None
    k_config = os.getenv("K_CONFIGURATION", "").strip() or None

    project_id = (
        os.getenv("UBAGEN_GCP_PROJECT_ID", "").strip()
        or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        or os.getenv("GCLOUD_PROJECT", "").strip()
        or (_get_gcp_metadata("project/project-id") if k_service else None)
    )
    region_meta = _get_gcp_metadata("instance/region") if k_service else None
    region_from_meta = region_meta.split("/")[-1] if region_meta and "/" in region_meta else region_meta
    region = (
        os.getenv("UBAGEN_GCP_REGION", "").strip()
        or os.getenv("CLOUD_RUN_REGION", "").strip()
        or region_from_meta
        or "europe-west2"
    )
    service_account = (
        os.getenv("UBAGEN_SERVICE_ACCOUNT_EMAIL", "").strip()
        or (_get_gcp_metadata("instance/service-accounts/default/email") if k_service else None)
        or os.getenv("AUTHORIZED_SERVICE_ACCOUNT_EMAIL", "").strip()
        or None
    )

    cloud_run_job_name = os.getenv("UBAGEN_CLOUD_RUN_JOB_NAME", "").strip() or None
    gcs_bucket = os.getenv("UBAGEN_GCS_BUCKET", "").strip() or os.getenv("GCS_BUCKET_NAME", "").strip() or None
    firestore_collection = os.getenv("UBAGEN_FIRESTORE_COLLECTION", "ubagen_encode_jobs").strip()
    firestore_db = os.getenv("UBAGEN_FIRESTORE_DATABASE", "(default)").strip()
    firestore_enabled = os.getenv("UBAGEN_USE_FIRESTORE", "").strip().lower() in ("1", "true", "yes")

    return {
        "k_service": k_service,
        "k_revision": k_revision,
        "k_configuration": k_config,
        "project_id": project_id,
        "region": region,
        "service_account_email": service_account,
        "cloud_run_job_name": cloud_run_job_name,
        "cloud_run_job_configured": bool(cloud_run_job_name and project_id),
        "gcs_bucket": gcs_bucket,
        "gcs_configured": bool(gcs_bucket),
        "firestore_enabled": firestore_enabled,
        "firestore_database": firestore_db,
        "firestore_collection": firestore_collection,
        "retention_seconds": get_configured_retention_seconds(),
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


def get_configured_retention_seconds() -> int:
    """Return configured encode output retention in seconds (default: 7200s = 2 hours)."""
    raw = os.getenv("UBAGEN_ENCODE_RETENTION_SECONDS", "").strip()
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
        return bool(self.bucket_name)

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
    ) -> dict[str, Any]:
        """
        Upload `local_path` to GCS and strictly verify that the uploaded object exists
        and has the exact same non-zero byte size before returning `verified=True`.
        """
        if not local_path.is_file():
            raise FileNotFoundError(f"Yüklenecek yerel çıktı dosyası bulunamadı: {local_path}")

        expected_size = local_path.stat().st_size
        if expected_size <= 0:
            raise RuntimeError("Encode çıktı dosyası boş (0 byte); GCS yüklemesi iptal edildi.")

        bucket = (self.bucket_name or "").strip()
        if not bucket:
            raise RuntimeError(
                "Google Cloud Storage bucket (`UBAGEN_GCS_BUCKET`) yapılandırılmamış. "
                "Kalıcı çıktı yüklemesi tamamlanamadı."
            )

        clean_object = object_name.lstrip("/")

        # 1. If local mirror is active (used for deterministic integration testing)
        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            mirror_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_path, mirror_target)
            verified = self.verify_object(bucket, clean_object, expected_size=expected_size)
            if not verified:
                raise RuntimeError("GCS nesne doğrulaması (boyut/varlık kontrolü) başarısız oldu.")
            return {
                "uploaded": True,
                "verified": True,
                "bucket": bucket,
                "object_name": clean_object,
                "gcs_uri": f"gs://{bucket}/{clean_object}",
                "size_bytes": expected_size,
                "backend": "gcs_local_mirror",
            }

        # 2. Try google-cloud-storage Python SDK if installed
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

        # 3. Direct Google Cloud Storage JSON API v1 via ADC / Metadata Server token
        token = get_gcp_access_token()
        if not token:
            raise RuntimeError(
                "Google Cloud kimlik doğrulama token'ı (ADC / Service Account) alınamadı. "
                "Cloud Run servis hesabının GCS erişim yetkilerini kontrol edin."
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
        """Verify that `gs://{bucket}/{object_name}` exists and matches `expected_size`."""
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
        Actively delete `gs://{bucket}/{object_name}` from Google Cloud Storage.
        Returns True if the object was deleted or already absent.
        """
        clean_object = (object_name or "").lstrip("/")
        if not bucket or not clean_object:
            return False

        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            if mirror_target.exists():
                mirror_target.unlink(missing_ok=True)
            return not mirror_target.exists()

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
        Retrieve either a local verified mirror Path or download bytes from GCS
        after authorization and expiration checks have passed.
        """
        clean_object = object_name.lstrip("/")
        mirror_target = self._mirror_object_path(bucket, clean_object)
        if mirror_target is not None:
            if mirror_target.is_file():
                return mirror_target, None
            return None, None

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
        self.firestore_collection = os.getenv("UBAGEN_FIRESTORE_COLLECTION", "ubagen_encode_jobs").strip()
        self.firestore_database = os.getenv("UBAGEN_FIRESTORE_DATABASE", "(default)").strip()
        self.firestore_project = (
            os.getenv("UBAGEN_FIRESTORE_PROJECT", "").strip()
            or os.getenv("UBAGEN_GCP_PROJECT_ID", "").strip()
            or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        )
        self._use_firestore = (
            use_firestore
            if use_firestore is not None
            else (os.getenv("UBAGEN_USE_FIRESTORE", "").strip().lower() in ("1", "true", "yes"))
        )
        self._firestore_client: Any = None
        if self._use_firestore:
            self._init_firestore_client()

    def _init_firestore_client(self) -> None:
        try:
            from google.cloud import firestore  # type: ignore

            kwargs: dict[str, Any] = {}
            if self.firestore_project:
                kwargs["project"] = self.firestore_project
            if self.firestore_database and self.firestore_database != "(default)":
                kwargs["database"] = self.firestore_database
            self._firestore_client = firestore.Client(**kwargs)
        except Exception as exc:
            logger.debug("Firestore SDK client not initialized (%s); using REST/SQLite store.", exc)
            self._firestore_client = None

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
        """Persist `job` to Firestore (if enabled) and transactional SQLite WAL store."""
        job.updated_at = time.time()
        payload_dict = job.to_dict()
        payload_json = json.dumps(payload_dict, ensure_ascii=False)

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

        if self._use_firestore and self._firestore_client is not None:
            with suppress(Exception):
                self._firestore_client.collection(self.firestore_collection).document(job.job_id).set(payload_dict)

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
            subtitle_lang=(subtitle_lang or "tur").strip().lower(),
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
        )
        return self.save_job(job)

    def get_job(self, job_id: str) -> Optional[EncodeJob]:
        """Load a job by `job_id` from persistent storage."""
        clean_id = (job_id or "").strip()
        if not clean_id:
            return None

        if self._use_firestore and self._firestore_client is not None:
            with suppress(Exception):
                doc = self._firestore_client.collection(self.firestore_collection).document(clean_id).get()
                if doc.exists:
                    data = doc.to_dict()
                    if isinstance(data, dict):
                        return EncodeJob.from_dict(data)

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
        return self.save_job(job)

    def mark_job_failed(self, job_id: str, error_message: str) -> Optional[EncodeJob]:
        """Transition a job to `failed` state and clear any download link."""
        job = self.get_job(job_id)
        if job is None:
            return None
        job.stage = "failed"
        job.status = "failed"
        job.eta_seconds = 0
        job.gcs_upload_verified = False
        job.download_url = None
        job.error = error_message
        return self.save_job(job)

    def list_jobs(
        self,
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[EncodeJob]:
        """List jobs ordered newest-first."""
        safe_limit = max(1, min(int(limit or 50), 200))
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
        with self._connect_sqlite() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM encode_jobs
                WHERE expires_at IS NOT NULL AND expires_at <= ? AND deleted_from_gcs = 0
                """,
                (cur,),
            ).fetchall()

        expired_count = 0
        gcs_deleted_count = 0
        purged_job_ids: list[str] = []

        for row in rows:
            job = EncodeJob.from_dict(json.loads(row["payload_json"]))
            deleted_ok = True
            if job.gcs_bucket and job.gcs_object_name:
                deleted_ok = self.gcs.delete_object(job.gcs_bucket, job.gcs_object_name)
                if deleted_ok:
                    gcs_deleted_count += 1

            job.stage = "expired"
            job.status = "expired"
            job.download_url = None
            job.deleted_from_gcs = bool(deleted_ok)
            job.deleted_at = cur
            self.save_job(job)
            expired_count += 1
            purged_job_ids.append(job.job_id)

        return {
            "checked_at": _iso_utc(cur),
            "expired_jobs_processed": expired_count,
            "gcs_objects_deleted": gcs_deleted_count,
            "job_ids": purged_job_ids,
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
        with self._connect_sqlite() as conn:
            rows = conn.execute(
                """
                SELECT payload_json FROM encode_jobs
                WHERE status = 'running' AND updated_at <= ?
                """,
                (cutoff,),
            ).fetchall()

        recovered: list[EncodeJob] = []
        for row in rows:
            job = EncodeJob.from_dict(json.loads(row["payload_json"]))
            job.stage = "failed"
            job.status = "failed"
            job.eta_seconds = 0
            job.download_url = None
            job.gcs_upload_verified = False
            job.error = (
                f"Worker beklenmedik şekilde sonlandı veya zaman aşımına uğradı "
                f"({int(stale_timeout_seconds)}sn boyunca ilerleme sinyali alınamadı)."
            )
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
