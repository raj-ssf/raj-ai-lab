terraform {
  required_version = ">= 1.5"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}

# ──────────────────────────────────────────────
# Variables
# ──────────────────────────────────────────────

variable "cluster_name" {
  description = "Name of the k3d cluster"
  type        = string
  default     = "raj-ai-lab"
}

variable "agents" {
  description = "Number of k3d agent nodes"
  type        = number
  default     = 2
}

variable "grafana_password" {
  description = "Grafana admin password"
  type        = string
  default     = "raj-ai-lab"
}

variable "myriad_ca_cert" {
  description = "Path to corporate CA cert for TLS proxy bypass"
  type        = string
  default     = "/etc/ssl/certs/Myriad_Plus_AmazonRootCAs.pem"
}

variable "github_repo" {
  description = "GitHub repo for ArgoCD"
  type        = string
  default     = "https://github.com/raj-ssf/raj-ai-lab.git"
}

variable "github_user" {
  description = "GitHub username for ArgoCD"
  type        = string
  default     = "raj-ssf"
}

variable "manifests_dir" {
  description = "Path to manifests directory"
  type        = string
  default     = "../manifests"
}

# ──────────────────────────────────────────────
# Providers (configured after cluster creation)
# ──────────────────────────────────────────────

provider "helm" {
  kubernetes {
    config_path    = "~/.kube/config"
    config_context = "k3d-${var.cluster_name}"
  }
}

# ──────────────────────────────────────────────
# Step 1: Create k3d cluster with CA cert
# ──────────────────────────────────────────────

resource "null_resource" "k3d_cluster" {
  provisioner "local-exec" {
    command = <<-EOT
      k3d cluster delete ${var.cluster_name} 2>/dev/null || true
      sleep 3

      CERT_FLAG=""
      if [ -f "${var.myriad_ca_cert}" ]; then
        CERT_FLAG="--volume ${var.myriad_ca_cert}:${var.myriad_ca_cert}"
      fi

      k3d cluster create ${var.cluster_name} \
        --servers 1 \
        --agents ${var.agents} \
        --port "8080:80@loadbalancer" \
        --port "8443:443@loadbalancer" \
        --k3s-arg "--disable=traefik@server:0" \
        $CERT_FLAG \
        --wait

      kubectl wait --for=condition=Ready nodes --all --timeout=120s
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = "k3d cluster delete ${self.triggers.name} 2>/dev/null || true"
  }

  triggers = {
    name   = var.cluster_name
    agents = var.agents
  }
}

# ──────────────────────────────────────────────
# Step 2: Import all Docker images
# ──────────────────────────────────────────────

resource "null_resource" "import_images" {
  depends_on = [null_resource.k3d_cluster]

  provisioner "local-exec" {
    command = <<-EOT
      k3d image import \
        raj/embedding-service:latest raj/rag-service:latest raj/rag-ui:latest \
        raj/normalizer:latest raj/chunker:latest raj/embedder:latest \
        raj/agent-orchestrator:latest raj/graph-updater:latest raj/evaluation:latest \
        redis:7-alpine postgres:16-alpine qdrant/qdrant:v1.12.0 \
        apache/kafka:3.8.0 minio/minio:latest neo4j:5.26-community \
        -c ${var.cluster_name} || true
    EOT
  }

  triggers = {
    cluster = null_resource.k3d_cluster.id
  }
}

# ──────────────────────────────────────────────
# Step 3: Create namespaces
# ──────────────────────────────────────────────

resource "null_resource" "namespaces" {
  depends_on = [null_resource.import_images]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      kubectl $CTX create namespace ai-data 2>/dev/null || true
      kubectl $CTX create namespace ai-platform 2>/dev/null || true
      kubectl $CTX create namespace argocd 2>/dev/null || true
    EOT
  }

  triggers = {
    cluster = null_resource.import_images.id
  }
}

# ──────────────────────────────────────────────
# Step 4: Deploy all manifests from git repo
# ──────────────────────────────────────────────

resource "null_resource" "deploy_ai_data" {
  depends_on = [null_resource.namespaces]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      kubectl $CTX apply -f ${var.manifests_dir}/ai-data/
      echo "ai-data deployed"
    EOT
  }

  triggers = {
    ns = null_resource.namespaces.id
  }
}

resource "null_resource" "deploy_ai_platform" {
  depends_on = [null_resource.namespaces]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      # Apply all — ignore errors for resources needing monitoring/argocd namespaces (created later)
      kubectl $CTX apply -f ${var.manifests_dir}/ai-platform/ 2>&1 | grep -v "NotFound" || true
      echo "ai-platform deployed"
    EOT
  }

  triggers = {
    ns = null_resource.namespaces.id
  }
}

# ──────────────────────────────────────────────
# Step 5: Create Kafka topics
# ──────────────────────────────────────────────

resource "null_resource" "kafka_topics" {
  depends_on = [null_resource.deploy_ai_data]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      echo "Waiting for Kafka..."
      kubectl $CTX wait --for=condition=Ready pod/kafka-0 -n ai-data --timeout=180s

      for topic in \
        document.uploaded document.canonical document.canonical-dlt \
        document.chunked document.embedded document.indexed \
        supply-chain.raw supply-chain.canonical supply-chain.canonical-dlt \
        agent.trace agent.action agent.cost query.log; do
        kubectl $CTX exec -n ai-data kafka-0 -- \
          /opt/kafka/bin/kafka-topics.sh \
          --bootstrap-server localhost:9092 \
          --create --topic "$topic" \
          --partitions 3 --replication-factor 1 2>/dev/null || true
      done
      echo "Kafka topics created"
    EOT
  }

  triggers = {
    data = null_resource.deploy_ai_data.id
  }
}

# ──────────────────────────────────────────────
# Step 6: Install ArgoCD
# ──────────────────────────────────────────────

resource "null_resource" "argocd" {
  depends_on = [null_resource.namespaces]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      kubectl $CTX apply -n argocd \
        -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
        --server-side 2>/dev/null || true

      echo "Waiting for ArgoCD..."
      kubectl $CTX wait --for=condition=Ready pods -l app.kubernetes.io/name=argocd-server -n argocd --timeout=180s 2>/dev/null || true
      echo "ArgoCD ready"
    EOT
  }

  triggers = {
    ns = null_resource.namespaces.id
  }
}

# ──────────────────────────────────────────────
# Step 7: Configure ArgoCD (repo + applications)
# ──────────────────────────────────────────────

resource "null_resource" "argocd_apps" {
  depends_on = [null_resource.argocd, null_resource.deploy_ai_data, null_resource.deploy_ai_platform]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"
      GH_TOKEN=$(gh auth token 2>/dev/null || echo "")

      kubectl $CTX apply -n argocd -f - <<YAML
apiVersion: v1
kind: Secret
metadata:
  name: repo-raj-ssf
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: repository
stringData:
  type: git
  url: ${var.github_repo}
  username: ${var.github_user}
  password: $GH_TOKEN
  insecure: "true"
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ai-data
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ${var.github_repo}
    targetRevision: main
    path: manifests/ai-data
  destination:
    server: https://kubernetes.default.svc
    namespace: ai-data
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: ai-platform
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ${var.github_repo}
    targetRevision: main
    path: manifests/ai-platform
  destination:
    server: https://kubernetes.default.svc
    namespace: ai-platform
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
YAML
      echo "ArgoCD applications configured"
    EOT
  }

  triggers = {
    argocd = null_resource.argocd.id
  }
}

# ──────────────────────────────────────────────
# Step 8: Install Prometheus + Grafana (Helm)
# ──────────────────────────────────────────────

resource "helm_release" "monitoring" {
  depends_on = [null_resource.k3d_cluster]

  name             = "monitoring"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  namespace        = "monitoring"
  create_namespace = true
  wait             = true
  timeout          = 300

  set {
    name  = "grafana.adminPassword"
    value = var.grafana_password
  }

  set {
    name  = "prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues"
    value = "false"
  }
}

# ──────────────────────────────────────────────
# Step 9: Apply manifests that need monitoring/argocd namespaces
# ──────────────────────────────────────────────

resource "null_resource" "post_install" {
  depends_on = [helm_release.monitoring, null_resource.argocd_apps]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"

      # Grafana dashboard (needs monitoring namespace)
      kubectl $CTX apply -f ${var.manifests_dir}/ai-platform/12-grafana-dashboard.yaml 2>/dev/null || true

      # Sealed secrets (needs monitoring + argocd namespaces)
      kubectl $CTX apply -f ${var.manifests_dir}/ai-platform/15-sealed-secrets.yaml 2>/dev/null || true

      # Kyverno policies
      kubectl $CTX apply -f ${var.manifests_dir}/kyverno-policies.yaml 2>/dev/null || true

      echo "Post-install manifests applied"
    EOT
  }

  triggers = {
    monitoring = helm_release.monitoring.id
    argocd     = null_resource.argocd_apps.id
  }
}

# ──────────────────────────────────────────────
# Step 10: Install Nginx Ingress (Helm)
# ──────────────────────────────────────────────

resource "helm_release" "ingress_nginx" {
  depends_on = [null_resource.k3d_cluster]

  name             = "ingress-nginx"
  repository       = "https://kubernetes.github.io/ingress-nginx"
  chart            = "ingress-nginx"
  namespace        = "ingress-nginx"
  create_namespace = true
  wait             = true
  timeout          = 120

  set {
    name  = "controller.service.type"
    value = "NodePort"
  }

  set {
    name  = "controller.service.nodePorts.http"
    value = "30080"
  }
}

# ──────────────────────────────────────────────
# Step 11: Install Kyverno (Helm)
# ──────────────────────────────────────────────

resource "helm_release" "kyverno" {
  depends_on = [null_resource.k3d_cluster]

  name             = "kyverno"
  repository       = "https://kyverno.github.io/kyverno/"
  chart            = "kyverno"
  namespace        = "kyverno"
  create_namespace = true
  wait             = true
  timeout          = 180

  set {
    name  = "replicaCount"
    value = "1"
  }
}

# ──────────────────────────────────────────────
# Outputs
# ──────────────────────────────────────────────

output "cluster_name" {
  value = var.cluster_name
}

output "kubeconfig_context" {
  value = "k3d-${var.cluster_name}"
}

output "port_forward_commands" {
  value = <<-EOT

    # Run these to access all UIs:
    kubectl port-forward svc/rag-ui -n ai-platform 7860:7860 &
    kubectl port-forward svc/rag-service -n ai-platform 8000:8000 &
    kubectl port-forward svc/agent-orchestrator -n ai-platform 8001:8001 &
    kubectl port-forward svc/langfuse -n ai-platform 3001:3001 &
    kubectl port-forward svc/kafka-ui -n ai-data 9091:8080 &
    kubectl port-forward svc/argocd-server -n argocd 9090:443 &
    kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80 &
    kubectl port-forward svc/qdrant -n ai-data 6333:6333 &
    kubectl port-forward svc/graphdb -n ai-data 7474:7474 &
    kubectl port-forward svc/graphdb -n ai-data 7687:7687 &
    kubectl port-forward svc/minio -n ai-data 9001:9001 &

  EOT
}

output "argocd_password" {
  value     = "Run: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
  sensitive = false
}

output "credentials" {
  value = <<-EOT

    Grafana:  admin / ${var.grafana_password}
    Neo4j:    neo4j / rajailab123
    MinIO:    rajailab / rajailab123
    Langfuse: raj@lab.local / rajailab123

  EOT
}
