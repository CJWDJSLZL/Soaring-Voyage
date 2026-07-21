"""Make the test adapter selection explicit before application modules import."""

import os

os.environ.setdefault("APP_ENV", "test")
