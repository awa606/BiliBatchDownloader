@echo off
setlocal
set "BILIDESK_ROOT=%~dp0"
if not exist "%BILIDESK_ROOT%.private\library-gallery-online-verify-r5\library.sqlite3" exit /b 2
if not exist "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.3.0-verify-r5\BiliBatchDownloader.exe" exit /b 3
start "" "%BILIDESK_ROOT%dist\BiliBatchDownloader-0.3.0-verify-r5\BiliBatchDownloader.exe" --data-dir "%BILIDESK_ROOT%.private\library-gallery-online-verify-r5" --offline --require-existing-library
exit /b
