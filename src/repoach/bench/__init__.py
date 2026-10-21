"""Precision and recall of findings on a labelled corpus (SP-PRECISION-MEASUREMENT).

``corpus`` reads and validates the case files under ``bench/corpus/``,
``runner`` reviews each case through the real pipeline and records its
findings, ``score`` turns a recorded run plus the human labels into the
per-reviewer and per-severity tables, and ``digest`` reads the ledger
for what the gate refused.
"""
