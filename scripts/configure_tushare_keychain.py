"""One-time user-terminal hidden input into the dedicated macOS Keychain item."""

import argparse
import getpass
import hashlib
import json
from pathlib import Path
import sys
import warnings

from ashare_data.financials import tushare_reference as keychain
from ashare_data.financials.expansion import atomic, encoded, immutable, now


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--status-file", type=Path, help="Non-secret storage acknowledgment only")
    args = parser.parse_args()
    policy = args.status_file.parent / "credential-policy.json" if args.status_file else None
    if policy and policy.exists():
        keychain.verify_helper(args.helper, policy)
    if keychain.status(args.helper)["configured"]:
        metadata = dict(configured=True, existing_item_preserved=True,
                        service=keychain.SERVICE, account=keychain.ACCOUNT)
    else:
        if not sys.stdin.isatty():
            parser.error("Use your local terminal for the hidden one-time credential entry")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                token = getpass.getpass("Tushare token（隐藏输入，保存到本机钥匙串）: ")
            except getpass.GetPassWarning:
                parser.error("Cannot disable terminal echo; no plaintext input fallback")
        metadata = keychain.store_token(args.helper, token)
        del token
    result = dict(at=now(), **metadata, credential_in_plaintext_file=False, credential_value_displayed=False)
    if args.status_file:
        if not policy.exists():
            immutable(policy, encoded(dict(schema="ad.tushare-credential-policy.v1", at=now(),
                authorization="user ran the local hidden-input Keychain setup command", service=keychain.SERVICE,
                account=keychain.ACCOUNT, helper=str(args.helper.resolve()),
                helper_sha256=hashlib.sha256(args.helper.read_bytes()).hexdigest(),
                storage="macOS Keychain; no plaintext credential files")))
        atomic(args.status_file, encoded(result))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
