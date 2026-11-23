from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory
import tkinter as tk
from tkinter import messagebox, ttk

from dotenv import dotenv_values, set_key


SERVICE_FIELDS = (
    ("DEEPSEEK_API_KEY", "DeepSeek Key（文字对话）"),
    ("VOLCENGINE_API_KEY", "火山 Key（语音识别和播报，可选）"),
    ("ARK_API_KEY", "方舟 Key（语音判意，可选）"),
)


def save_service_keys(path: Path, values: dict[str, str]) -> None:
    keys = {name: values[name].strip() for name, _ in SERVICE_FIELDS}
    if not keys["DEEPSEEK_API_KEY"]:
        raise ValueError("请输入 DeepSeek Key，也可以稍后设置。")
    if any(character in value for value in keys.values() for character in "\r\n\0"):
        raise ValueError("请填写单行密钥。")
    # 一次替换配置，保存失败时保留原文件及其他配置项。
    with TemporaryDirectory(dir=path.parent) as directory:
        proposed = Path(directory) / path.name
        if path.is_file():
            copyfile(path, proposed)
        for name, value in keys.items():
            set_key(proposed, name, value, encoding="utf-8-sig")
        proposed.replace(path)


def configure_services(path: Path) -> None:
    existing = dotenv_values(path, encoding="utf-8-sig", interpolate=False)
    window = tk.Tk()
    window.title("小博 · 配置服务")
    window.resizable(False, False)
    body = ttk.Frame(window, padding=22)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="填写服务密钥", font=("Microsoft YaHei UI", 15)).pack(anchor="w")
    ttk.Label(
        body, text="填写 DeepSeek Key 即可使用文字对话。\n语音可在开始菜单的“配置小博”中补齐。",
    ).pack(anchor="w", pady=(6, 12))

    fields = {}
    for name, label in SERVICE_FIELDS:
        ttk.Label(body, text=label).pack(anchor="w")
        entry = ttk.Entry(body, show="●", width=48)
        entry.insert(0, existing.get(name) or "")
        entry.pack(fill="x", pady=(3, 10))
        fields[name] = entry

    def save():
        try:
            save_service_keys(path, {name: entry.get() for name, entry in fields.items()})
        except (OSError, ValueError) as error:
            messagebox.showerror("配置未保存", str(error), parent=window)
            return
        window.destroy()

    buttons = ttk.Frame(body)
    buttons.pack(anchor="e", pady=(3, 0))
    ttk.Button(buttons, text="稍后设置", command=window.destroy).pack(side="left")
    ttk.Button(buttons, text="保存并打开小博", command=save).pack(side="left", padx=(8, 0))
    window.bind("<Return>", lambda _event: save())
    window.bind("<Escape>", lambda _event: window.destroy())
    window.update_idletasks()
    width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    left = (window.winfo_screenwidth() - width) // 2
    top = (window.winfo_screenheight() - height) // 2
    window.geometry(f"+{left}+{top}")
    fields["DEEPSEEK_API_KEY"].focus_set()
    window.mainloop()
