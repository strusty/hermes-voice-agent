#!/usr/bin/env python3
"""
Hermes Voice Agent — Standalone always-on voice assistant with wake word detection.

Unlike the built-in Hermes Agent voice mode (which requires gateway integration),
this runs as a standalone systemd service with ambient wake word listening.

Configuration: voice_agent_config.yaml
"""

import subprocess, os, sys, time, json, wave, struct, threading, math, random, re
from pathlib import Path

# Load configuration
import yaml
config_path = Path(__file__).parent / "voice_agent_config.yaml"
if config_path.exists():
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
else:
    cfg = {}

# Identity
ASSISTANT_NAME = cfg.get('assistant_name', 'Assistant')
OWNER_NAME = cfg.get('owner_name', 'User')
WAKE_WORD = cfg.get('wake_word', 'assistant')
WAKE_WORD_VARIANTS = cfg.get('wake_word_variants', [WAKE_WORD])

# Audio
MIC_DEVICE = cfg.get('mic_device', 'default')
SPEAKER_DEVICE = cfg.get('speaker_device', 'default')
TTS_VOICE = cfg.get('tts_voice', 'en-US-MichelleNeural')

# RMS detection
RMS_GAIN = cfg.get('rms_gain', 6)
MIN_RMS_ENERGY = cfg.get('rms_threshold', 0.03)
MIN_SPEECH_CHUNKS = cfg.get('min_speech_chunks', 1)

# Transcription
WHISPER_MODEL_NAME = cfg.get('whisper_model', 'medium')
WHISPER_THREADS = cfg.get('whisper_threads', 4)
CHUNK_DURATION = cfg.get('chunk_duration', 3)
SLIDING_WINDOW = cfg.get('sliding_window', 4)
SLIDING_STEP = cfg.get('sliding_step', 2)

# Conversation
CONVERSATION_TIMEOUT = cfg.get('conversation_timeout', 20)
COOLDOWN = cfg.get('cooldown', 15)
MAX_MEMORY_TURNS = cfg.get('max_memory_turns', 10)

# LLM
API_URL = cfg.get('llm_url', 'http://localhost:8080/v1/chat/completions')
LLM_MAX_TOKENS = cfg.get('llm_max_tokens', 2048)
LLM_TEMPERATURE = cfg.get('llm_temperature', 0.8)
LLM_TIMEOUT = cfg.get('llm_timeout', 120)
IS_REASONING_MODEL = cfg.get('is_reasoning_model', False)

# Paths
LOG_FILE = Path(cfg.get('log_file', '~/.hermes/logs/voice-agent.log').expanduser())
STATE_FILE = Path(cfg.get('state_file', '~/.hermes/voice-agent.state').expanduser())
MEMORY_FILE = Path(cfg.get('memory_file', '~/.hermes/voice-memory.json').expanduser())
CONV_FILE = Path(cfg.get('conversation_log', '~/.hermes/voice-conversations.log').expanduser())
RECORDING_DIR = Path(cfg.get('recording_dir', '~/.hermes/voice-recordings').expanduser())

# System prompt
SYSTEM_PROMPT = cfg.get('system_prompt', f'You are {ASSISTANT_NAME}, a warm AI voice assistant.').format(
    name=ASSISTANT_NAME, owner=OWNER_NAME
)

# Phrases
FALLBACK_PHRASES = cfg.get('fallback_phrases', [
    "That's a good question, let me think about that for a moment.",
    "Interesting question, give me a moment to reflect on that.",
])

GREETING_RESPONSES = cfg.get('greeting_responses', [
    f"Yes, {OWNER_NAME}. How can I help you?",
    "Yes, what's on your mind?",
    "I'm here. What do you need?",
])


class VoiceAgent:
    def __init__(self):
        self.running = True
        self.last_wake_time = 0
        self.whisper_model = None
        self.lock = threading.Lock()
        self.memory = []
        self.conversation_active = False
        self.last_speech_time = time.time()
        RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        self.log(f"Voice Agent v2.0 — Whisper '{WHISPER_MODEL_NAME}' ({WHISPER_THREADS} threads, int8)")
        self.log(f"Wake word: '{WAKE_WORD}' ({len(WAKE_WORD_VARIANTS)} variants)")
        self.log(f"RMS threshold: {MIN_RMS_ENERGY} | Gain: {RMS_GAIN}x | Mic: {MIC_DEVICE}")

        self._load_memory()
        self._load_whisper()
        self.log("Ready.")

    def _load_whisper(self):
        try:
            from faster_whisper import WhisperModel
            self.whisper_model = WhisperModel(
                WHISPER_MODEL_NAME,
                device='cpu',
                compute_type='int8',
                cpu_threads=WHISPER_THREADS,
            )
            self.log(f"Whisper '{WHISPER_MODEL_NAME}' loaded ({WHISPER_THREADS} threads, int8)")
        except Exception as e:
            self.log(f"ERROR loading whisper: {e}")
            sys.exit(1)

    def _load_memory(self):
        try:
            if MEMORY_FILE.exists() and MEMORY_FILE.stat().st_size > 0:
                self.memory = json.loads(MEMORY_FILE.read_text())
                self.log(f"Loaded {len(self.memory)} turns from memory")
        except:
            self.memory = []

    def _save_memory(self):
        try:
            MEMORY_FILE.write_text(json.dumps(self.memory))
        except:
            pass

    def _build_context(self, user_text):
        history = ""
        if self.memory:
            history = "Recent conversation:\n"
            for t in self.memory[-MAX_MEMORY_TURNS:]:
                history += f"User: {t['user']}\nAssistant: {t['assistant']}\n"
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"{history}User: {user_text}"},
        ]

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(line + '\n')
        except:
            pass
        print(line, flush=True)

    def record_chunk(self, duration):
        p = RECORDING_DIR / f"chunk_{time.time()}.wav"
        try:
            subprocess.run(
                ['arecord', '-D', MIC_DEVICE, '-f', 'cd', '-d', str(duration), str(p)],
                capture_output=True, timeout=duration + 5,
            )
            return str(p) if p.exists() and p.stat().st_size > 2000 else None
        except:
            return None

    def rms_energy(self, audio_path):
        try:
            with wave.open(audio_path, 'rb') as wf:
                nf = wf.getnframes()
                step = max(1, nf // 5000)
                samples = []
                for i in range(0, nf, step):
                    wf.setpos(i)
                    data = wf.readframes(min(step, nf - i))
                    if not data:
                        break
                    vals = struct.unpack(f"{len(data)//2}h", data)
                    samples.extend(vals)
                if not samples:
                    return 0.0
                mean = sum(samples) / len(samples)
                return math.sqrt(sum((x - mean) ** 2 for x in samples) / len(samples)) / 32768.0 * RMS_GAIN
        except:
            return 0.0

    def concat_wavs(self, paths, out):
        frames = b''
        ch, sw, rate = 2, 2, 44100
        for p in paths:
            try:
                with wave.open(p, 'rb') as wf:
                    ch, sw, rate = wf.getnchannels(), wf.getsampwidth(), wf.getframerate()
                    frames += wf.readframes(wf.getnframes())
            except:
                continue
        if frames:
            with wave.open(out, 'wb') as wf:
                wf.setnchannels(ch)
                wf.setsampwidth(sw)
                wf.setframerate(rate)
                wf.writeframes(frames)
            return True
        return False

    def transcribe(self, path, prompt=None):
        if not self.whisper_model or not Path(path).exists():
            return ""
        try:
            with self.lock:
                kw = {'language': 'en', 'beam_size': 5, 'word_timestamps': True}
                if prompt:
                    kw['initial_prompt'] = prompt
                segs, _ = self.whisper_model.transcribe(path, **kw)
            return ' '.join(s.text for s in segs).strip()
        except Exception as e:
            self.log(f"Transcribe error: {e}")
            return ""

    def has_wake(self, text):
        lower = text.lower().strip().rstrip('?.!,;:')
        return any(v in lower for v in WAKE_WORD_VARIANTS)

    def extract_command(self, text):
        lower = text.lower().strip()
        for v in WAKE_WORD_VARIANTS:
            if lower.startswith(v):
                rest = text[len(v):].lstrip(', .—-')
                if rest.strip():
                    return rest.strip()
        return text

    def speak(self, text):
        mp3 = RECORDING_DIR / "resp.mp3"
        wav = RECORDING_DIR / "resp.wav"
        try:
            import asyncio
            import edge_tts

            asyncio.run(edge_tts.Communicate(text, TTS_VOICE).save(str(mp3)))
            self.log(f"TTS mp3: {mp3.stat().st_size} bytes")

            result = subprocess.run(
                ['ffmpeg', '-i', str(mp3), '-acodec', 'pcm_s16le', '-ac', '2', '-ar', '48000', '-f', 'wav', '-y', str(wav)],
                capture_output=True, timeout=10,
            )
            if result.returncode != 0:
                self.log(f"ffmpeg failed: {result.stderr.decode()[:100]}")
                return
            self.log(f"TTS wav: {wav.stat().st_size} bytes")

            # Mute mic during playback to prevent feedback
            subprocess.run(['amixer', 'sset', 'Capture', '0%'], capture_output=True)
            self.log("Mic muted during TTS")
            result = subprocess.run(['aplay', '-D', SPEAKER_DEVICE, str(wav)], capture_output=True, timeout=30)
            if result.returncode != 0:
                self.log(f"aplay failed: {result.stderr.decode()[:100]}")

            subprocess.run(['amixer', 'sset', 'Capture', '100%'], capture_output=True)
            self.log("Mic unmuted")
            self.log(f"SPK {text[:60]}")
        except Exception as e:
            subprocess.run(['amixer', 'sset', 'Capture', '100%'], capture_output=True)
            self.log(f"TTS error: {e}")

    def _call_model(self, user_text, timeout=LLM_TIMEOUT, max_tokens=LLM_MAX_TOKENS):
        messages = self._build_context(user_text)
        body = json.dumps({
            "model": "voice",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": LLM_TEMPERATURE,
        }).encode()
        headers = {'Content-Type': 'application/json'}
        try:
            import urllib.request
            with urllib.request.urlopen(
                urllib.request.Request(API_URL, data=body, headers=headers), timeout=timeout
            ) as resp:
                data = json.loads(resp.read().decode())
                content = data['choices'][0]['message']['content'].strip()
                # Handle reasoning models (empty content, answer in reasoning_content)
                if IS_REASONING_MODEL and not content:
                    reasoning = data['choices'][0]['message'].get('reasoning_content', '').strip()
                    if reasoning:
                        # Extract the actual response from reasoning (look for quoted text)
                        quoted = re.findall(r'"([^"]{15,200})"', reasoning)
                        if quoted:
                            content = quoted[-1]  # Take last quoted sentence
                        else:
                            # Fallback: take last meaningful line
                            lines = [l.strip() for l in reasoning.split('\n') if l.strip() and len(l.strip()) > 20]
                            content = lines[-1] if lines else reasoning
                        # Clean up artifacts
                        for artifact in ['Matches constraints', 'Ready', '✅', 'Conclusion:', 'Answer:']:
                            content = content.replace(artifact, '')
                        content = ' '.join(content.split())[:200].strip()
                return content
        except Exception as e:
            self.log(f"API error: {e}")
            return None

    def _is_refusal(self, text):
        """Check if the model gave up or refused to answer."""
        lower = text.lower().strip()
        refusals = [
            "can't assist", "cannot assist", "can't answer", "cannot answer",
            "i don't know", "i'm not able", "i'm not sure",
            "i apologize", "sorry, i can't", "unable to",
            "i'm sorry", "i do not have",
            "as an ai", "i don't have access",
            "i can't help", "i cannot help",
        ]
        return any(r in lower for r in refusals) or len(text) < 15

    def get_response(self, user_text):
        # Handle bare wake word with no question
        if user_text.lower().strip() in WAKE_WORD_VARIANTS:
            greeting = random.choice(GREETING_RESPONSES)
            self.log(f"GREETING: {greeting}")
            self.speak(greeting)
            self.memory.append({'user': user_text, 'assistant': greeting, 'time': time.time()})
            self._save_memory()
            return greeting

        # Speak transition phrase while waiting for LLM
        phrase = random.choice(FALLBACK_PHRASES)
        self.log(f"Transition: {phrase}")
        self.speak(phrase)
        
        # Call LLM
        full = self._call_model(user_text)
        if not full:
            full = "I had trouble thinking about that one."
        self.log("LLM responded")

        # Clean response
        full = full.replace('*', '').replace('#', '').replace('`', '').replace('~~', '').strip()
        # Strip assistant name prefix if model adds it
        for prefix in [f"{ASSISTANT_NAME.lower()}:", f"{ASSISTANT_NAME.lower()} ", "assistant:", "assistant "]:
            if full.lower().startswith(prefix):
                full = full[len(prefix):].strip()
        
        self.memory.append({'user': user_text, 'assistant': full[:200], 'time': time.time()})
        if len(self.memory) > MAX_MEMORY_TURNS * 2:
            self.memory = self.memory[-MAX_MEMORY_TURNS:]
        self._save_memory()
        self.log(f"LLM {full[:80]}")
        self.speak(full)
        return full

    def save_state(self, s):
        try:
            with open(STATE_FILE, 'w') as f:
                json.dump({'state': s, 't': time.time(), 'pid': os.getpid()}, f)
        except:
            pass

    def log_conv(self, u, r):
        try:
            with open(CONV_FILE, 'a') as f:
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\nYou: {u}\n{ASSISTANT_NAME}: {r}\n")
        except:
            pass

    def process_speech(self, text):
        self.save_state("thinking")
        resp = self.get_response(text)
        self.save_state("speaking")
        self.log_conv(text, resp)
        self.last_speech_time = time.time()

    def run(self):
        self.log(f"Listening (PID: {os.getpid()})")
        self.save_state("idle")
        buf = []
        counter = 0

        while self.running:
            try:
                cp = self.record_chunk(CHUNK_DURATION)
                if cp:
                    e = self.rms_energy(cp)
                    buf.append((cp, e))
                    if len(buf) > SLIDING_WINDOW:
                        Path(buf[0][0]).unlink(missing_ok=True)
                        buf.pop(0)

                    if self.conversation_active:
                        silence = time.time() - self.last_speech_time
                        if silence > CONVERSATION_TIMEOUT:
                            self.log(f"ZZZ {silence:.0f}s silence — conversation ended")
                            self.conversation_active = False
                            self.save_state("idle")

                    speech_count = sum(1 for _, x in buf if x > MIN_RMS_ENERGY)
                    if speech_count >= MIN_SPEECH_CHUNKS and len(buf) >= SLIDING_WINDOW:
                        # During conversation, require louder speech to avoid ambient noise
                        if self.conversation_active:
                            loud_count = sum(1 for _, x in buf if x > MIN_RMS_ENERGY * 1.5)
                            if loud_count < 2:
                                continue  # Probably ambient noise, skip
                        counter += 1
                        if counter >= SLIDING_STEP:
                            counter = 0
                            sp = RECORDING_DIR / f"seg_{time.time()}.wav"
                            if self.concat_wavs([p for p, _ in buf], str(sp)):
                                prompt = WAKE_WORD if not self.conversation_active else None
                                text = self.transcribe(str(sp), prompt=prompt)
                                if text:
                                    self.log(f"EAR '{text}'")

                                    if self.has_wake(text):
                                        now = time.time()
                                        if now - self.last_wake_time >= COOLDOWN:
                                            self.last_wake_time = now
                                            self.conversation_active = True
                                            self.save_state("conversation")
                                            command = self.extract_command(text)
                                            self.log(f"WAKE Command: '{command}'")
                                            self.process_speech(command)
                                        elif self.conversation_active:
                                            # Wake word during cooldown, but already in conversation
                                            self.log(f"CHAT (cooldown) Response to: '{text}'")
                                            self.process_speech(text)
                                        else:
                                            self.log("WAKE on cooldown")
                                    elif self.conversation_active:
                                        self.log(f"CHAT Response to: '{text}'")
                                        self.process_speech(text)

                                sp.unlink(missing_ok=True)

                time.sleep(0.1)
            except KeyboardInterrupt:
                self.log("Stopping...")
                self.running = False
                break
            except Exception as e:
                self.log(f"Error: {e}")
                time.sleep(1)


if __name__ == '__main__':
    a = VoiceAgent()
    try:
        a.run()
    except KeyboardInterrupt:
        a.running = False
        a.log("Goodbye.")
