# Windows 11 x64 安装原型

## 安装并启动

1. 双击构建产物 `dist/installer/VoiceSuperAgent-Setup.exe`。首次安装时，分别选择程序安装目录和运行数据保存目录。
2. 在最后一步选择“打开小博”，或在 Windows 开始菜单找到“小博”。
3. 首次打开时在配置窗口填写自己的 DeepSeek Key，即可启用文字对话。需要语音时再填写火山 Key 和方舟 Key。也可以选择“稍后设置”，先查看应用和历史。

安装器自带 Python 运行时、前端页面和本地唤醒模型。目标电脑只需 Windows 11 x64，缺少 Microsoft Edge WebView2 Runtime 时，安装器会通过包内的微软引导程序联网安装。

程序的默认安装目录为 `%LOCALAPPDATA%\Programs\VoiceSuperAgent`。运行数据默认保存在 `%LOCALAPPDATA%\VoiceSuperAgent`，包含密钥文件 `.env`、设置、对话、任务记录、日志和 WebView2 数据。两个目录须可写、彼此独立，首次安装的程序目录须为空。

需要换密钥时，从系统托盘退出小博，再从开始菜单打开“配置小博”。直接覆盖升级会沿用原来的两个位置。需要更换位置时，从 Windows 的“已安装的应用”卸载后重新安装。卸载会保留原数据和数据目录选择。重装时选回原数据目录即可继续使用其中的记录。

安装版首次使用独立的用户数据目录。开发工作副本的数据继续保存在原项目的 `.local`。

## 构建安装包

在 Windows 11 x64 开发机准备 Git、uv、Node.js/npm、[NSIS 编译器](https://nsis.sourceforge.io/Download)，以及带 Microsoft 签名的 [WebView2 Evergreen Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/#download-section)。从项目根目录执行以下命令，将两个路径替换为实际位置。

```powershell
.\scripts\windows-installer\build.ps1 -MakeNSIS 'C:\tools\nsis\makensis.exe' -WebViewBootstrap 'C:\tools\MicrosoftEdgeWebview2Setup.exe'
```

构建通过 uv 同步锁定的应用依赖，并用 PyInstaller 生成应用目录。安装包输出到 `dist/installer/VoiceSuperAgent-Setup.exe`。打包内容来自构建产物和项目资源，密钥在用户首次启动时填写。
