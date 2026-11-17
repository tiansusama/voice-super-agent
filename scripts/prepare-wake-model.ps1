$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path $PSScriptRoot -Parent
$modelName = 'sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01'
$models = Join-Path $projectRoot 'resources/models'
$required = @('tokens.txt', 'encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx',
              'decoder-epoch-12-avg-2-chunk-16-left-64.onnx', 'joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx')
$missing = $required | Where-Object { -not (Test-Path -LiteralPath (Join-Path (Join-Path $models $modelName) $_)) }
if (-not $missing) {
    Write-Output "唤醒模型已准备：$(Join-Path $models $modelName)"
    return
}
New-Item -ItemType Directory -Force $models | Out-Null
$archive = Join-Path $models "$modelName.tar.bz2"
# 下载使用 Windows 的系统证书存储，解包由项目 uv 环境完成。
Invoke-WebRequest -Uri "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$modelName.tar.bz2" -OutFile $archive
uv run --locked --project $projectRoot python -c "import sys, tarfile; model=tarfile.open(sys.argv[1]); model.extractall(sys.argv[2], filter='data'); model.close()" $archive $models
if ($LASTEXITCODE -ne 0) { throw '唤醒模型解包失败' }
Write-Output "唤醒模型已准备：$(Join-Path $models $modelName)"
