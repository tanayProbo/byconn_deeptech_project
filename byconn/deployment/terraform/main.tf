terraform {
  required_version = ">= 1.3.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 4.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Network VPC configuration for secure database and crawler communication
resource "google_compute_network" "byconn_vpc" {
  name                    = "byconn-x-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "byconn_subnet" {
  name          = "byconn-x-subnet"
  ip_cidr_range = "10.0.0.0/16"
  region        = var.region
  network       = google_compute_network.byconn_vpc.id
}

# GKE Cluster setup for hosting the microservices
resource "google_container_cluster" "byconn_cluster" {
  name     = "byconn-x-gke"
  location = var.region
  network  = google_compute_network.byconn_vpc.name
  subnetwork = google_compute_subnetwork.byconn_subnet.name

  remove_default_node_pool = true
  initial_node_count       = 1
}

# Node pool optimized for browser instances and network-intensive crawlers
resource "google_container_node_pool" "crawler_nodes" {
  name       = "byconn-crawler-pool"
  cluster    = google_container_cluster.byconn_cluster.name
  node_count = var.crawler_node_count

  node_config {
    machine_type = "e2-standard-4" # 4 vCPUs, 16 GB RAM
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
    labels = {
      role = "crawler-runner"
    }
  }
}

# GPU Node pool for Vision understanding models and local LLM extractions
resource "google_container_node_pool" "gpu_ai_nodes" {
  name       = "byconn-gpu-pool"
  cluster    = google_container_cluster.byconn_cluster.name
  node_count = var.gpu_node_count

  node_config {
    machine_type = "a2-highgpu-1g" # NVIDIA A100 40GB GPU
    guest_accelerator {
      type  = "nvidia-tesla-a100"
      count = 1
    }
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform"
    ]
    labels = {
      role = "ai-extractor"
    }
  }
}

# Cloud SQL PostgreSQL Instance for configuration storage
resource "google_sql_database_instance" "postgres_instance" {
  name             = "byconn-postgres-instance"
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier = "db-f1-micro"
  }
}
