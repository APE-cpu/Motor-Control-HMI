@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   Motor Control HMI - Build Tool
echo ============================================
echo.
echo [1] Lite version  (no training, no scanned-PDF OCR, smaller)
echo [2] Full version  (training+DRL + scanned-PDF OCR, ~500MB+)
echo.
set /p choice=Enter option (1 or 2):

if "%choice%"=="1" goto :lite
if "%choice%"=="2" goto :full
echo Invalid option.
pause
exit /b 1

:lite
set BUILD_NAME=motor-control-hmi-lite
set ENTRY=main.py
set MODE=lite
goto :build

:full
set BUILD_NAME=motor-control-hmi-full
set ENTRY=main.py
set MODE=full
goto :build

:build
echo.
echo [0/3] Checking version consistency (APP_VERSION vs git tag)...
set APP_VERSION=
for /f "tokens=3" %%v in ('findstr /b /c:"APP_VERSION" main_window.py') do set APP_VERSION=%%~v
set GIT_TAG=
for /f "delims=" %%t in ('git describe --tags --abbrev=0 2^>nul') do set GIT_TAG=%%t
echo   APP_VERSION = v%APP_VERSION%   latest git tag = %GIT_TAG%
if "v%APP_VERSION%"=="%GIT_TAG%" goto :version_ok
echo   [!] Version mismatch: window title shows v%APP_VERSION% but latest git tag is %GIT_TAG%.
echo       Update APP_VERSION in main_window.py or create the tag before a release build.
set /p cont=Continue anyway (y/N)?
if /i "%cont%"=="y" goto :version_ok
exit /b 1
:version_ok
set BUILD_NAME=%BUILD_NAME%-v%APP_VERSION%

echo [1/3] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto :error

echo [2/3] Building %BUILD_NAME%...

rem 若项目根目录存在 ZLG 驱动，则一并打入 exe（放在 exe 同级可被加载）
set ZLG_DLL=
if exist "ControlCAN.dll" set ZLG_DLL=--add-binary "ControlCAN.dll;."
if defined ZLG_DLL (echo   [+] Found ControlCAN.dll, bundling into exe.) else (echo   [!] ControlCAN.dll not found, ZLG CAN will be unavailable in the exe.)

rem 致远原厂 zlgcan.dll + kerneldlls 整目录（ZCAN_* 新接口，USBCAN-II）
set ZLG_ZCAN=
if exist "zlgcan_x64\zlgcan.dll" set ZLG_ZCAN=--add-data "zlgcan_x64;zlgcan_x64"
if defined ZLG_ZCAN (echo   [+] Found zlgcan_x64, bundling into exe.) else (echo   [!] zlgcan_x64 not found, ZLGCAN-ZCAN backend will be unavailable in the exe.)

if "%MODE%"=="lite" (
    pyinstaller --noconfirm --clean --onefile --windowed ^
      --name "%BUILD_NAME%" ^
      --add-data "config/style.qss;config" ^
      --add-data "motor_anomaly.onnx;." ^
      %ZLG_DLL% ^
      %ZLG_ZCAN% ^
      --exclude-module torch ^
      --exclude-module torchvision ^
      --exclude-module torchaudio ^
      --exclude-module sklearn ^
      --exclude-module scikit-learn ^
      --exclude-module onnx ^
      --exclude-module rapidocr_onnxruntime ^
      --exclude-module cv2 ^
      %ENTRY%
) else (
    rem --collect-data: rapidocr 的 ONNX 模型/配置是包内数据文件，
    rem PyInstaller 默认不收，缺了它 exe 里扫描版 PDF OCR 会静默失效
    pyinstaller --noconfirm --clean --onefile --windowed ^
      --name "%BUILD_NAME%" ^
      --add-data "config/style.qss;config" ^
      --add-data "motor_anomaly.onnx;." ^
      --collect-data rapidocr_onnxruntime ^
      %ZLG_DLL% ^
      %ZLG_ZCAN% ^
      %ENTRY%
)
if errorlevel 1 goto :error

echo [3/3] Done.
echo Output: %CD%\dist\%BUILD_NAME%.exe
pause
exit /b 0

:error
echo Build failed.
pause
exit /b 1
