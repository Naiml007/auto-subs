import sys
import os

import mlx_whisper.tokenizer
import mlx_whisper.tokenizer
import mlx_whisper.whisper

# Set the default encoding to UTF-8
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'

class Unbuffered(object):
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        if self.stream:
            self.stream.write(data)
            self.stream.flush()

    def writelines(self, datas):
        if self.stream:
            self.stream.writelines(datas)
            self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)

# Reconfigure stdout and stderr to use UTF-8 encoding before wrapping
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# Ensure stdout and stderr are line-buffered after reconfiguration
sys.stdout = Unbuffered(sys.stdout)
sys.stderr = Unbuffered(sys.stderr)

from fastapi.middleware.cors import CORSMiddleware
import json
import random
import uvicorn
from pydantic import BaseModel
from fastapi import FastAPI, HTTPException, status
import asyncio
import appdirs
import time
import platform
import stable_whisper
from silero_vad import load_silero_vad, read_audio, get_speech_timestamps

# Define a base cache directory using appdirs
if platform.system() == 'Windows':
    cache_dir = appdirs.user_cache_dir("AutoSubs-Cache", "")
else:
    cache_dir = appdirs.user_cache_dir("AutoSubs", "")

# Matplotlib cache directory
matplotlib_cache_dir = os.path.join(cache_dir, 'matplotlib_cachedir')
os.makedirs(matplotlib_cache_dir, exist_ok=True)
os.environ['MPLCONFIGDIR'] = matplotlib_cache_dir

# Hugging Face cache directory
huggingface_cache_dir = os.path.join(cache_dir, 'hf_cache')
os.makedirs(huggingface_cache_dir, exist_ok=True)
os.environ['HF_HUB_CACHE'] = huggingface_cache_dir
os.environ['HF_HOME'] = huggingface_cache_dir

# Torch cache directory
pyannote_cache_dir = os.path.join(cache_dir, 'pyannote_cache')
os.makedirs(pyannote_cache_dir, exist_ok=True)
os.environ['PYANNOTE_CACHE'] = pyannote_cache_dir

# Print paths to verify
print(f"Matplotlib cache directory: {matplotlib_cache_dir}")
print(f"Hugging Face cache directory: {huggingface_cache_dir}")
print(f"Torch cache directory: {pyannote_cache_dir}")

from huggingface_hub import HfApi, HfFolder, login, snapshot_download
from huggingface_hub.utils import RepositoryNotFoundError, HfHubHTTPError
import torch

if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
    # Suppress the torch.load warning
    os.environ["PYTHONWARNINGS"] = "default"
    os.environ["TORCH_LOAD_IGNORE_POSSIBLE_SECURITY_RISK"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
else:
    base_path = os.path.dirname(__file__)

# Add FFmpeg binaries to PATH
if platform.system() == 'Windows':
    ffmpeg_path = os.path.join(base_path, 'ffmpeg_bin_win')
else:
    ffmpeg_path = os.path.join(base_path, 'ffmpeg_bin_mac')

os.environ["PATH"] = ffmpeg_path + os.pathsep + os.environ["PATH"]

app = FastAPI()

# Add CORS middleware to allow requests from your frontend
app.add_middleware(
    CORSMiddleware,
    # Adjust this to your frontend's domain or "*" for all
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods (GET, POST, OPTIONS, etc.)
    allow_headers=["*"],  # Allow all headers
)

win_models = {
    "tiny": "tiny",
    "base": "base",
    "small": "small",
    "medium": "medium",
    "large": "large-v3-turbo",
    "tiny.en": "tiny.en",
    "base.en": "base.en",
    "small.en": "small.en",
    "medium.en": "medium.en",
    "large.en": "large-v3-turbo",
}

mac_models = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx-q4",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large": "mlx-community/whisper-large-v3-turbo",
    "tiny.en": "mlx-community/whisper-tiny.en-mlx",
    "base.en": "mlx-community/whisper-base.en-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "medium.en": "mlx-community/whisper-medium.en-mlx",
    "large.en": "mlx-community/whisper-large-v3-turbo",
    "large.de": "mlx-community/whisper-large-v3-turbo-german-f16",
}

def sanitize_result(result):
    # Convert the result to a JSON string
    result_json = json.dumps(result, default=lambda o: None)
    # Parse the JSON string back to a dictionary
    sanitized_result = json.loads(result_json)
    return sanitized_result

def is_model_cached_locally(model_id, revision=None):
    try:
        snapshot_download(
            repo_id=model_id,
            revision=revision,  # Model version - use the latest revision if not specified
            local_files_only=True,
            allow_patterns=["*"],
        )
        return True
    except Exception:
        return False


def is_model_accessible(model_id, token=None, revision=None):
    # First, check if the model is cached locally
    if is_model_cached_locally(model_id, revision=revision):
        print(f"Model '{model_id}' is cached locally.")
        return True  # Model is cached locally and accessible

    print(
        f"Model '{model_id}' is not cached locally. Checking online access...")

    try:
        # Attempt to download a small file from the model repo to check access
        snapshot_download(
            repo_id=model_id,
            revision=revision,  # Use the latest revision if not specified
            token=token,
            # Adjust to download a minimal set of files
            allow_patterns=["config.yaml"],
            resume_download=False,  # Force download to check access
            local_files_only=False,
        )
        return True  # The model is accessible
    except RepositoryNotFoundError:
        print(f"Model '{model_id}' does not exist.")
        return False
    except HfHubHTTPError as e:
        if e.response.status_code == 403:
            print(f"Access denied to model '{
                  model_id}'. You may need to accept the model's terms or provide a valid token.")
        elif e.response.status_code == 401:
            print(f"Unauthorized access. Please check your Hugging Face access token.")
        else:
            print(f"An HTTP error occurred: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return False

# Function for transcribing the audio


def inference(audio, **kwargs) -> dict:
    import mlx_whisper
    if kwargs["language"] == "auto":
        output = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=kwargs["model"],
            word_timestamps=True,
            verbose=True,
            task=kwargs["task"]
        )
    else:
        output = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=kwargs["model"],
            word_timestamps=True,
            language=kwargs["language"],
            verbose=True,
            task=kwargs["task"]
        )
    return stable_whisper.result.WhisperResult(result=output, force_order=True)


def log_progress(seek, total_duration):
    # print progress as percentage
    print(f"Progress: {seek/total_duration*100:.0f}%")


async def transcribe_audio(audio_file, kwargs, max_words, max_chars, sensitive_words):
    if (platform.system() == 'Windows'):
        compute_type = "float16" if kwargs["device"] == "cuda" else "int8"
        model = stable_whisper.load_faster_whisper(kwargs["model"], device=kwargs["device"], compute_type=compute_type)
        if kwargs["language"] == "auto":
            result = model.transcribe_stable(
                audio_file, task=kwargs["task"], regroup=True, verbose=True, vad_filter=True, progress_callback=log_progress)
        else:
            result = model.transcribe_stable(
                audio_file, language=kwargs["language"], task=kwargs["task"], regroup=True, verbose=True, vad_filter=True, progress_callback=log_progress)
            model.align(audio_file, result, kwargs["language"])
            if kwargs["align_words"]:
                model.align_words(audio_file, result, kwargs["language"])
    else: # Use Whisper MLX on MacOS
        result = stable_whisper.transcribe_any(
            inference, audio_file, inference_kwargs=kwargs, vad=False, regroup=True)

    result = modify_result(result, max_words, max_chars, sensitive_words)

    return result.to_dict()


async def diarize_audio(audio_file, device, speaker_count):
    from pyannote.audio import Pipeline
    print("Starting diarization...")
    try:
        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
        pipeline.to(device)
        if speaker_count > 0:
            return pipeline(audio_file, num_speakers=speaker_count)
        else:
            return pipeline(audio_file)
    except Exception as e:
        error_message = f"failed to load diarization model. {e}"
        print(error_message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_message
        )

def merge_diarisation(transcript, diarization):
    # Array of colors to choose from
    colors = ['#0062ec', '#ed63d4', '#8b5eed', '#1a8bed', '#308800',
              '#886d4e', '#cb0000', '#6cb18c', '#d57312', '#000000']

    # Dictionary to store speaker information
    speakers_info = {}
    speaker_counter = 1

    # Match speakers to transcript segments
    new_segments = []
    transcript_segments = transcript["segments"]
    diarization_turns = list(diarization.itertracks(yield_label=True))
    diarization_segments = []

    i, j = 0, 0
    while i < len(transcript_segments) and j < len(diarization_turns):
        segment = transcript_segments[i]
        turn, _, speaker = diarization_turns[j]

        segment_start = segment["start"]
        segment_end = segment["end"]
        diar_start = turn.start
        diar_end = turn.end

        if diar_end <= segment_start:
            j += 1
        elif segment_end <= diar_start:
            i += 1
        elif segment_end - diar_end >= diar_end - segment_start and j < len(diarization_turns):
            j += 1
        else:
            # Overlapping segment
            speaker_label = f"Speaker {
                speaker_counter}" if speaker not in speakers_info else speakers_info[speaker]["label"]
            new_segment = {
                "start": segment_start,
                "end": segment_end,
                "speaker": speaker_label,
                "text": segment["text"],
                "words": segment["words"]
            }
            new_segments.append(new_segment)

            diarization_segments.append({
                "speaker": speaker_label,
                "start": diar_start,
                "end": diar_end
            })

            # Add speaker info if not already present
            if speaker not in speakers_info:
                # Select a random color and remove it from the list to avoid duplicates
                if colors:
                    color = random.choice(colors)
                    colors.remove(color)
                else:
                    # Generate a random color if we've run out
                    color = "#{:06x}".format(random.randint(0, 0xFFFFFF))
                # Store speaker information
                speakers_info[speaker] = {
                    "label": speaker_label,
                    "id": speaker_label,
                    "color": color,
                    "style": "Outline",
                    "sample": {
                        "start": segment_start,
                        "end": segment_end
                    },
                    "subtitle_lines": 0,
                    "word_count": 0
                }
                speaker_counter += 1
            # Update speaker's subtitle lines and word count
            speakers_info[speaker]["subtitle_lines"] += 1
            speakers_info[speaker]["word_count"] += len(segment["words"])
            i += 1  # Move to the next transcript segment

    # Assign 'Unknown' speaker to any remaining transcript segments
    for segment in transcript_segments[i:]:
        new_segment = {
            "start": segment["start"],
            "end": segment["end"],
            "speaker": "Unknown",
            "text": segment["text"],
            "words": segment["words"]
        }
        new_segments.append(new_segment)

        # Add 'Unknown' speaker info if not already present
        if "Unknown" not in speakers_info:
            if colors:
                color = random.choice(colors)
                colors.remove(color)
            else:
                color = "#{:06x}".format(random.randint(0, 0xFFFFFF))
            speakers_info["Unknown"] = {
                "label": "Unknown",
                "id": "Unknown",
                "color": color,
                "style": "outline",
                "sample": {
                    "start": segment["start"],
                    "end": segment["end"]
                },
                "subtitle_lines": 0,
                "word_count": 0
            }
        # Update 'Unknown' speaker's subtitle lines and word count
        speakers_info["Unknown"]["subtitle_lines"] += 1
        speakers_info["Unknown"]["word_count"] += len(segment["words"])

    # Convert speakers_info dict to a list
    speakers_list = list(speakers_info.values())
    top_speaker = max(
        speakers_list, key=lambda speaker: speaker["subtitle_lines"])

    # Add speakers list to the result
    result = {
        "text": transcript["text"],
        "language": transcript["language"],
        "speakers": speakers_list,
        "top_speaker": {
            "label": top_speaker["label"],
            "id": top_speaker["id"],
            "percentage": round((top_speaker["subtitle_lines"] / len(transcript_segments)) * 100)
        },
        "segments": new_segments,
        "diarization": diarization_segments
    }
    return result


async def process_audio(file_path, kwargs, max_words, max_chars, sensitive_words, device, diarize_enabled, speaker_count):
    """Process audio: transcription and diarization concurrently."""
    if diarize_enabled:
        # Run transcription and diarization concurrently
        transcript, diarization = await asyncio.gather(
            transcribe_audio(
                file_path, kwargs, max_words, max_chars, sensitive_words),
            diarize_audio(file_path, device, speaker_count)
        )
        # Merge diarization with transcription
        result = merge_diarisation(transcript, diarization)
    else:
        # Run transcription only
        transcript = await transcribe_audio(file_path, kwargs, max_words, max_chars, sensitive_words)
        transcript["speakers"] = []
        result = transcript

    return result

def modify_result(result, max_words, max_chars, sensitive_words):
    result.pad()
    # matching function to identify sensitive words
    def is_sensitive(word, sensitive_words):
        return word.word.lower().strip() in [w.lower() for w in sensitive_words]

    # replacement function to censor the sensitive words
    def censor_word(result, seg_index, word_index):
        word = result[seg_index][word_index]
        match = word.word.strip()
        # Replace each character with an asterisk
        word.word = word.word.replace(match, '*' * len(word.word.strip()))

    # Apply the custom_operation to censor sensitive words
    if len(sensitive_words) > 0:
        result.custom_operation(
            key='',                      # Empty string to use the word object directly
            operator=is_sensitive,       # Use the is_sensitive function as the operator
            value=sensitive_words,       # Pass the sensitive_words list as the value
            method=censor_word,          # Use the censor_word function to perform the replacement
            word_level=True              # Operate at the word level
        )

    (
        result
        .split_by_length(max_words=max_words, max_chars=max_chars)
        # .split_by_punctuation([('.', ' '), '。', '?', '？', ',', '，'])
        # .split_by_gap(0.4)
        # .merge_by_gap(0.1, max_words=3)
    )

    return result

class TranscriptionRequest(BaseModel):
    file_path: str
    output_dir: str
    timeline: str
    model: str
    language: str
    task: str
    diarize: bool
    diarize_speaker_count: int
    align_words: bool
    max_words: int
    max_chars: int
    sensitive_words: list
    mark_in: int
    mark_out: int

@app.post("/transcribe/")
async def transcribe(request: TranscriptionRequest):
    try:
        start_time = time.time()

        file_path = request.file_path
        timeline = request.timeline
        max_words = request.max_words
        max_chars = request.max_chars
        sensitive_words = request.sensitive_words

        # Check if the file exists
        if not os.path.exists(file_path):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found."
            )
        else:
            print(f"Processing file: {file_path}")

        # Select device
        if torch.cuda.is_available():
            device = torch.device("cuda")
        elif torch.backends.mps.is_available():
            device = torch.device("mps")
        else:
            device = torch.device("cpu")

        print(f"Using device: {device}")

        if request.language == "en":
            request.model = request.model + ".en"
            task = "transcribe"
        elif request.language == "de" and request.model == "large" and platform.system() != 'Windows':
            request.model = request.model + ".de"
            task = request.task
        else:
            task = request.task

        if platform.system() == 'Windows':
            model = win_models[request.model]
        else:
            model = mac_models[request.model]

        print(model)

        kwargs = {
            "model": model,
            "task": task,
            "language": request.language,
            "align_words": request.align_words,
            "device": "cuda" if torch.cuda.is_available() else "cpu"
        }

        # Process audio (transcription and optionally diarization)
        try:
            result = await process_audio(
                file_path, kwargs, max_words, max_chars, sensitive_words, device, request.diarize, request.diarize_speaker_count
            )
            result["mark_in"] = request.mark_in

        except Exception as e:
            print(f"Error during transcription: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error during transcription: {e}"
            )

        # Save the transcription to a JSON file
        json_filename = f"{timeline}.json"
        json_filepath = os.path.join(request.output_dir, json_filename)
        try:            
            if not os.path.exists(request.output_dir):
                os.makedirs(request.output_dir, exist_ok=True)

            # Save the transcription to a JSON file
            with open(json_filepath, 'w', encoding='utf-8') as f:
                json.dump(sanitize_result(result), f, indent=4, ensure_ascii=False)

            print(f"Transcription saved to: {json_filepath}")
        except Exception as e:
            print(f"Error saving JSON file: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error saving JSON file: {e}"
            )

        end_time = time.time()
        print(f"Transcription time: {end_time - start_time} seconds")

        # Return the path to the JSON file
        return {"result_file": json_filepath}

    except HTTPException as http_exc:
        # Re-raise HTTP exceptions to be handled by FastAPI
        raise http_exc
    except Exception as e:
        # Catch any other unexpected exceptions
        print(f"Unexpected error: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {e}"
        )
    
class SpeechSegmentsRequest(BaseModel):
    audio_file: str

@app.post("/non_speech_segments/")
async def get_speech_segments(request: SpeechSegmentsRequest):
    model = load_silero_vad()
    wav = read_audio(request.audio_file)
    speech_timestamps = get_speech_timestamps(
        wav,
        model,
        return_seconds=True,  # Return speech timestamps in seconds (default is samples)
    )

    # Calculate non-speech segments
    non_speech_timestamps = []
    prev_end = 0
    for segment in speech_timestamps:
        start, end = segment['start'], segment['end']
        if start > prev_end:
            non_speech_timestamps.append({'start': prev_end, 'end': start})
        prev_end = end
    
    return non_speech_timestamps

class ModifyRequest(BaseModel):
    file_path: str
    max_words: int
    max_chars: int
    sensitive_words: list

@app.post("/modify/")
async def modify(request: ModifyRequest):
    result = stable_whisper.WhisperResult(request.file_path)
    result.reset()
    result = modify_result(result, request.max_words, request.max_chars, request.sensitive_words)
    whisperResult = result.to_dict()
    
    return {"complete": True}


class ValidateRequest(BaseModel):
    token: str


@app.post("/validate/")
async def validate_model(request: ValidateRequest):
    token = request.token
    print(token)
    if token is None or token == "":
        # Check if token is cached
        token = HfFolder.get_token()
        if token is None:
            return {"isAvailable": False, "message": None}
    else:
        try:
            login(token)
        except Exception as e:
            return {"isAvailable": False, "message": "Hugging Face token is incorrect or expired."}

    required_models = ["pyannote/speaker-diarization-3.1",
                       "pyannote/segmentation-3.0"]
    if not is_model_accessible(required_models[0], token=token):
        return {"isAvailable": False, "message": f"Please accept the terms for model '{required_models[0]}' and provide a valid Hugging Face access token."}
    if not is_model_accessible(required_models[1], token=token):
        return {"isAvailable": False, "message": f"Please accept the terms for model '{required_models[1]}' and provide a valid Hugging Face access token."}

    return {"isAvailable": True, "message": "All required models are available"}

if __name__ == "__main__":
    uvicorn.run(app, host="localhost", port=55000, log_level="info")
