"""显式启用真实云服务的接口测量；合成音频仅在内存中经过 ASR，不收音、不播放。"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from time import perf_counter

import aiohttp
from dotenv import load_dotenv
from loguru import logger

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import StartFrame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.utils.asyncio.task_manager import TaskManager
from pipecat.audio.utils import create_stream_resampler

from voice_super_agent.runtime import Runtime
from voice_super_agent.voice.volcengine import VolcengineSTTService, VolcengineTTSService
from voice_super_agent.voice.interruption import SystemOneInterruptionProcessor, _Candidate


ROOT = Path(__file__).resolve().parents[1]
SENTENCE = "这是语音接口验收，项目代号是琥珀松鼠。"


async def fail_speech(error_msg, **kwargs):
    raise RuntimeError(error_msg)


async def wait_reply(runtime, reply_id):
    async with asyncio.timeout(90):
        while True:
            snapshot = runtime.connect("measure")
            reply = next(row for row in snapshot["messages"] if row["id"] == reply_id)
            if reply["status"] in {"completed", "failed", "unfinished"}:
                assert reply["status"] == "completed", reply["status"]
                return reply
            await asyncio.sleep(0.01)


async def llm_measure(directory):
    path = directory / "live.sqlite3"
    runtime = Runtime(database_path=path)
    runtime.start()
    first = {}
    original = runtime._conversation_event
    def event(data):
        if data["type"] == "text":
            first.setdefault(data["reply_id"], perf_counter())
        original(data)
    runtime._conversation_event = event
    samples = []
    code = "琥珀松鼠-7426"
    prompts = [f"我的项目代号是{code}。只回复已记住。", "我的项目代号是什么？只返回代号。",
               "请检索并读取以前的对话原文，找到我的项目代号。只返回代号。"]
    try:
        for index, text in enumerate(prompts):
            if index == 2:
                runtime.command("restart_conversation")
                assert not runtime.connect("measure")["messages"]
            started = perf_counter()
            receipt = runtime.send_text(text)
            reply = await wait_reply(runtime, receipt["reply_id"])
            samples.append({"scenario": ["首次文字", "共享上下文追问", "重新开始后历史回读"][index],
                            "first_text_ms": (first[receipt["reply_id"]] - started) * 1000,
                            "completed_ms": (perf_counter() - started) * 1000,
                            "answer": reply["text"], "history_status": runtime.connect("measure")["state"]["history_status"]})
            if index:
                assert code in reply["text"]
        tools = [record["message"] for record in runtime._model_records if record["message"]["role"] == "tool"]
        calls = [call["function"]["name"] for record in runtime._model_records
                 for call in record["message"].get("tool_calls", [])]
        assert "history_search" in calls and "history_read" in calls
        assert any(code in json.loads(tool["content"]).get("message", {}).get("text", "") for tool in tools)
    finally:
        runtime.stop()
    runtime = Runtime(database_path=path)
    runtime.start()
    try:
        receipt = runtime.send_text("刚才找到的项目代号是什么？直接返回代号。")
        reply = await wait_reply(runtime, receipt["reply_id"])
        assert code in reply["text"]
    finally:
        runtime.stop()
    return {"samples": samples, "restart_followup": "passed", "tool_results": len(tools), "tool_calls": calls}


async def tts_measure(session):
    timings = []
    audio = b""
    for index in range(3):
        service = VolcengineTTSService(api_key=os.environ["VOLCENGINE_API_KEY"], session=session)
        service.push_error = fail_speech
        started = perf_counter()
        first = None
        pcm = bytearray()
        async for frame in service.run_tts(SENTENCE, f"measure-{index}"):
            if isinstance(frame, TTSAudioRawFrame):
                if first is None:
                    first = perf_counter()
                pcm.extend(frame.audio)
        assert pcm
        timings.append({"first_audio_chunk_ms": (first - started) * 1000,
                        "completed_ms": (perf_counter() - started) * 1000, "pcm_bytes": len(pcm)})
        audio = bytes(pcm)
        await service.cleanup()
    return {"samples": timings, "sample_rate": 24000, "sentence": SENTENCE}, audio


async def stt_measure(audio):
    samples = []
    for _ in range(3):
        service = VolcengineSTTService(api_key=os.environ["VOLCENGINE_API_KEY"], defer_connection=True)
        finals = []
        async def capture(frame, *args, **kwargs):
            if isinstance(frame, TranscriptionFrame):
                finals.append({"text": frame.text, "at": perf_counter()})
        service.push_frame = capture
        service.push_error = fail_speech
        clock = SystemClock()
        clock.start()
        await service.setup(FrameProcessorSetup(clock=clock, task_manager=TaskManager(), pipeline_worker=None))
        await service.start(StartFrame(audio_in_sample_rate=16000))
        try:
            started = perf_counter()
            await service.connect()
            ready = perf_counter()
            resampler = create_stream_resampler()
            pcm = await resampler.resample(audio, 24000, 16000)
            pcm += bytes(32000)
            for offset in range(0, len(pcm), 6400):
                async for _frame in service.run_stt(pcm[offset:offset + 6400]):
                    pass
                await asyncio.sleep(0.2)
            async with asyncio.timeout(10):
                while "琥珀松鼠" not in "".join(row["text"] for row in finals):
                    await asyncio.sleep(0.02)
            samples.append({"ready_ms": (ready - started) * 1000,
                            "first_final_from_ready_ms": (finals[0]["at"] - ready) * 1000,
                            "transcript": "".join(row["text"] for row in finals)})
        finally:
            await service.cleanup()
    return {"samples": samples, "input": "真实 TTS 合成 PCM，重采样到16kHz并实时供帧；无麦克风"}


async def system_one_measure(session):
    context = LLMContext(messages=[{"role": "user", "content": "请介绍这个方案。"},
                                   {"role": "assistant", "content": "我正在介绍方案的三个部分，首先是语音输入。"}])
    results = []
    for text, expected in [("嗯嗯，你继续。", "backchannel"), ("等一下，我改一下。", "interrupt"), ("先别说了。", "interrupt")]:
        processor = SystemOneInterruptionProcessor(api_key=os.environ["ARK_API_KEY"], session=session, context=context)
        frames = []
        async def capture(frame, *args, **kwargs):
            frames.append(frame)
        processor.queue_frame = capture
        started = perf_counter()
        await processor._classify(_Candidate(), text)
        result = frames[-1]
        if result.error:
            raise RuntimeError("System One classification failed") from result.error
        results.append({"text": text, "decision": result.decision, "expected": expected,
                        "correct": result.decision == expected, "elapsed_ms": (perf_counter() - started) * 1000})
        await processor.cleanup()
    assert all(row["correct"] for row in results)
    return {"samples": results, "classification_median_ms": median(row["elapsed_ms"] for row in results)}


async def measure(output):
    load_dotenv(ROOT / ".env", encoding="utf-8-sig")
    logger.disable("pipecat")
    result = {"measured_at": datetime.now(timezone.utc).isoformat(), "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
              "condition": "真实云端接口与SDK，音频只在内存处理；各语音接口3次，文字为不同场景及重启追问，不报告小样本P95", "services": {}}
    failures = []
    with TemporaryDirectory(prefix="service-measure-", dir=output.parent) as directory:
        probes = {"llm": lambda: llm_measure(Path(directory))}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            probes["tts"] = lambda: tts_measure(session)
            probes["system_one"] = lambda: system_one_measure(session)
            for name, probe in probes.items():
                try:
                    value = await probe()
                    if name == "tts":
                        value, audio = value
                    result["services"][name] = {"status": "passed", **value}
                except (RuntimeError, TimeoutError, aiohttp.ClientError, AssertionError) as exc:
                    failures.append(name)
                    result["services"][name] = {"status": "failed", "error_type": type(exc).__name__}
                output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{name}: {result['services'][name]['status']}", flush=True)
            if "tts" not in failures:
                try:
                    result["services"]["asr"] = {"status": "passed", **await stt_measure(audio)}
                except (RuntimeError, TimeoutError, aiohttp.ClientError, AssertionError, OSError) as exc:
                    failures.append("asr")
                    result["services"]["asr"] = {"status": "failed", "error_type": type(exc).__name__}
                print(f"asr: {result['services']['asr']['status']}", flush=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return bool(failures)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-services", action="store_true", help="使用本地 .env 的真实云服务，产生服务调用")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/verification/stage-15/services.json")
    args = parser.parse_args()
    if not args.live_services:
        parser.error("真实服务测量需要显式提供 --live-services")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(measure(args.output)))
