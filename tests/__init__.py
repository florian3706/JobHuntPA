"""Test isolation, set before any test module imports the app.

``unittest discover`` imports this package first, so every module's tests use a throwaway database and .env, even
when a module that sorts early (test_accessibility, test_accounts, ...) imports ``backend`` before test_core sets its own
paths. backend.db and backend.config read these variables once, at import: setting them later has no effect.

The server key file and the first-run token file (which sits beside the database) are throwaway too, and the app
makes its account from the environment (tests/helpers.py has the details and the signed-in clients).
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="jobhunt-tests-")
# The whole data folder (uploads, backups, leftovers the encryption report lists) is throwaway: no test touches
# the app's real data/ folder.
os.environ["JOBHUNT_DATA_DIR"] = _TMP
os.environ["JOBHUNT_DB_PATH"] = os.path.join(_TMP, "test.db")
os.environ.pop("DATABASE_URL", None)  # would override JOBHUNT_DB_PATH
os.environ["JOBHUNT_ENV_PATH"] = os.path.join(_TMP, "test.env")  # never read or write the real .env
os.environ["JOBHUNT_SCHEDULER"] = "off"  # tests drive backend.schedule.tick() themselves
os.environ["JOBHUNT_KEY_FILE"] = os.path.join(_TMP, "server-keys.json")  # never data/server-keys.json
# The per-user configuration folder that keeps the data folder's pointer (backend/paths.py), and the folders the
# default data folder is worked out from: never the real ones.
os.environ["JOBHUNT_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "xdg-config")
os.environ["XDG_DATA_HOME"] = os.path.join(_TMP, "xdg-data")
# Accounts: the quick password key derivation (only honoured with JOBHUNT_TESTING=1) and the account, created
# from the environment when the app starts (tests/helpers.py ADMIN_EMAIL / ADMIN_PASSWORD).
os.environ["JOBHUNT_TESTING"] = "1"
os.environ["JOBHUNT_KDF_PROFILE"] = "test"
os.environ["JOBHUNT_ADMIN_EMAIL"] = "admin@example.invalid"
os.environ["JOBHUNT_ADMIN_PASSWORD"] = "test-admin-password"
