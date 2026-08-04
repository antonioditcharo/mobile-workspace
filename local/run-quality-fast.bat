@echo off
REM CogVideoX 2B - same 720x480 x 49-frame shape as the 5B, roughly half
REM the time and some of the detail. Try this first if the 5B is too slow.
set LOCAL_PATIENT=1
call "%~dp0start-local-gpu.bat" cogvideox-2b
