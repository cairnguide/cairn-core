#!/usr/bin/env bash
# Lists the API test files added or modified by a push, relative to api/.
#
# Usage: changed-tests.sh <before-sha> <after-sha>
# <before-sha> is github.event.before. On a branch's first push it is all
# zeros, and after a force push it may no longer exist. In both cases the
# comparison falls back to the point where the branch left origin/main, so
# "new tests" means every test file the branch has touched.
set -euo pipefail

before="${1:-}"
after="${2:-HEAD}"
zero="0000000000000000000000000000000000000000"

if [[ -z "$before" || "$before" == "$zero" ]] || ! git cat-file -e "${before}^{commit}" 2>/dev/null; then
  git fetch --quiet origin main
  before="$(git merge-base origin/main "$after")"
fi

git diff --name-only --diff-filter=AMR "$before" "$after" -- 'api/tests/test_*.py' \
  | sed 's#^api/##' \
  | while read -r f; do [[ -f "api/$f" ]] && echo "$f"; done \
  || true
