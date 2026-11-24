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
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"
!define APP_REG "Software\Microsoft\Windows\CurrentVersion\Uninstall\VoiceSuperAgent"
!define SETTINGS_REG "Software\VoiceSuperAgent"
!define WEBVIEW_REG "Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
Var DataDir
Var PreviousInstall

!define MUI_FINISHPAGE_RUN "$INSTDIR\VoiceSuperAgent.exe"
!define MUI_FINISHPAGE_RUN_TEXT "打开小博"
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_HEADER_TEXT "选择程序安装位置"
!define MUI_PAGE_HEADER_SUBTEXT "选择小博程序文件的存放目录。"
!define MUI_DIRECTORYPAGE_TEXT_TOP "请为程序文件选一个空目录。下一步可以单独选择运行数据的位置。"
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirectoryOnUpgrade
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateProgramDirectory
!insertmacro MUI_PAGE_DIRECTORY
!define MUI_PAGE_HEADER_TEXT "选择运行数据保存位置"
!define MUI_PAGE_HEADER_SUBTEXT "选择密钥、设置、对话和任务记录的保存目录。"
!define MUI_DIRECTORYPAGE_TEXT_TOP "请选择与程序安装目录分开的文件夹。卸载时会保留此处的数据。"
!define MUI_DIRECTORYPAGE_VARIABLE $DataDir
!define MUI_PAGE_CUSTOMFUNCTION_PRE SkipDirectoryOnUpgrade
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE ValidateDataDirectory
!insertmacro MUI_PAGE_DIRECTORY
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

!macro NormalizeDirectory Directory
    # Win32 可以解析尚未创建的目录，向导允许用户输入新的文件夹。
    System::Call 'kernel32::GetFullPathNameW(w "${Directory}\.", i ${NSIS_MAX_STRLEN}, w .r0, p 0) i.r1'
    ${If} $1 == 0
    ${OrIf} $1 >= ${NSIS_MAX_STRLEN}
        MessageBox MB_OK|MB_ICONEXCLAMATION "无法解析目录：${Directory}。请检查路径后重试。" /SD IDOK
        Abort
    ${EndIf}
    StrCpy ${Directory} $0
!macroend

!macro EnsureWritableDirectory Directory
    ClearErrors
    CreateDirectory "${Directory}"
    ${IfNot} ${Errors}
        GetTempFileName $0 "${Directory}"
    ${EndIf}
    ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "无法写入目录：${Directory}$\r$\n请重新安装并选择当前用户可写入的目录。" /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    Delete "$0"
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
    SetRegView 64
    ReadRegStr $PreviousInstall HKCU "${APP_REG}" "InstallLocation"
    ${If} $PreviousInstall != ""
        StrCpy $INSTDIR $PreviousInstall
    ${EndIf}
    ReadRegStr $DataDir HKCU "${SETTINGS_REG}" "DataLocation"
    ${If} $DataDir == ""
        StrCpy $DataDir "$LOCALAPPDATA\VoiceSuperAgent"
    ${EndIf}
FunctionEnd

Function SkipDirectoryOnUpgrade
    ${If} $PreviousInstall != ""
        Abort
    ${EndIf}
FunctionEnd

Function ValidateProgramDirectory
    !insertmacro NormalizeDirectory $INSTDIR
    ${If} $PreviousInstall == ""
        ${DirState} "$INSTDIR" $0
        ${If} $0 == 1
            MessageBox MB_OK|MB_ICONEXCLAMATION "请选择一个空目录或新建的文件夹，以保护现有文件。" /SD IDOK
            Abort
        ${EndIf}
    ${EndIf}
FunctionEnd

Function ValidateDataDirectory
    !insertmacro NormalizeDirectory $DataDir
    StrLen $0 "$INSTDIR\"
    StrLen $1 "$DataDir\"
    StrCpy $2 "$DataDir\" $0
    StrCpy $3 "$INSTDIR\" $1
    ${If} $2 == "$INSTDIR\"
    ${OrIf} $3 == "$DataDir\"
        MessageBox MB_OK|MB_ICONEXCLAMATION "程序安装目录和运行数据目录必须分开，且彼此不能包含。" /SD IDOK
        Abort
    ${EndIf}
FunctionEnd

Function un.onInit
    !insertmacro RequireAppClosed
    SetShellVarContext current
FunctionEnd

Section "安装"
    Call ValidateProgramDirectory
    Call ValidateDataDirectory
    !insertmacro EnsureWritableDirectory "$INSTDIR"
    !insertmacro EnsureWritableDirectory "$DataDir"

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
    WriteRegStr HKCU "${SETTINGS_REG}" "DataLocation" "$DataDir"
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
