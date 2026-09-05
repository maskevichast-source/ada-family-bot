"""Content-based detection and bounded rendering. Does not trust filename/MIME."""
import io
import os
import subprocess
import tempfile
from pathlib import Path
from PIL import Image, ImageOps, UnidentifiedImageError
from services.ingest_models import InputProblem

MAX_FILE=20*1024*1024
MAX_PAGES=20
MAX_PIXELS=45_000_000
Image.MAX_IMAGE_PIXELS=MAX_PIXELS

def is_audio(data,filename="",mime=""):
    # HEIF and images named .mp3 must stay images.
    if data.startswith((b"\x89PNG",b"\xff\xd8",b"%PDF",b"GIF8",b"II*\0",b"MM\0*")):
        return False
    if data[8:12] in (b"heic",b"heix",b"hevc",b"mif1",b"avif"): return False
    if data.startswith((b"OggS",b"fLaC",b"ID3")) or (data[:4]==b"RIFF" and data[8:12]==b"WAVE"):
        return True
    return str(mime).startswith("audio/") or Path(filename).suffix.lower() in {".mp3",".m4a",".wav",".ogg",".oga",".opus",".aac",".flac",".wma",".amr"}

def image_bytes(image):
    if image.width*image.height>MAX_PIXELS:
        raise InputProblem("Изображение слишком большое. Уменьши разрешение, не обрезая чек.")
    image=ImageOps.exif_transpose(image)
    if image.mode in {"RGBA","LA"} or (image.mode=="P" and "transparency" in image.info):
        rgba=image.convert("RGBA"); base=Image.new("RGB",rgba.size,"white");base.paste(rgba,mask=rgba.getchannel("A"));image=base
    else: image=image.convert("RGB")
    image.thumbnail((2500,3500))
    out=io.BytesIO();image.save(out,"JPEG",quality=94)
    return out.getvalue()

def render_document(data, filename="receipt"):
    if not data: raise InputProblem("Файл пустой.")
    if len(data)>MAX_FILE: raise InputProblem("Файл больше 20 МБ. Раздели его или сожми без потери читаемости.")
    if data.lstrip()[:5]==b"%PDF-":
        import pypdfium2 as pdfium
        try:
            with pdfium.PdfDocument(data) as doc:
                if len(doc)==0: raise InputProblem("PDF пустой.")
                if len(doc)>MAX_PAGES: raise InputProblem(f"В PDF больше {MAX_PAGES} страниц. Раздели его: страницы не будут молча пропущены.")
                pages=[]
                for idx in range(len(doc)):
                    page=doc[idx]
                    try:
                        w,h=page.get_size()
                        if w<=0 or h<=0: raise InputProblem("Повреждённая страница PDF.")
                        scale=min(2.0,2500/w,3500/h)
                        bitmap=page.render(scale=scale)
                        try: pages.append(image_bytes(bitmap.to_pil()))
                        finally: bitmap.close()
                    finally: page.close()
                return pages
        except InputProblem: raise
        except Exception as error:
            raise InputProblem("PDF повреждён или защищён паролем. Пришли доступную копию либо скриншоты страниц.") from error
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width*image.height>MAX_PIXELS:
                raise InputProblem("Слишком большое разрешение. Уменьши изображение без обрезки чека.")
            count=getattr(image,"n_frames",1)
            if image.format in {"GIF","WEBP","PNG"} and count>1:
                raise InputProblem("Это анимация. Для чека пришли статичный кадр или PDF.")
            if count>MAX_PAGES: raise InputProblem(f"Больше {MAX_PAGES} страниц изображения — раздели файл.")
            pages=[]
            for idx in range(count):
                image.seek(idx);pages.append(image_bytes(image.copy()))
            return pages
    except (Image.DecompressionBombError,Image.DecompressionBombWarning) as error:
        raise InputProblem("Слишком большое разрешение изображения.") from error
    except InputProblem: raise
    except (UnidentifiedImageError,OSError):
        # Optional HEIC decoder; no additional mandatory dependency.
        if data[8:12] in (b"heic",b"heix",b"hevc",b"mif1",b"avif"):
            try:
                with tempfile.TemporaryDirectory(prefix="ada_heif_") as tmp:
                    source=Path(tmp)/"image.heic";target=Path(tmp)/"image.png"
                    source.write_bytes(data)
                    proc=subprocess.run(["ffmpeg","-nostdin","-v","error","-i",str(source),
                                         "-frames:v","1",str(target)],capture_output=True,timeout=35)
                    if proc.returncode==0 and target.exists():
                        return render_document(target.read_bytes(),"image.png")
            except (OSError,subprocess.TimeoutExpired): pass
            raise InputProblem("HEIC/AVIF не удалось декодировать в этом окружении. Отправь как фото Telegram, JPG или PNG.")
        raise InputProblem("Не удалось открыть изображение. Поддерживаются фото/JPG/PNG/WebP, BMP/TIFF и PDF; проверь сам файл.")
