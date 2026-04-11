#!/usr/bin/env bash
# load-images.sh — Pull all third-party images via crane and push to the
# k3d local registry (k3d-registry.localhost:5555). k3s containerd is
# configured to mirror docker.io/quay.io/ghcr.io/registry.k8s.io through
# the local registry, so pods pull from cache instead of the internet.
#
# Prerequisites:
#   brew install crane
#   k3d registry running: docker ps | grep k3d-registry
#
# Usage:
#   bash scripts/load-images.sh          # load all images
#   bash scripts/load-images.sh --list   # just print the image list

set -euo pipefail

REGISTRY="localhost:5555"
PLATFORM="linux/arm64"

# All third-party images used by the lab (raj/* images are built locally
# and imported via k3d image import, so they're not included here).
IMAGES=(
  # Data layer
  "quay.io/strimzi/kafka:0.51.0-kafka-4.1.0"
  "quay.io/strimzi/operator:0.51.0"
  "postgres:16-alpine"
  "qdrant/qdrant:v1.12.0"
  "redis:7-alpine"
  "neo4j:5.26-community"
  "minio/minio:RELEASE.2025-04-03T14-56-28Z"
  "provectuslabs/kafka-ui:latest"

  # Auth
  "quay.io/keycloak/keycloak:26.2"
  "quay.io/oauth2-proxy/oauth2-proxy:v7.6.0"

  # Platform
  "langfuse/langfuse:2"
  "python:3.12-slim"
  "public.ecr.aws/docker/library/redis:8.2.3-alpine"

  # Infrastructure
  "quay.io/argoproj/argocd:v3.3.6"
  "quay.io/jetstack/cert-manager-cainjector:v1.20.1"
  "quay.io/jetstack/cert-manager-controller:v1.20.1"
  "quay.io/jetstack/cert-manager-webhook:v1.20.1"
  "ghcr.io/external-secrets/external-secrets:v2.2.0"
  "ghcr.io/stakater/reloader:v1.4.14"
  "docker.io/emberstack/kubernetes-reflector:10.0.29"
  "hashicorp/vault:1.17"
  "registry.k8s.io/ingress-nginx/controller:v1.15.1"

  # Monitoring
  "docker.io/grafana/tempo:2.7.2"
  "docker.io/grafana/grafana:12.4.2"
  "quay.io/kiwigrid/k8s-sidecar:2.5.5"
  "quay.io/prometheus/prometheus:v3.11.1"
  "quay.io/prometheus/alertmanager:v0.31.1"
  "quay.io/prometheus/node-exporter:v1.11.1"
  "quay.io/prometheus-operator/prometheus-operator:v0.90.1"
  "quay.io/prometheus-operator/prometheus-config-reloader:v0.90.1"
  "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.18.0"

  # Utilities (used by jobs/init containers)
  "curlimages/curl:latest"
)

if [[ "${1:-}" == "--list" ]]; then
  printf '%s\n' "${IMAGES[@]}"
  exit 0
fi

echo "Loading ${#IMAGES[@]} images into $REGISTRY..."
echo ""

FAILED=()
for img in "${IMAGES[@]}"; do
  # Strip the registry prefix to get the repo path for the local registry.
  # e.g. quay.io/strimzi/kafka:tag → strimzi/kafka:tag
  #      postgres:16-alpine → library/postgres:16-alpine (docker.io implicit)
  case "$img" in
    docker.io/*)    repo="${img#docker.io/}" ;;
    quay.io/*)      repo="${img#quay.io/}" ;;
    ghcr.io/*)      repo="${img#ghcr.io/}" ;;
    registry.k8s.io/*) repo="${img#registry.k8s.io/}" ;;
    public.ecr.aws/*) repo="${img#public.ecr.aws/}" ;;
    */*/*)          repo="${img#*/}" ;;  # strip first segment if 3-part
    */*)            repo="$img" ;;       # already repo/image:tag
    *)              repo="library/$img" ;; # bare image → library/
  esac

  dest="$REGISTRY/$repo"
  printf '  %-65s → %s\n' "$img" "$dest"

  if DOCKER_CONFIG=/tmp/dockerclean crane copy "$img" "$dest" --platform "$PLATFORM" 2>/dev/null; then
    printf '    \033[0;32m✓\033[0m\n'
  else
    printf '    \033[0;31m✗ failed\033[0m\n'
    FAILED+=("$img")
  fi
done

echo ""
echo "Done. ${#FAILED[@]} failures out of ${#IMAGES[@]} images."
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "Failed images:"
  printf '  %s\n' "${FAILED[@]}"
  exit 1
fi
