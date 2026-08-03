@echo off
REM Wan at a smaller frame, which buys a much longer clip.
REM
REM Memory during generation scales with pixels x frames, so halving the frame
REM area roughly doubles how many frames fit. 320x192 gets about 5 seconds on a
REM 4 GB card, against about 2 seconds at the default size.
set LOCAL_WIDTH=320
set LOCAL_HEIGHT=192
call "%~dp0start-local-gpu.bat" wan-1.3b
