"""Catalog services: every write and non-trivial read of this app.

Public entry points other contexts call (they may read the product master; they never write it):

* :func:`assert_selectable` — raise unless a component may go into a new pack, BOM or quotation (RETIRED and deleted
  components are refused); :func:`selection_warning` — the DEPRECATED warning with the suggested replacement;
* ``catalog.services.usage.register(name)(fn)`` — tell the catalog where components are used (delete guard, usage/);
* ``catalog.services.pricing_hooks.register(fn)`` — the price provider behind the public product pages.
"""

from catalog.services.lifecycle import ComponentNotSelectable, assert_selectable, selection_warning

__all__ = ["ComponentNotSelectable", "assert_selectable", "selection_warning"]
