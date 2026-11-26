# Greeting clips

Amir plays one of these clips when someone joins his voice channel.

## Naming

Lowercase kebab-case, numbered: `amir-greeting-01.qt`, `amir-greeting-02.mp3`, and so on.

## Supported formats

`.mp3`, `.wav`, `.ogg`, `.opus`, `.m4a`, `.mp4`, `.aac`, `.flac`, `.qt`, `.mov`. Any format
FFmpeg can decode works if it has one of these extensions. Files with other extensions (like
this README) are ignored.

## Startup check

Every clip is decoded with FFmpeg at startup. A clip that fails to decode is logged (by file
name) and never played. If no clip passes, Amir joins silently and logs a warning.

## What belongs here

Only audio the owner is entitled to use. Do not add recordings of other people without their
permission, or copyrighted material you do not have rights to.
