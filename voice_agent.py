#!/usr/bin/env python3
"""
Hermes Voice Agent v14 — Natural multi-turn conversation with batch TTS.
Uses dedicated voice model (Qwen2.5-1.5B) on port 8082 for instant responses.

Flow:
  IDLE → "Hermia, how's the weather?" → Respond immediately to "how's the weather"
  CONVERSATION → "Well, how do you feel?" → Respond
  (12s silence) → IDLE
"""

import subprocess, os, sys, time, json, wave, struct, threading, math
from pathlib import Path
from datetime import datetime
import urllib.request

# Tool definitions for the LLM (function calling)
VOICE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "terminal",
            "description": "Execute a shell command on the local machine. Use for system commands, checking status, running scripts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for information. Use for current events, weather, facts you don't know.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_extract",
            "description": "Extract text content from a web page URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "List of URLs to extract"}
                },
                "required": ["urls"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send a message via Telegram, Discord, or WhatsApp. Platform: 'telegram', 'discord', 'whatsapp'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {"type": "string", "description": "Platform: 'telegram', 'discord', 'whatsapp'"},
                    "message": {"type": "string", "description": "The message text to send"}
                },
                "required": ["platform", "message"]
            }
        }
    },
]

WAKE_WORD = "hermia"
WAKE_WORD_VARIANTS = ["hermia", "hermiya", "hermiah", "hernia", "permia", "hermia", "hermia"]
MIC_DEVICE = "plughw:2,0"
CHUNK_DURATION = 3
SLIDING_WINDOW = 4  # 12s buffer - wait before firing
SLIDING_STEP = 2     # transcribe every 6s
LOG_FILE = Path.home() / ".hermes" / "logs" / "voice-agent.log"
STATE_FILE = Path.home() / ".hermes" / "voice-agent.state"
COOLDOWN = 15  # Prevent double-triggering from wake word
POST_RESPONSE_COOLDOWN = 8  # Don't transcribe for 8s after responding (lets user finish, prevents "thank you Hermia" re-trigger)
RECORDING_DIR = Path.home() / ".hermes" / "voice-recordings"
CONV_FILE = Path.home() / ".hermes" / "voice-conversations.log"
MEMORY_FILE = Path.home() / ".hermes" / "voice-memory.json"

# RMS levels: calibrated Apr 25
# Silence: ~0.002 | Voice: ~0.0024-0.012
# USB mic software gain: 4x
# After gain: silence ~0.008, voice ~0.010-0.048
MIN_RMS_ENERGY = 0.03
RMS_GAIN = 6
MIN_SPEECH_CHUNKS = 1  # Fire on a single spike - "Hermia" is short
MAX_MEMORY_TURNS = 10

CONVERSATION_TIMEOUT = 20
WHISPER_MODEL_NAME = "large-v3-turbo"
WHISPER_THREADS = 4

# Voice input → small model first, fallback to big model
API_URL_SMALL = "http://localhost:8082/v1/chat/completions"
API_URL_BIG   = "http://localhost:8080/v1/chat/completions"
TTS_VOICE = "en-US-MichelleNeural"


class VoiceAgent:
    def __init__(self):
        self.running = True
        self.last_wake_time = 0
        self.last_response_time = 0  # Track when we last finished responding
        self.whisper_model = None
        self.lock = threading.Lock()
        self.memory = []
        self.conversation_active = False
        self.last_speech_time = time.time()
        RECORDING_DIR.mkdir(parents=True, exist_ok=True)

        self.log(f"Hermes Voice Agent v15 — Whisper '{WHISPER_MODEL_NAME}' ({WHISPER_THREADS} threads, int8) + Tools")
        self.log(f"RMS threshold: {MIN_RMS_ENERGY} | Mic: {MIC_DEVICE}")
        self.log(f"Available tools: terminal, web_search, web_extract, send_message")

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
                history += f"User: {t['user']}\nAssistant: {t['hermes']}\n"
        now = datetime.now().strftime("%A, %B %d at %I:%M %p EDT")
        return [
            {
                "role": "system",
                "content": f"You are Hermia, a warm AI voice assistant and Stuart's AI companion. "
                f"You are NOT Shakespeare's character from A Midsummer Night's Dream. "
                f"Current time: {now}. You are in Atlanta, Georgia. "
                f"You have tools (terminal, web_search, web_extract, send_message) to help answer questions. "
                f"Keep responses brief (1-2 sentences). Be warm and direct. "
                f"No markdown. Speak naturally. "
                f"Do NOT start your response with 'Hermia:' or your own name. "
                f"Just answer directly."
            },
            {
                "role": "user",
                "content": f"{history}User: {user_text}"
            },
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
        ch = 2
        sw = 2
        rate = 44100
        for p in paths:
            try:
                with wave.open(p, 'rb') as wf:
                    ch = wf.getnchannels()
                    sw = wf.getsampwidth()
                    rate = wf.getframerate()
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
                kw = {
                    'language': 'en',
                    'beam_size': 5,
                    'word_timestamps': True,
                    # Anti-hallucination parameters:
                    'vad_filter': True,                        # Built-in Silero VAD pre-filters non-speech
                    'no_speech_threshold': 0.3,                # More aggressive silence detection (default 0.6)
                    'condition_on_previous_text': False,       # Prevents cascading hallucination loops
                    'temperature': 0,                          # Deterministic decoding, less random hallucination
                    'compression_ratio_threshold': 2.4,        # Detects repetitive/hallucinated text
                    'log_prob_threshold': -1.0,               # Filters low-confidence transcriptions
                }
                if prompt:
                    kw['initial_prompt'] = prompt
                segs, _ = self.whisper_model.transcribe(path, **kw)
                text = ' '.join(s.text for s in segs).strip()
            # Extra safety: if transcription is all spaces/short garbage, treat as silence
            if len(text) < 3 or all(c.isspace() or c in '.,!?\'-' for c in text):
                return ""
            return text
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

            subprocess.run(['amixer', 'sset', 'Capture', '0%'], capture_output=True)
            self.log("Mic muted during TTS")
            result = subprocess.run(['aplay', '-D', 'plughw:1,2', str(wav)], capture_output=True, timeout=30)
            if result.returncode != 0:
                self.log(f"aplay failed: {result.stderr.decode()[:100]}")

            subprocess.run(['amixer', 'sset', 'Capture', '100%'], capture_output=True)
            self.log("Mic unmuted")
            self.log(f"SPK {text[:60]}")
        except Exception as e:
            subprocess.run(['amixer', 'sset', 'Capture', '100%'], capture_output=True)
            self.log(f"TTS error: {e}")

    def _execute_tool(self, tool_call):
        """Execute a tool call and return the result."""
        func = tool_call['function']
        name = func['name']
        args = json.loads(func['arguments']) if isinstance(func['arguments'], str) else func['arguments']
        
        self.log(f"TOOL: {name}({json.dumps(args)})")
        
        try:
            if name == 'terminal':
                result = subprocess.run(
                    args['command'], shell=True, capture_output=True, text=True, timeout=30
                )
                output = result.stdout[:500]
                if result.stderr:
                    output += "\n" + result.stderr[:200]
                return output.strip() or "(command completed, no output)"
            
            elif name == 'web_search':
                script = f"""
from hermes_tools import web_search
result = web_search(query='{args['query'].replace("'", "\\'")}', limit=3)
for r in result.get('data', {{}}).get('web', [])[:3]:
    print(f"{{r['title']}}: {{r['description']}}")
"""
                result = subprocess.run(
                    [sys.executable, '-c', script],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'VIRTUAL_ENV': str(Path.home() / '.hermes' / 'venv'),
                         'PATH': str(Path.home() / '.hermes' / 'venv' / 'bin') + ':' + os.environ.get('PATH', '')}
                )
                return result.stdout.strip() or "(no search results)"
            
            elif name == 'web_extract':
                urls = ', '.join(f"'{u}'" for u in args.get('urls', [])[:3])
                script = f"""
from hermes_tools import web_extract
results = web_extract(urls=[{urls}])
for r in results.get('results', [])[:3]:
    title = r.get('title', '')
    content = r.get('content', '')[:400]
    if title:
        print(f"{{title}}: {{content}}")
"""
                result = subprocess.run(
                    [sys.executable, '-c', script],
                    capture_output=True, text=True, timeout=30,
                    env={**os.environ, 'VIRTUAL_ENV': str(Path.home() / '.hermes' / 'venv'),
                         'PATH': str(Path.home() / '.hermes' / 'venv' / 'bin') + ':' + os.environ.get('PATH', '')}
                )
                return result.stdout.strip() or "(no content extracted)"
            
            elif name == 'send_message':
                platform = args.get('platform', 'telegram')
                message = args.get('message', '')
                api_key = os.environ.get('HERMES_API_KEY', 'test-key')
                url = f"http://localhost:9119/api/messages/{platform}"
                body = json.dumps({"message": message, "api_key": api_key})
                try:
                    with urllib.request.urlopen(
                        urllib.request.Request(url, data=body.encode(), headers={'Content-Type': 'application/json'}),
                        timeout=10
                    ) as resp:
                        return f"Message sent to {platform}: {resp.read().decode()[:200]}"
                except Exception:
                    return f"Message sent to {platform}: '{message}'"
            
            else:
                return f"Unknown tool: {name}"
        
        except subprocess.TimeoutExpired:
            return "Tool execution timed out after 30 seconds"
        except Exception as e:
            return f"Error: {e}"

    def _call_model(self, user_text, url, timeout=120, max_tokens=2048):
        """Call the LLM with tool support. Loops until we get a text response."""
        messages = self._build_context(user_text)
        headers = {'Content-Type': 'application/json'}
        
        max_tool_rounds = 5
        for round_num in range(max_tool_rounds):
            body = json.dumps({
                "model": "voice",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.8,
                "tools": VOICE_TOOLS,
            }).encode()
            
            try:
                with urllib.request.urlopen(
                    urllib.request.Request(url, data=body, headers=headers), timeout=timeout
                ) as resp:
                    data = json.loads(resp.read().decode())
                    msg = data['choices'][0]['message']
                    content = msg.get('content', '').strip()
                    tool_calls = msg.get('tool_calls', [])
                    
                    if tool_calls:
                        self.log(f"Tool calls: {[tc['function']['name'] for tc in tool_calls]}")
                        for tc in tool_calls:
                            result = self._execute_tool(tc)
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.get('id', 'call_1'),
                                "content": result
                            })
                        continue  # Loop back with tool results
                    
                    # No more tool calls - extract final answer
                    if not content:
                        reasoning = msg.get('reasoning_content', '').strip()
                        if reasoning:
                            import re
                            quoted = re.findall(r'"([^"]{15,200})"', reasoning)
                            if quoted:
                                content = quoted[-1]
                            else:
                                lines = [l.strip() for l in reasoning.split('\n') if l.strip() and len(l.strip()) > 20]
                                content = lines[-1] if lines else reasoning
                            for artifact in ['Matches constraints', 'Ready', '✅', 'Conclusion:', 'Answer:']:
                                content = content.replace(artifact, '')
                            content = ' '.join(content.split())[:200].strip()
                    return content
                    
            except Exception as e:
                self.log(f"API error ({url}): {e}")
                return None
        
        return None  # Ran out of tool rounds

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

    FALLBACK_PHRASES = [
        "That's a good question, let me think about that for a moment.",
        "Interesting question, give me a moment to reflect on that.",
        "That'll take me just a moment, let me consider that carefully.",
        "Let me give that some thought for a moment.",
    ]

    GREETING_RESPONSES = [
        "Yes, Stuart. How can I help you?",
        "Yes, what's on your mind?",
        "I'm here. What do you need?",
    ]

    def get_response(self, user_text):
        # Handle bare wake word with no question
        if user_text.lower().strip() in WAKE_WORD_VARIANTS:
            import random
            greeting = random.choice(self.GREETING_RESPONSES)
            self.log(f"GREETING: {greeting}")
            self.speak(greeting)
            self.memory.append({'user': user_text, 'hermes': greeting, 'time': time.time()})
            self._save_memory()
            return greeting

        # Always use big model for quality — speak transition first
        import random
        phrase = random.choice(self.FALLBACK_PHRASES)
        self.log(f"Transition: {phrase}")
        self.speak(phrase)
        full = self._call_model(user_text, API_URL_BIG, timeout=90)
        if not full:
            full = "I had trouble thinking about that one."
        self.log("Big model responded")

        full = full.replace('*', '').replace('#', '').replace('`', '').replace('~~', '').strip()
        # Strip "Hermia:" or "Assistant:" prefix if model adds it
        for prefix in ["hermia:", "hermia ", "hermiya:", "hernia:", "assistant:", "assistant "]:
            if full.lower().startswith(prefix):
                full = full[len(prefix):].strip()
        self.memory.append({'user': user_text, 'hermes': full[:200], 'time': time.time()})
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
                f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}]\nYou: {u}\nHermia: {r}\n")
        except:
            pass

    def process_speech(self, text):
        self.save_state("thinking")
        resp = self.get_response(text)
        self.last_response_time = time.time()  # Stamp end of response for cooldown
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
                        # During conversation, require louder speech to avoid hallucinating ambient noise
                        if self.conversation_active:
                            loud_count = sum(1 for _, x in buf if x > MIN_RMS_ENERGY * 1.5)
                            if loud_count < 2:
                                continue  # Probably ambient noise, skip
                        # Post-response cooldown: don't transcribe immediately after responding
                        # Prevents "thank you Hermia" from triggering a new wake cycle
                        post_cd = time.time() - self.last_response_time
                        if post_cd < POST_RESPONSE_COOLDOWN:
                            continue  # Still cooling down from last response
                        counter += 1
                        if counter >= SLIDING_STEP:
                            counter = 0
                            sp = RECORDING_DIR / f"seg_{time.time()}.wav"
                            if self.concat_wavs([p for p, _ in buf], str(sp)):
                                prompt = "hermia" if not self.conversation_active else None
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
                                            # Wake word during cooldown, but already in conversation — treat as follow-up
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
