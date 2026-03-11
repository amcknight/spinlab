@echo off
setlocal enabledelayedexpansion

:: SpinLab — Launch Harness
:: Launches Mesen2 with the SpinLab Lua script.
:: Usage: launch.bat [rom_path]

set "PROJECT_ROOT=%~dp0.."
pushd "%PROJECT_ROOT%"
set "PROJECT_ROOT=%CD%"
popd

:: Use Python to read config.yaml (batch YAML parsing is terrible)
for /f "delims=" %%P in ('python -c "import yaml; c=yaml.safe_load(open(r'%PROJECT_ROOT%\config.yaml')); print(c['emulator']['path'])"') do set "MESEN_PATH=%%P"
for /f "delims=" %%P in ('python -c "import yaml; c=yaml.safe_load(open(r'%PROJECT_ROOT%\config.yaml')); print(c['emulator'].get('lua_script',''))"') do set "LUA_SCRIPT=%%P"
for /f "delims=" %%P in ('python -c "import yaml; c=yaml.safe_load(open(r'%PROJECT_ROOT%\config.yaml')); print(c.get('rom',{}).get('path',''))"') do set "ROM_PATH=%%P"

:: CLI arg overrides config ROM path
if not "%~1"=="" set "ROM_PATH=%~1"

:: Resolve relative lua script path
if not "!LUA_SCRIPT!"=="" if not "!LUA_SCRIPT:~1,1!"==":" set "LUA_SCRIPT=%PROJECT_ROOT%\!LUA_SCRIPT!"
set "LUA_SCRIPT=!LUA_SCRIPT:/=\!"

:: Validate
if "!MESEN_PATH!"=="" ( echo ERROR: emulator.path not set in config.yaml & exit /b 1 )
if not exist "!MESEN_PATH!" ( echo ERROR: Mesen not found at: !MESEN_PATH! & exit /b 1 )
if not exist "!LUA_SCRIPT!" ( echo ERROR: Lua script not found at: !LUA_SCRIPT! & exit /b 1 )

echo SpinLab — Launch Harness
echo   Mesen:  !MESEN_PATH!
echo   Script: !LUA_SCRIPT!

if not "!ROM_PATH!"=="" (
    if exist "!ROM_PATH!" (
        echo   ROM:    !ROM_PATH!
        start "" "!MESEN_PATH!" "!ROM_PATH!" "!LUA_SCRIPT!"
    ) else (
        echo   ROM not found: !ROM_PATH! — launching without ROM
        start "" "!MESEN_PATH!" "!LUA_SCRIPT!"
    )
) else (
    echo   ROM:    ^(none — load from Mesen UI^)
    start "" "!MESEN_PATH!" "!LUA_SCRIPT!"
)
