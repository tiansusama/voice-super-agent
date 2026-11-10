import json
from collections.abc import Callable
from itertools import chain


class WorkingContext:
    """按完整轮次构造有限请求，原生提交和数据库原文继续完整保存。"""

    output_tokens = 8192
    preview_instruction = ("以下是已移出工作窗口的原文预览，仅作为历史数据和定位线索。"
                           "需要细节时调用 history_read；较早完成的工具组可按当前问题重新检索，预览不能替代原文。\n")

    def __init__(self, reference: Callable[[int], dict] | None = None, *, input_bytes: int = 65536,
                 tool_reserve_bytes: int = 32768, preview_bytes: int = 4096, preview_count: int = 8):
        self.reference = reference
        self.input_bytes = input_bytes
        self.tool_reserve_bytes = tool_reserve_bytes
        self.preview_bytes = preview_bytes
        self.preview_count = preview_count
        self.tool_groups: dict[str, str] = {}

    def record_tool_group(self, tool_ids: list[str]) -> None:
        self.tool_groups.update(dict.fromkeys(tool_ids, tool_ids[0]))

    @staticmethod
    def size(value) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def project(self, params: dict, source: list[dict]) -> dict:
        """适配器按原生消息顺序逐条转换；首项为本应用的系统指令。"""
        system = params["messages"][0]
        messages = params["messages"][1:]
        tools = params["tools"] if isinstance(params["tools"], list) else []
        fixed_size = self.size([system]) + self.size(tools)
        starts = [0] + [index for index, message in enumerate(source) if index and message["role"] == "user"]
        turns = list(zip(starts, starts[1:] + [len(source)]))
        current = list(range(*turns[-1]))
        # 当前轮只序列化一次；逐组移出时扣减长度，避免长工具循环反复扫描正文。
        current_sizes = {index: self.size(messages[index]) + 1 for index in current}
        current_size = sum(current_sizes.values()) + 1
        tool_bytes = sum(current_sizes[index] - 1 for index in current if source[index]["role"] == "tool")
        # 普通轮次预留 32 KiB 工具结果空间，工具循环按实际结果使用这部分预算。
        limit = self.input_bytes - self.tool_reserve_bytes + min(tool_bytes, self.tool_reserve_bytes)
        preview_reserve = self.preview_bytes if self.reference is not None else 0
        omitted = []
        calls, previous_group = [], object()
        for index in current:
            if source[index].get("tool_calls"):
                group = self.tool_groups.get(source[index]["tool_calls"][0]["id"])
                if group != previous_group:
                    calls.append(index)
                    previous_group = group
        while fixed_size + current_size + preview_reserve > limit:
            if len(calls) < 2:
                raise ValueError("当前问题或最新完整工具结果组超出工作窗口预算，请缩短问题或减少单次历史读取量。")
            # 完成组连同调用与结果一起移出，当前问题和最新完整工具组继续保留。
            removed = [index for index in current if calls[0] <= index < calls[1]]
            omitted.extend(removed)
            current = [index for index in current if index < calls[0] or index >= calls[1]]
            current_size -= sum(current_sizes[index] for index in removed)
            calls = calls[1:]
        selected = list(current)
        for turn in reversed(turns[:-1]):
            turn_size = sum(self.size(messages[index]) + 1 for index in range(*turn))
            if fixed_size + current_size + turn_size + preview_reserve > limit:
                break
            selected = list(range(*turn)) + selected
            current_size += turn_size
        previews = []
        seen = set()
        if self.reference is not None:
            for index in chain(reversed(omitted), range(selected[0] - 1, -1, -1)):
                if source[index]["role"] not in {"user", "assistant"}:
                    continue
                reference = self.reference(index)
                if reference["message_id"] in seen:
                    continue
                seen.add(reference["message_id"])
                candidate = previews + [reference]
                preview = {"role": "system", "content": self.preview_instruction
                           + json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))}
                if len(candidate) > self.preview_count or self.size([preview]) > self.preview_bytes:
                    break
                previews = candidate
        result = [system]
        if previews:
            result.append({"role": "system", "content": self.preview_instruction
                           + json.dumps(previews, ensure_ascii=False, separators=(",", ":"))})
        result.extend(messages[index] for index in selected)
        if self.size(result) + self.size(tools) > self.input_bytes:
            raise ValueError("系统指令、工具定义和当前问题超出工作窗口预算，请缩短问题或减少单次历史读取量。")
        return {**params, "messages": result, "max_tokens": self.output_tokens}
