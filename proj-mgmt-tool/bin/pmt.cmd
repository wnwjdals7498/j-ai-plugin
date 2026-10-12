@echo off
setlocal
set "PMT_ENTRY=%~dp0..\scripts\pmt_easy.py"

if defined PMT_PYTHON goto use_configured_python
where py >nul 2>nul
if not errorlevel 1 goto use_python_launcher
where python >nul 2>nul
if not errorlevel 1 goto use_python_command
echo PMT needs Python 3.13 or later. Finish setup, then retry. 1>&2
exit /b 3

:use_configured_python
"%PMT_PYTHON%" -B "%PMT_ENTRY%" %*
exit /b %ERRORLEVEL%

:use_python_launcher
py -3 -B "%PMT_ENTRY%" %*
exit /b %ERRORLEVEL%

:use_python_command
python -B "%PMT_ENTRY%" %*
exit /b %ERRORLEVEL%
