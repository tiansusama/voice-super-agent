# 本地模型资源

KWS 模型放在 `models/` 中，按具体模型建立子目录；资源目录已加入 Git 忽略。

从项目根目录执行：

```powershell
./scripts/prepare-wake-model.ps1
```

脚本从 [Sherpa 官方 kws-models 发布资源](https://github.com/k2-fsa/sherpa-onnx/releases/tag/kws-models)下载并解包 `sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01`，使用 Windows 证书存储验证 HTTPS，解包通过项目 uv 环境完成。解包后约 18 MB；保留发布包内 README、词表和模型文件。[官方中文模型说明](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)及发布包 README 标注模型采用 Apache License 2.0。

运行使用该目录中的 `tokens.txt`、`encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx`、`decoder-epoch-12-avg-2-chunk-16-left-64.onnx` 和 `joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx`，CPU 单线程推理。

默认词为“你好小博”。开发版通过 `.env` 的 `VOICE_WAKE_WORD` 配置一个中文短语，程序使用 pypinyin 按官方 ppinyin 格式转换为声母、带调韵母，并核对模型词表。非中文或词表无法表示的发音会显示具体错误。词表可表示意味着能够加载该词条；实际识别效果仍需针对所选短语实测。

Windows 会从系统目录查找原生 DLL；[Sherpa FAQ](https://k2-fsa.github.io/sherpa/onnx/faqs/index.html)记载了旧版 `onnxruntime.dll` 导致 API 版本冲突的问题。本项目在导入 Sherpa 前显式加载 uv 环境内锁定的 ONNX Runtime 1.31.1，供 KWS 与 Pipecat 的 VAD、Smart Turn 共用。
