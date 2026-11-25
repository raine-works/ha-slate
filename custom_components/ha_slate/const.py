"""Constants for the HA Slate integration."""

from typing import Final

DOMAIN: Final = "ha_slate"
DEFAULT_PORT: Final = 6054
PROTOCOL_VERSION: Final = 1

CONF_FINGERPRINT: Final = "fingerprint"
CONF_TOKEN: Final = "token"
CONF_DEVICE_ID: Final = "device_id"
CONF_PAIRING_CODE: Final = "pairing_code"

# Binary channels (see apps/slate-agent/slate_agent/protocol.py)
CH_MIC: Final = 0x01
CH_SPK: Final = 0x02
CH_FILE: Final = 0x03

# Same folder ESPHome satellites use: one set of model files for every satellite.
WAKE_WORDS_DIR_NAME: Final = "custom_wake_words"

TTS_SAMPLE_RATE: Final = 22050
AUDIO_CHUNK_BYTES: Final = 4096
UPLOAD_CHUNK_BYTES: Final = 32 * 1024
TTS_TIMEOUT_EXTRA: Final = 3.0

EDGE_EFFECTS: Final = ["None", "Breathe", "Scan", "Pulse"]
EVENT_WAKE_WORD_DETECTED: Final = f"{DOMAIN}_wake_word_detected"
SERVICE_SYNC_WAKE_WORDS: Final = "sync_wake_words"
