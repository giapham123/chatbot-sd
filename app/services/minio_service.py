"""MinIO service — generates image URLs per platform and downloads images for LLM.

WEB  → presigned URL (temporary, signed access)
ZALO → public URL   (direct, requires public bucket or MINIO_PUBLIC_BASE_URL)
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from datetime import timedelta

logger = logging.getLogger(__name__)

_PRESIGNED_EXPIRES_HOURS = 24


class MinioService:
    def __init__(self) -> None:
        self._client = None
        self._bucket = ""
        self._endpoint = ""
        self._secure = False
        self._public_base_url = ""
        self._enabled = False

    def init(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        secure: bool,
        public_base_url: str,
        bucket: str,
    ) -> None:
        if not endpoint or not access_key or not secret_key:
            logger.warning("MinIO credentials missing — image URL generation disabled")
            return
        try:
            from minio import Minio
            self._client = Minio(
                endpoint=endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=secure,
            )
            self._bucket = bucket
            self._endpoint = endpoint
            self._secure = secure
            self._public_base_url = public_base_url.rstrip("/") if public_base_url else ""
            self._enabled = True
            logger.info("MinIO initialized → %s / %s", endpoint, bucket)
        except Exception as exc:
            logger.warning("MinIO init failed (%s) — image URL generation disabled", exc)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_image_urls(self, object_keys: list[str], platform: str) -> list[str]:
        """Convert MinIO object keys to accessible URLs based on platform."""
        if not object_keys:
            return []
        if not self._enabled:
            return list(object_keys)

        use_public = (platform or "WEB").upper() == "ZALO"
        urls: list[str] = []
        for key in object_keys:
            if not key:
                continue
            try:
                key = self._normalize_key(key)
                url = self._public_url(key) if use_public else self._presigned_url(key)
                urls.append(url)
            except Exception as exc:
                logger.warning("MinIO URL generation failed for %s: %s", key, exc)
                urls.append(key)
        return urls

    def get_image_b64(self, object_key: str) -> tuple[str, str] | None:
        """Download object from MinIO and return (mime_type, base64_data) or None."""
        if not self._enabled:
            return None
        response = None
        try:
            key = self._normalize_key(object_key)
            logger.debug("MinIO downloading %s/%s", self._bucket, key)
            response = self._client.get_object(self._bucket, key)
            data = response.read()
            mime = mimetypes.guess_type(key)[0] or "image/png"
            logger.debug("MinIO downloaded %s (%d bytes, %s)", key, len(data), mime)
            return mime, base64.b64encode(data).decode("utf-8")
        except Exception as exc:
            logger.warning("MinIO download failed for %s: %s", object_key, exc)
            return None
        finally:
            if response:
                response.close()
                response.release_conn()

    async def aget_image_b64(self, object_key: str) -> tuple[str, str] | None:
        """Async wrapper — runs MinIO download in thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.get_image_b64, object_key)

    def _normalize_key(self, key: str) -> str:
        """Strip leading slash and bucket-name prefix if present."""
        key = key.lstrip("/")
        prefix = self._bucket + "/"
        if key.startswith(prefix):
            key = key[len(prefix):]
        return key

    def _presigned_url(self, object_key: str) -> str:
        return self._client.presigned_get_object(
            self._bucket,
            object_key,
            expires=timedelta(hours=_PRESIGNED_EXPIRES_HOURS),
        )

    def _public_url(self, object_key: str) -> str:
        if self._public_base_url:
            return f"{self._public_base_url}/{self._bucket}/{object_key}"
        protocol = "https" if self._secure else "http"
        return f"{protocol}://{self._endpoint}/{self._bucket}/{object_key}"


minio_service = MinioService()
