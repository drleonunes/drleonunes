# course_downloader/core/downloader.py
from __future__ import annotations

import concurrent.futures
import logging
import random
import string
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import m3u8
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from course_downloader.core.models import Attachment
from course_downloader.core.utils import (
    FFMPEG_CMD,
    MAX_WORKERS,
    clean,
    fetch_with_retry,
    human_delay,
)

logger = logging.getLogger(__name__)

# ─── Funções de Download de Conteúdo Extra ──────────────────────────────
def save_description(content: str, out_dir: Path) -> bool:
    """Salva a descrição da lição em um arquivo HTML."""
    if not content or not content.strip():
        return False
    desc_path = out_dir / "descricao.html"
    if desc_path.exists():
        return True
    try:
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Descrição</title></head>
<body>{content}</body></html>""")
        logger.info(f"✅ Descrição salva em {desc_path.name}")
        return True
    except IOError as e:
        logger.error(f"❌ Erro ao salvar descrição: {e}")
        return False

def save_complementary_readings(readings: list, out_dir: Path) -> bool:
    """Salva links de leitura complementar em um arquivo HTML."""
    if not readings:
        return False
    links_path = out_dir / "links_complementares.html"
    if links_path.exists():
        return True
    try:
        html = "<html><body><h1>Links Complementares</h1>"
        for r in readings:
            html += f'<p><a href="{r.get("articleUrl")}" target="_blank">{r.get("articleName")}</a></p>'
        html += "</body></html>"
        with open(links_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"✅ {len(readings)} links salvos em {links_path.name}")
        return True
    except IOError as e:
        logger.error(f"❌ Erro ao salvar links: {e}")
        return False

def save_attachment(
    sess: requests.Session,
    attachment: Attachment,
    out_dir: Path,
    download_url: str,
    headers: dict
) -> bool:
    """Baixa um único anexo."""
    try:
        materials_dir = out_dir / "Materiais"
        materials_dir.mkdir(parents=True, exist_ok=True)

        file_path = materials_dir / clean(attachment.file_name)

        if file_path.exists() and file_path.stat().st_size >= attachment.file_size * 0.95:
            logger.info(f"⏭️ Anexo já existe: {file_path.name}")
            return True

        logger.info(f"⬇️ Baixando anexo: {file_path.name}")

        resp = fetch_with_retry(sess, download_url, headers, stream=True)
        if not resp:
            logger.error(f"❌ Falha ao iniciar download do anexo: {attachment.file_name}")
            return False

        temp_path = file_path.with_suffix(file_path.suffix + '.part')
        with open(temp_path, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        temp_path.rename(file_path)
        logger.info(f"✅ Anexo baixado: {file_path.name}")
        return True

    except Exception as e:
        logger.error(f"❌ Erro catastrófico ao baixar anexo '{attachment.file_name}': {e}")
        return False

# ─── HLS Download & Processing ───────────────────────────────────────────
def _key_iv(pl_url: str, sess: requests.Session, headers: dict) -> Tuple[Optional[bytes], Optional[bytes]]:
    """Extrai chave de decriptografia e IV de uma playlist HLS."""
    resp = fetch_with_retry(sess, pl_url, headers)
    if not resp:
        return None, None

    pl = m3u8.loads(resp.text, uri=pl_url)
    if pl.keys and pl.keys[0] and pl.keys[0].uri:
        key_uri = urljoin(pl_url, pl.keys[0].uri)
        iv = pl.keys[0].iv or b'\x00' * 16

        key_resp = fetch_with_retry(sess, key_uri, headers)
        if key_resp:
            return key_resp.content, iv
    return None, None

def _dl_track(pl_url: str, out_ts: Path, sess: requests.Session, headers: dict) -> bool:
    """Baixa e decripta uma track HLS (vídeo ou áudio)."""
    key, iv = _key_iv(pl_url, sess, headers)
    pl = m3u8.load(pl_url, headers=headers)

    def fetch_segment(seg):
        resp = fetch_with_retry(sess, seg.absolute_uri, headers)
        if not resp:
            return b''
        data = resp.content
        if key and iv:
            try:
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                decryptor = cipher.decryptor()
                return decryptor.update(data) + decryptor.finalize()
            except Exception:
                # Se a decriptografia falhar, pode ser um segmento não criptografado (ex: intro)
                return data
        return data

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor, \
         open(out_ts, "wb") as f:

        futures = [executor.submit(fetch_segment, seg) for seg in pl.segments]
        total = len(pl.segments)

        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            chunk = future.result()
            if chunk:
                f.write(chunk)
            if i % 20 == 0:
                logger.info(f"   📥 Progresso: {i}/{total} segmentos baixados...")

    return True

def _ffmpeg_mux(v_ts: Path, a_ts: Optional[Path], vtt: Optional[Path], mp4: Path) -> bool:
    """Muxa os arquivos de áudio, vídeo e legenda em um MP4 final."""
    logger.info(f"🔧 Muxando para: {mp4.name}")
    cmd = [FFMPEG_CMD, "-y", "-i", str(v_ts)]
    if a_ts:
        cmd += ["-i", str(a_ts)]
    if vtt:
        cmd += ["-i", str(vtt)]

    cmd += ["-c:v", "copy", "-map", "0:v:0"]
    if a_ts:
        cmd += ["-c:a", "copy", "-map", "1:a:0"]
    else: # Áudio pode estar no stream de vídeo
        cmd += ["-c:a", "copy", "-map", "0:a:0?"]

    if vtt:
        map_idx = 2 if a_ts else 1
        cmd += ["-c:s", "mov_text", "-map", f"{map_idx}:0", "-metadata:s:s:0", "language=por"]

    cmd.append(str(mp4))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=True
        )
        logger.info(f"✅ Muxing concluído: {mp4.name}")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Erro no FFmpeg ao muxar: {e.stderr[:500]}")
        return False
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout no FFmpeg durante o muxing.")
        return False

# ─── Função Principal de Download de Vídeo ────────────────────────────────
def is_valid_video(path: Path, min_size_mb: float = 0.5) -> bool:
    """Verifica se um arquivo de vídeo é válido."""
    if not path.exists() or path.stat().st_size / (1024*1024) < min_size_mb:
        return False
    try:
        result = subprocess.run(
            [FFMPEG_CMD, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True, timeout=15
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True # Assume válido se o FFmpeg travar ou não for encontrado

def download_video_hls(
    sess: requests.Session,
    video_url: str,
    audio_url: Optional[str],
    sub_url: Optional[str],
    out_path: Path,
    headers: dict
) -> bool:
    """
    Orquestra o download de um vídeo HLS, incluindo áudio, legendas e muxing.
    """
    if is_valid_video(out_path):
        logger.info(f"⏭️ Vídeo já existe e é válido: {out_path.name}")
        return True

    out_dir = out_path.parent
    base_name = out_path.stem
    v_ts = out_dir / f"_{base_name}_v.ts"
    a_ts = out_dir / f"_{base_name}_a.ts" if audio_url else None
    vtt = out_dir / f"_{base_name}.vtt" if sub_url else None

    try:
        logger.info(f"📥 Baixando vídeo para {out_path.name}...")
        _dl_track(video_url, v_ts, sess, headers)

        if a_ts and audio_url:
            logger.info("📥 Baixando áudio...")
            _dl_track(audio_url, a_ts, sess, headers)

        if vtt and sub_url:
            logger.info("📥 Baixando legendas...")
            resp = fetch_with_retry(sess, sub_url, headers)
            if resp:
                vtt.write_text(resp.text, encoding='utf-8')

        if not _ffmpeg_mux(v_ts, a_ts, vtt, out_path):
            return False

        return True

    finally:
        # Limpeza dos arquivos temporários
        for f in [v_ts, a_ts, vtt]:
            if f and f.exists():
                f.unlink()
