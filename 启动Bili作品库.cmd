@echo off
setlocal
set "BILIDESK_ROOT=%~dp0"
if not exist "%BILIDESK_ROOT%.private\library\library.sqlite3" (
  echo The verified local library is not configured for this checkout.
  echo No files were imported or downloaded.
  pause
  exit /b 2
)
start "" "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.1.0-verify-r8\BiliBatchDownloader.exe" --data-dir "%BILIDESK_ROOT%.private\library"
exit /b
