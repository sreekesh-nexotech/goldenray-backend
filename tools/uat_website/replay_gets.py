"""Every distinct old-URL GET the platform copy's nginx saw during the UAT (browser AND Next.js server-side fetches),
replayed against both sides' nginx and compared as JSON (unordered legacy lists compared by id)."""

import json
import os
from pathlib import Path

from apisweep import SIDES, call, norm

LOG = Path(os.environ.get("UAT_DIR", ".")) / "nginx-platform/logs/access.log"
uris = sorted({d["uri"] for d in map(json.loads, LOG.read_text().splitlines()) if d["method"] == "GET" and d["legacy_group"]})
same = diff = 0
report = []
for uri in uris:
    res = {side: call(base, "GET", uri, None) for side, base in SIDES.items()}
    ok = res["legacy"][0] == res["platform"][0] and norm(uri, res["legacy"][1]) == norm(uri, res["platform"][1]) and res["legacy"][2] == res["platform"][2]
    same += ok
    diff += not ok
    report.append({"uri": uri, "same": ok, "status": [res["legacy"][0], res["platform"][0]]})
    if not ok:
        print("DIFF", uri, res["legacy"][0], res["platform"][0])
print(f"{len(uris)} distinct GETs: {same} identical, {diff} differ")
json.dump(report, open(LOG.parents[2] / "results/replay_gets.json", "w"), indent=1)
