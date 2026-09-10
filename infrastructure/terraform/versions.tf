terraform {
  required_version = ">= 1.14.8"

  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    archive  = { source = "hashicorp/archive", version = "~> 2.7" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
    local    = { source = "hashicorp/local", version = "~> 2.5" }
    null     = { source = "hashicorp/null", version = "~> 3.2" }
    random   = { source = "hashicorp/random", version = "~> 3.7" }
    tls      = { source = "hashicorp/tls", version = "~> 4.1" }
  }
}
