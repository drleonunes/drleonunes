#!/usr/bin/env python3
"""
hotmart_downloader_hybrid.py
============================
Versão híbrida: lógica de download do script original + melhorias essenciais

Mantém:
- Download sequencial de segmentos (ordem perfeita)
- Lógica original de muxing
- Rate limiting natural do ThreadPoolExecutor original

Adiciona:
- Correção de anexos (endpoint e headers corretos)
- Retry em falhas de rede
- Validação de arquivos
- Sistema de checkpoint
- Logging melhorado

Requisitos:
    pip install requests m3u8 beautifulsoup4 cryptography
    ffmpeg no PATH
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import random
import string
import logging
from pathlib import Path
from typing import List, Optional, Tuple, Dict
from urllib.parse import urljoin, urlparse
from datetime import datetime

try:
    import requests
    from bs4 import BeautifulSoup
    import m3u8
except ImportError as e:
    print(f"❌ ERRO: Biblioteca não encontrada: {e}")
    print("Instale com: pip install requests m3u8 beautifulsoup4 cryptography")
    sys.exit(1)

# ─── Configuração ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('hotmart_downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

URLS = {
    "courses": "https://api-hub.cb.hotmart.com/club-drive-api/rest/v2/purchase/?archived=UNARCHIVED",
    "modules": "https://api-club-course-consumption-gateway.hotmart.com/v1/navigation",
    "lessons": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/lessons/{lesson_hash}",
    "attachment": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/attachments/{attachment_id}",
    "complementary": "https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/pages/{page_hash}/complementary-content",
    "attachment_download": "https://api-club-hot-club-api.cb.hotmart.com/rest/v3/attachment/{attachment_id}/download",  # ADICIONE ESTA LINHA
}

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://hotmart.com",
    "Referer": "https://hotmart.com",
}

THREADS = 8  # Original
TIMEOUT_SEC = 30  # Aumentado
DL_ROOT = Path("downloads")
FFMPEG_CMD = "ffmpeg"
FORCE_REDOWNLOAD = False  # Será modificada no main()

# ─── Utilidades ──────────────────────────────────────────────────────────
_illegal = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

def clean(name: str) -> str:
    """Remove caracteres ilegais e limita a 80 caracteres."""
    name = _illegal.sub("", name).strip()
    return name[:80] or "file"

def headers_for(url: str) -> dict[str, str]:
    """Cabeçalhos adequados por host."""
    h = BASE_HEADERS.copy()
    host = (urlparse(url).hostname or "").lower()
    if host.startswith(("vod-akm.play.hotmart.com", "contentplayer.hotmart.com")):
        h.pop("Cookie", None)
    h["Referer"] = "https://cf-embed.play.hotmart.com/"
    return h

def ask_int(prompt: str, lo: int, hi: int) -> int:
    """Solicita input inteiro."""
    while True:
        try:
            n = int(input(prompt).strip())
            if lo <= n <= hi:
                return n
            print(f"❌ Valor deve estar entre {lo} e {hi}")
        except ValueError:
            print("❌ Valor inválido")
        except KeyboardInterrupt:
            print("\n\n👋 Cancelado pelo usuário")
            sys.exit(0)

# ─── Checkpoint ──────────────────────────────────────────────────────────
class CheckpointManager:
    """Gerencia checkpoints para retomar downloads."""
    
    def __init__(self, course_name: str):
        self.checkpoint_file = DL_ROOT / clean(course_name) / ".checkpoint.json"
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
    
    def save(self, module_idx: int, lesson_idx: int):
        try:
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "module": module_idx,
                    "lesson": lesson_idx,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.warning(f"Erro ao salvar checkpoint: {e}")
    
    def load(self) -> Dict[str, int]:
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"📍 Retomando: Módulo {data['module']}, Lição {data['lesson']}")
                    return {"module": data['module'], "lesson": data['lesson']}
            except:
                pass
        return {"module": 0, "lesson": 0}
    
    def clear(self):
        if self.checkpoint_file.exists():
            self.checkpoint_file.unlink()

# ─── Retry com Backoff ───────────────────────────────────────────────────
def fetch_with_retry(sess: requests.Session, url: str, headers: dict, 
                    max_retries: int = 3, **kwargs) -> Optional[requests.Response]:
    """Requisição com retry e backoff exponencial."""
    for attempt in range(max_retries):
        try:
            resp = sess.get(url, headers=headers, timeout=TIMEOUT_SEC, **kwargs)
            resp.raise_for_status()
            return resp
            
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⏳ Timeout, retry em {wait}s... ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                logger.error(f"❌ Timeout após {max_retries} tentativas")
                return None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = 10 * (2 ** attempt)
                logger.warning(f"⚠️  Rate limit, aguardando {wait}s...")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️  HTTP {e.response.status_code}, retry em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro HTTP após retries: {e}")
                return None
                
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️  Erro de rede, retry em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro de rede após retries: {e}")
                return None
    
    return None

# ─── Validação ───────────────────────────────────────────────────────────
def is_valid_video(path: Path, min_size_mb: float = 0.5) -> bool:
    """Verifica se vídeo existe e tem tamanho mínimo válido."""
    if not path.exists():
        return False
    
    size_mb = path.stat().st_size / (1024 * 1024)
    
    # Vídeos muito pequenos são suspeitos
    if size_mb < min_size_mb:
        logger.warning(f"Arquivo muito pequeno ({size_mb:.1f}MB): {path.name}")
        return False
    
    # Verificação adicional: arquivo não pode estar vazio ou corrompido
    if size_mb < 0.1:  # Menor que 100KB é definitivamente inválido
        logger.warning(f"Arquivo inválido/corrompido: {path.name}")
        return False
    
    return True

# ─── Anexos (CORRIGIDO) ──────────────────────────────────────────────────
def save_attachment(sess: requests.Session, attachment: dict, out_dir: Path, course_id: str) -> bool:
    """Download de anexos com endpoint V3 correto."""
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
        
        # VERIFICAÇÃO CORRIGIDA
        if attachment_path.exists():
            if FORCE_REDOWNLOAD:
                print(f"   🔄 Forçando re-download: {file_name}")
                attachment_path.unlink()
            else:
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
        
        # ENDPOINT V3 CORRETO
        download_url = URLS["attachment_download"].format(attachment_id=file_membership_id)
        
        # Headers exatos do navegador
        headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "authorization": sess.headers.get("Authorization", ""),
            "origin": "https://hotmart.com",
            "referer": "https://hotmart.com/",
            "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
            "x-app-name": "app-club-consumer_v1.213.0_production",
            "x-product-id": course_id,
        }
        
        print(f"   ⬇️  Baixando de: api-club-hot-club-api.cb.hotmart.com")
        
        # Primeira requisição - pode retornar JSON com URL ou já ser o arquivo
        file_resp = fetch_with_retry(sess, download_url, headers, stream=True)
        if not file_resp:
            print(f"   ❌ Falha ao acessar endpoint")
            return False
        
        # Verificar se retornou JSON com URL de redirect
        content_type = file_resp.headers.get('content-type', '').lower()
        
        if 'application/json' in content_type:
            try:
                # API retornou JSON, buscar URL real
                data = file_resp.json()
                print(f"   🔗 API retornou JSON, buscando URL...")
                
                actual_url = (
                    data.get('downloadUrl') or 
                    data.get('url') or 
                    data.get('directDownloadUrl') or
                    data.get('fileUrl')
                )
                
                if not actual_url:
                    print(f"   ❌ JSON não contém URL de download")
                    print(f"   📄 Resposta: {json.dumps(data, indent=2)[:200]}")
                    return False
                
                print(f"   🔗 Seguindo para URL de download...")
                
                # Buscar arquivo da URL real
                file_resp = fetch_with_retry(sess, actual_url, headers_for(actual_url), stream=True)
                if not file_resp:
                    print(f"   ❌ Falha ao baixar do redirect")
                    return False
                    
            except json.JSONDecodeError:
                print(f"   ⚠️  Resposta não é JSON válido, tentando salvar como arquivo...")
        
        # Salvar arquivo
        temp_path = attachment_path.with_suffix(attachment_path.suffix + '.part')
        try:
            with open(temp_path, 'wb') as f:
                downloaded = 0
                for chunk in file_resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                
                # Mostrar progresso
                if downloaded > 0:
                    print(f"   💾 Salvando: {downloaded/1024:.1f} KB")
            
            # Verificar tamanho
            actual_size = temp_path.stat().st_size
            if actual_size < 100:  # Arquivo muito pequeno, provavelmente erro
                with open(temp_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read(500)
                print(f"   ⚠️  Arquivo muito pequeno ({actual_size} bytes)")
                print(f"   📄 Conteúdo: {content[:200]}")
                temp_path.unlink()
                return False
            
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
        print(f"   ❌ Erro geral: {e}")
        logger.exception("Erro detalhado:")
        return False

def fetch_page_attachments(sess: requests.Session, page_hash: str, course_id: str) -> list:
    """Busca anexos do endpoint complementary-content."""
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
        
        # DEBUG: Ver estrutura completa
        print("\n" + "="*70)
        print("🔍 DEBUG - Resposta completa da API complementary-content:")
        print("="*70)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("="*70 + "\n")
        
        return data.get('attachments', [])
        
    except Exception as e:
        logger.warning(f"Erro ao buscar conteúdo complementar: {e}")
        return []
    
def save_description(content: str, out_dir: Path) -> bool:
    """Salva descrição da aula."""
    try:
        if not content or not content.strip():
            return False
        
        desc_path = out_dir / "descricao.html"
        
        if desc_path.exists():
            if FORCE_REDOWNLOAD:
                print("📝 Forçando re-salvamento da descrição")
                desc_path.unlink()
            else:
                print("📝 Descrição já existe")
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
        
        print("✅ Descrição salva")
        return True
        
    except Exception as e:
        print(f"❌ Erro ao salvar descrição: {e}")
        return False


def save_complementary_readings(readings: list, out_dir: Path) -> bool:
    """Salva links complementares."""
    try:
        if not readings:
            return False
        
        links_path = out_dir / "links_complementares.html"
        
        if links_path.exists():
            if FORCE_REDOWNLOAD:
                print("🔗 Forçando re-salvamento dos links")
                links_path.unlink()
            else:
                print("🔗 Links já existem")
                return True
        
        html_content = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Links Complementares</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        .link { margin: 10px 0; padding: 10px; border: 1px solid #ddd; border-radius: 5px; }
        .link a { color: #0066cc; font-weight: bold; text-decoration: none; }
    </style>
</head>
<body>
    <h1>Links Complementares</h1>
"""
        
        for reading in readings:
            title = reading.get('articleName', 'Link')
            url = reading.get('articleUrl', '#')
            html_content += f'    <div class="link"><a href="{url}" target="_blank">{title}</a></div>\n'
        
        html_content += "</body>\n</html>"
        
        with open(links_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        
        print(f"✅ {len(readings)} links salvos")
        return True
        
    except Exception as e:
        print(f"❌ Erro ao salvar links: {e}")
        return False

# ─── Embed & Playlists (ORIGINAL) ────────────────────────────────────────
def extract_streams(sess: requests.Session, embed: str) -> List[str]:
    """Extrai URLs de streams (lógica original)."""
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
            print("⚠️  Vídeo com DRM, pulando...")
            continue
        if any(ext in u for ext in (".m3u8", ".mpd")):
            out.append(u)
    return out

def choose_av(master: str, sess: requests.Session) -> Tuple[str, Optional[str]]:
    """Escolhe melhor variante com tratamento de erro."""
    print(f"🔍 Analisando playlist master")
    
    # Usar requests ao invés de m3u8.load direto
    resp = fetch_with_retry(sess, master, headers_for(master))
    if not resp:
        raise Exception("Falha ao carregar playlist master")
    
    pl = m3u8.loads(resp.text, uri=master)
    
    print(f"📊 {len(pl.playlists)} variantes encontradas")
    
    best = max(pl.playlists, key=lambda p: (p.stream_info.resolution or (0,0))[1])
    base = master.rsplit("/",1)[0]+"/"
    v = urljoin(base, best.uri)
    
    print(f"✅ Melhor: {best.stream_info.resolution}")
    
    a = None
    if best.stream_info.audio:
        gid = best.stream_info.audio
        for m in pl.media:
            if m.type=="AUDIO" and m.group_id==gid and m.uri:
                a = urljoin(base, m.uri)
                print(f"✅ Áudio separado encontrado")
                break
    
    return v, a

def subtitles_playlist(master: str, sess: requests.Session) -> Optional[str]:
    """Busca playlist de legendas com tratamento de erro."""
    try:
        resp = fetch_with_retry(sess, master, headers_for(master))
        if not resp:
            return None
        
        pl = m3u8.loads(resp.text, uri=master)
        
        for m in pl.media:
            if m.type=="SUBTITLES" and m.uri:
                base = master.rsplit("/",1)[0]+"/"
                return urljoin(base, m.uri)
    except Exception as e:
        logger.warning(f"Erro ao buscar legendas: {e}")
    
    return None

def download_subs(pl_url: str, vtt_out: Path, sess: requests.Session) -> None:
    """Download de legendas com tratamento de erro."""
    try:
        resp = fetch_with_retry(sess, pl_url, headers_for(pl_url))
        if not resp:
            logger.warning("Falha ao baixar legendas")
            return
        
        pl = m3u8.loads(resp.text, uri=pl_url)
        
        with open(vtt_out, "wb") as f:
            for seg in pl.segments:
                resp = fetch_with_retry(sess, seg.absolute_uri, headers_for(seg.absolute_uri))
                if resp:
                    f.write(resp.content)
    except Exception as e:
        logger.error(f"Erro ao baixar legendas: {e}")

def key_iv(pl_url: str, sess: requests.Session):
    """Extrai chave de criptografia."""
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

# ─── Download HLS ────────────────────────────────────────────────────────

def dl_track(pl_url: str, out_ts: Path, sess: requests.Session,
             key: Optional[bytes], iv: Optional[bytes]):
    """Download HLS com tratamento de erros melhorado."""
    import concurrent.futures
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    # Buscar playlist usando nossa sessão com retry
    resp = fetch_with_retry(sess, pl_url, headers_for(pl_url))
    if not resp:
        logger.error(f"Falha ao buscar playlist: {pl_url}")
        raise Exception("Não foi possível carregar playlist")
    
    # Parsear playlist com m3u8
    pl = m3u8.loads(resp.text, uri=pl_url)
    segs = pl.segments
    
    if not segs:
        logger.error("Playlist sem segmentos!")
        raise Exception("Playlist vazia")
    
    out_ts.parent.mkdir(parents=True, exist_ok=True)

    def fetch(seg):
        """Baixa e decripta um segmento com retry."""
        resp = fetch_with_retry(sess, seg.absolute_uri, headers_for(seg.absolute_uri))
        if not resp:
            logger.warning(f"Segmento falhou, retornando vazio")
            return b''
        
        d = resp.content
        
        if key and iv:
            try:
                cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
                d = cipher.decryptor().update(d) + cipher.decryptor().finalize()
            except Exception as e:
                logger.error(f"Erro ao decriptar: {e}")
                return b''
        
        return d

    # Download sequencial garantindo ordem
    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as ex, \
         open(out_ts, "wb") as f:
        for i, seg in enumerate(segs):
            try:
                # .result() bloqueia e garante ordem
                data = ex.submit(fetch, seg).result()
                f.write(data)
                
                if (i + 1) % 20 == 0 or (i + 1) == len(segs):
                    print(f"   Progresso: {i+1}/{len(segs)} segmentos")
                    
            except Exception as e:
                logger.warning(f"Erro no segmento {i+1}: {e}")
                # Continua mesmo com erro em segmento individual
                continue
    
    # Verificar se arquivo foi criado
    if not out_ts.exists() or out_ts.stat().st_size < 1024:
        raise Exception(f"Arquivo TS não foi criado ou está vazio: {out_ts}")

def ffmpeg_mux(v_ts: Path, a_ts: Optional[Path],
               vtt: Optional[Path], mp4: Path):
    """FFmpeg muxing (lógica original)."""
    print(f"🔧 Iniciando muxing FFmpeg")
    
    cmd = [FFMPEG_CMD, "-y", "-i", str(v_ts)]
    
    if a_ts:
        cmd += ["-i", str(a_ts)]
    if vtt:
        cmd += ["-i", str(vtt)]
    
    cmd += ["-c:v", "copy"]
    cmd += ["-map", "0:v:0"]
    
    if a_ts:
        cmd += ["-c:a", "copy", "-map", "1:a:0"]
    else:
        cmd += ["-c:a", "copy"]
        cmd += ["-map", "0:a:0?"]
    
    if vtt:
        cmd += ["-c:s", "mov_text", "-map", f"{2 if a_ts else 1}:0"]
        cmd += ["-metadata:s:s:0", "language=por"]
    
    cmd.append(str(mp4))
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"❌ Erro FFmpeg: {result.stderr[:500]}")
            raise subprocess.CalledProcessError(result.returncode, cmd)
        print("✅ Muxing concluído")
    except Exception as e:
        print(f"❌ Erro: {e}")
        raise

# ─── Download de Vídeo ───────────────────────────────────────────────────

def download_video(sess: requests.Session, course: dict, lesson: dict, video: dict,
                   module_name: str = "", lesson_out_dir: Path = None, 
                   video_order: int = 0) -> bool:
    """Download de vídeo com retry automático em caso de erro de rede."""
    
    max_retries = 2  # Total de 3 tentativas (0, 1, 2)
    
    for attempt in range(max_retries + 1):
        try:
            # Tentar download
            result = _download_video_attempt(sess, course, lesson, video, 
                                            module_name, lesson_out_dir, video_order)
            
            if result:
                return True
            
            # Se falhou mas não lançou exceção, não tentar novamente
            return False
            
        except Exception as e:
            error_str = str(e)
            
            # Verificar se é erro de rede que vale retry
            is_network_error = any(err in error_str for err in [
                "Connection reset",
                "URLError",
                "ConnectionError",
                "Timeout",
                "timed out",
                "Connection refused",
                "Connection aborted"
            ])
            
            if is_network_error and attempt < max_retries:
                wait = 5 * (attempt + 1)  # 5s, 10s
                logger.warning(f"⚠️  Erro de rede (tentativa {attempt + 1}/{max_retries + 1})")
                logger.warning(f"⏳ Aguardando {wait}s antes de tentar novamente...")
                time.sleep(wait)
                continue
            else:
                # Erro não recuperável ou última tentativa
                logger.error(f"❌ Erro ao baixar vídeo: {e}")
                logger.exception("Detalhes do erro:")
                return False
    
    return False


def _download_video_attempt(sess: requests.Session, course: dict, lesson: dict, video: dict,
                            module_name: str = "", lesson_out_dir: Path = None, 
                            video_order: int = 0) -> bool:
    """Tentativa única de download de vídeo."""
    
    video_order_str = str(video_order).zfill(2)
    video_name = f"{video_order_str}. {video['name']}"
    
    print(f"\n🎬 Processando: {video_name}")
    
    # Extract streams
    embed_urls = extract_streams(sess, video["url"])
    if not embed_urls:
        print("❌ DRM ou nada encontrado")
        return False
    
    master = embed_urls[0]
    print(f"📡 Master playlist obtida")
    
    try:
        video_url, audio_url = choose_av(master, sess)
    except Exception as e:
        print(f"❌ Erro ao ler variants: {e}")
        return False
    
    sub_pl = subtitles_playlist(master, sess)
    if sub_pl:
        print("📝 Legenda encontrada")
    
    # Output paths
    out_dir = lesson_out_dir or DL_ROOT / clean(course["name"])
    out_dir.mkdir(parents=True, exist_ok=True)
    
    base_nm = clean(Path(video["name"]).stem)
    numbered_base_nm = f"{video_order_str}. {base_nm}"
    mp4_out = out_dir / f"{numbered_base_nm}.mp4"
    v_tt_out = out_dir / f"{numbered_base_nm}.vtt" if sub_pl else None
    
    # Validação - CORRIGIDA
    if mp4_out.exists():
        file_size_mb = mp4_out.stat().st_size / (1024 * 1024)
        print(f"🔍 Arquivo existe: {mp4_out.name} ({file_size_mb:.1f}MB)")
        
        if not FORCE_REDOWNLOAD and is_valid_video(mp4_out):
            print(f"⏭️  Já existe e é válido: {mp4_out.name}")
            return True
        elif FORCE_REDOWNLOAD:
            print(f"🔄 Forçando re-download: {mp4_out.name}")
            mp4_out.unlink()
        else:
            print(f"⚠️  Arquivo inválido ou corrompido, re-downloading")
            mp4_out.unlink()
    
    print(f"⬇️  Baixando: {mp4_out.name}")
    
    # Download de streams
    try:
        if audio_url:
            # Áudio e vídeo separados
            print("🎵 Baixando áudio e vídeo separados")
            v_ts = out_dir / f"_v_{video_order}.ts"
            a_ts = out_dir / f"_a_{video_order}.ts"
            
            print("📥 Stream de vídeo...")
            dl_track(video_url, v_ts, sess, *key_iv(video_url, sess))
            
            print("📥 Stream de áudio...")
            dl_track(audio_url, a_ts, sess, *key_iv(audio_url, sess))
            
            if sub_pl:
                print("📥 Legendas...")
                download_subs(sub_pl, v_tt_out, sess)
            
            print("🔧 Muxing...")
            ffmpeg_mux(v_ts, a_ts, v_tt_out, mp4_out)
            
            # Cleanup
            v_ts.unlink(missing_ok=True)
            a_ts.unlink(missing_ok=True)
            if v_tt_out and v_tt_out.exists():
                v_tt_out.unlink(missing_ok=True)
            
        else:
            # Stream combinado
            print("🎵 Stream combinado (áudio+vídeo)")
            av_ts = out_dir / f"_av_{video_order}.ts"
            
            print("📥 Baixando stream...")
            dl_track(video_url, av_ts, sess, *key_iv(video_url, sess))
            
            if sub_pl:
                print("📥 Legendas...")
                download_subs(sub_pl, v_tt_out, sess)
                print("🔧 Muxing com legendas...")
                ffmpeg_mux(av_ts, None, v_tt_out, mp4_out)
                if v_tt_out and v_tt_out.exists():
                    v_tt_out.unlink(missing_ok=True)
            else:
                print("🔧 Convertendo para MP4...")
                subprocess.run(
                    [FFMPEG_CMD, "-y", "-i", str(av_ts), "-c", "copy", str(mp4_out)],
                    check=True, 
                    stdout=subprocess.DEVNULL, 
                    stderr=subprocess.DEVNULL,
                    timeout=300
                )
            
            # Cleanup
            av_ts.unlink(missing_ok=True)
        
        # Verificar se vídeo foi criado com sucesso
        if not mp4_out.exists():
            raise Exception("Arquivo MP4 não foi criado!")
        
        final_size_mb = mp4_out.stat().st_size / (1024 * 1024)
        print(f"✅ Concluído: {mp4_out.name} ({final_size_mb:.1f}MB)")
        return True
        
    except Exception as e:
        # Limpar arquivos temporários em caso de erro
        temp_files = [
            out_dir / f"_v_{video_order}.ts",
            out_dir / f"_a_{video_order}.ts", 
            out_dir / f"_av_{video_order}.ts"
        ]
        for temp_file in temp_files:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except:
                    pass
        
        # Re-lançar exceção para ser tratada pelo retry
        raise

def count_existing_files(out_dir: Path) -> dict:
    """Conta arquivos já baixados em um diretório."""
    if not out_dir.exists():
        return {"videos": 0, "attachments": 0, "descriptions": 0, "links": 0}
    
    videos = len(list(out_dir.rglob("*.mp4")))
    attachments = len(list((out_dir / "Materiais").glob("*"))) if (out_dir / "Materiais").exists() else 0
    descriptions = len(list(out_dir.rglob("descricao.html")))
    links = len(list(out_dir.rglob("links_complementares.html")))
    
    return {
        "videos": videos,
        "attachments": attachments, 
        "descriptions": descriptions,
        "links": links
    }

def download_lesson(sess: requests.Session, course: dict, lesson: dict, 
                   course_id: str, module_name: str = "", 
                   lesson_order: int = 0) -> int:
    """Download de lição completa."""
    
    lesson_order_str = str(lesson_order).zfill(2)
    lesson_name = f"{lesson_order_str}. {clean(lesson['name'])}"
    
    print(f"\n📚 Lição: {lesson_name}")
    
    lesson_url = URLS["lessons"].format(lesson_hash=lesson["hash"])
    h_ls = headers_for(lesson_url) | {
        "x-app-name": "app-club-consumer_v1.210.1_production",
        "x-product-id": course_id,
    }
    
    resp_lesson = fetch_with_retry(sess, lesson_url, h_ls)
    if not resp_lesson:
        print(f"❌ Erro ao buscar lição")
        return 0
    
    try:
        lesson_full = resp_lesson.json()
    except:
        print(f"❌ Resposta inválida")
        return 0
    
    if module_name:
        out_dir = DL_ROOT / clean(course["name"]) / clean(module_name) / lesson_name
    else:
        out_dir = DL_ROOT / clean(course["name"]) / lesson_name
    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Vídeos
    videos = [m for m in lesson_full.get("medias", []) if m.get("type") == "VIDEO"]
    success_count = 0
    
    if videos:
        print(f"🎬 {len(videos)} vídeos")
        for i, video in enumerate(videos, 1):
            print(f"\n[{i}/{len(videos)}]")
            if download_video(sess, course, lesson, video, module_name, 
                            lesson_out_dir=out_dir, video_order=i):
                success_count += 1
    else:
        print("⚠️  Sem vídeos")
    
    # Anexos
    attachments = lesson_full.get("attachments", [])
    
    if not attachments:
        page_hash = lesson.get("hash")
        if page_hash:
            print("🔍 Buscando conteúdo complementar...")
            attachments = fetch_page_attachments(sess, page_hash, course_id)
    
    if attachments:
        print(f"\n📎 {len(attachments)} anexos")
        attachment_count = 0
        for attachment in attachments:
            if save_attachment(sess, attachment, out_dir, course_id):
                attachment_count += 1
        print(f"📎 Anexos: {attachment_count}/{len(attachments)}")
    
    # Descrição
    description = lesson_full.get("description") or lesson_full.get("content")
    if description and description.strip():
        print("📝 Salvando descrição...")
        save_description(description, out_dir)
    
    # Links
    comp_readings = lesson_full.get("complementaryReadings", [])
    if comp_readings:
        print(f"🔗 {len(comp_readings)} links complementares")
        save_complementary_readings(comp_readings, out_dir)
    
    return success_count

def download_module(sess: requests.Session, course: dict, module: dict, 
                   course_id: str, module_order: int = 0, 
                   checkpoint: CheckpointManager = None) -> int:
    """Download de módulo completo."""
    
    module_order_str = str(module_order).zfill(2)
    module_name = f"{module_order_str}. {clean(module['name'])}"
    
    print(f"\n📖 Módulo: {module_name}")
    
    # Verificar arquivos existentes
    module_dir = DL_ROOT / clean(course["name"]) / module_name
    existing = count_existing_files(module_dir)
    if any(existing.values()):
        print(f"📊 Já existem:")
        if existing["videos"] > 0:
            print(f"   🎬 {existing['videos']} vídeos")
        if existing["attachments"] > 0:
            print(f"   📎 {existing['attachments']} anexos")
        if existing["descriptions"] > 0:
            print(f"   📝 {existing['descriptions']} descrições")
        if existing["links"] > 0:
            print(f"   🔗 {existing['links']} links")
    
    pages = module.get("pages", [])
    if not pages:
        print("⚠️  Sem lições")
        return 0
    
    print(f"📚 {len(pages)} lições")
    
    success_count = 0
    for i, lesson in enumerate(pages, 1):
        print(f"\n--- Lição {i}/{len(pages)} ---")
        
        count = download_lesson(sess, course, lesson, course_id, module_name, lesson_order=i)
        success_count += count
        
        if checkpoint:
            checkpoint.save(module_order, i)
    
    return success_count

# ─── Validação FFmpeg ────────────────────────────────────────────────────
def validate_ffmpeg() -> bool:
    """Valida FFmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], 
            capture_output=True, 
            text=True, 
            timeout=5
        )
        if result.returncode == 0:
            version = result.stdout.split('\n')[0]
            print(f"✅ FFmpeg: {version}")
            return True
    except:
        pass
    
    print("❌ FFmpeg não encontrado")
    print("Instale com:")
    print("  - Windows: winget install FFmpeg")
    print("  - Linux: sudo apt install ffmpeg")
    print("  - Mac: brew install ffmpeg")
    return False

def show_token_help():
    """Instruções para obter token."""
    print("\n" + "="*70)
    print("🔑 COMO OBTER O TOKEN BEARER")
    print("="*70)
    print("1. Abra https://hotmart.com e faça login")
    print("2. Acesse qualquer curso")
    print("3. Abra DevTools (F12) > Aba Network")
    print("4. Recarregue ou navegue em uma aula")
    print("5. Procure requisições para 'api-hub.cb.hotmart.com'")
    print("6. Clique na requisição > Headers")
    print("7. Copie o valor após 'Authorization: Bearer '")
    print("8. O token começa com 'eyJ' e é bem longo")
    print("="*70 + "\n")

def validate_token(token: str) -> bool:
    """Valida token."""
    if not token or len(token) < 100:
        print("❌ Token muito curto")
        return False
    
    if not token.startswith('eyJ'):
        print("⚠️  Token não parece JWT (deveria começar com 'eyJ')")
        return input("Continuar? [s/N] ").strip().lower() == 's'
    
    try:
        token.encode('latin-1')
    except UnicodeEncodeError:
        print("❌ Token com caracteres inválidos")
        return False
    
    print(f"✅ Token OK ({len(token)} chars)")
    return True

# ─── Main ────────────────────────────────────────────────────────────────
def main():
    """Programa principal."""
    print("🎓 HOTMART DOWNLOADER - VERSÃO HÍBRIDA")
    print("="*70)
    print("✨ Mantém lógica original de download (perfeita)")
    print("➕ Adiciona:")
    print("   🔄 Retry automático em falhas")
    print("   📎 Correção de download de anexos")
    print("   ✅ Validação de arquivos")
    print("   💾 Sistema de checkpoint")
    print("   📊 Logging detalhado")
    print("="*70 + "\n")

    force_redownload = False
    redownload_choice = input("Forçar re-download de arquivos existentes? [s/N] ").strip().lower()
    if redownload_choice == 's':
        force_redownload = True
        print("⚠️  Modo: Re-download forçado ATIVADO")
    
    if not validate_ffmpeg():
        sys.exit(1)
    
    print("Cole seu Bearer token:")
    print("(Digite 'help' para instruções)")
    TOKEN = input("Token: ").strip()
    
    if TOKEN.lower() == 'help':
        show_token_help()
        TOKEN = input("Token: ").strip()
    
    if not validate_token(TOKEN):
        sys.exit(1)
    
    sess = requests.Session()
    sess.headers.update(BASE_HEADERS | {"Authorization": f"Bearer {TOKEN}"})
    
    print("📡 Buscando cursos...")
    
    resp = fetch_with_retry(sess, URLS["courses"], headers_for(URLS["courses"]))
    if not resp:
        print("❌ Falha ao buscar cursos")
        sys.exit(1)
    
    print("✅ Cursos obtidos!")
    
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
        print("❌ Nenhum curso")
        sys.exit(1)
    
    available = sum(1 for c in courses if c['slug'])
    print(f"\n📊 {len(courses)} cursos ({available} disponíveis)")
    
    print("\n=== Cursos ===")
    for i, c in enumerate(courses, 1):
        status = "✅" if c['slug'] else "❌"
        print(f"{i}. {c['name']} {status}")
    
    course = courses[ask_int("Curso: ", 1, len(courses)) - 1]
    
    if not course['slug']:
        print("❌ Curso sem slug")
        sys.exit(1)
    
    print(f"✅ Curso: {course['name']}")
    
    checkpoint_mgr = CheckpointManager(course['name'])
    checkpoint = checkpoint_mgr.load()
    
    print("📡 Buscando módulos...")
    
    h_mod = headers_for(URLS["modules"]) | {
        "x-app-name": "app-club-consumer_v1.210.1_production",
        "slug": course["slug"],
        "x-product-id": str(course["id"]),
    }
    
    resp_mod = fetch_with_retry(sess, URLS["modules"], h_mod)
    if not resp_mod:
        print("❌ Falha ao buscar módulos")
        sys.exit(1)
    
    mods = resp_mod.json().get("modules", [])
    
    if not mods:
        print("❌ Sem módulos")
        sys.exit(1)
    
    print(f"✅ {len(mods)} módulos")
    
    print("\n=== Módulos ===")
    print("0. 📥 BAIXAR TODOS")
    for i, m in enumerate(mods, 1):
        print(f"{i}. {m['name']}")
    
    module_choice = ask_int("Módulo (0=todos): ", 0, len(mods))
    
    if module_choice == 0:
        print("\n🚀 Baixando TODOS os módulos...")
        if input("Confirma? [s/N] ").strip().lower() != 's':
            sys.exit(0)
        
        start_time = time.time()
        total_downloaded = 0
        
        print("💡 Ctrl+C para parar")
        print("="*70)
        
        try:
            start_module = checkpoint.get("module", 0)
            if start_module > 0:
                print(f"📍 Retomando do módulo {start_module + 1}")
            
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
                
                print(f"📊 Módulo: {downloaded} vídeos em {mod_time/60:.1f}min")
                
                if i < len(mods) - 1:
                    avg_time = (time.time() - start_time) / (i + 1 - start_module)
                    remaining = (len(mods) - i - 1) * avg_time
                    print(f"⏱️  Restante: ~{remaining/60:.1f}min")
        
        except KeyboardInterrupt:
            print(f"\n\n⏸️  INTERROMPIDO")
            print(f"📊 Baixados: {total_downloaded} vídeos")
            print(f"📁 Pasta: {DL_ROOT / clean(course['name'])}")
            print("💡 Execute novamente para retomar")
            sys.exit(0)
        
        checkpoint_mgr.clear()
        
        total_time = time.time() - start_time
        print(f"\n🎉 COMPLETO!")
        print(f"📊 Total: {total_downloaded} vídeos")
        print(f"⏱️  Tempo: {total_time/60:.1f}min")
        print(f"📁 Pasta: {DL_ROOT / clean(course['name'])}")
        
    else:
        mod = mods[module_choice - 1]
        
        pages = mod.get("pages", [])
        print(f"\n=== Lições: {mod['name']} ===")
        print("0. 📥 BAIXAR TODAS")
        for i, l in enumerate(pages, 1):
            print(f"{i}. {l['name']}")
        
        lesson_choice = ask_int("Lição (0=todas): ", 0, len(pages))
        
        if lesson_choice == 0:
            print(f"\n🚀 Baixando todas as lições")
            downloaded = download_module(
                sess, course, mod, str(course["id"]), 
                module_order=module_choice
            )
            print(f"📊 Total: {downloaded} vídeos")
        else:
            lesson = pages[lesson_choice - 1]
            module_order_str = str(module_choice).zfill(2)
            numbered_module_name = f"{module_order_str}. {clean(mod['name'])}"
            
            downloaded = download_lesson(
                sess, course, lesson, str(course["id"]), 
                numbered_module_name, lesson_order=lesson_choice
            )
            print(f"📊 Total: {downloaded} vídeos")
    
    print("\n🎉 Concluído!")
    logger.info("Script finalizado com sucesso")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Cancelado")
        logger.info("Interrompido pelo usuário")
        sys.exit(0)
    except Exception as e:
        logger.exception(f"❌ Erro fatal: {e}")
        print(f"\n❌ Erro: {e}")
        print("Veja detalhes em hotmart_downloader.log")
        sys.exit(1)
