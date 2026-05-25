@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   Motor Control HMI - Build Tool
echo ============================================
echo.
echo [1] Lite version  (no training, ~50MB)
echo [2] Full version  (with training+DRL, ~500MB+)
echo.
set /p choice=Enter option (1 or 2):

if "%choice%"=="1" goto :lite
if "%choice%"=="2" goto :full
echo Invalid option.
pause
exit /b 1

:lite
set BUILD_NAME=motor-control-hmi-lite
set ENTRY=main_dist.py
set MODE=lite
goto :build

:full
set BUILD_NAME=motor-control-hmi-full
set ENTRY=main.py
set MODE=full
goto :build

:build
echo.
echo [1/3] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto :error

echo [2/3] Building %BUILD_NAME%...

if "%MODE%"=="lite" (
    pyinstaller --noconfirm --clean --onefile --windowed ^
      --name "%BUILD_NAME%" ^
      --add-data "config/style.qss;config" ^
      --add-data "motor_anomaly.onnx;." ^
      --add-data "motor_anomaly.onnx.data;." ^
      --exclude-module torch ^
      --exclude-module torchvision ^
      --exclude-module torchaudio ^
      --exclude-module sklearn ^
      --exclude-module scikit-learn ^
      --exclude-module onnx ^
      %ENTRY%
) else (
    pyinstaller --noconfirm --clean --onefile --windowed ^
      --name "%BUILD_NAME%" ^
      --add-data "config/style.qss;config" ^
      --add-data "motor_anomaly.onnx;." ^
      --add-data "motor_anomaly.onnx.data;." ^
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
