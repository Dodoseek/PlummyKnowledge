#!/bin/sh
# The root filesystem is read-only; the rendered templates go to the tmpfs /tmp (NGINX_ENVSUBST_OUTPUT_DIR),
# which is empty on every start. Runs before the image's 20-envsubst-on-templates.sh.
set -eu
mkdir -p /tmp/nginx-conf
