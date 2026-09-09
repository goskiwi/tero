"""Session-scoped, bounded results and private byte-exact preimages."""

import json
import re
import uuid
from pathlib import Path

from .storage import atomic_write

CAPTURE_BYTES = 1024 * 1024
PREVIEW_BYTES = 12 * 1024
PAGE_BYTES = 8192


class ArtifactStore:
    def __init__(self, root, redactor):
        self.root = Path(root).absolute()
        self.redact = redactor

    def _path(self, artifact_id):
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise ValueError("Invalid artifact ID")
        path = self.root / (artifact_id + ".json")
        if path.resolve() != path:
            raise ValueError("Artifact storage must not be redirected")
        return path

    def write_bytes(self, content, *, kind, metadata=None):
        artifact_id = uuid.uuid4().hex
        path = self._path(artifact_id)
        data_path = path.with_suffix(".bin" if kind == "preimage" else ".txt")
        atomic_write(data_path, content, create=True)
        record = {"id": artifact_id, "kind": kind, "size_bytes": len(content), **(metadata or {})}
        atomic_write(path, json.dumps(record).encode(), create=True)
        return record

    def write_text(self, content, *, kind="tool_output", capture_truncated=False, redacted=False):
        safe = self.redact(content)
        raw = safe.encode()
        truncated = capture_truncated or len(raw) > CAPTURE_BYTES
        if len(raw) > CAPTURE_BYTES:
            marker = "\n[capture truncated: middle content not retained]\n"
            half = (CAPTURE_BYTES - len(marker.encode())) // 2
            safe = (
                raw[:half].decode("utf-8", errors="ignore")
                + marker
                + raw[-half:].decode("utf-8", errors="ignore")
            )
        record = self.write_bytes(
            safe.encode(),
            kind=kind,
            metadata={
                "capture_truncated": truncated,
                "redacted": redacted or self.redact(content) != content,
            },
        )
        return record

    def descriptor(self, artifact_id):
        record = json.loads(self._path(artifact_id).read_text())
        if record.get("id") != artifact_id:
            raise ValueError("Artifact identity mismatch")
        return record

    def read_bytes(self, artifact_id, *, kind):
        record = self.descriptor(artifact_id)
        if record["kind"] != kind:
            raise ValueError("Artifact kind is not allowed for this operation")
        path = self._path(artifact_id).with_suffix(".bin" if kind == "preimage" else ".txt")
        if path.resolve() != path:
            raise ValueError("Artifact content must not be redirected")
        content = path.read_bytes()
        if len(content) != record["size_bytes"]:
            raise ValueError("Artifact content is incomplete")
        return content

    def read_page(self, artifact_id, offset, max_bytes):
        record = self.descriptor(artifact_id)
        if record["kind"] not in {"tool_output", "diff"}:
            raise ValueError("Private preimages cannot be read through a model tool")
        content = self.read_bytes(artifact_id, kind=record["kind"])
        if not 0 <= offset <= len(content) or not 4 <= max_bytes <= PAGE_BYTES:
            raise ValueError("Invalid artifact page range")
        while offset < len(content) and content[offset] & 0xC0 == 0x80:
            offset += 1
        end = min(len(content), offset + max_bytes)
        while end < len(content) and end > offset and content[end] & 0xC0 == 0x80:
            end -= 1
        return {
            **record,
            "content": content[offset:end].decode(),
            "offset": offset,
            "next_offset": end if end < len(content) else None,
            "has_more": end < len(content),
        }
