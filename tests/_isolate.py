"""Point this test process -- and every process it starts -- at a throwaway home.

The hooks resolve their settings file and state from HOME (USERPROFILE on Windows) and
HYPERMNESIA_HOME, and HYPERMNESIA_ENV_FILE names the settings file outright. A test that overrides
HOME alone still inherits an exported HYPERMNESIA_HOME, and then writes profile caches, raises and
clears fault markers, and can retire a queue in the real state directory of the machine running
it. Importing this module first replaces all of them, before any hook module computes its paths:

    import _isolate  # noqa: F401  (first: before anything from hooks/ or ingest/ is imported)

The git identity is set here as well, because the real ~/.gitconfig is no longer visible to the
fixtures that commit.
"""
import os
import tempfile

HOME = tempfile.mkdtemp(prefix="hm-test-home-")
os.environ["HOME"] = HOME
os.environ["USERPROFILE"] = HOME
os.environ["HYPERMNESIA_HOME"] = os.path.join(HOME, ".HyperMnesia")
os.environ.pop("HYPERMNESIA_ENV_FILE", None)
for k, v in (("GIT_AUTHOR_NAME", "test"), ("GIT_AUTHOR_EMAIL", "test@example.invalid"),
             ("GIT_COMMITTER_NAME", "test"), ("GIT_COMMITTER_EMAIL", "test@example.invalid")):
    os.environ.setdefault(k, v)


def _cleanup():
    import shutil
    shutil.rmtree(HOME, ignore_errors=True)


import atexit  # noqa: E402
atexit.register(_cleanup)
