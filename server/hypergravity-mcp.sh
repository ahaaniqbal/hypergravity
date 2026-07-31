#!/bin/sh
# Launcher for the HyperGravity MCP server.
#
# VoiceOS splits its "command" field on whitespace, so a path containing a
# space ("Voice Hackathon") is parsed as command + arguments and fails with
# ENOENT. This wrapper lives at a space-free path and does the quoting itself.
exec "/Users/ahaaniqbal/Voice Hackathon/server/.venv/bin/python" \
     "/Users/ahaaniqbal/Voice Hackathon/server/mcp_server.py" "$@"
