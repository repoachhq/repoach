"""Project configuration: the committed ``repoach.toml`` of a host repository.

SP-UNIVERSAL-PROJECT-CONFIG. :mod:`repoach.project.config` owns the file
model and its loader, :mod:`repoach.project.resolve` layers the
environment and the origin remote on top of it, :mod:`repoach.project.measure`
and :mod:`repoach.project.toml_write` produce the file ``repoach init``
writes, and :mod:`repoach.project.spec_source` decides which spec a pull
request is reviewed against.  This package imports nothing at load time
so :mod:`repoach.core.config` and :mod:`repoach.review.spec` can depend on
:mod:`repoach.project.config` without a cycle.
"""
