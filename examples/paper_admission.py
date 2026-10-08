"""No network. Synthetic facts demonstrate independent state and data admission."""

import json
from pathlib import Path

from ashare_data import Client, CoverageContract

facts = json.loads((Path(__file__).parent / "fixtures/paper_contract.json").read_text())
contract = CoverageContract(facts)
client = Client(coverage_contract=contract)
for clock, labels in [
    ("2026-09-30T09:31:00+08:00", ["2026-09-30 09:31:00"]),
    ("2026-09-30T09:32:00+08:00", ["2026-09-30 09:32:00"]),
    ("2026-09-30T13:02:00+08:00", ["2026-09-30 09:32:00"]),
    ("2026-09-30T13:02:00+08:00", ["2026-09-30 13:02:00"]),
]:
    print(
        json.dumps(
            {
                "clock": clock,
                "trading": client.trading_status("600000.XSHG", as_of=clock),
                "admission": contract.freshness("600000.XSHG", labels, clock),
            },
            ensure_ascii=False,
        )
    )
