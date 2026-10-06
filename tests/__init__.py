"""Test isolation, set before any test module imports the app.

``unittest discover`` imports this package first, so every module's tests use a throwaway database and .env, even
when a module that sorts early (test_access, test_billing, ...) imports ``backend`` before test_core sets its own
paths. backend.db and backend.config read these variables once, at import: setting them later has no effect.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="jobhunt-tests-")
os.environ["JOBHUNT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ.pop("DATABASE_URL", None)  # would override JOBHUNT_DB_PATH
os.environ["JOBHUNT_ENV_PATH"] = os.path.join(_TMP, "test.env")  # never read or write the real .env
os.environ["JOBHUNT_SCHEDULER"] = "off"  # tests drive backend.schedule.tick() themselves
