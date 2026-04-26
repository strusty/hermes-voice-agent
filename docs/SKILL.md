---
name: hermes-voice-agent
category: hermes-agent
description: Persistent voice assistant on brahmaloka — wake word "Hermia", natural conversation, tool integration
---

# Hermia Voice Agent

Persistent voice assistant running on brahmaloka (Alienware Area-51, RTX 5090). Always-on ambient listening with wake word activation, natural follow-up conversation, and tool integration.

## Quick Start

```bash
# Restart service
sudo systemctl restart hermes-voice-agent.service

# Check status
sudo systemctl status hermes-voice-agent.service

# View logs
tail -f ~/.hermes/logs/voice-agent.log
```

## Configuration

**Script:** `~/hermes-voice-agent.py`  
**Service:** `/etc/systemd/system/hermes-voice-agent.service`  
**Memory:** `~/.hermes/voice-memory.json` (persists across restarts)

### Key Settings (DO NOT CHANGE)

| Setting | Value | Purpose |
|---------|-------|---------|
| MIN_RMS_ENERGY | 0.03 | Speech detection threshold |
| MIN_SPEECH_CHUNKS | 1 | Single spike triggers ("Hermia" is short) |
| POST_SPEECH_TIMEOUT | 1.5 | Wait after speech before transcribing |
| SLIDING_WINDOW | 4 | 12s buffer for idle mode |
| SLIDING_STEP | 3 | Transcribe every 9s during idle |
| CONVERSATION_TIMEOUT | 20 | 20s silence ends conversation |

## Features

### Wake Word Detection
- "Hermia" + variants: hermiya, hermiah, hernia, permia
- RMS energy threshold + Whisper transcription
- Post-speech timeout prevents cutting off phrases

### Natural Conversation Flow
- **Follow-up works** - no wake word needed after initial trigger
- `conversation_active` state tracks `last_speech_time`
- 20-second silence timeout ends conversation

### Tool Integration
- **terminal:** Execute shell commands
- **web_search:** Brave Search API (BRAVE_API_KEY in ~/.hermes/.env)
- **web_extract:** Pull content from URLs
- **send_message:** Telegram, Discord, WhatsApp via Gateway API

### Anti-Hallucination
- Whisper VAD disabled (cuts off short wake words)
- `condition_on_previous_text=False` prevents cascading loops
- `temperature=0` deterministic decoding
- Filters garbage transcriptions (<3 chars, all punctuation)

## Memory Management

### Pruning voice-memory.json
Over time, voice memory accumulates hallucinations ("Thank you", "I love you"), repetition loops ("I'm going to show you how to show you..."), and model timeout garbage.

**Tool:** `~/.hermes/skills/hermes-voice-agent/prune-voice-memory.py`

```bash
# Dry run (preview what would be removed)
python3 ~/.hermes/skills/hermes-voice-agent/prune-voice-memory.py --dry-run

# Execute (creates .backup before writing)
python3 ~/.hermes/skills/hermes-voice-agent/prune-voice-memory.py

# Rollback if needed
cp ~/.hermes/voice-memory.json.v1.0-prod ~/.hermes/voice-memory.json
```

**What it prunes:**
- Repetition loops ("I'm going to show you how to show you...")
- Hallucinated ambient noise ("Thank you", "I love you", "uh...")
- Model timeout garbage ("I had trouble thinking about that one")
- Very short fragments (<2 words)

**What it keeps:**
- Real conversational turns (even short ones like "soon." or "You" in context)
- Entries where Hermia gave substantive responses

## Critical Debugging Lessons (Apr 25, 2026)

### Follow-up conversation broken - what went wrong
The follow-up path was broken because it required `MIN_SPEECH_CHUNKS=3` (9 seconds of speech) before triggering. The original working code kept the follow-up INSIDE the `speech_count >= MIN_SPEECH_CHUNKS` block with `prompt=None` for conversation mode vs `prompt="hermia"` for idle mode.

**Wrong approach:** Created a separate follow-up path outside the threshold block. This bypassed speech detection entirely and transcribed ambient noise.

**Fix:** Keep follow-up inside the threshold block. The key is `prompt = "hermia" if not self.conversation_active else None`. During conversation, prompt=None lets Whisper transcribe naturally without bias toward the wake word.

### Config values that work (DO NOT CHANGE)
- `MIN_RMS_ENERGY=0.03` - Lower causes hallucinations, higher misses speech
- `MIN_SPEECH_CHUNKS=1` - "Hermia" is short, needs single spike to trigger
- `POST_SPEECH_TIMEOUT=1.5` - Shorter cuts off phrases, longer feels laggy
- `SLIDING_WINDOW=4` - 12s buffer is enough for idle mode
- `SLIDING_STEP=3` - Transcribe every 9s during idle

### Service management
```bash
# Restart
sudo systemctl restart hermes-voice-agent.service

# Status
sudo systemctl status hermes-voice-agent.service

# Logs
tail -f ~/.hermes/logs/voice-agent.log
```

## Troubleshooting

### Not hearing speech
- Check RMS threshold is 0.03 (lower causes hallucinations, higher misses speech)
- Verify mic device: `arecord -L | grep plug`
- Check logs: `tail -f ~/.hermes/logs/voice-agent.log`

### Hallucinating ("Thank you", "I love you")
- This was caused by SLIDING_WINDOW=1 (transcribing every 3s of noise)
- Fix: Revert to SLIDING_WINDOW=4, MIN_SPEECH_CHUNKS=1

### Not responding to follow-ups
- Make sure conversation is active (check logs for "CHAT Response to:")
- If conversation times out after 20s silence, speak again with wake word
- Check that follow-up path uses `prompt=None` (not `prompt="hermia"`)

### Service not running
```bash
# Restart
sudo systemctl restart hermes-voice-agent.service

# Check environment
cat /etc/systemd/system/hermes-voice-agent.service | grep EnvironmentFile
```

## Related Files
- Voice agent script: ~/hermes-voice-agent.py
- Status note: ~/.hermes/skills/hermes-voice-agent/voice-agent-status.md
- Conversation log: ~/.hermes/voice-conversations.log
- Recordings: ~/.hermes/voice-recordings/

## History
- v15.1 (Apr 25): Working follow-up conversation, tool integration, anti-hallucination fixes
- v15 (Apr 25): Tool integration with Brave Search API
- v14 (Apr 25): Natural multi-turn conversation with batch TTS
- v2.0 (Apr 25): Standalone always-on ambient voice assistant
