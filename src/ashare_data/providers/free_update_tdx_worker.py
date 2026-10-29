"""One bounded request through an explicitly pinned, already installed eltdx SDK.

Executed by that SDK's Python. No installation, host discovery or credentials.
The decoded values and original wire payload are returned together for replay.
"""

from pathlib import Path
import hashlib
import importlib.metadata
import json
import sys

_directory = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p).resolve() != _directory]


def main():
    config = json.loads(sys.stdin.read())
    client = None
    try:
        import eltdx
        from eltdx import TdxClient
        from eltdx.serialization import to_jsonable

        root = Path(eltdx.__file__).parent
        files = {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*"))
            if p.suffix in {".py", ".so", ".pyd"}
        }
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if importlib.metadata.version("eltdx") != "3.0.3" or fingerprint != config["sdk_sha256"]:
            raise ValueError("existing SDK changed; review and pin it before making requests")
        client = TdxClient(
            host=config["host"],
            timeout=4,
            pool_size=1,
            server_count=1,
            connections_per_server=1,
            runtime_workers=1,
            max_connections_per_host=1,
            connect_concurrency=1,
            connect_concurrency_per_host=1,
            probe_hosts=False,
            heartbeat_interval=None,
        )
        client.connect()
        if config["operation"] == "bars":
            data = client.bars.get(
                config["symbol"],
                period=config["period"],
                count=config["count"],
                start=0,
                adjust="none",
                kind="stock",
                include_raw=True,
            )
        elif config["operation"] == "finance":
            data = client.corporate.finance_batch([config["symbol"]], include_raw=True)
        elif config["operation"] == "depth":
            data = client.quotes.get_depth([config["symbol"]])
        else:
            raise ValueError("unreviewed native operation")
        output = {
            "ok": True,
            "request": config,
            "sdk_version": "3.0.3",
            "sdk_files": files,
            "data": to_jsonable(data),
        }
    except Exception as exc:
        output = {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:600]}
    finally:
        if client is not None:
            client.close()
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
