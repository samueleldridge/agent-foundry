# HashiCorp Nomad reference jobspec for the hello project
# (docs/84 § Sample manifests). The pipeline templates the image tag
# (foundry-hello:<system_version>) before `foundry deploy --platform nomad
# --jobspec deploy/nomad/hello.nomad`.
job "hello" {
  datacenters = ["dc1"]
  type        = "service"

  group "app" {
    count = 4

    network {
      port "http" {
        to = 8080
      }
    }

    service {
      name = "hello"
      port = "http"

      check {
        type     = "http"
        path     = "/health"
        interval = "10s"
        timeout  = "5s"
      }

      check {
        type     = "http"
        path     = "/health?deep=true"
        interval = "30s"
        timeout  = "15s"
        check_restart {
          limit = 3
          grace = "60s"
        }
      }
    }

    task "hello" {
      driver = "docker"

      config {
        image = "registry.example.com/foundry-hello:SYSTEM_VERSION"
        ports = ["http"]
      }

      env {
        FOUNDRY_ENV                 = "prod"
        FOUNDRY_TRACING             = "otel"
        OTEL_EXPORTER_OTLP_ENDPOINT = "http://otel-collector:4317"
      }

      template {
        data = <<EOH
{{ with secret "secret/foundry/prod" }}
FOUNDRY_CHECKPOINTER={{ .Data.checkpointer }}
FOUNDRY_RATE_LIMITER={{ .Data.redis }}
FOUNDRY_API_TOKENS={{ .Data.api_tokens }}
ANTHROPIC_API_KEY={{ .Data.anthropic_api_key }}
{{ end }}
EOH
        destination = "secrets/foundry.env"
        env         = true
      }

      resources {
        cpu    = 1000
        memory = 2048
      }
    }
  }
}
