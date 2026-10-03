"""Install deterministic environment isolation before package-based test imports."""

from .environment_isolation import install_test_environment


install_test_environment()
