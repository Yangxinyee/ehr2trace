"""A check that raises NameError reports itself as a failed check, not as a crash, so
an import slip can ship with the tests green. This reads every module the way the
interpreter would and refuses any name that nothing defines."""
from pathlib import Path

import pytest

pyflakes_api = pytest.importorskip("pyflakes.api")
from pyflakes import messages, reporter  # noqa: E402


class _Undefined(reporter.Reporter):
    def __init__(self):
        super().__init__(None, None)
        self.found: list[str] = []

    def flake(self, message):
        if isinstance(message, messages.UndefinedName):
            self.found.append(f"{message.filename}:{message.lineno}: {message.message % message.message_args}")

    def unexpectedError(self, filename, msg):
        self.found.append(f"{filename}: {msg}")

    def syntaxError(self, filename, msg, lineno, offset, text):
        self.found.append(f"{filename}:{lineno}: {msg}")


def test_every_name_the_package_uses_is_defined():
    package = Path(__file__).resolve().parents[2] / "src" / "ehr2trace"
    rep = _Undefined()
    for path in sorted(package.rglob("*.py")):
        pyflakes_api.checkPath(str(path), rep)
    assert not rep.found, "\n".join(rep.found)
