"""The package's own identity: a release, a digest of the source and the tags.

Content rather than a revision, for the same reason the deployment stamps its
binaries that way: what runs *is* the working tree, and an edit to a parser is a
different build. A revision would call the two the same and skip an upgrade that
was due.

The digest is over this package's own modules in name order, so the same files
listed differently are the same build, and the feature tags ride along — a
different capability set would be a different build.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

#: The released version. Must match ``project.version`` in ``pyproject.toml``:
#: ``tests/test_version.py`` fails if the two drift, so there is one answer.
RELEASE = "0.2.0"

#: A build's capability set. Empty means ``core``: the package has no optional
#: build variants, and inventing a tag for one build would make the stamp lie.
TAGS: tuple[str, ...] = ()

_PACKAGE = Path(__file__).parent


def source_digest(root: Path) -> str:
    """The digest of every module under ``root``: name, then bytes.

    The *name* is hashed as well, so moving code between modules is a change
    even when the total bytes are not.
    """
    parts: list[bytes] = []
    for path in sorted(root.glob("*.py")):
        parts.append(path.name.encode())
        parts.append(path.read_bytes())
    return hashlib.sha256(b"\0".join(parts)).hexdigest()


def version_id(root: Path | None = None) -> str:
    """``RELEASE-<digest16>-<tags|core>`` for a build of this package.

    Defaults to this installed package; ``root`` answers the same question for
    a checkout, which is what makes "is this host running what that tree
    builds?" answerable without installing it.
    """
    digest = source_digest(_PACKAGE if root is None else root)
    return f"{RELEASE}-{digest[:16]}-{'+'.join(TAGS) or 'core'}"
