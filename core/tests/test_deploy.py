"""Deployment artefacts (PLAN §5): compose topology, Dockerfile, nginx routing + the legacy switch, release script,
CI view budget. These are the files ops runs; a regression here takes the site down, so they are tested like code."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
DEPLOY = ROOT / "deploy"
NGINX = DEPLOY / "nginx"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())


def _command(service: dict) -> list[str]:
    command = service.get("command") or []
    return command if isinstance(command, list) else command.split()


def _option(command: list[str], flag: str) -> str | None:
    return command[command.index(flag) + 1] if flag in command else None


# ── compose ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_compose_has_every_plan_service(compose):
    services = set(compose["services"])
    assert {"nginx", "frontend", "api-a", "api-b", "worker-default", "worker-documents", "beat", "pgbouncer", "db", "redis", "redis-broker", "migrate", "legacy-backend", "legacy-cms"} <= services
    assert compose["services"]["legacy-backend"]["profiles"] == ["legacy"] and compose["services"]["legacy-cms"]["profiles"] == ["legacy"]
    assert compose["services"]["migrate"]["profiles"] == ["ops"]


def test_every_celery_queue_has_a_consumer(compose):
    """Standard §7.2 defect: a queue nobody consumes silently drops work."""
    consumed = set()
    for service in compose["services"].values():
        command = _command(service)
        if command[:4] == ["celery", "-A", "flarize", "worker"]:
            consumed |= set(_option(command, "-Q").split(","))
    assert consumed == {queue.name for queue in settings.CELERY_TASK_QUEUES}
    documents = _command(compose["services"]["worker-documents"])
    assert _option(documents, "-Q") == "documents" and _option(documents, "-c") == "2"
    assert "-documents:" in compose["services"]["worker-documents"]["image"]
    assert _option(_command(compose["services"]["worker-default"]), "-c") == "8"


def test_api_replicas_and_beat(compose):
    for name in ("api-a", "api-b"):
        api = compose["services"][name]
        assert _command(api)[:3] == ["gunicorn", "--config", "deploy/gunicorn.conf.py"]
        assert "/healthz" in " ".join(api["healthcheck"]["test"])
    beat = _command(compose["services"]["beat"])
    assert beat[:4] == ["celery", "-A", "flarize", "beat"] and "django_celery_beat.schedulers:DatabaseScheduler" in beat
    assert compose["services"]["beat"]["deploy"]["replicas"] == 1


def test_migrations_never_run_on_container_start(compose):
    for name, service in compose["services"].items():
        if name != "migrate":
            assert "migrate" not in " ".join(_command(service)), name
    migrate = compose["services"]["migrate"]
    assert migrate["environment"]["DB_HOST"] == "db"  # DDL bypasses PgBouncer
    assert any("owner.env" in path for path in migrate["env_file"])


def test_app_containers_use_pgbouncer_and_the_split_redis(compose):
    for name in ("api-a", "worker-default", "worker-documents", "beat"):
        environment = compose["services"][name]["environment"]
        assert environment["DB_HOST"] == "pgbouncer"
        assert environment["CELERY_BROKER_URL"].startswith("redis://redis-broker:") and environment["REDIS_URL"].startswith("redis://redis:")
        assert environment["USE_X_ACCEL"] == "True" and environment["PRIVATE_MEDIA_ROOT"] == "/srv/flarize/media/private"
    assert "allkeys-lru" in _command(compose["services"]["redis"]) and "noeviction" in _command(compose["services"]["redis-broker"])
    ini = (DEPLOY / "pgbouncer" / "pgbouncer.ini").read_text()
    assert "pool_mode = transaction" in ini and "max_client_conn = 500" in ini and "default_pool_size = 40" in ini


def test_no_secret_values_in_compose(compose):
    text = (DEPLOY / "docker-compose.yml").read_text()
    for service in compose["services"].values():
        for key, value in (service.get("environment") or {}).items():
            assert not re.search(r"PASSWORD|SECRET|TOKEN|KEY$", key), key
    assert "FERNET" not in text and "SECRET_KEY" not in text


def test_private_media_is_read_only_for_nginx(compose):
    assert "private_media:/srv/flarize/media/private:ro" in compose["services"]["nginx"]["volumes"]
    assert compose["networks"]["default"]["ipam"]["config"][0]["subnet"] == "172.28.0.0/16"


# ── Dockerfile ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_dockerfile_stages_and_hardening():
    dockerfile = (DEPLOY / "Dockerfile").read_text()
    assert re.findall(r"^FROM \S+ AS (\w+)", dockerfile, re.M) == ["builder", "runtime", "documents"]
    assert "USER appuser" in dockerfile and "--no-cache-dir" in dockerfile and "--chown=appuser" not in dockerfile  # code stays root-owned
    assert "fonts-noto-core" in dockerfile and "playwright install" in dockerfile
    assert "migrate" not in dockerfile.split("FROM ${PYTHON_IMAGE} AS runtime")[1].split("CMD")[-1]
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert {".git", ".env*", "var/"} <= set(ignored)


# ── nginx (structure) ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def nginx_conf() -> str:
    return (NGINX / "flarize.conf").read_text()


def _location_block(conf: str, header: str) -> str:
    start = conf.index(header)
    return conf[start : conf.index("}", start)]


def test_routing_matches_plan_5_4(nginx_conf):
    for location in (
        "location /api/public/v1/",
        "location /api/v1/",
        "location /api/agent/",
        "location /api/customer/",
        "location /api/docs/",
        "location = /healthz",
        "location /studio",
        "location / {",
    ):
        assert location in nginx_conf, location
    private = _location_block(nginx_conf, "location ^~ /media/private/")
    assert "internal;" in private and "alias /srv/flarize/media/private/;" in private


def test_rate_limits_and_body_sizes(nginx_conf):
    assert "zone=public:20m rate=20r/s" in nginx_conf and "zone=iclock:10m rate=5r/s" in nginx_conf and "zone=agent:10m rate=2r/s" in nginx_conf
    assert "limit_req zone=public burst=40 nodelay;" in _location_block(nginx_conf, "location /api/public/v1/ {")
    assert "client_max_body_size 2m;" in nginx_conf
    assert "client_max_body_size 20m;" in _location_block(nginx_conf, "location = /api/v1/media/upload/")
    assert "client_max_body_size 20m;" in _location_block(nginx_conf, "location = /api/public/v1/job-applications/")


def test_plain_http_listener_serves_only_iclock(nginx_conf):
    server = nginx_conf[nginx_conf.index("listen 8080;") :]
    locations = re.findall(r"location ([^{]+)\{", server)
    assert [location.strip() for location in locations] == ["^~ /iclock/", "/"]
    assert "return 404;" in server and "limit_req zone=iclock" in server


# ── nginx: the old website URLs (PLAN §6.2, DV-5) ──────────────────────────────────────────────────────────────────
def _map_entries(text: str, header: str) -> list[tuple[str, str]]:
    body = text[text.index(header) :]
    body = body[body.index("{") + 1 : body.index("\n}")]
    entries = []
    for line in body.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r'^("(?:[^"\\]|\\.)*"|\S+)\s+(\S+);$', line)
        assert match, line
        entries.append((match.group(1).strip('"'), match.group(2).strip('"')))
    return entries


GROUPS = _map_entries((NGINX / "legacy" / "groups.conf").read_text(), "map $uri $legacy_group")
SWITCH = dict(_map_entries((NGINX / "legacy" / "switch.conf").read_text(), "map $legacy_group $legacy_mode"))


def legacy_group(uri: str) -> str:
    """nginx `map` semantics: regexes in order of appearance, first match wins, else default."""
    default = ""
    for key, value in GROUPS:
        if key == "default":
            default = value
        elif key.startswith("~") and re.search(key[1:], uri):
            return value
    return default


OLD_URLS = [
    ("/studio-api/api/articles", "cms_content"),
    ("/studio-api/api/case-studies", "cms_content"),
    ("/studio-api/api/authors", "cms_content"),
    ("/studio-api/api/page-content", "cms_content"),
    ("/studio-api/api/faqs", "cms_content"),
    ("/studio-api/api/job-positions", "cms_content"),
    ("/studio-api/api/job-positions/sales-executive", "cms_content"),
    ("/studio-api/admin-api/auth/login/", "cms_other"),
    ("/api/calculate-solar/", "calculators"),
    ("/api/calculate-solar-new/", "calculators"),
    ("/api/calculate-solar-advanced/", "calculators"),
    ("/api/emi-calculator/", "calculators"),
    ("/api/emi-calculator/config/", "calculators"),
    ("/api/emi-calculator/quotation/", "calculators"),
    ("/api/emi-admin/banks/", "backend_other"),
    ("/api/send-otp/", "leads"),
    ("/api/verify-otp/", "leads"),
    ("/api/lead-collection-home/", "leads"),
    ("/api/job-applications/", "careers"),
    ("/api/solar-panels/", "products"),
    ("/api/solar-inverters/", "products"),
    ("/api/batteries/", "products"),
    ("/api/pincodes/", "reference"),
    ("/api/tariffs/", "reference"),
    ("/api/device-types/", "reference"),
    ("/api/wattages/", "reference"),
    ("/api/room-sizes/", "reference"),
    ("/api/ev-cars/", "reference"),
    ("/api/ev-scooters/", "reference"),
    ("/api/customer-installations/", "backend_other"),  # Studio-only in the legacy (401 anonymous): not shimmed
    ("/api/installation-stats", "installations"),
    ("/api/solar-panels/3/", "backend_other"),  # detail/Studio routes are not shimmed
    ("/api/job-applications/7/status/", "backend_other"),
    ("/api/emi-calculator/config", "calculators"),
    ("/studio-api/api/faqs/", "cms_other"),
    ("/studio-api/api/articles/", "cms_content"),
    ("/api/installation-stats/", "installations"),
    ("/api/affiliate-applications/", "forms"),
    ("/api/warranty-service-requests/", "forms"),
    ("/api/metadata/", "seo"),
    ("/bom/api/quotation-testimonials/", "bom_other"),  # joins bom_public when the quotations shim is wired
    ("/bom/api/quotation-settings/", "bom_public"),
    ("/bom/api/calculate/", "bom_calculate"),
    ("/bom/login/", "bom_other"),
    ("/somewhere/else/", ""),
    ("/api/v2/users/", ""),  # unknown API versions never reach a legacy backend
    ("/api/public/v9/company/", ""),
]


@pytest.mark.parametrize("uri,group", OLD_URLS)
def test_every_old_url_has_its_group(uri, group):
    assert legacy_group(uri) == group


def test_switch_covers_every_group_with_valid_modes():
    groups = {value for key, value in GROUPS if key != "default" and value}
    assert set(SWITCH) - {"default"} == groups
    assert set(SWITCH.values()) <= {"shim", "old", "gone"} and SWITCH["default"] == "gone"
    # Studio/admin/internal endpoints never get a shim (PLAN §6.2 "none").
    assert all(SWITCH[group] != "shim" for group in groups if group.endswith("_other"))


def test_shim_target_is_the_old_path_under_legacy():
    targets = dict(_map_entries((NGINX / "legacy" / "groups.conf").read_text(), 'map "$legacy_mode:$legacy_group" $legacy_target'))
    assert targets["~^shim:[a-z_]+$"] == "http://api/legacy$request_uri"
    assert targets["~^old:cms_[a-z_]+$"] == "http://legacy-cms:8000$legacy_cms_uri"
    assert targets["~^old:[a-z_]+$"] == "http://legacy-backend:8000$request_uri"
    routes = (NGINX / "legacy" / "routes.conf").read_text()
    assert routes.count('if ($legacy_target = "") { return 404; }') == 3


# ── nginx (live): real nginx against mock upstreams ─────────────────────────────────────────────────────────────────
nginx_binary = pytest.mark.skipif(not any((Path(path) / "nginx").exists() for path in os.environ.get("PATH", "").split(os.pathsep) + ["/usr/sbin"]), reason="nginx is not installed")


@nginx_binary
def test_live_nginx_routing(tmp_path):
    from core.tests.nginx_harness import NginxHarness

    with NginxHarness(tmp_path) as harness:
        expectations = [
            ("/api/v1/auth/me/", "api", "/api/v1/auth/me/"),
            ("/api/public/v1/company/?x=1", "api", "/api/public/v1/company/?x=1"),
            ("/api/calculate-solar/?kw=3", "legacy-backend", "/api/calculate-solar/?kw=3"),
            ("/studio-api/api/articles?populate=*", "legacy-cms", "/api/articles?populate=*"),
            ("/studio-api/admin-api/auth/login/", "legacy-cms", "/admin-api/auth/login/"),
            ("/bom/api/quotation-settings/", "legacy-backend", "/bom/api/quotation-settings/"),
            ("/", "frontend", "/"),
            ("/studio/login", "frontend", "/studio/login"),
        ]
        for path, upstream, received in expectations:
            status, body = harness.get(path)
            assert status == 200 and body["server"] == upstream and body["path"] == received, (path, body)
            assert body["x-forwarded-proto"] == "https" and len(body["x-request-id"]) == 32
        assert harness.get("/media/private/library/x.pdf")[0] == 404  # internal only
        assert harness.get("/healthz")[1]["server"] == "api"  # allowed from loopback
        status, body = harness.get("/iclock/cdata?SN=ABC", port="plain")
        assert status == 200 and (body["server"], body["path"], body["x-forwarded-proto"]) == ("api", "/iclock/cdata?SN=ABC", "http")
        assert harness.get("/api/v1/auth/me/", port="plain")[0] == 404  # :8080 serves /iclock/ only
        assert harness.get("/api/v2/auth/me/")[0] == 404  # unknown API version

        shimmed = {group: "shim" for group in SWITCH if group != "default" and not group.endswith("_other")}
        harness.set_switch({**shimmed, "cms_other": "old", "bom_other": "old", "backend_other": "gone"})
        assert harness.get("/api/calculate-solar/?kw=3")[1]["path"] == "/legacy/api/calculate-solar/?kw=3"
        body = harness.get("/bom/api/quotation-settings/")[1]
        assert (body["server"], body["path"]) == ("api", "/legacy/bom/api/quotation-settings/")
        assert harness.get("/studio-api/api/articles?populate=*")[1]["path"] == "/legacy/studio-api/api/articles?populate=*"
        assert harness.get("/studio-api/admin-api/auth/login/")[1]["server"] == "legacy-cms"  # never shimmed
        assert harness.get("/api/emi-admin/banks/")[0] == 404  # gone
        assert harness.get("/api/v1/auth/me/")[1]["server"] == "api"  # versioned surfaces unaffected


@nginx_binary
def test_live_nginx_access_log_redacts_capability_tokens(tmp_path):
    """Security review: nginx logged $request_uri, i.e. signed media/document tokens, per-device terminal tokens and
    customer link tokens in full. The upstream still receives the real path."""
    from core.tests.nginx_harness import NginxHarness

    requests = [
        ("/api/v1/media/download/SIGNED-MEDIA-TOKEN/", "https", "/api/v1/media/download/[redacted]/"),
        ("/api/v1/documents/download/SIGNED-DOC-TOKEN/", "https", "/api/v1/documents/download/[redacted]/"),
        ("/api/customer/v1/inspection-approvals/CUSTOMER-LINK-TOKEN/send-otp/", "https", "/api/customer/v1/inspection-approvals/[redacted]/send-otp/"),
        ("/api/public/v1/content/preview/BLOG-PREVIEW-TOKEN/", "https", "/api/public/v1/content/preview/[redacted]/"),
        ("/iclock/DEVICE-SECRET-TOKEN/cdata?SN=ABC", "plain", "/iclock/[redacted]/cdata?SN=ABC"),
        ("/api/v1/auth/me/?x=1", "https", "/api/v1/auth/me/?x=1"),
    ]
    with NginxHarness(tmp_path) as harness:
        for path, port, _ in requests:
            status, body = harness.get(path, port=port)
            assert status == 200 and body["path"] == path  # proxied untouched
    logged = [json.loads(line)["uri"] for line in (tmp_path / "logs" / "access.log").read_text().splitlines()]
    assert logged == [expected for _, _, expected in requests]
    assert "TOKEN" not in (tmp_path / "logs" / "access.log").read_text()


ERROR_CODES = {403: "permission_denied", 413: "request_too_large", 429: "throttled", 502: "bad_gateway", 503: "service_unavailable", 504: "gateway_timeout"}
API_LOCATIONS = [
    "location /api/public/v1/ {",
    "location = /api/public/v1/job-applications/ {",
    "location /api/v1/ {",
    "location = /api/v1/media/upload/ {",
    "location /api/agent/ {",
    "location /api/customer/ {",
    "location /api/docs/ {",
    "location /api/schema/ {",
    "location = /healthz {",
]


def test_nginx_errors_on_the_api_use_the_json_envelope(nginx_conf):
    """413 (body size), 429 (limit_req), 502/503/504 (upstream) and 403 (docs allow-list) are produced by nginx itself."""
    for header in API_LOCATIONS:
        assert "include /etc/nginx/flarize/snippets/api-errors.conf;" in _location_block(nginx_conf, header), header
    assert "include /etc/nginx/flarize/snippets/api-error-pages.conf;" in nginx_conf
    assert "proxy_intercept_errors" not in nginx_conf  # Django's own error responses pass through untouched
    errors = (NGINX / "snippets" / "api-errors.conf").read_text()
    pages = (NGINX / "snippets" / "api-error-pages.conf").read_text()
    for status, code in ERROR_CODES.items():
        assert f"error_page {status} = @api_{status};" in errors
        block = pages[pages.index(f"location @api_{status} {{") :]
        block = block[: block.index("\n}")]
        body = json.loads(re.search(rf"return {status} '(.+)';", block).group(1))
        assert set(body) == {"code", "message", "errors", "error_codes"} and body["code"] == code and body["error_codes"] == [code]
        assert "default_type application/json;" in block and "X-Request-ID $request_id" in block


@nginx_binary
def test_live_nginx_errors_are_json(tmp_path):
    from core.tests.nginx_harness import NginxHarness

    with NginxHarness(tmp_path) as harness:
        status, headers, body = harness.raw("POST", "/api/v1/auth/login/", headers={"Content-Type": "application/json", "Content-Length": str(3 * 1024 * 1024)})
        assert (status, headers["content-type"], json.loads(body)["code"]) == (413, "application/json", "request_too_large")
        assert len(headers["x-request-id"]) == 32

        # Django's own errors are never replaced (e.g. /healthz answering 503 with its checks).
        status, headers, body = harness.raw("GET", "/api/v1/auth/me/?upstream_status=503")
        assert status == 503 and json.loads(body)["server"] == "api"

        limited = [harness.raw("GET", "/api/agent/v1/ping/") for _ in range(40)]  # agent zone: 2 r/s, burst 10
        status, headers, body = next(result for result in limited if result[0] == 429)
        assert headers["content-type"] == "application/json" and json.loads(body)["code"] == "throttled" and headers["retry-after"] == "1"

        status, headers, body = harness.raw("GET", "/api/docs/", headers={"X-Forwarded-For": "203.0.113.9"})
        assert status == 200  # loopback is allow-listed; X-Forwarded-For from a client is not trusted by nginx

        harness.stop_upstream("api")
        status, headers, body = harness.raw("GET", "/api/v1/auth/me/")
        assert (status, headers["content-type"], json.loads(body)["code"]) == (502, "application/json", "bad_gateway")


# ── release script ──────────────────────────────────────────────────────────────────────────────────────────────────
FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
args="$*"
case "$args" in
  *" ps -q "*) echo "cid-${@: -1}" ;;
  "inspect -f "*) echo "${FAKE_HEALTH:-healthy}" ;;
esac
exit 0
"""
FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
url="${@: -1}"
case "$url" in
  */healthz) echo '{"status": "ok", "checks": {}}' ;;
  */auth/login/) echo '{"access":"ACCESS","refresh":"REFRESH"}' ;;
esac
exit 0
"""


def _fake_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def _release(tmp_path: Path, *args: str, **env) -> subprocess.CompletedProcess:
    state = tmp_path / "state"
    smoke = tmp_path / "smoke.env"
    smoke.write_text('SMOKE_EMAIL="smoke@flarize.com"\nSMOKE_PASSWORD="p\\"ss word"\n')
    environment = {
        "PATH": f"{_fake_bin(tmp_path)}:{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "STATE_DIR": str(state),
        "SMOKE_ENV_FILE": str(smoke),
        "HEALTH_TIMEOUT": "1",
        **env,
    }
    return subprocess.run(["bash", str(DEPLOY / "release.sh"), *args], env=environment, capture_output=True, text=True, timeout=60)


def test_release_runs_every_step_in_order(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "current").write_text("1111111\n")
    result = _release(tmp_path, "abcdef1")
    assert result.returncode == 0, result.stderr + result.stdout
    calls = (tmp_path / "calls.log").read_text().splitlines()
    order = [
        next(i for i, call in enumerate(calls) if " pull api-a api-b worker-default worker-documents beat migrate" in call),
        next(i for i, call in enumerate(calls) if "run --rm migrate python manage.py migrate --noinput" in call),
        next(i for i, call in enumerate(calls) if "ensure_audit_partitions --months 3" in call),
        next(i for i, call in enumerate(calls) if "run --rm migrate python manage.py seed_pages" in call),  # maintained-pages registry
        next(i for i, call in enumerate(calls) if "up -d --no-deps --force-recreate api-a" in call),
        next(i for i, call in enumerate(calls) if "inspect -f {{.State.Health.Status}} cid-api-a" in call),
        next(i for i, call in enumerate(calls) if "up -d --no-deps --force-recreate api-b" in call),
        next(i for i, call in enumerate(calls) if "up -d --no-deps --force-recreate worker-default worker-documents beat" in call),
        next(i for i, call in enumerate(calls) if call.startswith("curl") and call.endswith("/healthz")),
        next(i for i, call in enumerate(calls) if call.endswith("/api/public/v1/company/")),
        next(i for i, call in enumerate(calls) if call.endswith("/api/v1/auth/me/")),
    ]
    assert order == sorted(order)
    login = next(call for call in calls if call.endswith("/api/v1/auth/login/"))
    assert '"password": "p\\"ss word"' in login  # JSON-encoded, not string-spliced
    assert (tmp_path / "state" / "current").read_text().strip() == "abcdef1"
    assert "abcdef1 previous=1111111" in (tmp_path / "state" / "history").read_text()


def test_release_stops_and_prints_the_rollback_when_a_replica_is_unhealthy(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "current").write_text("1111111\n")
    result = _release(tmp_path, "abcdef1", FAKE_HEALTH="unhealthy")
    assert result.returncode != 0
    assert "FAILED during: rolling restart of api-a" in result.stderr and "deploy/release.sh 1111111" in result.stderr
    calls = (tmp_path / "calls.log").read_text()
    assert "force-recreate api-b" not in calls  # the second replica keeps serving the old release
    assert (tmp_path / "state" / "current").read_text().strip() == "1111111"


def test_release_refuses_a_non_sha(tmp_path):
    assert _release(tmp_path, "latest").returncode == 64


def test_shell_scripts_parse():
    for script in (DEPLOY / "release.sh", DEPLOY / "scripts" / "healthz-check.sh"):
        assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0, script
        assert os.access(script, os.X_OK), f"{script} must be executable"


# ── CI helpers ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _view_budget_module():
    spec = importlib.util.spec_from_file_location("check_view_budget", ROOT / "scripts" / "check_view_budget.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_view_budget_passes_on_this_repository():
    assert _view_budget_module().over_budget(ROOT) == []


def test_view_budget_flags_long_view_files(tmp_path, capsys):
    module = _view_budget_module()
    (tmp_path / "shop" / "views").mkdir(parents=True)
    (tmp_path / "shop" / "views" / "big.py").write_text("x = 1\n" * 251)
    (tmp_path / "shop" / "views" / "ok.py").write_text("x = 1\n" * 250)
    (tmp_path / "shop" / "services").mkdir()
    (tmp_path / "shop" / "services" / "long.py").write_text("x = 1\n" * 900)
    assert module.over_budget(tmp_path) == [("shop/views/big.py", 251)]
    assert module.main([str(tmp_path)]) == 1 and "shop/views/big.py: 251 lines" in capsys.readouterr().err
    assert module.main([str(tmp_path), "--max-lines", "300"]) == 0


def test_ci_workflow_runs_every_gate():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    steps = " ".join(str(step.get("run", "")) + str(step.get("uses", "")) for job in workflow["jobs"].values() for step in job["steps"])
    for gate in (
        "black --check",
        "isort --check-only",
        "flake8",
        "lint-imports",
        "makemigrations --check --dry-run",
        "pytest",
        "--fail-under=85",
        "gitleaks",
        "check_view_budget.py",
        "check_task_enqueues.py",
    ):
        assert gate in steps, gate
    services = workflow["jobs"]["test"]["services"]
    assert services["postgres"]["image"].startswith("postgres:16") and services["redis"]["image"].startswith("redis:7")
    hooks = (ROOT / ".pre-commit-config.yaml").read_text()
    for hook in ("black", "isort", "flake8", "gitleaks", "forbid-secret-files"):
        assert hook in hooks


# ── CI: Celery tasks are enqueued only inside transaction.on_commit (standard §7.2, §8) ────────────────────────────
def _enqueue_module():
    spec = importlib.util.spec_from_file_location("check_task_enqueues", ROOT / "scripts" / "check_task_enqueues.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_enqueue_check_passes_on_this_repository():
    assert _enqueue_module().violations(ROOT) == []


GOOD_ENQUEUES = {
    "shop/services/orders.py": """
from functools import partial
from django.db import transaction
from shop.tasks import notify, reindex, render


def _enqueue_render(uid):
    render.delay(uid)


class Orders:
    def _enqueue_reindex(self):
        reindex.apply_async(countdown=5)

    def save(self, uid):
        transaction.on_commit(partial(_enqueue_render, uid), robust=True)
        transaction.on_commit(self._enqueue_reindex)
        transaction.on_commit(lambda: notify.delay(uid))
        transaction.on_commit(partial(notify.delay, uid))
""",
    "shop/tasks.py": """
from celery import shared_task


@shared_task
def cleanup():
    reindex.delay()  # tasks may chain tasks: they run in the worker, not in a request transaction
""",
    "shop/tests/test_orders.py": "from shop.tasks import render\n\n\ndef test_x():\n    render.delay(1)\n",
}

BAD_ENQUEUES = {
    "shop/services/bare.py": ("from shop.tasks import render\n\n\ndef issue(uid):\n    render.delay(uid)\n", "shop/services/bare.py:5"),
    "shop/services/module_level.py": ("from shop.tasks import render\n\nrender.apply_async()\n", "shop/services/module_level.py:3"),
    "shop/services/leaky.py": (
        "from django.db import transaction\nfrom shop.tasks import render\n\n\ndef _enqueue_leaky(uid):\n    render.delay(uid)\n\n\n"
        "def issue(uid):\n    transaction.on_commit(lambda: _enqueue_leaky(uid))\n    _enqueue_leaky(uid)\n",
        "shop/services/leaky.py:6",
    ),
    "shop/views/unused.py": ("from shop.tasks import render\n\n\ndef _enqueue_nobody_schedules(uid):\n    render.delay(uid)\n", "shop/views/unused.py:5"),
    "shop/services/alias.py": ("from shop.tasks import render\n\nsend = render.delay\n", "shop/services/alias.py:3"),
}


def test_enqueue_check_accepts_on_commit_patterns(tmp_path):
    module = _enqueue_module()
    for relative, source in GOOD_ENQUEUES.items():
        _write(tmp_path, relative, source)
    assert module.violations(tmp_path) == []


@pytest.mark.parametrize("relative", sorted(BAD_ENQUEUES))
def test_enqueue_check_flags_enqueues_outside_on_commit(tmp_path, relative, capsys):
    module = _enqueue_module()
    source, location = BAD_ENQUEUES[relative]
    _write(tmp_path, relative, source)
    found = module.violations(tmp_path)
    assert [violation.split(" ", 1)[0] for violation in found] == [location], found
    assert module.main([str(tmp_path)]) == 1 and location in capsys.readouterr().err
