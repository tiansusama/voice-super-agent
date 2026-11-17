[CmdletBinding()]
param([ValidateSet('All', 'Python', 'Frontend', 'Build', 'Model')][string]$Step = 'All')

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$projectRoot = Split-Path $PSScriptRoot -Parent
$frontend = Join-Path $projectRoot 'frontend'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.venv'
$env:PYTHONIOENCODING = 'utf-8'
$steps = if ($Step -eq 'All') { @('Python', 'Frontend', 'Build', 'Model') } else { @($Step) }

foreach ($current in $steps) {
    Write-Output "正在准备：$current"
    switch ($current) {
        'Python' {
            uv sync --locked --project $projectRoot
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            $configuration = Join-Path $projectRoot '.env'
            if (-not (Test-Path -LiteralPath $configuration)) {
                Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $configuration
            }
        }
        'Frontend' {
            npm --prefix $frontend ls --all
            if ($LASTEXITCODE -ne 0) {
                npm --prefix $frontend ci --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
            }
        }
        'Build' {
            npm --prefix $frontend run build
            if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        }
        'Model' {
            & (Join-Path $PSScriptRoot 'prepare-wake-model.ps1')
        }
    }
}
