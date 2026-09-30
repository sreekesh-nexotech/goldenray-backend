"""Generate two UAT nginx configs from the repo's deploy/nginx files (same maps, same routes, local upstreams).

platform: website copy on :18311, every old-URL group -> shim (api = platform on :18300); served on :18310
legacy:   website copy on :18321, every old-URL group -> old (private backend :18322, shared CMS :18009); served on :18320
"""

import re
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])
NG = REPO / "deploy/nginx"


SNIP = OUT / "snippets"
SNIP.mkdir(parents=True, exist_ok=True)


def fix(text: str) -> str:
    return text.replace("/etc/nginx/flarize/snippets/", f"{SNIP}/").replace("/etc/nginx/flarize/", f"{NG}/")


for f in (NG / "snippets").iterdir():
    (SNIP / f.name).write_text(fix(f.read_text()))


routes = fix((NG / "legacy/routes.conf").read_text())
switch_src = (NG / "legacy/switch.conf").read_text()

for side, port, fe_port, mode in (("platform", 18310, 18311, "shim"), ("legacy", 18320, 18321, "old")):
    d = OUT / f"nginx-{side}"
    (d / "logs").mkdir(parents=True, exist_ok=True)
    switch = re.sub(r"^(\s+[a-z_]+\s+)old;", rf"\g<1>{mode};", switch_src, flags=re.M)
    (d / "switch.conf").write_text(switch)
    (d / "routes.conf").write_text(routes)
    # compose service names carry :8000; the local upstream blocks are matched by name without the port
    groups = (NG / "legacy/groups.conf").read_text().replace("legacy-backend:8000", "legacy-backend").replace("legacy-cms:8000", "legacy-cms")
    (d / "groups.conf").write_text(groups)
    conf = f"""
worker_processes 1;
pid {d}/nginx.pid;
error_log {d}/logs/error.log warn;
events {{ worker_connections 256; }}
http {{
  client_body_temp_path {d}/tmp_body;
  proxy_temp_path {d}/tmp_proxy;
  fastcgi_temp_path {d}/tmp_fcgi;
  uwsgi_temp_path {d}/tmp_uwsgi;
  scgi_temp_path {d}/tmp_scgi;
  include {d}/groups.conf;
  include {d}/switch.conf;
  log_format uat escape=json '{{"ts":"$time_iso8601","method":"$request_method","uri":"$request_uri","status":$status,'
    '"legacy_group":"$legacy_group","legacy_target":"$legacy_target","rt":$request_time}}';
  access_log {d}/logs/access.log uat;
  limit_req_zone $binary_remote_addr zone=public:20m rate=20r/s;
  limit_req_status 429;
  upstream api {{ server 127.0.0.1:18300; keepalive 8; }}
  upstream legacy-backend {{ server 127.0.0.1:18322; }}
  upstream legacy-cms {{ server 127.0.0.1:18009; }}
  upstream frontend {{ server 127.0.0.1:{fe_port}; keepalive 8; }}
  server {{
    listen 127.0.0.1:{port};
    server_name _;
    client_max_body_size 2m;
    include {SNIP}/api-error-pages.conf;
    location /api/public/v1/ {{
      include {SNIP}/api-errors.conf;
      include {SNIP}/proxy-api.conf;
      proxy_pass http://api;
    }}
    location = /healthz {{ include {SNIP}/proxy-api.conf; proxy_pass http://api; }}
    include {d}/routes.conf;
    location / {{
      include {SNIP}/proxy-frontend.conf;
      proxy_pass http://frontend;
    }}
  }}
}}
"""
    (d / "nginx.conf").write_text(conf)
    print(d / "nginx.conf")
