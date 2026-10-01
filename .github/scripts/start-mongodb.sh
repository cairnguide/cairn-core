#!/usr/bin/env bash
# Starts a throwaway MongoDB single-node replica set with authentication, for CI.
# The API runs every request in a transaction, which needs a replica set, and the
# tests connect as restricted users, which needs authentication on.
#
# Usage: start-mongodb.sh <version>   for example: start-mongodb.sh 8.0
# Then: CAIRN_TEST_MONGODB_URI=mongodb://admin:admin@localhost:27017/?replicaSet=rs0
# The credentials are for this throwaway container only, never a real deployment.
set -euo pipefail

version="${1:?Pass the MongoDB version, for example 8.0}"
docker run -d --name mongodb -p 27017:27017 \
  -e MONGO_INITDB_ROOT_USERNAME=admin -e MONGO_INITDB_ROOT_PASSWORD=admin \
  --entrypoint bash "mongo:${version}" -c \
  'head -c 756 /dev/urandom | base64 > /tmp/keyfile && chmod 400 /tmp/keyfile && chown 999:999 /tmp/keyfile && exec docker-entrypoint.sh mongod --replSet rs0 --keyFile /tmp/keyfile --bind_ip_all' \
  >/dev/null

# The entrypoint creates the admin user on a temporary server first, then restarts mongod.
python3 "$(dirname "$0")/../../scripts/init_replica_set.py" "mongodb://admin:admin@localhost:27017/?replicaSet=rs0" \
  || { docker logs mongodb | tail -n 50; exit 1; }
