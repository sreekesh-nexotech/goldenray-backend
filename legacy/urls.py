"""Legacy shim URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: ``/legacy/…`` only (``legacy_urlpatterns``).

DRF views mounted here must set ``versioning_class = None``: under ``URLPathVersioning`` a DRF view reached without
a ``version`` kwarg answers 404 by design (strict versioning), and legacy paths carry no version.
"""

staff_urlpatterns: list = []
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
legacy_urlpatterns: list = []  # old contracts mounted at /legacy/ (flag LEGACY_API_SHIM)
