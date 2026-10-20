"""``python -m repoach``: the same entry point as the ``repoach`` console script.

``repoach ci review`` starts its review child this way; ``-m repoach.cli.main``
re-ran an already imported module and printed runpy's RuntimeWarning.
"""

from repoach.cli import app

if __name__ == "__main__":
    app()
