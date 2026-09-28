"""Company services: every write and non-trivial read of this app.

* ``profile`` — the company profile singleton (+ the typed quotation offer settings);
* ``bank_accounts`` — accounts with one primary; ``integrations`` — Fernet-encrypted provider settings and the
  :mod:`core.integrations` resolver.
"""
