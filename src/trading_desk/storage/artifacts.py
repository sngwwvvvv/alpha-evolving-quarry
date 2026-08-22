from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

from trading_desk.config import canonical_json, sha256_hex

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: str) -> Path:
        if _DIGEST_RE.fullmatch(digest) is None:
            raise ValueError("invalid artifact digest")
        return self.root / digest[:2] / digest

    def put_bytes(self, data: bytes) -> str:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes")
        payload = bytes(data)
        digest = sha256_hex(payload)
        dest = self.path_for(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            existing = dest.read_bytes()
            if existing != payload:
                raise ValueError("artifact hash collision")
            return digest

        fd, tmp_name = tempfile.mkstemp(prefix=f".{digest}.", suffix=".tmp", dir=dest.parent)
        tmp_owned = True
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if dest.exists():
                existing = dest.read_bytes()
                if existing != payload:
                    raise ValueError("artifact hash collision")
                return digest
            os.replace(tmp_name, dest)
            tmp_owned = False
        finally:
            if fd >= 0:
                os.close(fd)
            if tmp_owned and os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return digest

    def put_json(self, value: Any) -> str:
        return self.put_bytes(canonical_json(value).encode("utf-8"))
