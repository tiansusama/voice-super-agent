Unicode true
RequestExecutionLevel user
SetCompressor /SOLID lzma
Name "小博"
OutFile "${SETUP_FILE}"
InstallDir "$LOCALAPPDATA\Programs\VoiceSuperAgent"
VIProductVersion "1.0.1.0"
VIAddVersionKey /LANG=2052 "ProductName" "小博"
VIAddVersionKey /LANG=2052 "FileDescription" "小博 Windows 11 x64 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion" "1.0.1.0"
VIAddVersionKey /LANG=2052 "LegalCopyright" "Copyright (c) 2026 Xuyu Wang"

!include "MUI2.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!define APP_REG "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceSuperAgent"
!define WEBVIEW_REG "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\VoiceSuperAgent.exe"
!define MUI_FINISHPAGE_RUN_TEXT "打开小博"
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "SimpChinese"

!macro RequireAppClosed
    System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w "Local\VoiceSuperAgent") p.r0'
    ${If} $0 != 0
        System::Call 'kernel32::CloseHandle(p r0)'
        MessageBox MB_OK|MB_ICONINFORMATION "请先从系统托盘退出小博，再继续。" /SD IDOK
        Abort
    ${EndIf}
!macroend

Function .onInit
    ${WinVerGetBuild} $0
    ${IfNot} ${IsNativeAMD64}
    ${OrIf} $0 < 22000
    ${OrIf} ${IsServerOS}
        MessageBox MB_OK|MB_ICONSTOP "这个安装包适用于 Windows 11 x64。" /SD IDOK
        Abort
    ${EndIf}
    !insertmacro RequireAppClosed
    SetShellVarContext current
FunctionEnd

Function un.onInit
    !insertmacro RequireAppClosed
    SetShellVarContext current
FunctionEnd

Section "安装"
    # WebView2 官方安装信息在当前用户或 32 位机器注册表视图中。
    SetRegView 32
    ReadRegStr $0 HKCU "${WEBVIEW_REG}" "pv"
    ${If} $0 == ""
    ${OrIf} $0 == "0.0.0.0"
        ReadRegStr $0 HKLM "${WEBVIEW_REG}" "pv"
    ${EndIf}
    ${If} $0 == ""
    ${OrIf} $0 == "0.0.0.0"
        DetailPrint "正在安装 Microsoft Edge WebView2 Runtime…"
        InitPluginsDir
        SetOutPath "$PLUGINSDIR"
        File /oname=MicrosoftEdgeWebview2Setup.exe "${WEBVIEW_BOOTSTRAP}"
        ClearErrors
        ExecWait '"$PLUGINSDIR\MicrosoftEdgeWebview2Setup.exe" /silent /install' $0
        ${If} ${Errors}
        ${OrIf} $0 != 0
            MessageBox MB_OK|MB_ICONSTOP "WebView2 安装失败。请联网后重新运行安装程序。" /SD IDOK
            SetErrorLevel 1
            Abort
        ${EndIf}
    ${EndIf}

    !insertmacro RequireAppClosed
    SetRegView 64
    SetOutPath "$INSTDIR"
    File /r "${BUNDLE_DIR}\*"
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    CreateDirectory "$SMPROGRAMS\小博"
    CreateShortcut "$SMPROGRAMS\小博\小博.lnk" "$INSTDIR\VoiceSuperAgent.exe"
    CreateShortcut "$SMPROGRAMS\小博\配置小博.lnk" "$INSTDIR\VoiceSuperAgent.exe" "--configure"
    WriteRegStr HKCU "${APP_REG}" "DisplayName" "小博（安装原型）"
    WriteRegStr HKCU "${APP_REG}" "DisplayVersion" "1.0.1-prototype"
    WriteRegStr HKCU "${APP_REG}" "DisplayIcon" "$INSTDIR\VoiceSuperAgent.exe"
    WriteRegStr HKCU "${APP_REG}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${APP_REG}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegDWORD HKCU "${APP_REG}" "NoModify" 1
    WriteRegDWORD HKCU "${APP_REG}" "NoRepair" 1
SectionEnd

Section "Uninstall"
    !insertmacro RequireAppClosed
    SetRegView 64
    ClearErrors
    Delete "$INSTDIR\VoiceSuperAgent.exe"
    ${If} ${FileExists} "$INSTDIR\_internal\*"
        RMDir /r "$INSTDIR\_internal"
    ${EndIf}
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "部分程序文件仍在使用，请关闭小博后重试卸载。" /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    Delete "$SMPROGRAMS\小博\小博.lnk"
    Delete "$SMPROGRAMS\小博\配置小博.lnk"
    RMDir "$SMPROGRAMS\小博"
    DeleteRegKey HKCU "${APP_REG}"
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
SectionEnd
