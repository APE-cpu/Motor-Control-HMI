@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/3] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 goto :error

echo [2/3] Building single-file executable...
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name "电机控制上位机" ^
  --add-data "config/style.qss;config" ^
  --add-data "motor_anomaly.onnx;." ^
  --add-data "motor_anomaly.onnx.data;." ^
  --exclude-module torch ^
  --exclude-module torchvision ^
  --exclude-module torchaudio ^
  --exclude-module sklearn ^
  --exclude-module scikit-learn ^
  --exclude-module onnx ^
  main_dist.py
if errorlevel 1 goto :error

echo [3/3] Done.
echo Executable: %CD%\dist\电机控制上位机.exe
pause
exit /b 0

:error
echo Build failed. Please check the output above.
pause
exit /b 1
