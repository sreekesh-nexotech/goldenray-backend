"""Export the Flarize files that no other package committed (read-only from the Flarize ``data/`` folder), masked:

* ``users.json`` — the 5 users with their ``userId`` and ``role`` (the ids pack-config/procurement/quotations refer to);
  names and e-mails replaced, password hashes dropped;
* ``company-profile.json`` — the profile with the phone and the bank/UPI values replaced (the originals are demo values
  anyway; the importer never writes them);
* ``cms-state.json`` + ``cms-assets/`` — two ACTIVE assets (with their files) and one ARCHIVED record, plus one page
  and one campaign stub (the page designer is reported, never migrated).

Every other Flarize file of the test source comes from the fixtures the owning packages committed (see
``migrations_tools/tests/ops_fixtures.py``). Re-run::

    python migrations_tools/tests/fixtures/ops/flarize/export_flarize_fixtures.py [/path/to/flarize/data]
"""

import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/user/flarize-main/flarize/data")


def read(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def write(name: str, value) -> None:
    (HERE / name).write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


users = []
for index, user in enumerate(read("users.json"), start=1):
    users.append({key: value for key, value in user.items() if key not in ("passwordHash", "passwordSalt")} | {"name": f"Flarize User {index}", "email": f"flarize.user{index}@example.com"})
write("users.json", users)

profile = read("company-profile.json")
profile.update(phone="9000000001", email="sales@example.com", bankName="Demo Bank", bankAccountName="DEMO", bankAccountNumber="000000000000", bankIfsc="DEMO0000001", upiId="demo@upi")
write("company-profile.json", profile)

state = read("cms-state.json")
assets = list(state["assets"].values())
chosen = [asset for asset in assets if asset["status"] == "ACTIVE"][:2] + [asset for asset in assets if asset["status"] != "ACTIVE"][:1]
page_id = next(iter(state["pages"]))
campaign_id = next(iter(state["campaigns"]))
write(
    "cms-state.json",
    {
        "storeVersion": state["storeVersion"],
        "pages": {page_id: {"pageId": page_id, "note": "stub (page designer not migrated)"}},
        "pageVersions": {},
        "assets": {asset["assetId"]: asset for asset in chosen},
        "campaigns": {campaign_id: {"campaignId": campaign_id, "note": "stub"}},
        "fieldRegistry": {},
    },
)
(HERE / "cms-assets").mkdir(exist_ok=True)
for asset in chosen:
    if asset["status"] == "ACTIVE":
        shutil.copyfile(DATA / "cms-assets" / asset["storagePath"], HERE / "cms-assets" / asset["storagePath"])
