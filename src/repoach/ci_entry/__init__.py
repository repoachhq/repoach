"""Run repoach from any CI: one entry point, not a per-project shell driver.

SP-UNIVERSAL-CI-ENTRY. :mod:`.context` reads the forge and the pull
request from the CI environment, :mod:`.advisory` decides and runs an
advisory review, :mod:`.gateway` starts a loopback gateway when the job
holds a provider key instead of a gateway, :mod:`.snippets` renders the
GitLab and GitHub jobs ``repoach init --ci`` writes, and :mod:`.cli`
exposes ``repoach ci review``.
"""
