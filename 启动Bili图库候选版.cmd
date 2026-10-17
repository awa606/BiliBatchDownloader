@echo off
setlocal
set "BILIDESK_ROOT=%~dp0"
if not exist "%BILIDESK_ROOT%.private\library-gallery-verify\library.sqlite3" (
  echo The offline gallery candidate library is not configured for this checkout.
  echo No library was created, imported or downloaded.
  pause
  exit /b 2
)
if not exist "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.2.0-verify-r1\BiliBatchDownloader.exe" (
  echo The 0.2.0 VERIFY candidate package is missing.
  pause
  exit /b 3
)
start "" "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.2.0-verify-r1\BiliBatchDownloader.exe" --data-dir "%BILIDESK_ROOT%.private\library-gallery-verify" --offline --require-existing-library
exit /b
