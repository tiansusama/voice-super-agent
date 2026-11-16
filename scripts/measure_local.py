"""测量本地历史、上下文投影和离线 KWS；数据写入独立临时库。"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
from statistics import median, quantiles
from tempfile import TemporaryDirectory
from time import perf_counter, process_time

import psutil

from voice_super_agent.memory.context import WorkingContext
from voice_super_agent.memory.store import ConversationStore
from voice_super_agent.voice.wake import KeywordDetector, MODEL_NAME


ROOT = Path(__file__).resolve().parents[1]


def summary(samples):
    result = {"n": len(samples), "min": min(samples), "median": median(samples), "max": max(samples)}
    if len(samples) >= 20:
        result["p95"] = quantiles(samples, n=100, method="inclusive")[94]
    return result


async def measure(output):
    result = {"measured_at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
              "windows": platform.platform(), "logical_cpus": psutil.cpu_count(),
              "physical_memory_gib": psutil.virtual_memory().total / 2**30, "history": [], "kws": {}}
    with TemporaryDirectory(prefix="local-measure-", dir=output.parent) as directory:
        for count in (2000, 20000):
            store = ConversationStore(Path(directory) / f"history-{count}.sqlite3")
            await store.open()
            messages = [{"id": f"m{i}", "reply_id": f"r{i // 2}", "role": "user" if i % 2 == 0 else "assistant",
                         "source": "text", "status": "completed", "created_at": "2026-11-16T01:00:00+00:00",
                         "error": "", "text": f"主题{i}：杭州旅行方案。" + "这段原文用于测量历史增长后的查询与上下文整理。" * 10}
                        for i in range(count)]
            await store.save(messages, [], {"display_start_position": 0, "model_start_position": 0})
            native = [{"role": row["role"], "content": row["text"]} for row in messages]
            reference = lambda index: {"message_id": messages[index]["id"], "created_at": messages[index]["created_at"],
                                       "source": "text", "status": "completed", "preview": messages[index]["text"][:120]}
            window = WorkingContext(reference)
            params = {"messages": [{"role": "system", "content": "你是中文助手。"}, *native], "tools": []}
            timings = {"list_ms": [], "search_ms": [], "read_ms": [], "context_ms": []}
            try:
                for _ in range(30):
                    start = perf_counter()
                    page = await store.list_history()
                    timings["list_ms"].append((perf_counter() - start) * 1000)
                    assert len(page["messages"]) == 20 and page["messages"][0]["id"] == f"m{count - 1}"
                    start = perf_counter()
                    found = await store.history_search(keywords=[f"主题{count - 1}："])
                    timings["search_ms"].append((perf_counter() - start) * 1000)
                    assert found["matches"][0]["id"] == f"m{count - 1}"
                    start = perf_counter()
                    original = await store.history_read(message_id=f"m{count - 1}", limit=8000)
                    timings["read_ms"].append((perf_counter() - start) * 1000)
                    assert original["message"]["text"] == messages[-1]["text"]
                    start = perf_counter()
                    projected = window.project(params, native)
                    timings["context_ms"].append((perf_counter() - start) * 1000)
                result["history"].append({"messages": count, "dataset_utf8_bytes": len(json.dumps(native, ensure_ascii=False).encode()),
                                          "projected_bytes": window.size(projected["messages"]) + window.size(projected["tools"]),
                                          **{name: summary(values) for name, values in timings.items()}})
            finally:
                await store.close()
    model = ROOT / "resources" / "models" / MODEL_NAME
    loads = []
    for _ in range(3):
        start = perf_counter()
        detector = KeywordDetector(model, "你好小博")
        loads.append((perf_counter() - start) * 1000)
    start, cpu = perf_counter(), process_time()
    for _ in range(1000):
        assert not detector.feed(bytes(320), 16000)
    result["kws"] = {"initialization_ms": summary(loads), "input_audio_ms": 10000,
                     "batch_wall_ms": (perf_counter() - start) * 1000, "batch_cpu_ms": (process_time() - cpu) * 1000,
                     "condition": "1000 个 10ms、16kHz、16bit 单声道静音块，批量离线处理，不打开音频设备"}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/verification/stage-15/local.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(measure(args.output))
