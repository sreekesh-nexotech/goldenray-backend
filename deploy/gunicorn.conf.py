"""Gunicorn settings for the api service (PLAN §5.2: gthread, 4 workers × 8 threads, /healthz readiness).

Access logging is left to Django (one JSON line per request with the request id, core.middleware); gunicorn logs
only errors, to stderr. Timeouts stay under nginx's proxy_read_timeout (60 s).
"""

import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "55"))
graceful_timeout = 30
keepalive = 5
# Recycle workers now and then (bounded memory growth), staggered so replicas never restart together.
max_requests = 2000
max_requests_jitter = 200
worker_tmp_dir = "/dev/shm"
accesslog = None
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "warning")
# Django decides which proxies to trust (TRUSTED_PROXIES / SECURE_PROXY_SSL_HEADER); gunicorn stays out of it.
forwarded_allow_ips = "127.0.0.1"
proc_name = "flarize-api"
