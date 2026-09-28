"""Documents services: every write and non-trivial read of this app.

* ``jobs`` — :func:`jobs.request_render` (called by owning contexts), the task body, the sweeper, queue stats;
* ``downloads`` — single-use signed download links (``signed_url`` / ``redeem``);
* ``renderers`` — Playwright and stub renderers, page counting; ``templates`` — template resolution/rendering.
"""
