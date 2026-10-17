@echo off
setlocal
set "BILIDESK_ROOT=%~dp0"
if not exist "%BILIDESK_ROOT%.private\library-gallery-online-verify-r3\library.sqlite3" (
  echo The 0.3.0 r3 candidate library is missing. No empty library was created.
  pause
  exit /b 2
)
if not exist "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.3.0-verify-r3\BiliBatchDownloader.exe" (
  echo The 0.3.0 VERIFY r3 package is missing.
  pause
  exit /b 3
)
start "" "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.3.0-verify-r3\BiliBatchDownloader.exe" --data-dir "%BILIDESK_ROOT%.private\library-gallery-online-verify-r3" --enable-gallery-online --require-existing-library
exit /b
