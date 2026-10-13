"""Run with an explicitly selected existing eltdx Python, without importing AD."""
from pathlib import Path
import importlib.metadata
import json
import sys

from tdx_bars_core import SCHEMA, collect_pages, error_kind, now, save_journal, sha


def main():
    root = Path(sys.argv[1])
    config = json.loads((root / "request.json").read_bytes())
    client = None
    identity = {}
    try:
        import eltdx
        from eltdx import TdxClient
        version = importlib.metadata.version("eltdx")
        if version != "3.0.3":
            raise ValueError("This source profile requires existing eltdx 3.0.3; no auto-install")
        sdk_root = Path(eltdx.__file__).parent
        identity = dict(name="eltdx", version=version, path=str(sdk_root),
                        hashes={str(p.relative_to(sdk_root)): sha(p.read_bytes())
                                for p in [sdk_root / "api/bars.py", sdk_root / "models/kline.py",
                                          *sdk_root.glob("_native*.so"), *sdk_root.glob("_native*.pyd")]})
        client = TdxClient(host=config["host"], timeout=config["request_timeout"],
                           pool_size=1, server_count=1, connections_per_server=1,
                           runtime_workers=1, max_connections_per_host=1,
                           connect_concurrency=1, connect_concurrency_per_host=1,
                           probe_hosts=False, heartbeat_interval=None)
        client.connect()
        code = ("sh" if config["security"].endswith("XSHG") else "sz") + config["security"][:6]
        def fetch(offset, count):
            return client.bars.get(code, period=config["frequency"], start=offset,
                                   count=count, adjust="none", kind="stock", include_raw=True)
        collect_pages(root, config, fetch, identity)
    except Exception as exc:
        save_journal(root, dict(schema=SCHEMA, sdk=identity, pages=[], accepted_rows=0,
                               next_offset=config["start_offset"], complete_history=False,
                               stop_reason=error_kind(exc), error_type=type(exc).__name__,
                               error=str(exc)[:400], completed_at=now()))
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    main()
