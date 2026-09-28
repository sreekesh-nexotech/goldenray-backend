"""Devices URL lists, mounted by flarize/urls.py under the versioned surfaces.

Owns: staff ``devices/``; agent protocol; ``/iclock/<device_token>/`` terminals.
"""

staff_urlpatterns: list = []
public_urlpatterns: list = []
agent_urlpatterns: list = []
customer_urlpatterns: list = []
iclock_urlpatterns: list = []  # plain Django views mounted at /iclock/<device_token>/ (flag ADMS_RECEIVER)
