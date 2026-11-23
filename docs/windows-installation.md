# Windows 11 x64 安装原型

## 安装并启动

1. 双击构建产物 `dist/installer/VoiceSuperAgent-Setup.exe`，跟随中文界面完成安装。
2. 在最后一步选择“打开小博”，或在 Windows 开始菜单找到“小博”。
3. 首次打开时在配置窗口填写自己的 DeepSeek Key，即可启用文字对话。需要语音时再填写火山 Key 和方舟 Key。也可以选择“稍后设置”，先查看应用和历史。

安装器自带 Python 运行时、前端页面和本地唤醒模型。目标电脑只需 Windows 11 x64，缺少 Microsoft Edge WebView2 Runtime 时，安装器会通过包内的微软引导程序联网安装。

需要换密钥时，从系统托盘退出小博，再从开始菜单打开“配置小博”。密钥保存在本机用户目录的 `.env`。程序目录为 `%LOCALAPPDATA%\Programs\VoiceSuperAgent`，用户配置、对话、任务、日志和 WebView2 数据在 `%LOCALAPPDATA%\VoiceSuperAgent`。从 Windows 的“已安装的应用”卸载时，用户数据继续保存在该目录。

安装版首次使用独立的用户数据目录。开发工作副本的数据继续保存在原项目的 `.local`。

## 构建安装包

在 Windows 11 x64 开发机准备 Git、uv、Node.js/npm、[NSIS 编译器](https://nsis.sourceforge.io/Download)，以及带 Microsoft 签名的 [WebView2 Evergreen Bootstrapper](https://developer.microsoft.com/microsoft-edge/webview2/#download-section)。从项目根目录执行以下命令，将两个路径替换为实际位置。

```powershell
.\scripts\windows-installer\build.ps1 -MakeNSIS 'C:\tools\nsis\makensis.exe' -WebViewBootstrap 'C:\tools\MicrosoftEdgeWebview2Setup.exe'
```

构建通过 uv 同步锁定的应用依赖，并用 PyInstaller 生成应用目录。安装包输出到 `dist/installer/VoiceSuperAgent-Setup.exe`。打包内容来自构建产物和项目资源，密钥在用户首次启动时填写。
