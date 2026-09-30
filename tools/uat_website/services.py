"""Start/stop every UAT process: platform :18300, private legacy backend :18322, nginx :18310/:18320, Next.js :18311/:18321.
Usage: services.py start|stop|status"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

U = Path(os.environ["UAT_DIR"])  # scratch folder: web-legacy/, web-platform/, nginx-*/ (gen_nginx.py), logs, pids
REPO = str(Path(__file__).resolve().parents[2])
PY = os.environ.get("PLATFORM_PYTHON", "/home/user/.venvs/platform/bin/python")
LEGACY_PY = os.environ.get("LEGACY_PYTHON", "/home/user/.venvs/legacy-backend/bin/python")
LEGACY_DIR = os.environ.get("LEGACY_BACKEND_DIR", "/home/user/goldenray/goldenray-backend/backend")
PIDS = U / "pids"
PIDS.mkdir(exist_ok=True)
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
LEGACY_ENV = {
    "PGPASSWORD": "postgres",
    "DB_USER": "postgres",
    "DB_PASSWORD": "postgres",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_ENGINE": "django.db.backends.postgresql",
    "DJANGO_ENV": "development",
    "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
    "DJANGO_SECRET_KEY": "uat-legacy-secret",
    "TWILIO_ACCOUNT_SID": "ACfake",
    "TWILIO_AUTH_TOKEN": "fake",
    "TWILIO_VERIFY_SERVICE_SID": "VAfake",
    "STUDIO_API_KEY": "uat",
    "STUDIO_JWT_SIGNING_KEY": "uat-shared-signing-key",
    "DB_NAME": "uat_legacy_backend",
    # no outbound network for the private legacy server: its Twilio calls can never leave the machine
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "HTTP_PROXY": "http://127.0.0.1:9",
    "https_proxy": "http://127.0.0.1:9",
    "http_proxy": "http://127.0.0.1:9",
    "NO_PROXY": "localhost,127.0.0.1",
    "no_proxy": "localhost,127.0.0.1",
}
PLATFORM_ENV = {
    "DJANGO_SETTINGS_MODULE": "flarize.settings.dev",
    "DB_NAME": "uat_platform",
    "DB_PASSWORD": "postgres",
    "MEDIA_ROOT": str(U / "media"),
    "REDIS_URL": "redis://localhost:6379/12",
    "FRONTEND_BASE_URL": "http://localhost:3000",
    "DEBUG": "False",
    "ALLOWED_HOSTS": "127.0.0.1,localhost",
}


def next_env(port):
    return {"PATH": "/opt/node22/bin:" + os.environ["PATH"], "NEXT_TELEMETRY_DISABLED": "1", "PUPPETEER_EXECUTABLE_PATH": CHROME, "PDF_RENDER_ORIGIN": f"http://127.0.0.1:{port}"}


SERVICES = {
    "platform": (18300, [PY, "manage.py", "runserver", "127.0.0.1:18300", "--noreload"], REPO, PLATFORM_ENV),
    "legacy-backend": (18322, [LEGACY_PY, "manage.py", "runserver", "127.0.0.1:18322", "--noreload"], LEGACY_DIR, LEGACY_ENV),
    "next-platform": (18311, ["./node_modules/.bin/next", "start", "-p", "18311", "-H", "127.0.0.1"], str(U / "web-platform"), next_env(18311)),
    "next-legacy": (18321, ["./node_modules/.bin/next", "start", "-p", "18321", "-H", "127.0.0.1"], str(U / "web-legacy"), next_env(18321)),
    "nginx-platform": (18310, ["nginx", "-g", "daemon off;", "-c", str(U / "nginx-platform/nginx.conf"), "-e", str(U / "nginx-platform/logs/error.log")], str(U), {}),
    "nginx-legacy": (18320, ["nginx", "-g", "daemon off;", "-c", str(U / "nginx-legacy/nginx.conf"), "-e", str(U / "nginx-legacy/logs/error.log")], str(U), {}),
}


def up(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start(names):
    for name in names:
        port, cmd, cwd, env = SERVICES[name]
        if up(port):
            print(name, "already up")
            continue
        log = open(U / f"{name}.log", "ab")
        p = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **env}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (PIDS / name).write_text(str(p.pid))
    for name in names:
        port = SERVICES[name][0]
        for _ in range(60):
            if up(port):
                break
            time.sleep(1)
        print(name, port, "up" if up(port) else "DOWN")


def stop(names):
    for name in names:
        f = PIDS / name
        if f.exists():
            try:
                os.killpg(int(f.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
            f.unlink()
    time.sleep(2)
    for name in names:
        print(name, "up" if up(SERVICES[name][0]) else "down")


cmd = sys.argv[1]
names = sys.argv[2:] or list(SERVICES)
{"start": start, "stop": stop, "status": lambda n: [print(x, "up" if up(SERVICES[x][0]) else "down") for x in n]}[cmd](names)
