import sounddevice as sd
import numpy as np
import queue
import threading
import json
import os
from typing import Optional

# Load settings
import yaml

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config', 'voice_settings.yaml')
if os.path.exists(SETTINGS_PATH):
    with open(SETTINGS_PATH, 'r') as f:
        voice_config = yaml.safe_load(f)
else:
    voice_config = {
        'provider': 'whisper',
        'language': 'en',
        'model_path': None,
    }

# Simple wrapper for Whisper (offline) or placeholder for other providers
try:
    import whisper
    _whisper_model = None
    def _load_whisper_model():
        global _whisper_model
        if _whisper_model is None:
            model_name = voice_config.get('model_path') or 'base'
            _whisper_model = whisper.load_model(model_name)
        return _whisper_model
except Exception:
    whisper = None
    _whisper_model = None
    def _load_whisper_model():
        raise RuntimeError('Whisper not available')

# Audio capture parameters
_SAMPLE_RATE = 16000
_BLOCK_SIZE = 1024

class VoiceInterface:
    def __init__(self):
        self.q = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = None

    def _callback(self, indata, frames, time, status):
        """Callback from sounddevice, puts raw audio into a queue."""
        if status:
            print(f'Audio status: {status}', flush=True)
        # Convert to mono float32
        audio = indata[:, 0].astype(np.float32)
        self.q.put(audio)

    def start_listening(self):
        """Start background audio capture."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._record_loop, daemon=True)
        self._thread.start()

    def _record_loop(self):
        with sd.InputStream(samplerate=_SAMPLE_RATE, channels=1, callback=self._callback, blocksize=_BLOCK_SIZE):
            while not self._stop_event.is_set():
                sd.sleep(100)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join()

    def listen_once(self, timeout: Optional[float] = None) -> str:
        """Capture audio until silence is detected (simple heuristic) and return transcription.
        Returns empty string on failure.
        """
        # Record for up to timeout seconds (default 5s)
        duration = timeout or 5.0
        try:
            with sd.InputStream(samplerate=_SAMPLE_RATE, channels=1, dtype='float32') as stream:
                sd.sleep(int(duration * 1000))
                data, _ = stream.read(int(duration * _SAMPLE_RATE))
                audio = data[:, 0]
        except Exception as e:
            print(f'Audio capture error: {e}')
            return ''
        # Run STT
        if voice_config.get('provider') == 'whisper' and whisper:
            model = _load_whisper_model()
            result = model.transcribe(audio, language=voice_config.get('language', 'en'))
            return result.get('text', '').strip()
        else:
            # Placeholder for other providers – raise to signal missing implementation
            raise NotImplementedError('Only Whisper provider is implemented currently')

# Export a singleton for easy import
voice_interface = VoiceInterface()
