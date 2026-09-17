param(
    [ValidateSet(
        "all",
        "python",
        "java",
        "integration",
        "package",
        "docs",
        "hygiene",
        "compose",
        "docker"
    )]
    [string]$Target = "all"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Checked {
    param(
        [string]$Label,
        [scriptblock]$Command
    )

    Write-Host "==> $Label"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Test-Python {
    Invoke-Checked "Python tests" {
        Push-Location $ProjectRoot
        try {
            python -m pytest agent\tests -q
        } finally {
            Pop-Location
        }
    }
}

function Test-Package {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $packageTemp = Join-Path $tempRoot (
        "local-life-agent-package-" + [guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $packageTemp | Out-Null
    try {
        $wheelDir = Join-Path $packageTemp "wheel"
        $installDir = Join-Path $packageTemp "install"
        New-Item -ItemType Directory -Path $wheelDir, $installDir | Out-Null
        Invoke-Checked "Build wheel" {
            Push-Location (Join-Path $ProjectRoot "agent")
            try {
                python -m pip wheel . --no-deps --wheel-dir $wheelDir
            } finally {
                Pop-Location
            }
        }
        $wheel = Get-ChildItem $wheelDir -Filter "*.whl" | Select-Object -First 1
        Invoke-Checked "Install wheel into clean directory" {
            python -m pip install --no-deps --target $installDir $wheel.FullName
        }
        Invoke-Checked "Import packaged modules" {
            $previousPythonPath = $env:PYTHONPATH
            $env:PYTHONPATH = $installDir
            try {
                Push-Location $packageTemp
                python -c (
                    "import app.main, app.control.planning, " +
                    "app.domains.ecommerce, evaluation, recommendation, scripts"
                )
            } finally {
                Pop-Location
                $env:PYTHONPATH = $previousPythonPath
            }
        }
    } finally {
        $resolved = [IO.Path]::GetFullPath($packageTemp)
        if (-not $resolved.StartsWith(
            $tempRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to remove unsafe package temp path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

function Test-Java {
    $javaCommand = Get-Command java -ErrorAction SilentlyContinue
    $javaOutput = if ($javaCommand) {
        (& java --version | Out-String)
    } else {
        ""
    }
    $versionMatch = [regex]::Match($javaOutput, 'version "(\d+)')
    $javaMajor = if ($versionMatch.Success) {
        [int]$versionMatch.Groups[1].Value
    } else {
        0
    }

    if ($javaMajor -eq 17) {
        Invoke-Checked "Java tests with Maven Wrapper" {
            Push-Location (Join-Path $ProjectRoot "backend")
            try {
                if ($IsWindows -or $env:OS -eq "Windows_NT") {
                    .\mvnw.cmd -q test
                } else {
                    ./mvnw -q test
                }
            } finally {
                Pop-Location
            }
        }
        return
    }

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "JDK 17 is required; current Java major version is $javaMajor."
    }
    Invoke-Checked "Java tests in JDK 17 container" {
        docker run --rm `
            -v "local-life-maven-cache:/root/.m2" `
            -v "$ProjectRoot\backend:/workspace" `
            -w /workspace `
            maven:3.9.16-eclipse-temurin-17 `
            mvn -s settings.xml -q test
    }
}

function Test-Integration {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required for Testcontainers integration tests."
    }
    Invoke-Checked "Docker daemon availability" {
        docker info --format "{{.ServerVersion}}"
    }

    $javaCommand = Get-Command java -ErrorAction SilentlyContinue
    $javaOutput = if ($javaCommand) {
        (& java --version | Out-String)
    } else {
        ""
    }
    $versionMatch = [regex]::Match($javaOutput, 'version "(\d+)')
    $javaMajor = if ($versionMatch.Success) {
        [int]$versionMatch.Groups[1].Value
    } else {
        0
    }

    if ($javaMajor -eq 17) {
        Invoke-Checked "MySQL and Redis Testcontainers integration tests" {
            Push-Location (Join-Path $ProjectRoot "backend")
            try {
                if ($IsWindows -or $env:OS -eq "Windows_NT") {
                    .\mvnw.cmd -q -Ptestcontainers `
                        "-Dskip.unit.tests=true" verify
                } else {
                    ./mvnw -q -Ptestcontainers `
                        "-Dskip.unit.tests=true" verify
                }
            } finally {
                Pop-Location
            }
        }
        return
    }

    Invoke-Checked "MySQL and Redis Testcontainers tests in JDK 17 container" {
        docker run --rm `
            -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal `
            -v /var/run/docker.sock:/var/run/docker.sock `
            -v "local-life-maven-cache:/root/.m2" `
            -v "$ProjectRoot\backend:/workspace" `
            -w /workspace `
            maven:3.9.16-eclipse-temurin-17 `
            mvn -s settings.xml -q -Ptestcontainers `
                "-Dskip.unit.tests=true" verify
    }
}

function Test-Docs {
    Invoke-Checked "Markdown links" {
        Push-Location $ProjectRoot
        try {
            python scripts\check_markdown_links.py
        } finally {
            Pop-Location
        }
    }
}

function Test-Hygiene {
    Invoke-Checked "Repository secret, artifact, and large-file hygiene" {
        Push-Location $ProjectRoot
        try {
            python scripts\check_repository_hygiene.py
        } finally {
            Pop-Location
        }
    }
}

function Test-Compose {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required to validate docker-compose.yml."
    }
    Invoke-Checked "Docker Compose configuration" {
        Push-Location $ProjectRoot
        try {
            docker compose config --quiet
            docker compose --profile rag --profile observability `
                --profile loadtest config --quiet
        } finally {
            Pop-Location
        }
    }
}

function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required to build runtime images."
    }
    Invoke-Checked "Agent runtime image (includes Python tests)" {
        docker build -t local-life-agent-verify `
            (Join-Path $ProjectRoot "agent")
    }
    Invoke-Checked "Backend runtime image (includes Java tests)" {
        $arguments = @(
            "build",
            "-t",
            "local-life-backend-verify"
        )
        if ($env:BACKEND_RUNTIME_IMAGE) {
            $arguments += @(
                "--build-arg",
                "RUNTIME_IMAGE=$($env:BACKEND_RUNTIME_IMAGE)"
            )
        }
        $arguments += (Join-Path $ProjectRoot "backend")
        docker @arguments
    }
}

switch ($Target) {
    "python" { Test-Python }
    "java" { Test-Java }
    "integration" { Test-Integration }
    "package" { Test-Package }
    "docs" { Test-Docs }
    "hygiene" { Test-Hygiene }
    "compose" { Test-Compose }
    "docker" { Test-Docker }
    "all" {
        Test-Hygiene
        Test-Python
        Test-Package
        Test-Java
        Test-Integration
        Test-Docs
        Test-Compose
        Test-Docker
    }
}
