@echo off
python test_integration.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_duplicate_concurrent.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_advanced_features.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_websocket.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_dom_snapshots.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_navigation_history.py
if %errorlevel% neq 0 exit /b %errorlevel%
python test_interactions.py
if %errorlevel% neq 0 exit /b %errorlevel%
echo ALL TESTS PASSED!
