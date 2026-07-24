"""Local-filesystem attachment managers for the SQLAlchemy backend.

The backend never touches a byte itself: it persists ``AttachmentFileRecord`` /
``NotificationAttachment`` rows and hands the opaque ``storage_identifiers`` back to
whichever manager was injected. These managers own the bytes, storing each uploaded file
under ``base_path`` keyed by a generated id.

``storage_identifiers`` always carries a non-empty ``id`` (the on-disk filename) plus the
``base_path`` used to write it, so a handle can be rebuilt even if a differently-configured
manager instance reconstructs it later.
"""

import io
import os
import uuid
from pathlib import Path
from typing import BinaryIO

from vintasend.exceptions import UnsupportedAttachmentFileTypeError
from vintasend.services.attachment_managers.asyncio_base import AsyncIOBaseAttachmentManager
from vintasend.services.attachment_managers.base import BaseAttachmentManager
from vintasend.services.dataclasses import (
    AttachmentFile,
    AttachmentFileRecord,
    FileAttachment,
    StorageIdentifiers,
)


DEFAULT_BASE_PATH = os.environ.get("VINTASEND_ATTACHMENTS_PATH", "vintasend_attachments")


def _resolve_path(storage_identifiers: StorageIdentifiers, fallback_base: Path) -> Path:
    file_id = storage_identifiers.get("id")
    if not file_id:
        raise UnsupportedAttachmentFileTypeError(
            "storage_identifiers must carry a non-empty 'id'"
        )
    base = storage_identifiers.get("base_path")
    base_path = Path(base) if base else fallback_base
    return base_path / str(file_id)


class FilesystemStoredFile(AttachmentFile):
    """Lazy handle to a file stored on the local filesystem.

    Built with no I/O by ``reconstruct_attachment_file``; the bytes are only touched when
    ``read``/``stream``/``delete`` is called.
    """

    def __init__(self, path: Path):
        self._path = path

    def read(self) -> bytes:
        try:
            with open(self._path, "rb") as f:
                return f.read()
        except FileNotFoundError as e:
            raise FileNotFoundError(f"No file stored at {self._path!s}") from e

    def stream(self) -> BinaryIO:
        return io.BytesIO(self.read())

    def url(self, expires_in: int = 3600) -> str:
        # No signed-URL scheme for a local path; return a file:// URL so callers that only
        # need a locator still get one.
        return self._path.resolve().as_uri()

    def delete(self) -> None:
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass


class _FilesystemManagerMixin:
    """Shared upload/reconstruct/delete mechanics for both the sync and async managers."""

    base_path: Path

    def _write(self, data: bytes, filename: str, content_type: str | None) -> AttachmentFileRecord:
        self.base_path.mkdir(parents=True, exist_ok=True)
        file_id = str(uuid.uuid4())
        destination = self.base_path / file_id
        with open(destination, "wb") as f:
            f.write(data)
        now = _now()
        return AttachmentFileRecord(
            id=file_id,
            filename=filename,
            content_type=content_type or self.detect_content_type(filename),  # type: ignore[attr-defined]
            size=len(data),
            checksum=self.calculate_checksum(data),  # type: ignore[attr-defined]
            created_at=now,
            updated_at=now,
            storage_identifiers={"id": file_id, "base_path": str(self.base_path)},
        )

    def reconstruct_attachment_file(
        self, storage_identifiers: StorageIdentifiers
    ) -> AttachmentFile:
        return FilesystemStoredFile(_resolve_path(storage_identifiers, self.base_path))


def _now():
    import datetime

    return datetime.datetime.now(tz=datetime.timezone.utc)


class FilesystemAttachmentManager(_FilesystemManagerMixin, BaseAttachmentManager):
    """Sync local-filesystem attachment manager. Reference implementation and the
    default the SQLAlchemy backend falls back to when none is injected."""

    def __init__(self, base_path: str | Path = DEFAULT_BASE_PATH) -> None:
        self.base_path = Path(base_path)

    def upload_file(
        self,
        file: FileAttachment,
        filename: str,
        content_type: str | None = None,
    ) -> AttachmentFileRecord:
        return self._write(self.file_to_bytes(file), filename, content_type)

    def delete_file_by_identifiers(self, storage_identifiers: StorageIdentifiers) -> None:
        path = _resolve_path(storage_identifiers, self.base_path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


class FilesystemAsyncIOAttachmentManager(_FilesystemManagerMixin, AsyncIOBaseAttachmentManager):
    """AsyncIO local-filesystem attachment manager.

    ``reconstruct_attachment_file`` stays synchronous (it only builds a lazy handle); the
    disk writes in ``upload_file`` / ``delete_file_by_identifiers`` are blocking but small,
    matching the library's other AsyncIO seams that wrap otherwise-blocking work.
    """

    def __init__(self, base_path: str | Path = DEFAULT_BASE_PATH) -> None:
        self.base_path = Path(base_path)

    async def upload_file(
        self,
        file: FileAttachment,
        filename: str,
        content_type: str | None = None,
    ) -> AttachmentFileRecord:
        return self._write(self.file_to_bytes(file), filename, content_type)

    async def delete_file_by_identifiers(self, storage_identifiers: StorageIdentifiers) -> None:
        path = _resolve_path(storage_identifiers, self.base_path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
