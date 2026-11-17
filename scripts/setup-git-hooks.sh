#!/bin/sh
# One-time setup of automatic versioning for a clone (docs/adr/0009-automatic-versioning.md).
set -eu
root=$(git rev-parse --show-toplevel)
cd "$root"
git config core.hooksPath .githooks
git config merge.plummy-version.name "PlummyKnowledge VERSION merge driver"
git config merge.plummy-version.driver 'python3 "$(git rev-parse --show-toplevel)/scripts/version.py" merge %O %A %B'
chmod +x .githooks/pre-commit .githooks/post-commit scripts/version.py
echo "Automatic versioning enabled. Current version: $(python3 scripts/version.py show)"
