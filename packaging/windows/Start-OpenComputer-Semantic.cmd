@echo off
if not exist "%~dp0optional\opencomputer-native.exe" exit /b 1
start "" "%~dp0OpenComputer.exe" --native-helper "%~dp0optional\opencomputer-native.exe" %*
