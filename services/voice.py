"""Audio files/Telegram voice -> ffmpeg-normalized WAV chunks -> complete transcript."""
import asyncio
import io
import logging
import subprocess
import tempfile
import wave
from pathlib import Path
from openai import AsyncOpenAI
from config import OPENAI_API_KEY
from services.ingest_models import InputProblem

def _chunks(data,filename):
    if not data or len(data)>20*1024*1024:
        raise InputProblem("Аудиофайл пустой или больше 20 МБ.")
    with tempfile.TemporaryDirectory(prefix="ada_audio_") as tmp:
        source=Path(tmp)/("audio"+Path(filename).suffix[:8])
        wav=Path(tmp)/"normalized.wav";source.write_bytes(data)
        try:
            # Decode at most 30 minutes + 1 second; detect overflow and refuse truncation.
            proc=subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(source),
                "-vn","-t","1801","-ar","16000","-ac","1","-c:a","pcm_s16le",str(wav)],
                capture_output=True,timeout=100)
        except (OSError,subprocess.TimeoutExpired) as error:
            raise InputProblem("Не удалось преобразовать аудио. Проверь ffmpeg в Railway.") from error
        if proc.returncode or not wav.exists():
            raise InputProblem("Аудиофайл повреждён или его кодек не поддерживается.")
        with wave.open(str(wav),"rb") as reader:
            if reader.getnframes()>16000*1800:
                raise InputProblem("Аудио длиннее 30 минут. Раздели его на части.")
            result=[]
            while True:
                frames=reader.readframes(16000*240)
                if not frames: break
                out=io.BytesIO()
                with wave.open(out,"wb") as writer:
                    writer.setnchannels(1);writer.setsampwidth(2);writer.setframerate(16000)
                    writer.writeframes(frames)
                result.append(out.getvalue())
            return result

async def transcribe_voice(file_bytes,filename="voice.oga"):
    client=None
    try:
        chunks=await asyncio.to_thread(_chunks,file_bytes,filename)
        client=AsyncOpenAI(api_key=OPENAI_API_KEY,timeout=90,max_retries=1)
        pieces=[]
        for idx,chunk in enumerate(chunks):
            result=await client.audio.transcriptions.create(model="whisper-1",
                file=(f"part_{idx}.wav",chunk),language="ru")
            text=str(result.text or "").strip()
            if not text: raise InputProblem("Не распознан фрагмент аудио; частичная команда не выполнена.")
            pieces.append(text)
        return " ".join(pieces)
    except Exception:
        logging.exception("HF_AUDIO_FAILED")
        return ""
    finally:
        if client:
            try: await client.close()
            except Exception: logging.warning("HF_AUDIO_CLOSE_FAILED")
