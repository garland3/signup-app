#!/usr/bin/env bash
#
# cve_scan.sh -- Build the previous and current Dockerfile images and run a
# Trivy IMAGE scan against each, printing a before/after CVE comparison.
#
# Run this on a host that can reach registry.access.redhat.com (the Red Hat
# Hardened Images registry) and has Docker + Trivy installed. The CI sandbox
# used to author this change cannot reach container registries, so the scan
# must be run here.
#
# Usage:
#   ./scripts/cve_scan.sh
#
# Requirements:
#   - docker
#   - trivy (https://aquasecurity.github.io/trivy)
#   - network access to registry.access.redhat.com and registry.redhat.io
#
set -euo pipefail

cd "$(dirname "$0")/.."

BEFORE_REF="${BEFORE_REF:-main}"   # git ref whose Dockerfile is the "before"
BEFORE_IMAGE="signup-app:before"
AFTER_IMAGE="signup-app:after"
SEVERITY="${SEVERITY:-CRITICAL,HIGH,MEDIUM}"

tmp_before_dockerfile="$(mktemp)"
trap 'rm -f "$tmp_before_dockerfile"' EXIT

echo "==> Extracting previous Dockerfile from '${BEFORE_REF}'"
git show "${BEFORE_REF}:Dockerfile" > "$tmp_before_dockerfile"

echo "==> Building BEFORE image (${BEFORE_REF} Dockerfile) -> ${BEFORE_IMAGE}"
docker build -f "$tmp_before_dockerfile" -t "${BEFORE_IMAGE}" .

echo "==> Building AFTER image (working-tree Dockerfile) -> ${AFTER_IMAGE}"
docker build -f Dockerfile -t "${AFTER_IMAGE}" .

scan() {
  local image="$1"
  echo
  echo "############################################################"
  echo "# Trivy scan: ${image} (severity: ${SEVERITY})"
  echo "############################################################"
  trivy image --quiet --severity "${SEVERITY}" --no-progress "${image}"
  echo "--- summary for ${image} ---"
  trivy image --quiet --severity "${SEVERITY}" --no-progress \
    --format json "${image}" \
    | python3 -c 'import sys,json,collections; \
d=json.load(sys.stdin); c=collections.Counter(); \
[c.update([v["Severity"]]) for r in (d.get("Results") or []) for v in (r.get("Vulnerabilities") or [])]; \
print(dict(c), "total:", sum(c.values()))'
}

scan "${BEFORE_IMAGE}"
scan "${AFTER_IMAGE}"

echo
echo "==> Done. Compare the per-image summaries above for the before/after CVE counts."
