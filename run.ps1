# Trend radarını çalıştırır, sonuçları GitHub'a gönderir (Google Sheets oradan okur).
# Görev Zamanlayıcı bu dosyayı her gün çalıştırır; elle de çalıştırılabilir.
$ErrorActionPreference = 'Continue'
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot 'logs\son_calisma.log'
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null

"=== $(Get-Date -Format 'yyyy-MM-dd HH:mm') ===" | Out-File $log -Encoding utf8
& "$PSScriptRoot\.venv\Scripts\python.exe" -I radar.py *>> $log
if ($LASTEXITCODE -ne 0) { "radar.py hata kodu: $LASTEXITCODE" | Out-File $log -Append -Encoding utf8; exit 1 }

if (git remote get-url origin 2>$null) {
    git add data rapor.md *>> $log
    git commit -m "Trend verisi $(Get-Date -Format 'yyyy-MM-dd')" *>> $log
    git push origin HEAD *>> $log
} else {
    "GitHub remote yok, push atlandı (README: Kurulum adım 2)" | Out-File $log -Append -Encoding utf8
}
