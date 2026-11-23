from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


scripts = Path(SPECPATH)
root = scripts.parents[1]
model_name = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
model_dir = root / "resources" / "models" / model_name
model_files = (
    "README.md",
    "tokens.txt",
    "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
)

datas = [
    (str(root / "frontend" / "dist"), "frontend/dist"),
    (str(root / "src" / "voice_super_agent" / "memory" / "migrations"), "voice_super_agent/memory/migrations"),
    (str(root / "LICENSE"), "."),
    *[(str(model_dir / name), f"resources/models/{model_name}") for name in model_files],
]
datas += collect_data_files("pipecat", includes=["**/*.onnx"])
datas += collect_data_files("pypinyin", includes=["**/*.json"])
datas += collect_data_files("webview")
datas += copy_metadata("pipecat-ai")
datas += copy_metadata("mcp")

binaries = collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("pythonnet") + collect_dynamic_libs("clr_loader")
analysis = Analysis(
    [str(scripts / "launch.pyw")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=["webview.platforms.winforms", "webview.platforms.edgechromium", "pythonnet", "clr"],
)
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True,
    name="VoiceSuperAgent", console=False, upx=False,
    disable_windowed_traceback=True,
)
bundle = COLLECT(exe, analysis.binaries, analysis.datas, upx=False, name="VoiceSuperAgent")
