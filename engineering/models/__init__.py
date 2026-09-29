"""Engineering models (PLAN §2.5): rule sets, checker runs, findings and their acknowledgements.

* ``engineering_rule_set`` — a versioned rule document (``engines.engineering_checker.RuleSet.as_json()``); exactly
  one ACTIVE set per engine (partial unique index), the seed is ``phase1e.1`` (35 PBC rules) plus the ENG register
  ``phase1e.eng``.
* ``engineering_run`` — one checker pass over a subject (a pack config version, a project BOM, a quotation draft):
  ``result`` PASS/WARN/FAIL and ``summary`` (counts, rules version, per-pack verdicts for pack config versions).
* ``engineering_finding`` — one finding (``Finding.as_row()``) with a stable ``identity`` (rule, components, pack) so an
  acknowledgement given on one run carries to the next run of the same subject.
* ``engineering_acknowledgement`` — one per finding: who accepted it and why (a WARN acknowledged, a BLOCK waived).
"""

from engineering.models.choices import Engine, RunResult, Severity, SubjectType
from engineering.models.models import Acknowledgement, Finding, RuleSet, Run

__all__ = ["Acknowledgement", "Engine", "Finding", "Run", "RuleSet", "RunResult", "Severity", "SubjectType"]
