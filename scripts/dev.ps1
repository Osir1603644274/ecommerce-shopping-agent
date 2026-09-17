param(
    [ValidateSet(
        "infra",
        "app",
        "all",
        "rag",
        "observability",
        "load",
        "down",
        "logs"
    )]
    [string]$Target = "all"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot "docker-compose.yml"
$ComposeArgs = @("compose", "--project-directory", $ProjectRoot, "-f", $ComposeFile)

switch ($Target) {
    "infra" {
        & docker @ComposeArgs up -d mysql redis kafka elasticsearch
    }
    "app" {
        & docker @ComposeArgs up --build backend agent
    }
    "all" {
        & docker @ComposeArgs up -d --build
    }
    "rag" {
        & docker @ComposeArgs --profile rag up -d qdrant
    }
    "observability" {
        & docker @ComposeArgs --profile observability up -d prometheus
    }
    "load" {
        & docker @ComposeArgs --profile loadtest run --rm `
            k6 run /scripts/read-paths.js
    }
    "down" {
        & docker @ComposeArgs down
    }
    "logs" {
        & docker @ComposeArgs logs -f
    }
}
