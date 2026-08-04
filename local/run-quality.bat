@echo off
REM ===================================================================
REM  Highest quality per second: CogVideoX 5B at 720x480, 49 frames.
REM
REM  Six seconds at its native 8fps, which RealFrame's Smoothness
REM  control fills out to 48fps afterwards. The frame is more than twice
REM  the area Wan manages on this card, which is where the quality
REM  difference comes from.
REM
REM  Patient mode is on: sequential offloading and VAE tiling, which
REM  trade speed for the ability to hold a frame this large at all.
REM  Expect 30-60 minutes per clip on a 4 GB card. That is the deal.
REM
REM  First run downloads about 20 GB.
REM ===================================================================
set LOCAL_PATIENT=1
call "%~dp0start-local-gpu.bat" cogvideox-5b
