
"""
Voice Pipeline Configuration
============================

This file contains all tunable parameters for the voice agent pipeline.
Each setting is documented with:
- What it does
- Valid range
- Trade-offs
- Recommended values for different use cases

USAGE:
    from voice_pipeline_config import PipelineConfig
    config = PipelineConfig.load()  # Loads from this file with defaults

To customize: Edit the values below. Invalid values fall back to defaults.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# VOICE ACTIVITY DETECTION (VAD) - Silero
# =============================================================================
# VAD detects when the user starts/stops speaking.
# These settings control how sensitive and fast the detection is.

@dataclass
class VADConfig:
    """Silero Voice Activity Detection configuration."""

    # -------------------------------------------------------------------------
    # min_silence_duration (seconds)
    # -------------------------------------------------------------------------
    # How long the user must be silent before VAD triggers "speech ended".
    #
    # Range: 0.1 - 2.0 seconds
    # Default: 0.55 (Silero default)
    #
    # Lower = Faster response, but may cut off users who pause mid-sentence
    # Higher = More patient, waits for natural pauses
    #
    # Recommendations:
    #   - Fast/Snappy agent: 0.25 - 0.35
    #   - Balanced: 0.4 - 0.5
    #   - Patient/Careful: 0.6 - 0.8
    #
    # Trade-off: Speed vs. not interrupting users who say "I want... umm..."
    min_silence_duration: float = 0.45

    # -------------------------------------------------------------------------
    # min_speech_duration (seconds)
    # -------------------------------------------------------------------------
    # Minimum duration of audio to be classified as speech (not noise).
    #
    # Range: 0.01 - 0.5 seconds
    # Default: 0.05
    #
    # Lower = More sensitive, detects short utterances like "yes", "no"
    # Higher = Ignores very short sounds (coughs, "um")
    #
    # Recommendations:
    #   - Most cases: 0.05 (default is good)
    #   - Noisy environment: 0.1 - 0.15
    min_speech_duration: float = 0.05

    # -------------------------------------------------------------------------
    # activation_threshold (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Confidence score required to classify audio as "speech".
    #
    # Range: 0.0 - 1.0 (probability)
    # Default: 0.5
    #
    # Lower = More sensitive, triggers on quieter speech
    # Higher = Less sensitive, requires clearer speech
    #
    # Recommendations:
    #   - Clean audio (headset, quiet room): 0.4 - 0.5
    #   - Phone calls (some noise): 0.5 - 0.6
    #   - Noisy environment: 0.6 - 0.7
    #
    # Trade-off: Lower catches more speech but may false-trigger on noise
    activation_threshold: float = 0.5

    # -------------------------------------------------------------------------
    # prefix_padding_duration (seconds)
    # -------------------------------------------------------------------------
    # Audio to include BEFORE detected speech start (captures word beginnings).
    #
    # Range: 0.1 - 1.0 seconds
    # Default: 0.5
    #
    # Recommendations:
    #   - Most cases: 0.3 - 0.5
    #   - If words are getting cut off at start: increase to 0.6 - 0.8
    prefix_padding_duration: float = 0.5

    # -------------------------------------------------------------------------
    # max_buffered_speech (seconds)
    # -------------------------------------------------------------------------
    # Maximum speech duration to buffer before forcing processing.
    #
    # Range: 10 - 120 seconds
    # Default: 60
    #
    # Recommendations:
    #   - Most cases: 60 (default is fine)
    #   - Long monologues expected: 90 - 120
    max_buffered_speech: float = 60.0

    # -------------------------------------------------------------------------
    # sample_rate (Hz)
    # -------------------------------------------------------------------------
    # Audio sample rate for VAD processing.
    #
    # Values: 8000 or 16000
    # Default: 16000
    #
    # Note: 16000 is standard for speech recognition
    sample_rate: int = 16000

    # -------------------------------------------------------------------------
    # force_cpu (boolean)
    # -------------------------------------------------------------------------
    # Force CPU processing even if GPU is available.
    #
    # Default: True
    #
    # Note: VAD is lightweight, CPU is usually fine and more predictable
    force_cpu: bool = True


# =============================================================================
# SPEECH-TO-TEXT (STT) - Deepgram
# =============================================================================
# STT converts user speech to text. These settings affect transcription
# speed and accuracy.

@dataclass
class STTConfig:
    """Deepgram Speech-to-Text configuration."""

    # -------------------------------------------------------------------------
    # model
    # -------------------------------------------------------------------------
    # Deepgram model to use for transcription.
    #
    # Options:
    #   - "nova-3": Latest, fastest, most accurate (recommended)
    #   - "nova-2": Previous generation, still good
    #   - "nova-2-phonecall": Optimized for phone audio
    #   - "nova-2-meeting": Optimized for meeting audio
    #
    # Recommendations:
    #   - General use: "nova-3"
    #   - Phone calls with poor audio: "nova-2-phonecall"
    model: str = "nova-3"

    # -------------------------------------------------------------------------
    # language
    # -------------------------------------------------------------------------
    # Primary language for transcription.
    #
    # Options: "en", "en-US", "en-IN", "hi", "es", "fr", etc.
    # Default: "en"
    #
    # Note: Can be overridden per-call based on agent configuration
    language: str = "en"

    # -------------------------------------------------------------------------
    # endpointing_ms (milliseconds)
    # -------------------------------------------------------------------------
    # How quickly Deepgram finalizes a transcript after detecting silence.
    #
    # Range: 10 - 1000 ms
    # Deepgram Plugin Default: 25ms
    #
    # Lower = Faster finalization, but may split words incorrectly
    # Higher = More accurate word boundaries, but slower
    #
    # Recommendations:
    #   - Fast response: 10 - 20 ms (may split words)
    #   - Balanced: 25 - 50 ms (Deepgram default is 25)
    #   - Accurate transcription: 100+ ms
    #
    # Trade-off: Speed vs. transcript quality
    # NOTE: This value IS sent to Deepgram. 10ms is aggressive but valid.
    endpointing_ms: int = 10  # Aggressive - compensates for SIP transfer latency

    # -------------------------------------------------------------------------
    # interim_results (boolean)
    # -------------------------------------------------------------------------
    # Send partial transcripts while user is still speaking.
    #
    # Default: True
    #
    # True = Enables preemptive LLM generation (faster overall response)
    # False = Only sends complete utterances (more accurate but slower)
    #
    # Recommendation: Always True for real-time voice agents
    interim_results: bool = True

    # -------------------------------------------------------------------------
    # no_delay (boolean)
    # -------------------------------------------------------------------------
    # Disable artificial latency that improves punctuation/formatting.
    #
    # Default: True
    #
    # True = Fastest possible transcription
    # False = Slightly delayed but better punctuation
    #
    # Recommendation: True for voice agents (punctuation less important)
    no_delay: bool = True

    # -------------------------------------------------------------------------
    # smart_format (boolean)
    # -------------------------------------------------------------------------
    # Enable smart formatting (numbers, dates, etc.).
    #
    # Default: False
    #
    # True = "January 15th 2024" instead of "january fifteenth twenty twenty four"
    # False = Raw transcription
    #
    # Recommendation: False for speed, True if formatting matters
    smart_format: bool = False

    # -------------------------------------------------------------------------
    # punctuate (boolean)
    # -------------------------------------------------------------------------
    # Add punctuation to transcripts.
    #
    # Default: True
    #
    # Recommendation: True (helps LLM understand sentence structure)
    punctuate: bool = True

    # -------------------------------------------------------------------------
    # profanity_filter (boolean)
    # -------------------------------------------------------------------------
    # Filter profanity from transcripts.
    #
    # Default: False
    #
    # Recommendation: False (let LLM handle appropriately)
    profanity_filter: bool = False

    # -------------------------------------------------------------------------
    # diarize (boolean)
    # -------------------------------------------------------------------------
    # Speaker diarization (identify different speakers).
    #
    # Default: False
    #
    # Note: Adds latency, usually not needed for 1:1 voice agent calls
    diarize: bool = False


# =============================================================================
# TURN DETECTION
# =============================================================================
# Turn detector predicts when the user has finished their turn and expects
# a response. Uses AI to understand conversational patterns.

@dataclass
class TurnDetectorConfig:
    """Turn detection configuration."""

    # -------------------------------------------------------------------------
    # unlikely_threshold (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Probability threshold for "user is unlikely to continue speaking".
    #
    # Range: 0.0 - 1.0
    # Default: ~0.3-0.5 (model default)
    #
    # Lower = Faster turn detection, but may interrupt users
    # Higher = More patient, waits for clearer end-of-turn signals
    #
    # Recommendations:
    #   - Fast/Snappy: 0.1 - 0.2
    #   - Balanced: 0.3 - 0.4
    #   - Patient: 0.5 - 0.6
    #
    # Trade-off: Speed vs. not interrupting users mid-thought
    #
    # Set to None to use model default
    unlikely_threshold: Optional[float] = 0.18  # Aggressive - trigger faster turn detection


# =============================================================================
# AGENT SESSION / ENDPOINTING
# =============================================================================
# These are the final decision parameters that combine VAD, STT, and turn
# detection signals to decide when to trigger the agent response.

@dataclass
class EndpointingConfig:
    """Agent session endpointing configuration."""

    # -------------------------------------------------------------------------
    # min_endpointing_delay (seconds)
    # -------------------------------------------------------------------------
    # Minimum time to wait after last detected speech before responding.
    #
    # Range: 0.0 - 1.0 seconds
    # Default: 0.5
    #
    # This is a safety buffer to prevent cutting off users.
    # Even if VAD and turn detector say "user is done", wait at least this long.
    #
    # Recommendations:
    #   - Fast response: 0.1 - 0.15
    #   - Balanced: 0.2 - 0.3
    #   - Safe/Patient: 0.4 - 0.5
    #
    # Trade-off: Lower = faster but may interrupt users
    min_endpointing_delay: float = 0.1

    # -------------------------------------------------------------------------
    # max_endpointing_delay (seconds)
    # -------------------------------------------------------------------------
    # Maximum time to wait even if turn detector is uncertain.
    #
    # Range: 0.5 - 5.0 seconds
    # Default: 3.0
    #
    # If the turn detector can't decide after this long, respond anyway.
    # Prevents awkward long silences.
    #
    # Recommendations:
    #   - Most cases: 1.0 - 1.5 seconds
    #   - Very patient: 2.0 - 3.0 seconds
    max_endpointing_delay: float = 0.4

    # -------------------------------------------------------------------------
    # preemptive_generation (boolean)
    # -------------------------------------------------------------------------
    # Start LLM generation BEFORE confirming user is done speaking.
    #
    # Default: True
    #
    # How it works:
    #   Normal:     User stops → Confirm done → Start LLM → Wait → Speak
    #   Preemptive: User stops → Start LLM immediately → Confirm → Speak
    #
    # If user continues speaking, the preemptive response is discarded.
    #
    # Recommendation: Always True for low-latency agents
    #
    # Note: This is a WIN condition - faster response with minimal downside
    preemptive_generation: bool = True


# =============================================================================
# INTERRUPTION HANDLING
# =============================================================================
# Controls how the agent handles being interrupted by the user.

@dataclass
class InterruptionConfig:
    """Interruption handling configuration."""

    # -------------------------------------------------------------------------
    # allow_interruptions (boolean)
    # -------------------------------------------------------------------------
    # Allow users to interrupt the agent while it's speaking.
    #
    # Default: True
    #
    # True = Natural conversation, agent stops when user speaks
    # False = Agent finishes speaking before listening
    #
    # Recommendation: True for natural conversations
    allow_interruptions: bool = True

    # -------------------------------------------------------------------------
    # min_interruption_duration (seconds)
    # -------------------------------------------------------------------------
    # Minimum speech duration to count as an interruption.
    #
    # Range: 0.01 - 0.5 seconds
    # Default: 0.5
    #
    # Lower = More sensitive, even "um" or coughs may trigger
    # Higher = Only deliberate, sustained speech triggers interruption
    #
    # This works WITH min_interruption_words:
    #   - Audio must exceed this duration AND contain enough words
    #
    # Recommendations:
    #   - Very responsive: 0.05 - 0.1
    #   - Balanced: 0.15 - 0.25
    #   - Ignore brief sounds: 0.3 - 0.5
    min_interruption_duration: float = 0.12

    # -------------------------------------------------------------------------
    # min_interruption_words (int)
    # -------------------------------------------------------------------------
    # Minimum words required to count as an interruption WHILE AGENT IS SPEAKING.
    #
    # Range: 0 - 5
    # Default: 0
    #
    # 0 = Any speech interrupts (most responsive)
    # 1+ = Requires actual words, not just sounds
    #
    # IMPORTANT: This setting ONLY affects interruptions during agent speech.
    # When the agent is listening (waiting for user response), even single-word
    # answers like "yes" or "no" will register as valid responses.
    #
    # Recommendations:
    #   - 0: Most responsive, but backchannels like "yes", "uh-huh" interrupt
    #   - 2: Ignores single-word backchannels, requires "okay stop" or similar
    #   - 3: Very patient, requires deliberate multi-word interruption
    #
    # Use 2 to prevent "yes", "okay", "hum hum" from interrupting agent speech
    # while still allowing single-word answers when agent asks questions.
    min_interruption_words: int = 2

    # -------------------------------------------------------------------------
    # resume_false_interruption (boolean)
    # -------------------------------------------------------------------------
    # Resume speaking if interruption was too brief (false positive).
    #
    # Default: True
    #
    # True = If user just coughed, agent continues where it left off
    # False = Agent stops and waits for user to speak
    #
    # Recommendation: True
    resume_false_interruption: bool = True

    # -------------------------------------------------------------------------
    # false_interruption_timeout (seconds)
    # -------------------------------------------------------------------------
    # Time to wait before deciding an interruption was false.
    #
    # Range: 0.3 - 2.0 seconds
    # Default: 2.0
    #
    # Lower = Faster recovery from false interruptions
    # Higher = More patient, waits to see if user will speak
    #
    # Recommendations:
    #   - Fast recovery: 0.5 - 0.7
    #   - Balanced: 0.8 - 1.0
    #   - Patient: 1.5 - 2.0
    false_interruption_timeout: float = 0.4


# =============================================================================
# BACKGROUND AUDIO / AMBIENCE
# =============================================================================
# Settings for background sounds that make the agent feel more natural.

@dataclass
class AmbienceConfig:
    """Background audio and ambience configuration.

    LiveKit provides 3 built-in audio clips:
      - OFFICE_AMBIENCE: Subtle office background sounds
      - KEYBOARD_TYPING: Keyboard typing sound
      - KEYBOARD_TYPING2: Alternative typing sound

    For custom audio (like call center chatter), provide a path to an .ogg file.
    Example: "data/audio/call_center_ambience.ogg"

    Recommended sources for royalty-free audio:
      - https://freesound.org (search "call center ambience")
      - https://pixabay.com/sound-effects/
      - https://mixkit.co/free-sound-effects/
    """

    # -------------------------------------------------------------------------
    # enable_office_ambience (boolean)
    # -------------------------------------------------------------------------
    # Play subtle office background sounds during calls.
    #
    # Default: True
    #
    # Creates a more natural, human-like atmosphere.
    enable_office_ambience: bool = False

    # -------------------------------------------------------------------------
    # enable_typing_noise (boolean)
    # -------------------------------------------------------------------------
    # Play typing sounds when agent is "thinking" (during LLM processing).
    #
    # Default: True
    #
    # Provides audio feedback that agent is working on a response.
    enable_typing_noise: bool = True

    # -------------------------------------------------------------------------
    # enable_people_talking (boolean)
    # -------------------------------------------------------------------------
    # Play faint people talking / call center ambience.
    #
    # Default: False
    #
    # Requires a custom audio file - set people_talking_audio_path below.
    # LiveKit doesn't include this by default.
    enable_people_talking: bool = True

    # -------------------------------------------------------------------------
    # people_talking_audio_path (string or None)
    # -------------------------------------------------------------------------
    # Path to custom audio file for people talking ambience.
    #
    # Default: None (disabled)
    #
    # Example: "data/audio/call_center_ambience.ogg"
    #
    # Supported formats: .ogg, .wav, .mp3
    # Recommendation: Use a loopable audio file for continuous playback.
    # Default: Uses the office-ambience-6322.mp3 file in v2/data/audio/
    people_talking_audio_path: str | None = r"data/audio/office-ambience-6322.mp3"
    
   # -------------------------------------------------------------------------
    # people_talking_volume (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Volume level for people talking ambience.
    #
    # Range: 0.0 - 1.0
    # Default: 0.15
    #
    # Keep this low so it doesn't interfere with conversation.
    # Recommendation: 0.1 - 0.2 for subtle background chatter
    people_talking_volume: float = 0.2

    # -------------------------------------------------------------------------
    # ambience_volume (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Volume level for background ambience.
    #
    # Range: 0.0 - 1.0
    # Default: 0.35
    #
    # Recommendations:
    #   - Subtle: 0.1 - 0.2
    #   - Noticeable: 0.3 - 0.4
    #   - Prominent: 0.5+
    ambience_volume: float = 0.35
    # -------------------------------------------------------------------------
    # custom_ambience_audio_path (string or None)
    # -------------------------------------------------------------------------
    # Path to custom audio file to replace the built-in office ambience.
    #
    # Default: None (uses LiveKit's built-in OFFICE_AMBIENCE)
    #
    # Example: "data/audio/custom_office.ogg"
    #
    # If set, this replaces the built-in OFFICE_AMBIENCE clip.
    #custom_ambience_audio_path: str | None = r"data\ambient_sounds\people-talking-at-cafe-ambience-6159.mp3"
    custom_ambience_audio_path: str | None = None

    # -------------------------------------------------------------------------
    # typing_volume (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Volume level for typing sounds.
    #
    # Range: 0.0 - 1.0
    # Default: 0.12
    #
    # Note: Lower than ambience since typing is more attention-grabbing
    typing_volume: float = 0.09

    # -------------------------------------------------------------------------
    # typing_interval_min (seconds)
    # -------------------------------------------------------------------------
    # Minimum delay between typing sounds.
    #
    # Range: 1.0 - 20.0 seconds
    # Default: 6.0
    #
    # Lower = More frequent typing (busier sounding)
    # Higher = Less frequent typing (more relaxed)
    typing_interval_min: float = 6.0

    # -------------------------------------------------------------------------
    # typing_interval_max (seconds)
    # -------------------------------------------------------------------------
    # Maximum delay between typing sounds.
    #
    # Range: 5.0 - 30.0 seconds
    # Default: 10.0
    #
    # Must be greater than typing_interval_min
    typing_interval_max: float = 10.0

    # -------------------------------------------------------------------------
    # typing_probability (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Probability of playing typing sound on each thinking event.
    #
    # Range: 0.0 - 1.0
    # Default: 0.6
    #
    # Lower = Typing sounds less frequently during thinking
    # Higher = Typing sounds more consistently during thinking
    typing_probability: float = 0.6

    # -------------------------------------------------------------------------
    # delay_after_first_speech (seconds)
    # -------------------------------------------------------------------------
    # Delay before starting background audio after agent's first utterance.
    #
    # Range: 0.0 - 5.0 seconds
    # Default: 2.0
    #
    # This gives a moment of "clean" audio before ambient sounds begin.
    delay_after_first_speech: float = 0.2


# =============================================================================
# NOISE REDUCTION / AUDIO PROCESSING
# =============================================================================
# Settings for cleaning up audio before processing.

@dataclass
class NoiseReductionConfig:
    """Audio noise reduction configuration."""

    # -------------------------------------------------------------------------
    # enable_noise_reduction (boolean)
    # -------------------------------------------------------------------------
    # Apply noise reduction to incoming audio.
    #
    # Default: False
    #
    # Note: Most STT services (Deepgram) have built-in noise handling.
    # Only enable if you have very noisy audio.
    #
    # Trade-off: Can reduce latency but may affect speech quality
    enable_noise_reduction: bool = False

    # -------------------------------------------------------------------------
    # noise_reduction_level (0.0 - 1.0)
    # -------------------------------------------------------------------------
    # Intensity of noise reduction if enabled.
    #
    # Range: 0.0 - 1.0
    # Default: 0.5
    #
    # Lower = Subtle noise reduction, preserves speech quality
    # Higher = Aggressive noise removal, may distort speech
    noise_reduction_level: float = 0.5

    # -------------------------------------------------------------------------
    # echo_cancellation (boolean)
    # -------------------------------------------------------------------------
    # Apply echo cancellation.
    #
    # Default: False
    #
    # Note: Usually handled by phone/VoIP system. Only enable if needed.
    echo_cancellation: bool = False


# =============================================================================
# CALL TIMING / SILENCE HANDLING
# =============================================================================
# Settings for handling silence and call timeouts.

@dataclass
class SilenceConfig:
    """Call silence and timeout configuration."""

    # -------------------------------------------------------------------------
    # silence_warning_seconds (seconds)
    # -------------------------------------------------------------------------
    # Time of silence before agent prompts user ("Are you still there?").
    #
    # Range: 5 - 60 seconds
    # Default: 15
    #
    # Recommendations:
    #   - Active conversations: 10 - 15
    #   - Forms/data entry: 20 - 30
    #   - Patient waiting: 30 - 45
    silence_warning_seconds: float = 20.0

    # -------------------------------------------------------------------------
    # silence_timeout_seconds (seconds)
    # -------------------------------------------------------------------------
    # Time of silence before agent ends the call.
    #
    # Range: 15 - 120 seconds
    # Default: 35
    #
    # Note: Should be > silence_warning_seconds
    silence_timeout_seconds: float = 35.0


# =============================================================================
# INITIAL GREETING / CALL START SETTINGS
# =============================================================================
# Settings for handling the initial greeting when calls start.
# These help prevent the "hello collision" where both agent and user speak at once.

@dataclass
class GreetingConfig:
    """Initial greeting configuration for call start."""

    # -------------------------------------------------------------------------
    # greeting_uninterruptible (boolean)
    # -------------------------------------------------------------------------
    # Make the initial greeting uninterruptible.
    #
    # Default: True
    #
    # When True:
    #   - Agent's first greeting cannot be interrupted by user's "hello"
    #   - Prevents 2-3 turn sync issues at call start
    #   - Allows AEC (Acoustic Echo Cancellation) calibration
    #   - After greeting finishes, normal interruption rules apply
    #
    # When False:
    #   - User can interrupt agent's first greeting
    #   - May cause collision if both say "hello" simultaneously
    #
    # Recommendation: True (as per LiveKit best practices)
    greeting_uninterruptible: bool = True

    # -------------------------------------------------------------------------
    # greeting_delay_seconds (seconds)
    # -------------------------------------------------------------------------
    # Delay after call connects before agent speaks.
    #
    # Range: 0.0 - 2.0 seconds
    # Default: 0.3
    #
    # Allows time for:
    #   - Call audio to stabilize
    #   - User to say "Hello?" first (if greeting_uninterruptible=False)
    #   - Natural conversation start
    #
    # Recommendations:
    #   - Immediate greeting: 0.2 - 0.3
    #   - Wait for "Hello?": 0.5 - 1.0
    greeting_delay_seconds: float = 0.3


# =============================================================================
# OUTBOUND CALL SETTINGS (DEPRECATED - use GreetingConfig)
# =============================================================================
# Kept for backward compatibility.

@dataclass
class OutboundCallConfig:
    """Outbound call configuration (deprecated - use GreetingConfig)."""

    greeting_delay_seconds: float = 0.3


# =============================================================================
# MASTER CONFIGURATION
# =============================================================================

@dataclass
class PipelineConfig:
    """
    Master configuration for the entire voice pipeline.

    Usage:
        config = PipelineConfig.load()

        # Access individual configs:
        config.vad.min_silence_duration
        config.stt.endpointing_ms
        config.endpointing.preemptive_generation
    """

    vad: VADConfig = field(default_factory=VADConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    turn_detector: TurnDetectorConfig = field(default_factory=TurnDetectorConfig)
    endpointing: EndpointingConfig = field(default_factory=EndpointingConfig)
    greeting: GreetingConfig = field(default_factory=GreetingConfig)
    interruption: InterruptionConfig = field(default_factory=InterruptionConfig)
    ambience: AmbienceConfig = field(default_factory=AmbienceConfig)
    noise_reduction: NoiseReductionConfig = field(default_factory=NoiseReductionConfig)
    silence: SilenceConfig = field(default_factory=SilenceConfig)
    outbound: OutboundCallConfig = field(default_factory=OutboundCallConfig)

    @classmethod
    def load(cls) -> "PipelineConfig":
        """
        Load configuration from this file.

        Values defined in this file are used directly.
        This method exists for future extensibility (e.g., loading from YAML).
        """
        config = cls()
        logger.info("Voice pipeline configuration loaded:")
        logger.info(f"  VAD: min_silence={config.vad.min_silence_duration}s, threshold={config.vad.activation_threshold}")
        logger.info(f"  STT: model={config.stt.model}, endpointing={config.stt.endpointing_ms}ms")
        logger.info(f"  Endpointing: min={config.endpointing.min_endpointing_delay}s, max={config.endpointing.max_endpointing_delay}s")
        logger.info(f"  Preemptive generation: {config.endpointing.preemptive_generation}")
        return config

    def log_summary(self) -> None:
        """Log a summary of the current configuration."""
        logger.info("=" * 60)
        logger.info("VOICE PIPELINE CONFIGURATION")
        logger.info("=" * 60)
        logger.info(f"VAD (Silero):")
        logger.info(f"  min_silence_duration: {self.vad.min_silence_duration}s")
        logger.info(f"  activation_threshold: {self.vad.activation_threshold}")
        logger.info(f"STT (Deepgram):")
        logger.info(f"  model: {self.stt.model}")
        logger.info(f"  endpointing_ms: {self.stt.endpointing_ms}ms")
        logger.info(f"  interim_results: {self.stt.interim_results}")
        logger.info(f"Endpointing:")
        logger.info(f"  min_delay: {self.endpointing.min_endpointing_delay}s")
        logger.info(f"  max_delay: {self.endpointing.max_endpointing_delay}s")
        logger.info(f"  preemptive: {self.endpointing.preemptive_generation}")
        logger.info(f"Interruption:")
        logger.info(f"  allow: {self.interruption.allow_interruptions}")
        logger.info(f"  min_duration: {self.interruption.min_interruption_duration}s")
        logger.info(f"  false_timeout: {self.interruption.false_interruption_timeout}s")
        logger.info("=" * 60)


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================
# Pre-load configuration for easy access

_config: Optional[PipelineConfig] = None


def get_config() -> PipelineConfig:
    """Get the singleton pipeline configuration."""
    global _config
    if _config is None:
        _config = PipelineConfig.load()
    return _config


# =============================================================================
# QUICK PRESETS
# =============================================================================
# Pre-configured settings for common use cases

def get_fast_preset() -> PipelineConfig:
    """
    Preset for fastest possible response time.

    Trade-off: May interrupt users more often.
    """
    config = PipelineConfig()
    config.vad.min_silence_duration = 0.25
    config.vad.activation_threshold = 0.4
    config.stt.endpointing_ms = 10
    config.endpointing.min_endpointing_delay = 0.08
    config.endpointing.max_endpointing_delay = 0.8
    config.interruption.min_interruption_duration = 0.03
    config.interruption.false_interruption_timeout = 0.5
    return config


def get_balanced_preset() -> PipelineConfig:
    """
    Preset for balanced speed and accuracy.

    Good for most use cases.
    """
    return PipelineConfig()  # Defaults are balanced


def get_patient_preset() -> PipelineConfig:
    """
    Preset for patient, non-interrupting agent.

    Good for complex conversations where users need time to think.
    """
    config = PipelineConfig()
    config.vad.min_silence_duration = 0.8
    config.stt.endpointing_ms = 50
    config.endpointing.min_endpointing_delay = 0.3
    config.endpointing.max_endpointing_delay = 2.0
    config.interruption.min_interruption_duration = 0.2
    config.interruption.false_interruption_timeout = 1.0
    return config

