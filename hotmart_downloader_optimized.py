#!/usr/bin/env python3
"""
hotmart_downloader_optimized.py
================================
Script otimizado para download de cursos Hotmart com segurança e resiliência.

Melhorias implementadas:
- Rate limiting inteligente para evitar detecção
- Retry com backoff exponencial
- Validação de integridade de arquivos
- Sistema de checkpoint para retomar downloads
- Conexões HTTP otimizadas
- Headers corretos para anexos
- Logging detalhado
- Simulação de comportamento humano

Requisitos:
    pip install requests m3u8 beautifulsoup4 cryptography
    ffmpeg no PATH
"""
from __future__ import annotations

import json
import getpass
import re
import subprocess
import sys
import time
import random
import string
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse
from datetime import datetime

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
    import m3u8
except ImportError as e:
    print(f"❌ ERRO: Biblioteca Python não encontrada: {e}")
    print("Instale as dependências com: pip install requests m3u8 beautifulsoup4 cryptography")
    sys.exit(1)

# ─── Configuração de Logging ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hotmart_downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── Configurações Globais ───────────────────────────────────────────────
URLS = {
    "courses": "https://api-hub.cb.hotmart.com/club-drive-api/rest/v2/purchase/?archived=UNARCHIVED",
    "modules": "https://api-club-course-consumption-gateway.hotmart.com/v1/navigation",
    "lessons": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/lessons/{lesson_hash}",
    "attachment": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/attachments/{attachment_id}",
    "complementary": "https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/pages/{page_hash}/complementary-content",
}

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://hotmart.com",
    "Referer": "https://hotmart.com",
}

# Configurações conservadoras para segurança
MAX_WORKERS = 6  # Reduzido de 8 para evitar detecção
TIMEOUT_SEC = 30  # Aumentado para conexões lentas
REQUEST_DELAY = (0.5, 1.5)  # Delay aleatório entre requests (segundos)
DL_ROOT = Path("downloads")
FFMPEG_CMD = "ffmpeg"

# ─── Utilidades Gerais ───────────────────────────────────────────────────
_illegal = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

def clean(name: str) -> str:
    """Remove caracteres ilegais e limita a 80 caracteres."""
    name = _illegal.sub("", name).strip()
    return name[:80] or "file"

def human_delay():
    """Simula delay humano entre requisições."""
    delay = random.uniform(*REQUEST_DELAY)
    time.sleep(delay)

def headers_for(url: str) -> dict[str, str]:
    """Cabeçalhos adequados por host."""
    h = BASE_HEADERS.copy()
    host = (urlparse(url).hostname or "").lower()
    if host.startswith(("vod-akm.play.hotmart.com", "contentplayer.hotmart.com")):
        h.pop("Cookie", None)
    h["Referer"] = "https://cf-embed.play.hotmart.com/"
    return h

# ─── Sistema de Checkpoint ───────────────────────────────────────────────
class CheckpointManager:
    """Gerencia checkpoints para retomar downloads."""
    
    def __init__(self, course_name: str):
        self.checkpoint_file = DL_ROOT / clean(course_name) / ".checkpoint.json"
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    
    def save(self, module_idx: int, lesson_idx: int):
        """Salva checkpoint atual."""
        try:
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "module": module_idx,
                    "lesson": lesson_idx,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Não foi possível salvar checkpoint: {e}")
    
    def load(self) -> Dict[str, int]:
        """Carrega último checkpoint."""
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"📍 Checkpoint encontrado: Módulo {data['module']}, Lição {data['lesson']}")
                    return {"module": data['module'], "lesson": data['lesson']}
            except Exception as e:
                logger.warning(f"Erro ao carregar checkpoint: {e}")
        return {"module": 0, "lesson": 0}
    
    def clear(self):
        """Remove checkpoint após conclusão."""
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()

# ─── Cliente HTTP com Retry ──────────────────────────────────────────────
def create_session(token: str) -> requests.Session:
    """Cria sessão HTTP otimizada com retry."""
    sess = requests.Session()
    
    # Retry strategy com backoff exponencial
    retry_strategy = Retry(
        total=3,
        backoff_factor=2,  # 1s, 2s, 4s
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS,
        pool_maxsize=MAX_WORKERS * 2,
        max_retries=retry_strategy
    )
    
    sess.mount('https://', adapter)
    sess.mount('http://', adapter)
    sess.headers.update(BASE_HEADERS | {"Authorization": f"Bearer {token}"})
    
    return sess

def fetch_with_retry(sess: requests.Session, url: str, headers: dict, max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
    """Requisição com retry manual e delay."""
    for attempt in range(max_retries):
        try:
            human_delay()  # Simula comportamento humano
            
            resp = sess.get(url, headers=headers, timeout=TIMEOUT_SEC, **kwargs)
            resp.raise_for_status()
            return resp
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⏳ Timeout, tentando novamente em {wait}s... (tentativa {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                logger.error(f"❌ Timeout após {max_retries} tentativas: {url}")
                return None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limit
                wait = 5 * (2 ** attempt)  # Espera mais tempo
                logger.warning(f"⚠️  Rate limit detectado, aguardando {wait}s...")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️  Erro HTTP {e.response.status_code}, tentando novamente em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro HTTP após {max_retries} tentativas: {e}")
                return None
                
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️  Erro de rede, tentando novamente em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro de rede após {max_retries} tentativas: {e}")
                return None
    
    return None

# ─── Validação de Arquivos ───────────────────────────────────────────────
def is_valid_video(path: Path, min_size_mb: float = 1.0) -> bool:
    """Verifica se vídeo existe e é válido."""
    if not path.exists():
        return False
    
    # Verifica tamanho mínimo
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb < min_size_mb:
        logger.warning(f"Arquivo muito pequeno ({size_mb:.1f}MB): {path.name}")
        return False
    
    # Verifica se FFmpeg consegue ler (opcional, mais lento)
    try:
        result = subprocess.run(
            [FFMPEG_CMD, "-v", "error", "-i", str(path), "-f", "null", "-"],
            capture_output=True,
            timeout=10
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning(f"Timeout ao validar vídeo: {path.name}")
        return True  # Assume válido se timeout
    except Exception as e:
        logger.warning(f"Erro ao validar vídeo: {e}")
        return True  # Assume válido se erro

def check_video_audio(video_path: Path) -> bool:
    """Verifica se vídeo tem áudio (não-crítico)."""
    try:
        cmd = [FFMPEG_CMD, "-i", str(video_path), "-f", "null", "-"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if "Audio:" in result.stderr:
            logger.info("🔊 Áudio verificado no vídeo")
            return True
        else:
            logger.warning("⚠️  Nenhum stream de áudio detectado")
            return False
            
    except subprocess.TimeoutExpired:
        logger.warning("⏳ Verificação de áudio demorou muito, pulando...")
        return True  # Assume OK
    except Exception as e:
        logger.warning(f"⚠️  Não foi possível verificar áudio: {e}")
        return True  # Assume OK

# ─── Funções de Download de Conteúdo Extra ──────────────────────────────
def save_attachment(sess: requests.Session, attachment: dict, out_dir: Path, course_id: str) -> bool:
    """Download de anexos - MÉTODO ALTERNATIVO."""
    try:
        file_name = clean(attachment.get('fileName', 'attachment'))
        file_size_bytes = attachment.get('fileSize', 0)
        
        print(f"📎 Processando: {file_name} ({file_size_bytes/1024:.1f} KB)")
        
        materials_dir = out_dir / "Materiais"
        materials_dir.mkdir(parents=True, exist_ok=True)
        
        file_ext = file_name.split('.')[-1] if '.' in file_name else 'file'
        attachment_path = materials_dir / file_name
        
        if len(str(attachment_path)) > 200:
            random_name = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            attachment_path = materials_dir / f"{random_name}.{file_ext}"
            
            mapping_file = out_dir.parent / "long_names.txt"
            with open(mapping_file, "a", encoding="utf-8") as f:
                f.write(f"{random_name}.{file_ext} = {file_name}\n")
        
        if attachment_path.exists():
            existing_size = attachment_path.stat().st_size
            if existing_size >= file_size_bytes * 0.95:
                print(f"   ⏭️  Já existe: {file_name}")
                return True
            else:
                print(f"   ⚠️  Arquivo incompleto, re-downloading")
                attachment_path.unlink()
        
        file_membership_id = attachment.get('fileMembershipId')
        if not file_membership_id:
            print(f"   ❌ Sem fileMembershipId")
            return False
        
        # MÉTODO 1: Tentar endpoint v2 (pode não funcionar)
        att_url_v2 = URLS["attachment"].format(attachment_id=file_membership_id)
        
        headers = headers_for(att_url_v2) | {
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "x-product-id": course_id,
        }
        
        resp = fetch_with_retry(sess, att_url_v2, headers, max_retries=1)
        
        download_url = None
        
        if resp:
            try:
                att_data = resp.json()
                download_url = (
                    att_data.get('downloadUrl') or 
                    att_data.get('directDownloadUrl') or
                    att_data.get('url') or
                    att_data.get('fileUrl')
                )
            except json.JSONDecodeError:
                pass
        
        # MÉTODO 2: Tentar endpoint v1 de attachments
        if not download_url:
            print(f"   🔄 Tentando endpoint alternativo...")
            att_url_v1 = f"https://api-club-course-consumption-gateway.hotmart.com/v1/attachments/{file_membership_id}"
            
            resp = fetch_with_retry(sess, att_url_v1, headers, max_retries=1)
            
            if resp:
                try:
                    att_data = resp.json()
                    download_url = (
                        att_data.get('downloadUrl') or 
                        att_data.get('directDownloadUrl') or
                        att_data.get('url') or
                        att_data.get('fileUrl')
                    )
                except json.JSONDecodeError:
                    pass
        
        # MÉTODO 3: Tentar endpoint de download direto
        if not download_url:
            print(f"   🔄 Tentando download direto...")
            # Alguns anexos podem ter URL direta no próprio objeto
            download_url = (
                attachment.get('url') or
                attachment.get('downloadUrl') or
                attachment.get('fileUrl')
            )
        
        # MÉTODO 4: Construir URL de download baseado no padrão Hotmart
        if not download_url:
            print(f"   🔄 Tentando URL construída...")
            # Hotmart usa CDN para arquivos
            download_url = f"https://api-club-file.cb.hotmart.com/v1/files/{file_membership_id}/download"
        
        if not download_url:
            print(f"   ❌ Não foi possível obter URL de download")
            return False
        
        print(f"   ⬇️  Baixando: {file_name}")
        print(f"   🔗 URL: {download_url[:80]}...")
        
        file_resp = fetch_with_retry(sess, download_url, headers_for(download_url), stream=True)
        if not file_resp:
            print(f"   ❌ Falha ao baixar arquivo")
            return False
        
        temp_path = attachment_path.with_suffix(attachment_path.suffix + '.part')
        try:
            with open(temp_path, 'wb') as f:
                downloaded = 0
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
            
            temp_path.rename(attachment_path)
            
            downloaded_size = attachment_path.stat().st_size
            print(f"   ✅ Baixado: {file_name} ({downloaded_size/1024:.1f} KB)")
            return True
            
        except Exception as e:
            print(f"   ❌ Erro ao salvar: {e}")
            if temp_path.exists():
                temp_path.unlink()
            return False
            
    except Exception as e:
        print(f"   ❌ Erro: {e}")
        logger.exception("Erro detalhado no anexo:")
        return False

def fetch_page_attachments(sess: requests.Session, page_hash: str, course_id: str) -> list:
    """Busca anexos de uma página usando o endpoint complementary-content."""
    try:
        comp_url = URLS["complementary"].format(page_hash=page_hash)
        headers = headers_for(comp_url) | {
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "x-product-id": course_id,
        }
        
        resp = fetch_with_retry(sess, comp_url, headers)
        if not resp:
            return []
        
        data = resp.json()
        return data.get('attachments', [])
        
    except Exception as e:
        logger.warning(f"⚠️  Erro ao buscar conteúdo complementar: {e}")
        return []

def save_description(content: str, out_dir: Path) -> bool:
    """Saves lesson description as HTML file."""
    try:
        if not content or not content.strip():
            return False
        
        desc_path = out_dir / "descricao.html"
        
        if desc_path.exists():
            logger.info("📝 Descrição já existe")
            return True
        
        with open(desc_path, "w", encoding="utf-8") as f:
            f.write(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Descrição da Aula</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 20px; line-height: 1.6; }}
    </style>
</head>
<body>
    <h1>Descrição da Aula</h1>
    <div>{content}</div>
</body>
</html>""")
        
        logger.info("✅ Descrição salva")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro ao salvar descrição: {e}")
        return False

def save_complementary_readings(readings: list, out_dir: Path) -> bool:
    """Saves complementary reading links as HTML file."""
    try:
        if not readings:
            return False
        
        links_path = out_dir / "links_complementares.html"
        
        if links_path.exists():
            logger.info("🔗 Links complementares já existem")
            return True
        
        html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Links Complementares</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .link { margin: 10px 0; padding: 10px; border: 1px solid #ddd; border-radius: 5px; }
        .link a { text-decoration: none; color: #0066cc; font-weight: bold; }
    </style>
</head>
<body>
    <h1>Links Complementares</h1>
"""
        
        for reading in readings:
            title = reading.get('articleName', 'Link sem título')
            url = reading.get('articleUrl', '#')
            html_content += f'    <div class="link"><a href="{url}" target="_blank">{title}</a></div>\n'
        
        html_content += "</body>\n</html>"
        
        with open(links_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        logger.info(f"✅ Links complementares salvos ({len(readings)} links)")
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro ao salvar links: {e}")
        return False

# ─── Embed & Playlists ───────────────────────────────────────────────────
def extract_streams(sess: requests.Session, embed: str) -> List[str]:
    """Retorna URLs .m3u8/.mpd não-DRM da página embed."""
    resp = fetch_with_retry(sess, embed, headers_for(embed))
    if not resp:
        return []
    
    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        return []
    
    data = json.loads(tag.string)
    assets = (data.get("props", {}).get("pageProps", {})
                    .get("applicationData", {}).get("mediaAssets", []))
    out: List[str] = []
    for a in assets:
        u = a.get("urlEncrypted") or a.get("url") or ""
        if "/drm/" in u:
            logger.warning("⚠️  Vídeo com DRM detectado, pulando...")
            continue
        if any(ext in u for ext in (".m3u8", ".mpd")):
            out.append(u)
    return out

def choose_av(master: str) -> Tuple[str, Optional[str]]:
    """Escolhe a melhor variante; retorna (video_url, audio_url ou None)."""
    logger.info(f"🔍 Analisando playlist master")
    
    pl = m3u8.load(master, headers=headers_for(master))
    
    logger.info(f"📊 Encontradas {len(pl.playlists)} variantes de vídeo")
    
    best = max(pl.playlists, key=lambda p: (p.stream_info.resolution or (0,0))[1])
    base = master.rsplit("/",1)[0]+"/"
    v = urljoin(base, best.uri)
    
    resolution = best.stream_info.resolution or (0, 0)
    logger.info(f"✅ Melhor variante: {resolution[0]}x{resolution[1]}")
    
    a = None
    if best.stream_info.audio:
        gid = best.stream_info.audio
        logger.info(f"🔍 Procurando áudio do grupo: {gid}")
        
        for m in pl.media:
            if m.type=="AUDIO" and m.group_id==gid and m.uri:
                a = urljoin(base, m.uri)
                logger.info(f"✅ Stream de áudio encontrado")
                break
        
        if not a:
            logger.warning("⚠️  Áudio não encontrado, assumindo stream combinado")
    
    return v, a

def subtitles_playlist(master: str) -> Optional[str]:
    """Retorna URL da playlist de legendas, se existir."""
    pl = m3u8.load(master, headers=headers_for(master))
    for m in pl.media:
        if m.type=="SUBTITLES" and m.uri:
            base = master.rsplit("/",1)[0]+"/"
            return urljoin(base, m.uri)
    return None

def download_subs(pl_url: str, vtt_out: Path, sess: requests.Session) -> None:
    """Baixa e combina segmentos de legenda."""
    pl = m3u8.load(pl_url, headers=headers_for(pl_url))
    with open(vtt_out, "wb") as f:
        for seg in pl.segments:
            resp = fetch_with_retry(sess, seg.absolute_uri, headers_for(seg.absolute_uri))
            if resp:
                f.write(resp.content)

def key_iv(pl_url: str, sess: requests.Session):
    """Extrai chave e IV de criptografia da playlist."""
    resp = fetch_with_retry(sess, pl_url, headers_for(pl_url))
    if not resp:
        return None, None
    
    pl = m3u8.loads(resp.text, uri=pl_url)
    if pl.keys and pl.keys[0] and pl.keys[0].uri:
        key_uri = urljoin(pl_url, pl.keys[0].uri)
        iv  = pl.keys[0].iv or bytes(16)
        
        key_resp = fetch_with_retry(sess, key_uri, headers_for(key_uri))
        if key_resp:
            return key_resp.content, iv
    
    return None, None

# ─── HLS Download ────────────────────────────────────────────────────────
def dl_track(pl_url: str, out_ts: Path, sess: requests.Session,
             key: Optional[bytes], iv: Optional[bytes]):
    """Download de track HLS com decriptografia AES-128."""
    import concurrent.futures
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pl = m3u8.load(pl_url, headers=headers_for(pl_url))
    segs = pl.segments
    out_ts.parent.mkdir(parents=True, exist_ok=True)

    def fetch(seg):
        """Baixa e decripta um segmento."""
        human_delay()  # Simula comportamento humano
        
        resp = fetch_with_retry(sess, seg.absolute_uri, headers_for(seg.absolute_uri))
        if not resp:
            logger.error(f"Falha ao baixar segmento: {seg.absolute_uri}")
            return b''
        
        d = resp.content
        
        if key and iv:
            try:
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                d = cipher.decryptor().update(d) + cipher.decryptor().finalize()
            except Exception as e:
                logger.error(f"Erro ao decriptar segmento: {e}")
        
        return d

    # Download com workers limitados
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex, \
         open(out_ts,"wb") as f:
        
        futures = [ex.submit(fetch, seg) for seg in segs]
        
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            data = future.result()
            if data:
                f.write(data)
            
            if i % 10 == 0:
                logger.info(f"   Progresso: {i}/{len(segs)} segmentos")

def ffmpeg_mux(v_ts: Path, a_ts: Optional[Path],
               vtt: Optional[Path], mp4: Path):
    """FFmpeg muxing otimizado."""
    logger.info(f"🔧 Iniciando muxing FFmpeg")
    
    cmd = [FFMPEG_CMD, "-y", "-i", str(v_ts)]
    
    if a_ts:
        cmd += ["-i", str(a_ts)]
    if vtt:
        cmd += ["-i", str(vtt)]
    
    # Codecs
    cmd += ["-c:v", "copy", "-map", "0:v:0"]
    
    # Audio mapping
    if a_ts:
        cmd += ["-c:a", "copy", "-map", "1:a:0"]
    else:
        cmd += ["-c:a", "copy", "-map", "0:a:0?"]
    
    # Subtitles
    if vtt:
        map_idx = 2 if a_ts else 1
        cmd += ["-c:s", "mov_text", "-map", f"{map_idx}:0"]
        cmd += ["-metadata:s:s:0", "language=por"]
    
    cmd.append(str(mp4))
    
    try:
        result = subprocess.run(
            cmd, 
            capture_output=True, 
            text=True, 
            timeout=300
        )
        
        if result.returncode != 0:
            logger.error(f"❌ Erro no FFmpeg: {result.stderr[:500]}")
            raise subprocess.CalledProcessError(result.returncode, cmd)
        
        logger.info("✅ Muxing concluído com sucesso")
        
    except subprocess.TimeoutExpired:
        logger.error("❌ Timeout no FFmpeg (5 minutos)")
        raise
    except Exception as e:
        logger.error(f"❌ Erro no FFmpeg: {e}")
        raise

# ─── Download de Vídeo ───────────────────────────────────────────────────
def download_video(sess: requests.Session, course: dict, lesson: dict, 
                   video: dict, module_name: str = "", 
                   lesson_out_dir: Path = None, video_order: int = 0) -> bool:
    """Downloads a single video and returns True if successful."""
    try:
        video_order_str = str(video_order).zfill(2)
        video_name = f"{video_order_str}. {video['name']}"
        
        logger.info(f"\n🎬 Processando vídeo: {video_name}")
        
        # Extract streams
        embed_urls = extract_streams(sess, video["url"])
        if not embed_urls:
            logger.error("❌ DRM ou nada encontrado")
            return False
        
        master = embed_urls[0]
        logger.info(f"📡 Master playlist obtida")
        
        try:
            video_url, audio_url = choose_av(master)
        except Exception as e:
            logger.error(f"❌ Erro ao ler variants: {e}")
            return False
        
        sub_pl = subtitles_playlist(master)
        if sub_pl:
            logger.info("📝 Legenda encontrada")
        
        # Output paths
        out_dir = lesson_out_dir or DL_ROOT / clean(course["name"])
        out_dir.mkdir(parents=True, exist_ok=True)
        
        base_nm = clean(Path(video["name"]).stem)
        numbered_base_nm = f"{video_order_str}. {base_nm}"
        mp4_out = out_dir / f"{numbered_base_nm}.mp4"
        v_tt_out = out_dir / f"{numbered_base_nm}.vtt" if sub_pl else None
        
        # Check if already downloaded
        if mp4_out.exists() and is_valid_video(mp4_out):
            logger.info(f"⏭️  Já existe e é válido: {mp4_out.name}")
            return True
        
        logger.info(f"⬇️  Baixando para: {mp4_out.name}")
        
        # Download tracks
        if audio_url:
            logger.info("🎵 Baixando áudio e vídeo separados")
            v_ts, a_ts = out_dir/"_v.ts", out_dir/"_a.ts"
            
            logger.info("📥 Baixando stream de vídeo...")
            dl_track(video_url, v_ts, sess, *key_iv(video_url, sess))
            
            logger.info("📥 Baixando stream de áudio...")
            dl_track(audio_url, a_ts, sess, *key_iv(audio_url, sess))
            
            if sub_pl:
                logger.info("📥 Baixando legendas...")
                download_subs(sub_pl, v_tt_out, sess)
            
            logger.info("🔧 Fazendo muxing com FFmpeg...")
            ffmpeg_mux(v_ts, a_ts, v_tt_out, mp4_out)
            
            # Cleanup
            v_ts.unlink(missing_ok=True)
            a_ts.unlink(missing_ok=True)
            if v_tt_out and v_tt_out.exists():
                v_tt_out.unlink(missing_ok=True)
            
        else:
            logger.info("🎵 Baixando stream combinado (áudio+vídeo)")
            av_ts = out_dir / "_av.ts"
            
            logger.info("📥 Baixando stream combinado...")
            dl_track(video_url, av_ts, sess, *key_iv(video_url, sess))
            
            if sub_pl:
                logger.info("📥 Baixando legendas...")
                download_subs(sub_pl, v_tt_out, sess)
                logger.info("🔧 Fazendo muxing com legendas...")
                ffmpeg_mux(av_ts, None, v_tt_out, mp4_out)
                if v_tt_out and v_tt_out.exists():
                    v_tt_out.unlink(missing_ok=True)
            else:
                logger.info("🔧 Convertendo para MP4...")
                subprocess.run(
                    [FFMPEG_CMD, "-y", "-i", str(av_ts), "-c", "copy", str(mp4_out)],
                    check=True, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL,
                    timeout=300
                )
            
            # Cleanup
            av_ts.unlink(missing_ok=True)
        
        logger.info(f"✅ Concluído: {mp4_out.name}")
        
        # Verificação não-crítica de áudio
        check_video_audio(mp4_out)
        
        return True
        
    except Exception as e:
        logger.error(f"❌ Erro ao baixar vídeo '{video['name']}': {e}")
        return False

# ─── Download de Lição ───────────────────────────────────────────────────
def download_lesson(sess: requests.Session, course: dict, lesson: dict, 
                   course_id: str, module_name: str = "", 
                   lesson_order: int = 0) -> int:
    """Downloads all videos from a lesson and returns count of successful downloads."""
    
    lesson_order_str = str(lesson_order).zfill(2)
    lesson_name = f"{lesson_order_str}. {clean(lesson['name'])}"
    
    logger.info(f"\n📚 Processando lição: {lesson_name}")
    
    # Get lesson details
    lesson_url = URLS["lessons"].format(lesson_hash=lesson["hash"])
    h_ls = headers_for(lesson_url) | {
        "x-app-name": "app-club-consumer_v1.210.1_production",
        "x-product-id": course_id,
    }
    
    resp_lesson = fetch_with_retry(sess, lesson_url, h_ls)
    if not resp_lesson:
        logger.error(f"❌ Erro ao buscar detalhes da lição")
        return 0
    
    try:
        lesson_full = resp_lesson.json()
    except json.JSONDecodeError:
        logger.error(f"❌ Resposta inválida da API")
        return 0
    
    # Setup output directory
    if module_name:
        out_dir = DL_ROOT / clean(course["name"]) / clean(module_name) / lesson_name
    else:
        out_dir = DL_ROOT / clean(course["name"]) / lesson_name
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Process videos
    videos = [m for m in lesson_full.get("medias", []) if m.get("type") == "VIDEO"]
    success_count = 0
    
    if videos:
        logger.info(f"🎬 Encontrados {len(videos)} vídeos")
        for i, video in enumerate(videos, 1):
            logger.info(f"\n[{i}/{len(videos)}]")
            if download_video(sess, course, lesson, video, module_name, 
                            lesson_out_dir=out_dir, video_order=i):
                success_count += 1
    else:
        logger.info("⚠️  Lição sem vídeos")
    
    # ─── Process Extra Content ───────────────────────────────────────────
    
    # Método 1: Anexos da resposta da lição
    attachments = lesson_full.get("attachments", [])
    
    # Método 2: Buscar do endpoint complementary-content
    if not attachments:
        page_hash = lesson.get("hash")
        if page_hash:
            logger.info("🔍 Buscando conteúdo complementar...")
            attachments = fetch_page_attachments(sess, page_hash, course_id)
    
    # Process attachments
    if attachments:
        logger.info(f"\n📎 Processando {len(attachments)} anexos...")
        attachment_count = 0
        for attachment in attachments:
            if save_attachment(sess, attachment, out_dir, course_id):
                attachment_count += 1
        logger.info(f"📎 Anexos processados: {attachment_count}/{len(attachments)}")
    
    # Process description
    description = lesson_full.get("description") or lesson_full.get("content")
    if description and description.strip():
        logger.info("📝 Processando descrição...")
        save_description(description, out_dir)
    
    # Process complementary readings
    comp_readings = lesson_full.get("complementaryReadings", [])
    if comp_readings:
        logger.info(f"🔗 Processando {len(comp_readings)} links complementares...")
        save_complementary_readings(comp_readings, out_dir)
    
    return success_count

# ─── Download de Módulo ──────────────────────────────────────────────────
def download_module(sess: requests.Session, course: dict, module: dict, 
                   course_id: str, module_order: int = 0, 
                   checkpoint: CheckpointManager = None) -> int:
    """Downloads all lessons from a module and returns count of successful downloads."""
    
    module_order_str = str(module_order).zfill(2)
    module_name = f"{module_order_str}. {clean(module['name'])}"
    
    logger.info(f"\n📖 Processando módulo: {module_name}")
    
    pages = module.get("pages", [])
    if not pages:
        logger.warning("⚠️  Módulo sem lições")
        return 0
    
    logger.info(f"📚 Encontradas {len(pages)} lições")
    
    success_count = 0
    for i, lesson in enumerate(pages, 1):
        logger.info(f"\n--- Lição {i}/{len(pages)} ---")
        
        count = download_lesson(sess, course, lesson, course_id, module_name, lesson_order=i)
        success_count += count
        
        # Save checkpoint
        if checkpoint:
            checkpoint.save(module_order, i)
    
    return success_count

# ─── Validação de FFmpeg ─────────────────────────────────────────────────
def validate_ffmpeg() -> bool:
    """Valida se FFmpeg está disponível."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], 
            capture_output=True, 
            text=True, 
            timeout=5
        )
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            logger.info(f"✅ FFmpeg encontrado: {version_line}")
            return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pass
    
    logger.error("❌ FFmpeg não encontrado no PATH")
    logger.error("Por favor, instale o FFmpeg:")
    logger.error("  - Windows: winget install FFmpeg")
    logger.error("  - Linux: sudo apt install ffmpeg")
    logger.error("  - Mac: brew install ffmpeg")
    return False

# ─── Token Helpers ───────────────────────────────────────────────────────
def show_token_help():
    """Mostra instruções para obter o token Bearer."""
    print("\n" + "="*70)
    print("🔑 COMO OBTER O TOKEN BEARER DO HOTMART")
    print("="*70)
    print("1. Abra o navegador e vá para https://hotmart.com")
    print("2. Faça login na sua conta")
    print("3. Acesse qualquer curso que você possui")
    print("4. Abra as Ferramentas do Desenvolvedor (F12)")
    print("5. Vá para a aba 'Network' ou 'Rede'")
    print("6. Recarregue a página ou navegue para uma aula")
    print("7. Procure por requisições para 'api-hub.cb.hotmart.com'")
    print("8. Clique na requisição e vá para 'Headers'")
    print("9. Copie o valor do header 'Authorization' (após 'Bearer ')")
    print("10. Cole o token completo aqui (deve começar com 'eyJ')")
    print("\n⚠️  IMPORTANTE:")
    print("   - O token deve ser copiado por INTEIRO")
    print("   - Deve começar com 'eyJ' e ser uma string longa")
    print("   - Tokens expiram, use um token recente")
    print("="*70 + "\n")

def validate_token(token: str) -> bool:
    """Valida formato básico do token JWT."""
    if not token or len(token) < 100:
        logger.error("❌ Token muito curto")
        return False
    
    if not token.startswith('eyJ'):
        logger.warning("⚠️  Token não parece ser um JWT válido (deveria começar com 'eyJ')")
        response = input("Continuar mesmo assim? [s/N] ").strip().lower()
        return response == 's'
    
    try:
        token.encode('latin-1')
    except UnicodeEncodeError:
        logger.error("❌ Token contém caracteres inválidos")
        return False
    
    logger.info(f"✅ Token validado (comprimento: {len(token)} caracteres)")
    return True

# ─── Interface de Usuário ────────────────────────────────────────────────
def ask_int(prompt: str, lo: int, hi: int) -> int:
    """Solicita input inteiro do usuário."""
    while True:
        try:
            n = int(input(prompt).strip())
            if lo <= n <= hi:
                return n
            print(f"❌ Valor deve estar entre {lo} e {hi}")
        except ValueError:
            print("❌ Valor inválido, digite um número")
        except KeyboardInterrupt:
            print("\n\n👋 Download cancelado pelo usuário")
            sys.exit(0)

# ─── Programa Principal ──────────────────────────────────────────────────
def main():
    """Função principal do programa."""
    print("🎓 HOTMART DOWNLOADER - VERSÃO OTIMIZADA")
    print("="*70)
    print("✨ MELHORIAS:")
    print("   🔒 Rate limiting para evitar bloqueio")
    print("   🔄 Retry automático com backoff exponencial")
    print("   ✅ Validação de integridade de arquivos")
    print("   💾 Sistema de checkpoint para retomar downloads")
    print("   📎 Download correto de anexos (PDFs, DOCs, etc)")
    print("   🎯 Otimização de conexões HTTP")
    print("   📊 Logging detalhado em arquivo")
    print("="*70 + "\n")
    
    # Validate FFmpeg
    if not validate_ffmpeg():
        sys.exit(1)
    
    # Get token
    print("Cole seu Bearer token Hotmart:")
    print("(Digite 'help' para ver instruções)")
    TOKEN = input("Token: ").strip()
    
    if TOKEN.lower() == 'help':
        show_token_help()
        TOKEN = input("Token: ").strip()
    
    if not validate_token(TOKEN):
        sys.exit(1)
    
    # Create session
    logger.info("🔌 Criando sessão HTTP otimizada...")
    sess = create_session(TOKEN)
    
    # Fetch courses
    logger.info("📡 Buscando cursos...")
    try:
        resp = fetch_with_retry(sess, URLS["courses"], headers_for(URLS["courses"]))
        if not resp:
            logger.error("❌ Falha ao buscar cursos")
            sys.exit(1)
        
        logger.info("✅ Dados dos cursos obtidos com sucesso!")
    except Exception as e:
        logger.error(f"❌ ERRO: {e}")
        sys.exit(1)
    
    raw = resp.json().get("data", [])
    courses = []
    
    for p in raw:
        if p.get("purchase", {}).get("purchaseCancelled", False):
            continue
        
        product = p["product"]
        slug = ""
        if "hotmartClub" in product and product["hotmartClub"]:
            slug = product["hotmartClub"].get("slug", "")
        
        courses.append({
            "id": product["id"],
            "name": product["name"],
            "slug": slug
        })
    
    if not courses:
        logger.error("❌ Nenhum curso encontrado")
        sys.exit(1)
    
    available = sum(1 for c in courses if c['slug'])
    logger.info(f"\n📊 Encontrados {len(courses)} cursos ({available} disponíveis)")
    
    print("\n=== Cursos ===")
    for i, c in enumerate(courses, 1):
        status = "✅" if c['slug'] else "❌ (indisponível)"
        print(f"{i}. {c['name']} {status}")
    
    course = courses[ask_int("Selecione o curso: ", 1, len(courses)) - 1]
    
    if not course['slug']:
        logger.error("❌ Curso selecionado não possui slug necessário")
        sys.exit(1)
    
    logger.info(f"✅ Curso selecionado: {course['name']}")
    
    # Initialize checkpoint manager
    checkpoint_mgr = CheckpointManager(course['name'])
    checkpoint = checkpoint_mgr.load()
    
    # Fetch modules
    logger.info("📡 Buscando módulos do curso...")
    h_mod = headers_for(URLS["modules"]) | {
        "x-app-name": "app-club-consumer_v1.210.1_production",
        "slug": course["slug"],
        "x-product-id": str(course["id"]),
    }
    
    resp_mod = fetch_with_retry(sess, URLS["modules"], h_mod)
    if not resp_mod:
        logger.error("❌ Falha ao buscar módulos")
        sys.exit(1)
    
    mods = resp_mod.json().get("modules", [])
    
    if not mods:
        logger.error("❌ Nenhum módulo encontrado")
        sys.exit(1)
    
    logger.info(f"✅ Encontrados {len(mods)} módulos")
    
    print("\n=== Módulos ===")
    print("0. 📥 BAIXAR TODOS OS MÓDULOS")
    for i, m in enumerate(mods, 1):
        print(f"{i}. {m['name']}")
    
    module_choice = ask_int("Selecione o módulo (0 para todos): ", 0, len(mods))
    
    if module_choice == 0:
        # Download all modules
        print("\n🚀 Iniciando download de TODOS os módulos...")
        if input("Confirma download de todos os módulos? [s/N] ").strip().lower() != 's':
            sys.exit(0)
        
        start_time = time.time()
        total_downloaded = 0
        
        logger.info("💡 Pressione Ctrl+C a qualquer momento para parar")
        print("="*70)
        
        try:
            # Resume from checkpoint if available
            start_module = checkpoint.get("module", 0)
            if start_module > 0:
                logger.info(f"📍 Retomando do módulo {start_module + 1}")
            
            for i in range(start_module, len(mods)):
                mod = mods[i]
                
                print(f"\n{'='*70}")
                print(f"🔄 MÓDULO {i+1}/{len(mods)}: {mod['name']}")
                print(f"{'='*70}")
                
                mod_start = time.time()
                downloaded = download_module(
                    sess, course, mod, str(course["id"]), 
                    module_order=i+1, checkpoint=checkpoint_mgr
                )
                mod_time = time.time() - mod_start
                total_downloaded += downloaded
                
                logger.info(f"📊 Módulo concluído: {downloaded} vídeos em {mod_time/60:.1f}min")
                
                # Estimate remaining time
                if i < len(mods) - 1:
                    avg_time = (time.time() - start_time) / (i + 1 - start_module)
                    remaining = (len(mods) - i - 1) * avg_time
                    logger.info(f"⏱️  Tempo estimado restante: {remaining/60:.1f} minutos")
        
        except KeyboardInterrupt:
            logger.warning(f"\n\n⏸️  DOWNLOAD INTERROMPIDO PELO USUÁRIO")
            logger.info(f"📊 Vídeos baixados até agora: {total_downloaded}")
            logger.info(f"📁 Pasta de destino: {DL_ROOT / clean(course['name'])}")
            logger.info("💡 Execute o script novamente para retomar de onde parou")
            sys.exit(0)
        
        # Clear checkpoint on completion
        checkpoint_mgr.clear()
        
        total_time = time.time() - start_time
        print(f"\n🎉 DOWNLOAD COMPLETO!")
        print(f"📊 Total de vídeos baixados: {total_downloaded}")
        print(f"⏱️  Tempo total: {total_time/60:.1f} minutos")
        print(f"📁 Pasta de destino: {DL_ROOT / clean(course['name'])}")
        
    else:
        # Download specific module
        mod = mods[module_choice - 1]
        
        pages = mod.get("pages", [])
        print(f"\n=== Lições do módulo: {mod['name']} ===")
        print("0. 📥 BAIXAR TODAS AS LIÇÕES")
        for i, l in enumerate(pages, 1):
            print(f"{i}. {l['name']}")
        
        lesson_choice = ask_int("Selecione a lição (0 para todas): ", 0, len(pages))
        
        if lesson_choice == 0:
            # Download all lessons from module
            logger.info(f"\n🚀 Baixando todas as lições do módulo: {mod['name']}")
            downloaded = download_module(
                sess, course, mod, str(course["id"]), 
                module_order=module_choice
            )
            logger.info(f"📊 Total de vídeos baixados: {downloaded}")
        else:
            # Download specific lesson
            lesson = pages[lesson_choice - 1]
            module_order_str = str(module_choice).zfill(2)
            numbered_module_name = f"{module_order_str}. {clean(mod['name'])}"
            
            downloaded = download_lesson(
                sess, course, lesson, str(course["id"]), 
                numbered_module_name, lesson_order=lesson_choice
            )
            logger.info(f"📊 Total de vídeos baixados: {downloaded}")
    
    print("\n🎉 Download concluído!")
    logger.info("✅ Script finalizado com sucesso")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Download cancelado pelo usuário")
        logger.info("Script interrompido pelo usuário")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"❌ Erro fatal: {e}")
        sys.exit(1)
