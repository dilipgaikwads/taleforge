# Storybook animation test (experimental)

A 10-second prototype: a storybook-style Roman soldier walks in, talks with lip sync,
gestures, blinks and shrugs. It is **not** part of the production line yet; it shows the
look before "animated story mode" is built into the factory.

## Run it on a Mac (Terminal, from the VideoFactory folder)

1. Add-ons for drawing (once):

       venv/bin/python -m pip install -r tools/storybook-animation-test/requirements.txt

2. Lip-sync tool (free, open source, MIT licence), once:

       cd tools/storybook-animation-test && curl -L -o rhubarb.zip https://github.com/DanielSWolf/rhubarb-lip-sync/releases/download/v1.14.0/Rhubarb-Lip-Sync-1.14.0-macOS.zip && unzip -o -q rhubarb.zip && rm rhubarb.zip && cd ../..

3. A voice line: use the narration of any finished video, for example video 1:

       ffmpeg_in=data/jobs/1/audio/voice.wav; bin/ffmpeg -y -v error -i "$ffmpeg_in" -t 5 -ar 44100 -ac 1 tools/storybook-animation-test/voice.wav

4. Mouth shapes from the voice, then the frames, then the video:

       cd tools/storybook-animation-test
       ./Rhubarb-Lip-Sync-1.14.0-macOS/rhubarb -q -r phonetic -f json voice.wav -o mouth.json
       ../../venv/bin/python render_test.py
       ../../bin/ffmpeg -y -v error -framerate 30 -i frames/f%04d.png -i voice.wav -filter_complex "[1:a]adelay=3400|3400,apad[a];[0:v]scale=1080:1920,format=yuv420p[v]" -map "[v]" -map "[a]" -c:v libx264 -crf 19 -c:a aac -t 10 test.mp4
       open test.mp4

If macOS blocks `rhubarb` ("cannot be opened"), run once:
`xattr -dr com.apple.quarantine Rhubarb-Lip-Sync-1.14.0-macOS`

The speech bubbles in `render_test.py` show fixed sample lines; edit them to match your voice line.
