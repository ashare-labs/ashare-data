"""User-authorized macOS Keychain access for one fixed Tushare service/account.

The native helper never prints a secret: storage uses stdin, retrieval uses an
anonymous pipe. Call read_token only inside the authorized collector, never from
a status command or interactive diagnostic that displays return values.
"""

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess

SERVICE = "ashare-data.tushare"
ACCOUNT = "tushare"
MAX_BYTES = 4096


def verify_helper(helper, policy_path):
    policy = json.loads(Path(policy_path).read_text())
    helper = Path(helper).resolve()
    if (policy.get("schema") != "ad.tushare-credential-policy.v1"
            or policy.get("service") != SERVICE or policy.get("account") != ACCOUNT
            or Path(policy.get("helper", "")).resolve() != helper
            or hashlib.sha256(helper.read_bytes()).hexdigest() != policy.get("helper_sha256")):
        raise ValueError("Keychain helper or dedicated service policy changed; credential not read")


def _environment():
    return {"PATH": os.defpath}


def _failure(code, stderr=b""):
    match = re.search(rb"keychain_osstatus=(-?\d+)", stderr)
    status = int(match[1]) if match else None
    reason = {-25300: "item_not_configured", -25308: "keychain_access_requires_user_unlock_or_permission",
              -128: "user_canceled_keychain_access", -25293: "keychain_access_denied"}.get(status, "native_keychain_operation_failed")
    return RuntimeError(f"{reason}; exit={code}; osstatus={status}; credential omitted")


def store_token(helper, token):
    if not isinstance(token, str) or len(token.strip()) < 16:
        raise ValueError("Tushare credential is absent or invalid; configure locally")
    value = token.strip().encode("utf-8")
    if len(value) > MAX_BYTES:
        raise ValueError("Credential exceeds the bounded storage size")
    result = subprocess.run([str(Path(helper).resolve()), "store"], input=value, capture_output=True,
                            env=_environment(), check=False)
    if result.returncode:
        raise _failure(result.returncode, result.stderr)
    metadata = json.loads(result.stdout)
    if metadata != dict(stored_in_keychain=True, service=SERVICE, account=ACCOUNT):
        raise RuntimeError("Unexpected keychain storage acknowledgment; credential omitted")
    return metadata


def status(helper):
    result = subprocess.run([str(Path(helper).resolve()), "status"], capture_output=True,
                            env=_environment(), timeout=20, check=False)
    if result.returncode:
        raise _failure(result.returncode, result.stderr)
    value = json.loads(result.stdout)
    if value not in ({"configured": True}, {"configured": False}):
        raise RuntimeError("Unexpected keychain metadata response")
    return value


def read_token(helper):
    read_fd, write_fd = os.pipe()
    try:
        child = subprocess.Popen([str(Path(helper).resolve()), "read", str(write_fd)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            pass_fds=(write_fd,), close_fds=True, env=_environment())
        os.close(write_fd)
        write_fd = None
        try:
            _, errors = child.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()  # Only this credential helper, before any source request.
            child.communicate()
            raise RuntimeError("Keychain read timed out; collector not started; credential omitted") from None
        if child.returncode:
            raise _failure(child.returncode, errors)
        value = os.read(read_fd, MAX_BYTES + 1)
        if not 16 <= len(value) <= MAX_BYTES:
            raise RuntimeError("Invalid bounded keychain value; credential omitted")
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            raise RuntimeError("Invalid keychain value encoding; credential omitted") from None
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
