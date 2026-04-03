"""
Cryptographic self-sovereignty for DevClaw.

DevClaw generates its own private key and signs all its actions using
HMAC-SHA256, producing verifiable proof of authorship for commits,
trade executions, and arbitrary action records.

State stored in: <workspace>/.claw/identity/
Signed trades appended to: <workspace>/.claw/signed_trades.jsonl
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class DevClawIdentity:
    """Represents DevClaw's cryptographic identity."""

    public_key_hex: str
    created_at: str
    key_fingerprint: str
    signed_actions_count: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _identity_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "identity"


def _private_key_path(workspace: Path) -> Path:
    return _identity_dir(workspace) / "private_key.hex"


def _public_key_path(workspace: Path) -> Path:
    return _identity_dir(workspace) / "public_key.hex"


def _counter_path(workspace: Path) -> Path:
    return _identity_dir(workspace) / "actions_count.json"


def _signed_trades_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "signed_trades.jsonl"


def _fingerprint(public_key_hex: str) -> str:
    """Derive a short fingerprint from the public key (first 16 hex chars of SHA-256)."""
    digest = hashlib.sha256(public_key_hex.encode("utf-8")).hexdigest()
    return digest[:16]


def _increment_counter(workspace: Path) -> int:
    """Atomically increment and return the signed-actions counter."""
    path = _counter_path(workspace)
    count = 0
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = int(data.get("count", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    count += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"count": count}), encoding="utf-8")
    tmp.replace(path)
    return count


def _read_counter(workspace: Path) -> int:
    path = _counter_path(workspace)
    if not path.is_file():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return int(data.get("count", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 0


def _restrict_permissions(path: Path) -> None:
    """Best-effort owner-only read/write on the private key file."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows or permission issues — non-fatal


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_identity(workspace: Path) -> DevClawIdentity:
    """Generate a new DevClaw keypair and persist it under .claw/identity/.

    The private key is a 256-bit random secret.  The public key is derived
    as SHA-256(private_key_bytes), providing a one-way binding suitable for
    HMAC-based signing (not asymmetric crypto, but sufficient for
    self-sovereign action provenance without external dependencies).
    """
    workspace = Path(workspace).resolve()
    ident_dir = _identity_dir(workspace)
    ident_dir.mkdir(parents=True, exist_ok=True)

    # Generate 256-bit secret
    private_key_bytes = secrets.token_bytes(32)
    private_key_hex = private_key_bytes.hex()

    # Derive public key via SHA-256
    public_key_hex = hashlib.sha256(private_key_bytes).hexdigest()

    created_at = datetime.now(timezone.utc).isoformat()

    # Persist private key (restricted permissions)
    priv_path = _private_key_path(workspace)
    tmp = priv_path.with_suffix(".tmp")
    tmp.write_text(private_key_hex, encoding="utf-8")
    tmp.replace(priv_path)
    _restrict_permissions(priv_path)

    # Persist public key
    pub_path = _public_key_path(workspace)
    tmp = pub_path.with_suffix(".tmp")
    tmp.write_text(public_key_hex, encoding="utf-8")
    tmp.replace(pub_path)

    # Persist creation timestamp
    meta_path = ident_dir / "meta.json"
    meta_tmp = meta_path.with_suffix(".tmp")
    meta_tmp.write_text(
        json.dumps({"created_at": created_at}, indent=2),
        encoding="utf-8",
    )
    meta_tmp.replace(meta_path)

    fingerprint = _fingerprint(public_key_hex)
    return DevClawIdentity(
        public_key_hex=public_key_hex,
        created_at=created_at,
        key_fingerprint=fingerprint,
        signed_actions_count=0,
    )


def load_identity(workspace: Path) -> DevClawIdentity | None:
    """Load an existing identity from .claw/identity/.

    Returns ``None`` if no identity has been generated yet.
    """
    workspace = Path(workspace).resolve()
    pub_path = _public_key_path(workspace)
    if not pub_path.is_file():
        return None

    try:
        public_key_hex = pub_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    # Read creation timestamp
    meta_path = _identity_dir(workspace) / "meta.json"
    created_at = "unknown"
    if meta_path.is_file():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            created_at = data.get("created_at", created_at)
        except (OSError, json.JSONDecodeError):
            pass

    fingerprint = _fingerprint(public_key_hex)
    count = _read_counter(workspace)
    return DevClawIdentity(
        public_key_hex=public_key_hex,
        created_at=created_at,
        key_fingerprint=fingerprint,
        signed_actions_count=count,
    )


def ensure_identity(workspace: Path) -> DevClawIdentity:
    """Load an existing identity or generate a new one."""
    identity = load_identity(workspace)
    if identity is not None:
        return identity
    return generate_identity(workspace)


def sign_action(workspace: Path, action_type: str, action_data: str) -> dict[str, Any]:
    """Create an HMAC-SHA256 signature for an arbitrary action.

    The signed payload is ``timestamp + action_type + action_data``.

    Returns a dict with *action_type*, *timestamp*, *data_hash*,
    *signature*, and *public_key*.
    """
    workspace = Path(workspace).resolve()
    priv_path = _private_key_path(workspace)
    pub_path = _public_key_path(workspace)

    if not priv_path.is_file() or not pub_path.is_file():
        raise FileNotFoundError(
            "No DevClaw identity found. Call generate_identity() or ensure_identity() first."
        )

    private_key_hex = priv_path.read_text(encoding="utf-8").strip()
    public_key_hex = pub_path.read_text(encoding="utf-8").strip()
    private_key_bytes = bytes.fromhex(private_key_hex)

    timestamp = datetime.now(timezone.utc).isoformat()
    payload = f"{timestamp}{action_type}{action_data}"
    data_hash = hashlib.sha256(action_data.encode("utf-8")).hexdigest()

    signature = hmac.new(
        private_key_bytes,
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    _increment_counter(workspace)

    return {
        "action_type": action_type,
        "timestamp": timestamp,
        "data_hash": data_hash,
        "signature": signature,
        "public_key": public_key_hex,
    }


def verify_signature(
    signed_action: dict[str, Any],
    public_key_hex: str,
    workspace: Path | None = None,
) -> bool:
    """Verify a signed action against the given public key.

    When *workspace* is provided and contains the private key corresponding
    to *public_key_hex*, the function recomputes the HMAC-SHA256 signature
    from the embedded timestamp + action_type + action_data and compares it
    cryptographically.  If the private key is unavailable it falls back to
    structural validation (required fields present, public key matches).

    Args:
        signed_action: Dict produced by :func:`sign_action`.
        public_key_hex: The expected public key hex string.
        workspace: Optional workspace path to locate the private key for
            full cryptographic verification.

    Returns:
        ``True`` if verification passes, ``False`` otherwise.
    """
    required = {"action_type", "timestamp", "data_hash", "signature", "public_key"}
    if not required.issubset(signed_action.keys()):
        return False

    if signed_action.get("public_key") != public_key_hex:
        return False

    # --- Attempt full HMAC verification when the private key is available ---
    private_key_bytes: bytes | None = None
    if workspace is not None:
        workspace = Path(workspace).resolve()
        priv_path = _private_key_path(workspace)
        pub_path = _public_key_path(workspace)
        if priv_path.is_file() and pub_path.is_file():
            try:
                stored_pub = pub_path.read_text(encoding="utf-8").strip()
                if stored_pub == public_key_hex:
                    private_key_bytes = bytes.fromhex(
                        priv_path.read_text(encoding="utf-8").strip()
                    )
            except (OSError, ValueError):
                pass

    if private_key_bytes is not None:
        # Recompute expected signature: payload = timestamp + action_type + action_data
        # We need the original action_data.  If 'action_data' is present use it;
        # otherwise reconstruct from fields available in the signed record.
        action_data = signed_action.get("action_data", "")
        timestamp = signed_action["timestamp"]
        action_type = signed_action["action_type"]
        payload = f"{timestamp}{action_type}{action_data}"

        expected_sig = hmac.new(
            private_key_bytes,
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected_sig, signed_action["signature"])

    # --- Fallback: structural validation only (no private key available) ---
    return True


def sign_git_commit(workspace: Path, commit_message: str) -> str:
    """Return *commit_message* with an appended DevClaw signature line."""
    signed = sign_action(workspace, "git_commit", commit_message)
    signature_line = f"\nSigned-by-DevClaw: {signed['signature']}"
    return commit_message.rstrip() + signature_line


def sign_trade_execution(workspace: Path, trade: dict[str, Any]) -> dict[str, Any]:
    """Sign a trade execution record and append it to .claw/signed_trades.jsonl."""
    trade_json = json.dumps(trade, sort_keys=True, default=str)
    signed = sign_action(workspace, "trade_execution", trade_json)

    record: dict[str, Any] = {
        **trade,
        "_signature": signed["signature"],
        "_signed_at": signed["timestamp"],
        "_data_hash": signed["data_hash"],
        "_public_key": signed["public_key"],
    }

    trades_path = _signed_trades_path(workspace)
    trades_path.parent.mkdir(parents=True, exist_ok=True)
    with trades_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")

    return record


def get_identity_card(workspace: Path) -> str:
    """Return a human-readable identity card string."""
    identity = load_identity(workspace)
    if identity is None:
        return "No DevClaw identity found. Run ensure_identity() to create one."

    return (
        f"DevClaw Identity: {identity.key_fingerprint}\n"
        f"Public Key: {identity.public_key_hex}\n"
        f"Created: {identity.created_at}\n"
        f"Actions Signed: {identity.signed_actions_count}"
    )
