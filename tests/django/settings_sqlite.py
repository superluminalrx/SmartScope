"""
Settings for running Django tests without MySQL or Redis.

Usage (from the repository root):
    python -m django test tests.django.test_api_group_scoping --settings=tests.django.settings_sqlite

Wraps the server settings; only the database, cache and channel layer are swapped for
in-process backends, and the filesystem paths the settings require point at a temp dir.
"""
import os
import tempfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(tempfile.mkdtemp(prefix='smartscope-tests-'))

for name, value in {
    'SECRET_KEY': 'test-only-secret-key',
    'AUTOSCREENDIR': str(TEST_ROOT / 'data'),
    'TEMPDIR': str(TEST_ROOT / 'tmp'),
    'SCRATCH_DIR': str(TEST_ROOT / 'scratch'),
    'LOGDIR': str(TEST_ROOT / 'logs'),
    'CONFIG': str(REPO_DIR / 'config' / 'smartscope'),
    'EXTERNAL_PLUGINS_DIRECTORY': str(TEST_ROOT / 'external_plugins'),
    'TEMPLATE_FILES': str(TEST_ROOT / 'templates'),
    'DEBUG': 'False',
    'DEPLOY': 'False',
    'USE_MICROSCOPE': 'False',
    'USE_AWS': 'False',
}.items():
    os.environ.setdefault(name, value)

for directory in ('data', 'tmp', 'scratch', 'logs', 'external_plugins', 'templates'):
    (TEST_ROOT / directory).mkdir(parents=True, exist_ok=True)

from Smartscope.core.settings.server_docker import *  # noqa: E402,F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': str(TEST_ROOT / 'test.sqlite3'),
    }
}

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}
