# Hermes Voice Agent

Standalone, always-on voice assistant with wake word detection. Runs as a systemd service with ambient listening, local STT, and direct LLM inference.

## Changelog

### v14 — Apr 25, 2026 (Anti-Hallucination + Cooldown)
- **Whisper anti-hallucination:** Added `vad_filter=True`, `no_speech_threshold=0.3`, `condition_on_previous_text=False`, `temperature=0`, `compression_ratio_threshold=2.4`, `log_prob_threshold=-1.0`
- **Garbage filtering:** Transcriptions <3 chars or pure punctuation now silently discarded
- **Post-response cooldown:** 8-second silence after responding — prevents "thank you Hermia" from triggering a new wake cycle
- **Fix:** Double responses when saying thank you with wake word included
- **Fix:** "Iteration budget exhausted (60/60)" warning from Whisper decoding silence

### v13 — Apr 25, 2026
- Reverted to single big model (Qwen3.6-27B) — small model produced hallucinated answers
- Fixed reasoning model integration: `max_tokens=1024`, extract answer from `reasoning_content`
- Increased `SLIDING_WINDOW` to 4s, `SLIDING_STEP` to 2s
- Added conversation-mode ambient noise filter (requires 1.5x threshold)

### v12-v11 — Apr 25, 2026
- RMS VAD with 6x gain, threshold 0.03
- Whisper medium model, int8 quantization
- Mic mute during TTS to prevent feedback loop
- Systemd services: `Restart=no`, `After=llama-voice.service`

## Difference from Hermes Agent built-in Voice Mode

| Feature | Built-in Voice Mode | This Agent |
|---------|-------------------|------------|
| Integration | Part of `hermes-agent` gateway | Standalone systemd service |
| Activation | Ctrl+B (CLI) or message-triggered | Ambient wake word detection |
| STT | Cloud (Groq/OpenAI) or local Whisper | Local Whisper medium |
| TTS | Cloud (ElevenLabs/OpenAI) or NeuTTS | Local edge-tts |
| LLM | Via gateway (shares session) | Direct llama.cpp (separate session) |
| Session | Integrated with Telegram/Discord | Independent conversation history |
| Always-on | No | Yes — ambient listening 24/7 |
| Config | `~/.hermes/config.yaml` | `voice_agent_config.yaml` |
| Dependencies | `pip install hermes-agent[voice]` | `faster-whisper`, `edge_tts`, `pyyaml` |

**Use this agent when you want:** a dedicated voice assistant that always listens, works offline (local models), and has its own conversation context separate from your messaging platforms.

**Use built-in voice mode when you want:** voice integrated with your Telegram/Discord conversations, sharing the same session context.

## Quick Start

```bash
# Clone and setup
git clone https://github.com/username/hermes-voice-agent.git
cd hermes-voice-agent

# Install dependencies
pip install faster-whisper edge_tts pyyaml

# Configure
cp voice_agent_config.yaml voice_agent_config.local.yaml
nano voice_agent_config.local.yaml  # Edit your settings

# Test
python3 voice_agent.py

# Install as service (optional)
sudo cp services/hermes-voice-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hermes-voice-agent
sudo systemctl start hermes-voice-agent
```

## Configuration

Edit `voice_agent_config.yaml`:

### Identity
```yaml
assistant_name: "Aria"              # Your assistant's name
owner_name: "Alex"                  # Your name
wake_word: "aria"                   # Wake word
wake_word_variants:
  - "aria"                          # Include phonetic variants
  - "area"                          # That Whisper might produce
  - "arya"
```

### Audio
```yaml
mic_device: "plughw:2,0"           # USB mic device
speaker_device: "plughw:1,2"       # Audio output device
tts_voice: "en-US-MichelleNeural"  # Edge TTS voice
```

### Voice Detection
```yaml
rms_gain: 6                        # Software gain multiplier
rms_threshold: 0.03                # Detection threshold (after gain)
min_speech_chunks: 1               # Chunks above threshold to trigger
sliding_window: 4                  # Seconds of audio buffer
sliding_step: 2                    # Transcribe every N*chunk seconds
```

### Whisper
```yaml
whisper_model: "medium"            # "base", "small", "medium"
whisper_threads: 4                 # CPU threads for transcription
```

**Anti-hallucination parameters** (baked into `voice_agent.py`):
The agent uses these faster-whisper parameters to prevent hallucinating text from silence:
- `vad_filter=True` — Built-in Silero VAD pre-filters non-speech segments
- `no_speech_threshold=0.3` — Aggressive silence detection (lower = more aggressive)
- `condition_on_previous_text=False` — Prevents cascading hallucination loops
- `temperature=0` — Deterministic decoding (no random hallucinated words)
- `compression_ratio_threshold=2.4` — Catches repetitive garbage text
- `log_prob_threshold=-1.0` — Filters low-confidence transcriptions

Transcriptions under 3 characters or pure punctuation are silently discarded.

### Post-Response Cooldown
```yaml
# Prevents "thank you Hermia" from triggering a new wake cycle
# After responding, agent ignores transcription for 8 seconds
# (built into voice_agent.py, not configurable via YAML yet)
```

### LLM
```yaml
llm_url: "http://localhost:8080/v1/chat/completions"
llm_max_tokens: 2048               # Higher for reasoning models
llm_temperature: 0.8
llm_timeout: 120
is_reasoning_model: true           # Extract from reasoning_content if empty
```

### Conversation
```yaml
conversation_timeout: 20           # Seconds before conversation ends
cooldown: 15                       # Seconds between wake word triggers
max_memory_turns: 10               # Conversation history length
```

## Tuning Guide

### Wake word not detected
- Upgrade `whisper_model` from "base" to "medium"
- Add phonetic variants to `wake_word_variants`
- Increase `rms_gain` (try 8 instead of 6)
- Lower `rms_threshold` (try 0.02)

### Too many false triggers
- Increase `rms_threshold` (try 0.04)
- Decrease `rms_gain` (try 4)
- Increase `sliding_window` (try 6)
- Add ambient noise filter during conversation

### Response too slow
- Reduce `llm_max_tokens` (1024 instead of 2048)
- Use smaller Whisper model ("small" instead of "medium")
- Reduce `whisper_threads`

### Model speaking reasoning instead of answers
- Increase `llm_max_tokens` to give model room to think AND answer
- Set `is_reasoning_model: true` to extract from reasoning_content

### Conversation cutting off mid-sentence
- Increase `sliding_step` (try 3 instead of 2)
- Increase `sliding_window` (try 6 instead of 4)

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│  Microphone  │────▶│ RMS Detector │────▶│ Whisper STT  │
└─────────────┘     └─────────────┘     └──────────────┘
                                                        │
                                                        ▼
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│  Speakers   │◀────│  edge-tts   │◀────│ LLM (llama)  │
└─────────────┘     └─────────────┘     └──────────────┘
```

## System Requirements

- Linux with ALSA audio (arecord, aplay, amixer)
- Python 3.10+ with `faster-whisper`, `edge_tts`, `pyyaml`
- LLM server accessible at configured `llm_url`
- ~2GB RAM for Whisper medium model

## Dependencies

```bash
pip install faster-whisper edge_tts pyyaml
```

## License

MIT
