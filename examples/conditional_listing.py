"""只读固定CONDITIONAL的有限投影；示例上下文不是原生run token。"""

import argparse
import json

from ashare_data import DataError, Store
from ashare_data.core.model import digest

p = argparse.ArgumentParser()
p.add_argument("--store", required=True)
p.add_argument("--dataset", required=True, help="本版本生成的条件研究数据ID")
p.add_argument("--contract", required=True, help="固定的listing契约hash，不使用latest")
args = p.parse_args()
view = Store(args.store).conditional(
    args.dataset,
    mode="conditional_research",
)
listing = view.listing(contract_sha256=args.contract)
request = dict(
    security="600000.XSHG",
    current_date="2020-01-03",
    target_date="2020-01-03",
    role="current",
    context_sha256=digest({"example_only": True, "generation": 1}),
)
current = listing.status(**request)
assert listing.revalidate(current, **request) == current
successor = listing.successor(
    security="600000.XSHG",
    current_date="2020-01-06",
    candidate_date="2020-01-07",
    context_sha256=request["context_sha256"],
)
try:
    listing.successor(
        security="600000.XSHG",
        current_date="2020-01-07",
        candidate_date="2020-01-07",
        context_sha256=request["context_sha256"],
    )
except DataError as exc:
    assert exc.code == "CONDITIONAL_LISTING_SUCCESSOR"
    rejected = exc.as_dict()
else:
    raise AssertionError("RQ末端同日回退必须拒绝")
print(
    json.dumps(
        {
            "current": current.to_dict(),
            "successor": successor.to_dict(),
            "tail_rejection": rejected,
            "native_execution": "NOT_RUN",
        },
        ensure_ascii=False,
        indent=2,
    )
)
