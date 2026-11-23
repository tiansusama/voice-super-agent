[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$MakeNSIS,
    [Parameter(Mandatory)][string]$WebViewBootstrap
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$compiler = (Resolve-Path -LiteralPath $MakeNSIS).Path
$bootstrap = (Resolve-Path -LiteralPath $WebViewBootstrap).Path
$signature = Get-AuthenticodeSignature -LiteralPath $bootstrap
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw 'WebView2 安装器必须具有有效的 Microsoft 签名。'
}
$work = Join-Path $projectRoot '.local/installer-build'
$bundle = Join-Path $projectRoot '.local/installer-bundle'
$output = Join-Path $projectRoot 'dist/installer'
New-Item -ItemType Directory -Path $output -Force | Out-Null

Push-Location $projectRoot
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & (Join-Path $projectRoot 'scripts/prepare-dev.ps1') -Step Frontend
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & (Join-Path $projectRoot 'scripts/prepare-wake-model.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    uv run --locked --no-sync --with pyinstaller==6.22.3 pyinstaller --noconfirm --workpath $work --distpath $bundle (Join-Path $PSScriptRoot 'VoiceSuperAgent.spec')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $compiler /INPUTCHARSET UTF8 "/DSETUP_FILE=$(Join-Path $output 'VoiceSuperAgent-Setup.exe')" "/DBUNDLE_DIR=$(Join-Path $bundle 'VoiceSuperAgent')" "/DWEBVIEW_BOOTSTRAP=$bootstrap" (Join-Path $PSScriptRoot 'Setup.nsi')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
