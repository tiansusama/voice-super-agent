$ErrorActionPreference = 'Stop'
$modelName = 'sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01'
$models = Join-Path $PSScriptRoot '../resources/models'
New-Item -ItemType Directory -Force $models | Out-Null
$archive = Join-Path $models "$modelName.tar.bz2"
# 下载使用 Windows 的系统证书存储，解包由项目 uv 环境完成。
Invoke-WebRequest -Uri "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$modelName.tar.bz2" -OutFile $archive
uv run --locked python -c "import sys, tarfile; model=tarfile.open(sys.argv[1]); model.extractall(sys.argv[2], filter='data'); model.close()" $archive $models
if ($LASTEXITCODE -ne 0) { throw '唤醒模型解包失败' }
Write-Output "唤醒模型已准备：$(Join-Path $models $modelName)"
