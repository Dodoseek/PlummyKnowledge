#!/bin/sh
# One-shot init of the bundled S3 server (compose.prod.yaml, profile s3; ADR-022): the application user with the
# "user" role and the documents bucket owned by it. Idempotent; prints no keys.
set -eu

ADMIN_ACCESS_KEY=$(cat /run/secrets/s3_root_access_key)
ADMIN_SECRET_KEY=$(cat /run/secrets/s3_root_secret_key)
ADMIN_ENDPOINT_URL=${ADMIN_ENDPOINT:-http://127.0.0.1:7071}
export ADMIN_ACCESS_KEY ADMIN_SECRET_KEY ADMIN_ENDPOINT_URL
APP_KEY=$(cat /run/secrets/s3_access_key_id)
APP_SECRET=$(cat /run/secrets/s3_secret_access_key)
BUCKET=${S3_BUCKET:?S3_BUCKET is required}

if versitygw admin list-users | awk 'NR > 2 { print $1 }' | grep -qx "$APP_KEY"; then
  echo "application user exists"
else
  versitygw admin create-user --access "$APP_KEY" --secret "$APP_SECRET" --role user >/dev/null
  echo "application user created"
fi

if versitygw admin list-buckets | awk 'NR > 2 { print $1 }' | grep -qx "$BUCKET"; then
  echo "bucket exists"
else
  versitygw admin create-bucket --bucket "$BUCKET" --owner "$APP_KEY" >/dev/null
  echo "bucket created"
fi
