"""``python -m plummy.check_config``: exit 0 if the configuration is valid, otherwise 1.

The error message never contains secret values.
"""

from plummy.main import check_configuration

if __name__ == "__main__":
    raise SystemExit(check_configuration())
