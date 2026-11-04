"""Fallback temp directory for constrained sessions.

Why this file exists
--------------------
This project is sometimes driven from a sandboxed session (agent shell, low-integrity
runner) in which ``python.exe`` starts at LOW mandatory integrity while ``%TEMP%`` is a
normal MEDIUM directory. Windows Mandatory Integrity Control forbids writing "up", so
every code path that needs a temp file fails. Observed symptoms:

* ``pip`` dies with ``PermissionError: ...\\pip-unpack-*.whl``, or hangs at 100% CPU
  while ``tempfile`` keeps retrying directory creation;
* ``jieba`` cannot write its cache and silently degrades to the builtin rule engine, so
  ``scripts/check_nlp.py`` reports ``backend: builtin`` although jieba is installed and
  importable.

Both are cured by pointing ``tempfile`` at a directory the process can actually write:
``<project>/.pytemp``. ``scripts/setup.ps1`` creates that directory and gives it a LOW
integrity label, so a LOW-integrity process may create its files there.

The probe below is cheap (one temp file) and does nothing on a normal MEDIUM-integrity
terminal, where ``%TEMP%`` already works. ``scripts/setup.ps1`` copies this file into the
venv's ``Lib/site-packages`` so it applies to every Python entry point using that
interpreter. Keep the source of truth here in ``scripts/``; the venv copy is generated.
"""

import os
import tempfile


def _usable_tempdir() -> bool:
    try:
        tempfile.gettempdir()
        fd, probe = tempfile.mkstemp(prefix=".temp-probe-")
        os.close(fd)
        os.unlink(probe)
        return True
    except Exception:
        return False


def _install_fallback() -> None:
    if _usable_tempdir():
        return

    # This file is copied to <project>/<venv>/Lib/site-packages/sitecustomize.py
    site_packages = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.dirname(site_packages)
    venv = os.path.dirname(lib)
    project = os.path.dirname(venv)

    candidate = os.path.join(project, ".pytemp")
    try:
        os.makedirs(candidate, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".temp-probe-", dir=candidate)
        os.close(fd)
        os.unlink(probe)
    except Exception:
        return

    tempfile.tempdir = candidate
    os.environ["TMP"] = candidate
    os.environ["TEMP"] = candidate


try:
    _install_fallback()
except Exception:
    # Never break interpreter startup because of this convenience fallback.
    pass