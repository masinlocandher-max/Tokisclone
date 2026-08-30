# Architecture

## Lean MVP

ChatGPT / MCP Client
        |
        v
FMB Video MCP
        |
        +--> yt-dlp --------> public video/profile metadata
        |
        +--> ffmpeg --------> audio extraction / media conversion
        |
        +--> faster-whisper -> local transcription
        |
        +--> JSON/CSV/TXT --> export

## Production

ChatGPT / MCP Client
        |
        v
Authenticated MCP Gateway
        |
        +--> Metadata service
        +--> Download job queue
        +--> Transcription workers
        +--> Export service
        |
        +--> Postgres/Supabase
        +--> S3-compatible object storage

Long-running work should eventually become queued jobs instead of one long MCP request.
