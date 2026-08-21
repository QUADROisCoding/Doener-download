@echo off
REM Build DonerExec.exe - the managed helper behind the Executor tab.
REM
REM Uses the C# compiler that ships with Windows, so building Doener never
REM requires the .NET SDK. QuorumAPI targets netstandard, which a .NET Framework
REM exe reaches through the netstandard.dll facade; System.Drawing is needed
REM because Logger.OnLog is Action<string, Color>.
REM
REM Output lands beside this script. Doener downloads it from the GitHub
REM release at runtime, so nothing is embedded and nothing ships loose.

setlocal
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
set FW=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319
set ROOT=%~dp0..\..
set OUT=%ROOT%\x64\Release

if not exist "%CSC%" (
    echo [!] csc.exe not found at %CSC%
    echo     .NET Framework 4.x is missing - install it or build with the .NET SDK instead.
    exit /b 1
)
if not exist "%ROOT%\third_party\QuorumAPI\QuorumAPI.dll" (
    echo [!] third_party\QuorumAPI\QuorumAPI.dll is missing.
    exit /b 1
)

"%CSC%" /nologo /target:winexe /platform:x64 ^
    /out:"%OUT%\DonerExec.exe" ^
    /win32manifest:"%~dp0app.manifest" ^
    /reference:"%ROOT%\third_party\QuorumAPI\QuorumAPI.dll" ^
    /reference:"%FW%\netstandard.dll" ^
    /reference:"%FW%\System.Drawing.dll" ^
    "%~dp0Program.cs"
if errorlevel 1 exit /b 1

copy /y "%ROOT%\third_party\QuorumAPI\QuorumAPI.dll" "%OUT%\QuorumAPI.dll" >nul
echo [+] DonerExec.exe built - publish it to the doener-exec release
endlocal
