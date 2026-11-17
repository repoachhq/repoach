"""Which CI jobs a diff affects: one table, one reader (SP-AFFECTED-JOBS-TABLE).

:mod:`.table` reads ``.github/affected-jobs.toml``; :mod:`.select`
turns changed paths into the selected ``ci.yml`` jobs; :mod:`.cli` is
``repoach ci affected``.
"""
