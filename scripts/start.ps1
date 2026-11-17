$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# 已运行时复用现有实例的通知入口，直接显示原主窗口。
try {
    $activation = [System.Threading.EventWaitHandle]::OpenExisting('Local\VoiceSuperAgent.Activate')
} catch [System.Threading.WaitHandleCannotBeOpenedException] {
    $activation = $null
}
if ($null -ne $activation) {
    [void]$activation.Set()
    $activation.Dispose()
    exit 0
}

$preparation = [System.Threading.Mutex]::new($false, 'Local\VoiceSuperAgent.Prepare')
if (-not $preparation.WaitOne(0)) {
    [void][System.Windows.Forms.MessageBox]::Show('运行环境正在准备，请等待已有进度窗口完成。', '小博')
    $preparation.Dispose()
    exit 0
}

$projectRoot = Split-Path $PSScriptRoot -Parent
$logDirectory = Join-Path $projectRoot '.local/logs'
$logPath = Join-Path $logDirectory 'setup.log'
$script:prepareProcess = $null
$script:preparing = $true
$script:cancelled = $false
$script:exitCode = 1
$steps = @(
    @{ Name = 'Python'; Label = '准备 Python 运行环境' },
    @{ Name = 'Frontend'; Label = '准备界面依赖' },
    @{ Name = 'Build'; Label = '构建桌面界面' },
    @{ Name = 'Model'; Label = '准备本地唤醒模型' }
)

$form = New-Object System.Windows.Forms.Form
$form.Text = '小博 · 启动准备'
$form.ClientSize = New-Object System.Drawing.Size(480, 228)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.ColorTranslator]::FromHtml('#FBFAF7')
$form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$title = New-Object System.Windows.Forms.Label
$title.Text = '正在为小博准备运行环境'
$title.Location = New-Object System.Drawing.Point(24, 22)
$title.Size = New-Object System.Drawing.Size(432, 34)
$title.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 16)
$status = New-Object System.Windows.Forms.Label
$status.Location = New-Object System.Drawing.Point(24, 72)
$status.Size = New-Object System.Drawing.Size(432, 24)
$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(24, 104)
$progress.Size = New-Object System.Drawing.Size(432, 16)
$progress.Maximum = $steps.Count
$detail = New-Object System.Windows.Forms.Label
$detail.Text = '首次运行需要下载依赖和模型，后续会复用已准备的内容。'
$detail.Location = New-Object System.Drawing.Point(24, 132)
$detail.Size = New-Object System.Drawing.Size(432, 42)
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = '取消'
$cancel.Location = New-Object System.Drawing.Point(372, 182)
$cancel.Size = New-Object System.Drawing.Size(84, 30)
$cancel.Add_Click({ $form.Close() })
$form.Controls.AddRange(@($title, $status, $progress, $detail, $cancel))
$form.Add_FormClosing({
    if ($script:preparing) {
        $script:cancelled = $true
        if ($null -ne $script:prepareProcess -and -not $script:prepareProcess.HasExited) {
            # 只取消本次准备创建的进程树，释放 uv/npm 子进程后再允许下次准备。
            Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
                -ArgumentList "/PID $($script:prepareProcess.Id) /T /F" -WindowStyle Hidden -Wait
        }
    }
})
$form.Add_Shown({
    try {
        New-Item -ItemType Directory -Force $logDirectory | Out-Null
        foreach ($command in @('git', 'uv', 'node', 'npm')) {
            if ($null -eq (Get-Command $command -ErrorAction SilentlyContinue)) {
                [void][System.Windows.Forms.MessageBox]::Show("缺少 $command，请先按 docs/development.md 安装开发工具后重新启动。", '准备未完成')
                $script:preparing = $false
                $form.Close()
                return
            }
        }
        for ($index = 0; $index -lt $steps.Count; $index++) {
            $step = $steps[$index]
            $status.Text = "$($index + 1)/$($steps.Count)  $($step.Label)"
            $progress.Value = $index
            $form.Refresh()
            Add-Content -LiteralPath $logPath -Value "`n$(Get-Date -Format o) $($step.Name)" -Encoding UTF8
            $process = New-Object System.Diagnostics.Process
            $process.StartInfo.FileName = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
            $process.StartInfo.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'prepare-dev.ps1') + '" -Step ' + $step.Name
            $process.StartInfo.WorkingDirectory = $projectRoot
            $process.StartInfo.UseShellExecute = $false
            $process.StartInfo.CreateNoWindow = $true
            $process.StartInfo.RedirectStandardOutput = $true
            $process.StartInfo.RedirectStandardError = $true
            $process.StartInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
            $process.StartInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
            [void]$process.Start()
            $script:prepareProcess = $process
            $stdout = $process.StandardOutput.ReadToEndAsync()
            $stderr = $process.StandardError.ReadToEndAsync()
            $elapsed = [System.Diagnostics.Stopwatch]::StartNew()
            while (-not $process.HasExited) {
                $detail.Text = "当前步骤已用时 $([int]$elapsed.Elapsed.TotalSeconds) 秒。首次准备需要下载依赖或模型，请保持网络连接。"
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 100
            }
            $process.WaitForExit()
            Add-Content -LiteralPath $logPath -Value ($stdout.Result + $stderr.Result) -Encoding UTF8
            $code = $process.ExitCode
            $process.Dispose()
            $script:prepareProcess = $null
            if ($script:cancelled) { return }
            if ($code -ne 0) {
                $script:preparing = $false
                [void][System.Windows.Forms.MessageBox]::Show("$($step.Label)失败。请查看日志：`n$logPath`n`n处理原因后重新双击 start.cmd。", '准备未完成')
                $form.Close()
                return
            }
            $progress.Value = $index + 1
        }
        $status.Text = '准备完成，正在启动小博…'
        $detail.Text = '正在打开桌面，首次加载可能需要几秒钟。'
        $form.Refresh()
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $projectRoot '.venv'
        $arguments = 'run --project "' + $projectRoot + '" --no-sync --offline "' + (Join-Path $projectRoot 'desktop.pyw') + '"'
        $script:prepareProcess = Start-Process -FilePath (Get-Command uv).Source -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
        $activated = $false
        # 等待应用接过单实例通知，覆盖准备窗口关闭到 Python 初始化之间的交接。
        while (-not $script:prepareProcess.HasExited) {
            try {
                $activation = [System.Threading.EventWaitHandle]::OpenExisting('Local\VoiceSuperAgent.Activate')
                $activation.Dispose()
                $activated = $true
                break
            } catch [System.Threading.WaitHandleCannotBeOpenedException] {
                [System.Windows.Forms.Application]::DoEvents()
                Start-Sleep -Milliseconds 100
            }
            if ($script:cancelled) { return }
        }
        $script:prepareProcess.Dispose()
        $script:prepareProcess = $null
        $script:preparing = $false
        $script:exitCode = if ($activated) { 0 } else { 1 }
        $form.Close()
    } catch [System.IO.IOException], [System.UnauthorizedAccessException], [System.ComponentModel.Win32Exception], [System.InvalidOperationException] {
        $failure = $_.Exception.Message
        # 关闭准备窗口先终止本次子进程，再显示具体失败原因并退出。
        $form.Close()
        [void][System.Windows.Forms.MessageBox]::Show("启动准备失败：$failure`n`n处理原因后重新双击 start.cmd。", '准备未完成')
    } finally {
        if ($null -ne $script:prepareProcess) {
            $script:prepareProcess.Dispose()
            $script:prepareProcess = $null
        }
    }
})
try {
    [void]$form.ShowDialog()
} finally {
    $form.Dispose()
    $preparation.ReleaseMutex()
    $preparation.Dispose()
}
exit $script:exitCode
