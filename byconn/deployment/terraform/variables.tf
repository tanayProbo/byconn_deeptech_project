variable "project_id" {
  type        = string
  description = "GCP Project ID to provision the resources into"
  default     = "byconn-x-platform"
}

variable "region" {
  type        = string
  description = "GCP region for cluster and databases deployment"
  default     = "us-central1"
}

variable "crawler_node_count" {
  type        = number
  description = "Number of browser engine workers nodes to start"
  default     = 10
}

variable "gpu_node_count" {
  type        = number
  description = "Number of GPU-backed AI model processing nodes to start"
  default     = 2
}
