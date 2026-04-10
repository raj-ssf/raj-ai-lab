#!/usr/bin/env bash
# patch-qdrant-header.sh — inject the Qdrant API key into the dashboard ingress.
# Reads the key from the qdrant-credentials Secret and patches the live
# raj-ai-lab-qdrant-ingress with a configuration-snippet that sets the
# proxy_set_header directive. ArgoCD's ignoreDifferences on this annotation
# prevents selfHeal from reverting it.
#
# Run after: cluster bootstrap, key rotation in Vault, or ArgoCD full sync.

set -euo pipefail

NS=ai-data
ING=raj-ai-lab-qdrant-ingress

KEY=$(kubectl get secret -n "$NS" qdrant-credentials -o jsonpath='{.data.api_key}' | base64 -d)

if [[ -z "$KEY" ]]; then
  echo "ERROR: could not read api_key from qdrant-credentials secret" >&2
  exit 1
fi

SNIPPET="proxy_set_header api-key \"${KEY}\";\n"

kubectl annotate ingress -n "$NS" "$ING" \
  "nginx.ingress.kubernetes.io/configuration-snippet=proxy_set_header api-key \"${KEY}\";" \
  --overwrite

echo "Patched $ING with api-key header"
