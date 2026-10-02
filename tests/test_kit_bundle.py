"""What leaves your machine on ``pawabase deploy``."""

import os
import textwrap

import pytest

from pawabase import bundle


def project(tmp_path, files):
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text))
    return tmp_path


BASE = {"functions/a.py": "X = 1\n", "functions/b.py": "Y = 2\n"}


def test_the_same_source_always_gives_the_same_bytes(tmp_path):
    root = project(tmp_path, BASE)
    first = bundle.build(root)
    os.utime(root / "functions/a.py", (1, 1))  # a different mtime must not matter
    second = bundle.build(root)
    assert first.checksum == second.checksum and first.archive == second.archive
    (root / "functions/a.py").write_text("X = 3\n")
    assert bundle.build(root).checksum != first.checksum


def test_layout_and_what_is_left_out(tmp_path):
    root = project(tmp_path, {**BASE, "functions/__pycache__/a.cpython-312.pyc": "x", "functions/.hidden": "x", "functions/tests/test_a.py": "x",
                              "functions/test_b.py": "x", "functions/data.json": "{}", "pkg/__init__.py": "", "pkg/mod.py": "Z = 1\n", "pkg/tests/test_mod.py": "x"})
    packed = bundle.build(root, include=["pkg"])
    names = sorted(bundle.read(packed.archive))
    assert names == ["functions/a.py", "functions/b.py", "functions/data.json", "pkg/__init__.py", "pkg/mod.py", "pkg/tests/test_mod.py"]
    assert any("test_a.py" in s for s in packed.skipped) and any(".hidden" in s for s in packed.skipped)


def test_credentials_stop_the_deploy_and_name_the_file_and_line(tmp_path):
    root = project(tmp_path, {**BASE, "functions/.env": "PAWABASE_API_KEY=pb_sk_aaaaaaaaaaaaaaaaaaaaaaaa\n"})
    with pytest.raises(bundle.BundleError, match=r"\.env"):
        bundle.build(root)
    root2 = project(tmp_path / "two", {**BASE, "functions/c.py": 'KEY = "pb_sk_abcdefghijklmnopqrstuvwx"\n'})
    with pytest.raises(bundle.BundleError) as raised:
        bundle.build(root2)
    assert "functions/c.py:1" in str(raised.value) and "secret key" in str(raised.value)
    assert bundle.build(root2, allow_secrets=True).files  # the explicit opt-out
    root3 = project(tmp_path / "three", {**BASE, "functions/key.pem": "-----BEGIN PRIVATE KEY-----"})
    with pytest.raises(bundle.BundleError):
        bundle.build(root3)
    root4 = project(tmp_path / "four", {**BASE, "functions/.env.example": "PAWABASE_API_KEY=\n"})
    assert "functions/.env.example" not in bundle.read(bundle.build(root4).archive)  # documents a setting, is not one


def test_nothing_to_deploy_and_bad_includes_are_clear_errors(tmp_path):
    with pytest.raises(bundle.BundleError, match="pawabase init"):
        bundle.build(tmp_path)
    (tmp_path / "functions").mkdir()
    with pytest.raises(bundle.BundleError, match="no .py files"):
        bundle.build(tmp_path)
    project(tmp_path, BASE)
    with pytest.raises(bundle.BundleError, match="does not exist"):
        bundle.build(tmp_path, include=["missing"])
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "functions").mkdir()
    with pytest.raises(bundle.BundleError, match="already taken"):
        bundle.build(tmp_path, include=["other/functions"])


def test_symlinks_are_never_followed_out_of_the_project(tmp_path):
    root = project(tmp_path, BASE)
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret")
    os.symlink(outside, root / "functions" / "link.txt")
    packed = bundle.build(root)
    assert "functions/link.txt" not in bundle.read(packed.archive)
    assert any("symbolic link" in s for s in packed.skipped)
