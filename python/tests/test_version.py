"""The package's own identity: a release, a source digest and the tags."""
from __future__ import annotations

import tomllib
from pathlib import Path

from cisco_exporter.version import RELEASE, TAGS, source_digest, version_id

#: This package's own manifest, so the release has exactly one answer.
_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def test_the_release_matches_pyproject() -> None:
    """Two versions in one repository drift. Reading the other one here is what
    stops the stamp from naming a release nobody published."""
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    assert declared["project"]["version"] == RELEASE


def test_version_id_is_release_digest_and_tags() -> None:
    release, digest, tags = version_id().split("-")
    assert release == RELEASE
    assert len(digest) == 16
    int(digest, 16)  # hex, not a word that merely looks like one
    # No tags means `core`: the field is never empty, which would be a
    # different shape for whatever parses this line.
    assert tags == ("+".join(TAGS) or "core")


def test_version_id_is_stable_for_the_same_tree() -> None:
    assert version_id() == version_id()


def test_source_digest_follows_content_and_names(tmp_path: Path) -> None:
    """A build is its bytes: an edit to one module is a new build, and so is
    moving code to another module even though the total bytes are the same."""
    module = tmp_path / "one.py"
    module.write_text("a = 1\n", encoding="utf-8")
    before = source_digest(tmp_path)

    module.write_text("a = 2\n", encoding="utf-8")
    assert source_digest(tmp_path) != before

    module.rename(tmp_path / "moved.py")
    assert source_digest(tmp_path) != before

    (tmp_path / "two.py").write_text("b = 1\n", encoding="utf-8")
    with_two = source_digest(tmp_path)
    (tmp_path / "notes.txt").write_text("not a module\n", encoding="utf-8")
    assert source_digest(tmp_path) == with_two  # only modules are the build
