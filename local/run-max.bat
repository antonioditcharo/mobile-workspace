@echo off
REM ===================================================================
REM  Maximum length: 20-second clips by chaining.
REM
REM  Uses LTX-Video because it has both text-to-video and image-to-video.
REM  Chaining continues each segment from the last frame of the previous
REM  one, which needs image-to-video - Wan 1.3B and AnimateDiff have no
REM  such counterpart, so they cannot be extended past a single pass.
REM
REM  384x256 leaves room for a useful number of frames per segment while
REM  keeping the picture larger than the 320x192 long preset.
REM
REM  Expect this to take a long time. Four or five passes at several
REM  minutes each, plus interpolation. Leave it running.
REM ===================================================================
set LOCAL_WIDTH=384
set LOCAL_HEIGHT=256
call "%~dp0start-local-gpu.bat" ltx
