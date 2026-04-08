terraform {
  required_version = ">= 1.5"
  required_providers {
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.25"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.12"
    }
  }
}

# Variables
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

variable "neo4j_password" {
  description = "Neo4j password"
  type        = string
  default     = "rajailab123"
}

variable "minio_password" {
  description = "MinIO root password"
  type        = string
  default     = "rajailab123"
}

variable "postgres_password" {
  description = "PostgreSQL password"
  type        = string
  default     = "raj-lab-password"
}

# Step 1: Create k3d cluster
resource "null_resource" "k3d_cluster" {
  provisioner "local-exec" {
    command = <<-EOT
      # Delete existing cluster if present
      k3d cluster delete ${var.cluster_name} 2>/dev/null || true

      # Create cluster
      k3d cluster create ${var.cluster_name} \
        --servers 1 \
        --agents ${var.agents} \
        --port "8080:80@loadbalancer" \
        --port "8443:443@loadbalancer" \
        --k3s-arg "--disable=traefik@server:0" \
        --wait

      # Wait for cluster to be ready
      kubectl wait --for=condition=Ready nodes --all --timeout=120s
    EOT
  }

  provisioner "local-exec" {
    when    = destroy
    command = "k3d cluster delete ${self.triggers.cluster_name} 2>/dev/null || true"
  }

  triggers = {
    cluster_name = var.cluster_name
    agents       = var.agents
  }
}

# Step 2: Import Docker images into k3d
resource "null_resource" "import_images" {
  depends_on = [null_resource.k3d_cluster]

  provisioner "local-exec" {
    command = <<-EOT
      echo "Importing images into k3d..."
      k3d image import \
        redis:7-alpine \
        postgres:16-alpine \
        qdrant/qdrant:v1.12.0 \
        apache/kafka:3.8.0 \
        minio/minio:latest \
        neo4j:5.26-community \
        raj/embedding-service:latest \
        raj/rag-service:latest \
        raj/rag-ui:latest \
        raj/normalizer:latest \
        raj/chunker:latest \
        raj/embedder:latest \
        raj/agent-orchestrator:latest \
        raj/graph-updater:latest \
        raj/evaluation:latest \
        -c ${var.cluster_name} || true
      echo "Images imported"
    EOT
  }

  triggers = {
    cluster_id = null_resource.k3d_cluster.id
  }
}

# Configure providers after cluster creation
provider "kubernetes" {
  config_path    = "~/.kube/config"
  config_context = "k3d-${var.cluster_name}"
}

provider "helm" {
  kubernetes {
    config_path    = "~/.kube/config"
    config_context = "k3d-${var.cluster_name}"
  }
}

# Step 3: Create namespaces
resource "kubernetes_namespace" "ai_data" {
  depends_on = [null_resource.import_images]
  metadata {
    name = "ai-data"
  }
}

resource "kubernetes_namespace" "ai_platform" {
  depends_on = [null_resource.import_images]
  metadata {
    name = "ai-platform"
  }
}

resource "kubernetes_namespace" "argocd" {
  depends_on = [null_resource.import_images]
  metadata {
    name = "argocd"
  }
}

# Step 4: Deploy data layer manifests
resource "null_resource" "deploy_ai_data" {
  depends_on = [kubernetes_namespace.ai_data]

  provisioner "local-exec" {
    command = <<-EOT
      kubectl --context k3d-${var.cluster_name} apply -f ${path.module}/../manifests/ai-data/
    EOT
  }

  triggers = {
    namespace = kubernetes_namespace.ai_data.id
  }
}

# Step 5: Create Kafka topics
resource "null_resource" "kafka_topics" {
  depends_on = [null_resource.deploy_ai_data]

  provisioner "local-exec" {
    command = <<-EOT
      echo "Waiting for Kafka to be ready..."
      kubectl --context k3d-${var.cluster_name} wait --for=condition=Ready pod/kafka-0 -n ai-data --timeout=120s

      for topic in \
        document.uploaded document.canonical document.canonical-dlt \
        document.chunked document.embedded document.indexed \
        supply-chain.raw supply-chain.canonical supply-chain.canonical-dlt \
        agent.trace agent.action agent.cost query.log; do
        kubectl --context k3d-${var.cluster_name} exec -n ai-data kafka-0 -- \
          /opt/kafka/bin/kafka-topics.sh \
          --bootstrap-server localhost:9092 \
          --create --topic "$topic" \
          --partitions 3 --replication-factor 1 2>/dev/null || true
      done
      echo "Kafka topics created"
    EOT
  }

  triggers = {
    data_layer = null_resource.deploy_ai_data.id
  }
}

# Step 6: Deploy AI platform manifests
resource "null_resource" "deploy_ai_platform" {
  depends_on = [kubernetes_namespace.ai_platform, null_resource.kafka_topics]

  provisioner "local-exec" {
    command = <<-EOT
      kubectl --context k3d-${var.cluster_name} apply -f ${path.module}/../manifests/ai-platform/
    EOT
  }

  triggers = {
    namespace = kubernetes_namespace.ai_platform.id
  }
}

# Step 7: Deploy pipeline microservices (not in manifests — deployed via kubectl)
resource "null_resource" "deploy_microservices" {
  depends_on = [null_resource.deploy_ai_platform, null_resource.kafka_topics]

  provisioner "local-exec" {
    command = <<-EOT
      CTX="--context k3d-${var.cluster_name}"

      # Normalizer
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: normalizer
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: normalizer
        template:
          metadata:
            labels:
              app: normalizer
          spec:
            containers:
            - name: normalizer
              image: raj/normalizer:latest
              imagePullPolicy: Never
              env:
              - name: KAFKA_BOOTSTRAP
                value: kafka.ai-data:9092
YAML

      # Chunker
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: chunker
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: chunker
        template:
          metadata:
            labels:
              app: chunker
          spec:
            containers:
            - name: chunker
              image: raj/chunker:latest
              imagePullPolicy: Never
              env:
              - name: KAFKA_BOOTSTRAP
                value: kafka.ai-data:9092
YAML

      # Embedder
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: embedder
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: embedder
        template:
          metadata:
            labels:
              app: embedder
          spec:
            containers:
            - name: embedder
              image: raj/embedder:latest
              imagePullPolicy: Never
              env:
              - name: KAFKA_BOOTSTRAP
                value: kafka.ai-data:9092
              - name: EMBEDDING_URL
                value: http://embedding-service.ai-platform:8080
              - name: QDRANT_URL
                value: http://qdrant.ai-data:6333
              - name: QDRANT_COLLECTION
                value: raj-docs
YAML

      # RAG UI
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: rag-ui
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: rag-ui
        template:
          metadata:
            labels:
              app: rag-ui
          spec:
            containers:
            - name: ui
              image: raj/rag-ui:latest
              imagePullPolicy: Never
              env:
              - name: RAG_URL
                value: http://rag-service.ai-platform:8000
              ports:
              - containerPort: 7860
---
      apiVersion: v1
      kind: Service
      metadata:
        name: rag-ui
        namespace: ai-platform
      spec:
        selector:
          app: rag-ui
        ports:
        - port: 7860
YAML

      # Agent orchestrator
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: agent-orchestrator
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: agent-orchestrator
        template:
          metadata:
            labels:
              app: agent-orchestrator
          spec:
            containers:
            - name: agent
              image: raj/agent-orchestrator:latest
              imagePullPolicy: Never
              env:
              - name: KAFKA_BOOTSTRAP
                value: kafka.ai-data:9092
              - name: OLLAMA_URL
                value: http://host.k3d.internal:11434
              - name: OLLAMA_MODEL
                value: llama3.2:latest
              - name: RAG_URL
                value: http://rag-service.ai-platform:8000
              - name: REDIS_URL
                value: redis://redis.ai-data:6379
              - name: MAX_STEPS
                value: "10"
              - name: MAX_TOKENS
                value: "50000"
              ports:
              - containerPort: 8001
---
      apiVersion: v1
      kind: Service
      metadata:
        name: agent-orchestrator
        namespace: ai-platform
      spec:
        selector:
          app: agent-orchestrator
        ports:
        - port: 8001
YAML

      # Graph updater
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: graph-updater
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: graph-updater
        template:
          metadata:
            labels:
              app: graph-updater
          spec:
            containers:
            - name: graph-updater
              image: raj/graph-updater:latest
              imagePullPolicy: Never
              env:
              - name: KAFKA_BOOTSTRAP
                value: kafka.ai-data:9092
              - name: NEO4J_URI
                value: bolt://graphdb.ai-data:7687
              - name: NEO4J_USER
                value: neo4j
              - name: NEO4J_PASSWORD
                value: ${var.neo4j_password}
YAML

      # Langfuse
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: langfuse
        namespace: ai-platform
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: langfuse
        template:
          metadata:
            labels:
              app: langfuse
          spec:
            containers:
            - name: langfuse
              image: langfuse/langfuse:2
              env:
              - name: DATABASE_URL
                value: postgresql://ai_app:${var.postgres_password}@postgresql.ai-data:5432/ai_metadata
              - name: NEXTAUTH_SECRET
                value: raj-lab-secret-key
              - name: NEXTAUTH_URL
                value: http://localhost:3001
              - name: SALT
                value: raj-lab-salt
              - name: HOSTNAME
                value: "0.0.0.0"
              ports:
              - containerPort: 3000
---
      apiVersion: v1
      kind: Service
      metadata:
        name: langfuse
        namespace: ai-platform
      spec:
        selector:
          app: langfuse
        ports:
        - port: 3001
          targetPort: 3000
YAML

      # Kafka UI
      kubectl $CTX apply -f - <<'YAML'
      apiVersion: apps/v1
      kind: Deployment
      metadata:
        name: kafka-ui
        namespace: ai-data
      spec:
        replicas: 1
        selector:
          matchLabels:
            app: kafka-ui
        template:
          metadata:
            labels:
              app: kafka-ui
          spec:
            containers:
            - name: kafka-ui
              image: provectuslabs/kafka-ui:latest
              imagePullPolicy: IfNotPresent
              env:
              - name: KAFKA_CLUSTERS_0_NAME
                value: raj-ai-lab
              - name: KAFKA_CLUSTERS_0_BOOTSTRAPSERVERS
                value: kafka.ai-data:9092
              ports:
              - containerPort: 8080
---
      apiVersion: v1
      kind: Service
      metadata:
        name: kafka-ui
        namespace: ai-data
      spec:
        selector:
          app: kafka-ui
        ports:
        - port: 8080
YAML

      echo "Microservices deployed"
    EOT
  }

  triggers = {
    platform = null_resource.deploy_ai_platform.id
  }
}

# Step 8: Install ArgoCD
resource "null_resource" "install_argocd" {
  depends_on = [kubernetes_namespace.argocd]

  provisioner "local-exec" {
    command = <<-EOT
      kubectl --context k3d-${var.cluster_name} apply -n argocd \
        -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml \
        --server-side 2>/dev/null || true
      echo "ArgoCD installed"
    EOT
  }

  triggers = {
    namespace = kubernetes_namespace.argocd.id
  }
}

# Step 9: Install Prometheus + Grafana
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

# Outputs
output "cluster_name" {
  value = var.cluster_name
}

output "kubeconfig_context" {
  value = "k3d-${var.cluster_name}"
}

output "access_urls" {
  value = {
    rag_ui     = "kubectl port-forward svc/rag-ui -n ai-platform 7860:7860"
    rag_api    = "kubectl port-forward svc/rag-service -n ai-platform 8000:8000"
    agent_api  = "kubectl port-forward svc/agent-orchestrator -n ai-platform 8001:8001"
    langfuse   = "kubectl port-forward svc/langfuse -n ai-platform 3001:3001"
    kafka_ui   = "kubectl port-forward svc/kafka-ui -n ai-data 9091:8080"
    argocd     = "kubectl port-forward svc/argocd-server -n argocd 9090:443"
    grafana    = "kubectl port-forward svc/monitoring-grafana -n monitoring 3000:80"
    prometheus = "kubectl port-forward svc/monitoring-kube-prometheus-prometheus -n monitoring 9092:9090"
    qdrant     = "kubectl port-forward svc/qdrant -n ai-data 6333:6333"
    neo4j      = "kubectl port-forward svc/graphdb -n ai-data 7474:7474"
    minio      = "kubectl port-forward svc/minio -n ai-data 9001:9001"
  }
}

output "credentials" {
  sensitive = true
  value = {
    grafana  = "admin / ${var.grafana_password}"
    neo4j    = "neo4j / ${var.neo4j_password}"
    minio    = "rajailab / ${var.minio_password}"
    postgres = "ai_app / ${var.postgres_password}"
  }
}
