import streamlit as st
import os
from dotenv import load_dotenv
import time
import tempfile
from audiorecorder import audiorecorder # Import the recorder component
import io
import warnings
warnings.filterwarnings("ignore") # Ignore warnings for cleaner output

# Import functions from other modules
from processing import (
    load_diarization_pipeline,
    load_transcription_model,
    perform_diarization,
    transcribe_audio,
    align_transcription_with_diarization,
    SUPPORTED_WHISPER_MODELS,
    DEFAULT_WHISPER_MODEL,
    DEFAULT_PYANNOTE_PIPELINE
)
from utils import (
    format_aligned_transcript,
    save_uploaded_file,
    get_transcript_text,
    save_recorded_audio_to_wav # Import new function
)
from summarization import (
    summarize_text_map_reduce,
    LMSTUDIO_DEFAULT_URL,
    DEFAULT_LOCAL_MODEL
)

# Disable Streamlit's file watcher to prevent PyTorch compatibility errors
os.environ['STREAMLIT_SERVER_WATCHDOG_TIMEOUT'] = '1'


# --- Page Configuration ---
st.set_page_config(
    page_title="Audio Analysis: Transcribe, Diarize & Summarize (Local LLM)",
    page_icon="🎙️", # Changed icon
    layout="wide"
)

# --- Load Environment Variables ---
load_dotenv()
HF_TOKEN = st.secrets.get("HF_TOKEN", os.getenv("HF_TOKEN"))
LMSTUDIO_API_URL_ENV = st.secrets.get("LMSTUDIO_API_URL", os.getenv("LMSTUDIO_API_URL"))

# --- App Title ---
st.title("🎙️ Transcibio")
st.markdown("Transcribe (Whisper), Diarize (Pyannote), and Summarize (Local LLM via LM Studio). Use file upload or direct recording.")

# --- Session State Initialization ---
if 'aligned_data' not in st.session_state:
    st.session_state.aligned_data = None
if 'full_transcript_text' not in st.session_state:
    st.session_state.full_transcript_text = None
if 'audio_processed' not in st.session_state:
     st.session_state.audio_processed = False # Flag to track if processing occurred


# --- Sidebar Configuration ---
st.sidebar.header("⚙️ Configuration")

# --- Transcription/Diarization Config ---
st.sidebar.subheader("Transcription & Diarization")
selected_whisper_model = st.sidebar.selectbox(
    "Whisper Model:",
    options=SUPPORTED_WHISPER_MODELS,
    index=SUPPORTED_WHISPER_MODELS.index(DEFAULT_WHISPER_MODEL),
    help="Select Whisper model size (accuracy vs. speed)."
)
num_speakers = st.sidebar.number_input(
    "Number of Speakers (0=Auto):", min_value=0, value=0,
    help="0 for auto-detect, or specify known number."
)
num_speakers_param = num_speakers if num_speakers > 0 else None

# --- HF Token Info ---
st.sidebar.markdown("---")
st.sidebar.subheader("Hugging Face (for Diarization)")
# (Keep HF Token display logic as before)
if HF_TOKEN:
    st.sidebar.success("✅ HF Token found.")
else:
    st.sidebar.error("❌ HF Token not found.")
    st.sidebar.info("Add `HF_TOKEN` to secrets/env for diarization.")

# --- Summarization Config (Local LLM Only) ---
st.sidebar.markdown("---")
st.sidebar.subheader("Summarization (LM Studio)")
lmstudio_url_input = st.sidebar.text_input(
    "LM Studio API URL:",
    value=LMSTUDIO_API_URL_ENV or LMSTUDIO_DEFAULT_URL,
    help="URL for your running LM Studio server."
)
lmstudio_url = lmstudio_url_input.strip() if lmstudio_url_input else None

# Add Model Name Input
local_model_name_input = st.sidebar.text_input(
    "Local Model Name:",
    value=DEFAULT_LOCAL_MODEL,
    help="Specify the model identifier LM Studio expects (e.g., from model list in LM Studio)."
)

if not lmstudio_url:
     st.sidebar.warning("LM Studio URL needed for summarization.")
else:
     # Display the URL and Model being used
     st.sidebar.info(f"LM Studio URL: `{lmstudio_url}`")
     st.sidebar.info(f"Summarization Model: `{local_model_name_input}`")
     st.sidebar.caption("Ensure LM Studio is running with the specified model loaded and the server started.")

# Chunking controls
st.sidebar.markdown("---")
st.sidebar.subheader("Detail Control (Chunking)")
chunk_size = st.sidebar.slider(
    "Chunk Size (chars):", min_value=500, max_value=8000, value=1500, step=100,
    help="Size of text chunks sent to the LLM. The larger the chunk, the less detail in the summary."
)
# Add the toggle for final summary
combine_summaries = st.sidebar.toggle(
    "Generate Final Combined Summary", 
    value=True,
    help="When enabled, creates a final summary from all chunk summaries. When disabled, returns individual chunk summaries."
)

# --- Main Area ---
st.markdown("---")

# --- Input Method Selection ---
input_method = st.radio("Choose Audio Input Method:", ("Upload File", "Record Audio"), horizontal=True)

uploaded_file = None
audio_bytes = None
temp_audio_path_from_input = None # Store path from either upload or record

if input_method == "Upload File":
    uploaded_file = st.file_uploader(
        "Upload Audio File",
        type=["wav"],
        key="file_uploader" # Add key to potentially reset
    )
    if uploaded_file:
        st.audio(uploaded_file, format=uploaded_file.type)
        # Save uploaded file temporarily when processing starts
        # temp_audio_path_from_input = save_uploaded_file(uploaded_file) # Moved to button logic

elif input_method == "Record Audio":
    st.info("Click the microphone icon to start recording. Click again to stop.")
    # Configure the recorder - pauses on silence by default
    audio_bytes = audiorecorder("Click to Record", "Click to Stop Recording", key="recorder")

    if len(audio_bytes) > 0:  # Check if audio was recorded
        # Convert AudioSegment to bytes that Streamlit can display
        buf = io.BytesIO()
        audio_bytes.export(buf, format="wav")
        buf.seek(0)
        
        # Display the audio
        st.audio(buf, format="audio/wav")
        # Save recorded bytes temporarily when processing starts
        # temp_audio_path_from_input = save_recorded_audio_to_wav(audio_bytes) # Moved to button logic


# --- Processing Button ---
st.markdown("---")
process_button_disabled = not (uploaded_file or audio_bytes) or not HF_TOKEN
if not HF_TOKEN:
     st.warning("Hugging Face Token needed in sidebar to enable processing.")

if st.button("📊 Process Audio", type="primary", disabled=process_button_disabled):
    # Clear previous results
    st.session_state.aligned_data = None
    st.session_state.full_transcript_text = None
    st.session_state.audio_processed = False

    temp_audio_path = None # Reset path
    input_audio_source = None # Track if we got data from upload or record

    start_time = time.time()
    try:
        # --- Step 1: Get audio data and save temporarily ---
        with st.spinner("Preparing audio data..."):
            if uploaded_file:
                temp_audio_path = save_uploaded_file(uploaded_file)
                input_audio_source = "upload"
            elif audio_bytes:
                temp_audio_path = save_recorded_audio_to_wav(audio_bytes)
                input_audio_source = "record"

            if not temp_audio_path:
                 st.error("Could not prepare audio data for processing.")
                 st.stop()

        # --- Steps 2-5: Load models, Diarize, Transcribe, Align ---
        # (This part remains the same as before, using temp_audio_path)
        # ... Load Models ...
        # ... Diarize ...
        # ... Transcribe ...
        # ... Align ... 
        # --- Start Copy ---
        # 2. Load Models
        diarization_pipeline = load_diarization_pipeline(auth_token=HF_TOKEN)
        transcription_model = load_transcription_model(selected_whisper_model)
        if not diarization_pipeline or not transcription_model: st.stop()

        # 3. Perform Diarization
        speaker_segments = perform_diarization(
            diarization_pipeline, temp_audio_path, num_speakers_param
        )

        # 4. Perform Transcription
        transcription_result = transcribe_audio(
            transcription_model, temp_audio_path
        )

        # 5. Align Results
        with st.spinner("Aligning transcription..."):
            aligned_data = align_transcription_with_diarization(
                transcription_result, speaker_segments
            )
            st.session_state.aligned_data = aligned_data
            st.session_state.full_transcript_text = get_transcript_text(aligned_data)
            st.session_state.audio_processed = True # Mark as processed
         # --- End Copy ---


        end_time = time.time()
        processing_time = end_time - start_time
        st.success(f"🕒 Transcription & Diarization complete in {processing_time:.2f} seconds!")
        if lmstudio_url:
            st.info("Now you can optionally generate a summary below.")


    except Exception as e:
        st.session_state.audio_processed = False
        st.error(f"An error occurred during Transcription/Diarization: {e}")

    finally:
        # Clean up temporary file
        if temp_audio_path and os.path.exists(temp_audio_path):
            try: os.remove(temp_audio_path)
            except Exception as e_clean: st.warning(f"Could not remove temp file: {e_clean}")


# --- Display Transcript Results ---
if st.session_state.get('audio_processed'):
    st.markdown("---")
    st.subheader("🗣️ Speaker-Aligned Transcript:")
    if st.session_state.aligned_data:
        formatted_lines = format_aligned_transcript(st.session_state.aligned_data)
        st.markdown("\n\n".join(formatted_lines))
        st.text_area(
            "Full Transcript Text (for summarization input)",
            st.session_state.full_transcript_text,
            height=150
        )
    else:
        st.warning("No aligned transcript data generated.")

# --- Summarization Trigger ---
# Check if processing happened AND lmstudio_url is configured
if st.session_state.get('audio_processed') and lmstudio_url:
    st.markdown("---")
    st.subheader(f"✍️ Generate Summary (LM Studio: `{local_model_name_input}`) ")

    if st.button(f"Generate Summary using LM Studio"):
        summary_display = st.empty() # Placeholder for summary output
        with summary_display.container():
            if not st.session_state.full_transcript_text:
                 st.warning("No transcript text available to summarize.")
            else:
                with st.spinner(f"Generating summary via LM Studio... (Chunk Size: {chunk_size})"):
                    start_summary_time = time.time()
                    summary_text = summarize_text_map_reduce(
                        full_text=st.session_state.full_transcript_text,
                        chunk_size=chunk_size,
                        chunk_overlap=150,
                        base_url=lmstudio_url,
                        model_name=local_model_name_input, # Pass the user-specified model name
                        combine_summaries=combine_summaries # Pass the combine_summaries flag
                    )
                    end_summary_time = time.time()

                    if summary_text:
                        st.markdown("### Summary Result:")
                        st.markdown(summary_text)
                        st.info(f"Summarization took {end_summary_time - start_summary_time:.2f} seconds.")
                    else:
                        st.error("Summarization failed. Check LM Studio status and logs.")


# --- Footer Info ---
st.markdown("---")
st.markdown("App built using Streamlit, Whisper, Pyannote.audio, Langchain, and LM Studio compatible local LLMs.")