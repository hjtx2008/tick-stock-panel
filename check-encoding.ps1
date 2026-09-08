[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
$OutputEncoding           = New-Object System.Text.UTF8Encoding $false

Write-Host "=== PARENT process ===" -ForegroundColor Cyan
Write-Host ("OutputEncoding  : {0}" -f [Console]::OutputEncoding.WebName)
$chcpOut = & cmd /c "chcp"
$chcpNum = ($chcpOut | Select-String -Pattern "\d+").Matches[0].Value
Write-Host ("ActiveCodePage  : {0}" -f $chcpNum)
Write-Host ("PYTHONIOENCODING: {0}" -f $env:PYTHONIOENCODING)
Write-Host ("NO_COLOR        : {0}" -f $env:NO_COLOR)

$probe = Start-Job -ScriptBlock {
    try { & cmd /c "chcp 65001 >nul" 2>$null } catch {}
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding           = New-Object System.Text.UTF8Encoding $false
    $env:PYTHONIOENCODING = 'utf-8'
    $chcpOut = & cmd /c "chcp"
    $chcpNum = ($chcpOut | Select-String -Pattern "\d+").Matches[0].Value
    [pscustomobject]@{
        OutputEncoding   = [Console]::OutputEncoding.WebName
        Chcp             = $chcpNum
        PythonIoEncoding = $env:PYTHONIOENCODING
        ChineseTest      = "你好世界, UTF-8 should display correctly"
    } | ConvertTo-Json
}
$out = Receive-Job $probe -Wait -AutoRemoveJob

Write-Host ""
Write-Host "=== CHILD job (Start-Job) ===" -ForegroundColor Cyan
Write-Host $out
Write-Host ""
Write-Host "If the Chinese line above reads as 'ni hao shi jie', the fix works." -ForegroundColor Yellow