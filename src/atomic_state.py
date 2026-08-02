"""Collision-safe atomic JSON publication for workspace state records."""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Callable, Optional


def write_json(
    path: Path,
    data,
    *,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> bool:
    """Write compact JSON through a unique sibling temp and atomic replace.

    The destination is unchanged on serialization/write/replace failure. Any
    failed staging file is removed best-effort, and failures never escape into
    a caller's bridge loop.
    """
    tmp = path.with_suffix(
        path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
        return True
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                pass
        return False
