@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   电机控制上位机 打包工具
echo ============================================
echo.
echo 请选择打包版本：
echo   [1] 精简版（不含模型训练，体积小，约 50MB）
echo   [2] 完整版（含模型训练+DRL，体积大，约 500MB+）
echo.
set /p choice=请输入选项 (1 或 2)：

if "%choice%"=="1" goto :lite
if "%choice%"=="2" goto :full
echo 无效选项，请输入 1 或 2。
pause
exit /b 1

:lite
set BUILD_NAME=电机控制上位机_精简版
set ENTRY=main_dist.py
set EXCLUDES=--exclude-module torch --exclude-module torchvision --exclude-module torchaudio --exclude-module sklearn --exclude-module scikit-learn --exclude-module onnx
echo.
echo [精简版] 开始打包...
goto :build

:full
set BUILD_NAME=电机控制上位机_完整版
set ENTRY=main.py
set EXCLUDES=
echo.
echo [完整版] 开始打包（包含 PyTorch，耗时较长）...
goto :build

:build
echo [1/3] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto :error

echo [2/3] Building executable...
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "%BUILD_NAME%" ^
  --add-data "config/style.qss;config" ^
  --add-data "motor_anomaly.onnx;." ^
  --add-data "motor_anomaly.onnx.data;." ^
  %EXCLUDES% ^
  %ENTRY%
if errorlevel 1 goto :error

echo [3/3] Done.
echo Executable: %CD%\dist\%BUILD_NAME%.exe
pause
exit /b 0

:error
echo Build failed. Please check the output above.
pause
exit /b 1
