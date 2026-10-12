@echo off
setlocal
if not defined PMT_SERVER_PYTHON (
  echo Set PMT_SERVER_PYTHON to the installed Host venv Python. 1>&2
  exit /b 3
)
if not defined PMT_HOST_CONFIG_ROOT (
  echo Set PMT_HOST_CONFIG_ROOT to the Host configuration folder. 1>&2
  exit /b 3
)
if not exist "%PMT_SERVER_PYTHON%" (
  echo PMT_SERVER_PYTHON does not point to an installed Host venv Python. 1>&2
  exit /b 3
)
"%PMT_SERVER_PYTHON%" -m pmt.server_admin --config-root "%PMT_HOST_CONFIG_ROOT%" %*
exit /b %errorlevel%
