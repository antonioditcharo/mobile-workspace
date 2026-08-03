@echo off
REM ===================================================================
REM  Longer clips: LTX-Video at 512x320.
REM
REM  Wan cannot do this. Its VAE compresses 8x spatially, so a 5-second
REM  clip on a 4 GB card would need a frame so small that Wan produces
REM  smears instead of a picture. LTX compresses 32x - roughly sixteen
REM  times fewer tokens for the same frame - so it holds far more frames
REM  at a size that still resolves.
REM
REM  Use run-wan.bat when the shot is short and quality matters most.
REM ===================================================================
set LOCAL_WIDTH=512
set LOCAL_HEIGHT=320
call "%~dp0start-local-gpu.bat" ltx
