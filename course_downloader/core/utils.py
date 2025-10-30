# course_downloader/core/utils.py
from __future__ import annotations

import json
import logging
import re
import random
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─── Configuração de Logging ─────────────────────────────────────────────
# Configura o logger para ser usado em todo o projeto
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('course_downloader.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ─── Configurações Globais ───────────────────────────────────────────────
MAX_WORKERS = 6
TIMEOUT_SEC = 30
REQUEST_DELAY = (0.5, 1.5)
DL_ROOT = Path("downloads")
FFMPEG_CMD = "ffmpeg"

# ─── Utilidades Gerais ───────────────────────────────────────────────────
_illegal = re.compile(r'[<>:"/\\|?*\x00-\x1F]')

def clean(name: str) -> str:
    """Remove caracteres ilegais de nomes de arquivo e limita o comprimento."""
    name = _illegal.sub("", name).strip()
    return name[:120] or "file" # Aumentado o limite para nomes mais descritivos

def human_delay():
    """Pausa a execução por um curto período aleatório para simular comportamento humano."""
    delay = random.uniform(*REQUEST_DELAY)
    time.sleep(delay)

# ─── Cliente HTTP com Retry ──────────────────────────────────────────────
def create_session() -> requests.Session:
    """Cria uma sessão HTTP otimizada com uma estratégia de retry robusta."""
    sess = requests.Session()

    retry_strategy = Retry(
        total=3,
        backoff_factor=2,
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

    # Headers básicos. O provedor será responsável por adicionar headers de autenticação.
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:139.0) Gecko/20100101 Firefox/139.0",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    })

    return sess

def fetch_with_retry(
    sess: requests.Session,
    url: str,
    headers: dict,
    max_retries: int = 3,
    **kwargs
) -> Optional[requests.Response]:
    """Executa uma requisição GET com retries manuais e delays para robustez."""
    for attempt in range(max_retries):
        try:
            human_delay()
            resp = sess.get(url, headers=headers, timeout=TIMEOUT_SEC, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⏳ Timeout, tentando novamente em {wait}s... (URL: {url[:80]})")
                time.sleep(wait)
            else:
                logger.error(f"❌ Timeout final após {max_retries} tentativas: {url}")
                return None
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                wait = 5 * (2 ** attempt)
                logger.warning(f"⚠️ Rate limit detectado. Aguardando {wait}s...")
                time.sleep(wait)
            elif attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️ Erro HTTP {e.response.status_code}. Tentando novamente em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro HTTP final após {max_retries} tentativas: {e}")
                return None
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                logger.warning(f"⚠️ Erro de rede. Tentando novamente em {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"❌ Erro de rede final após {max_retries} tentativas: {e}")
                return None
    return None

# ─── Validação de FFmpeg ─────────────────────────────────────────────────
def validate_ffmpeg() -> bool:
    """Valida se o FFmpeg está instalado e acessível no PATH do sistema."""
    try:
        result = subprocess.run(
            [FFMPEG_CMD, "-version"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            logger.info(f"✅ FFmpeg encontrado: {version_line}")
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    logger.error("❌ FFmpeg não encontrado no PATH.")
    logger.error("   Por favor, instale o FFmpeg e adicione-o ao PATH do seu sistema.")
    return False

# ─── Sistema de Checkpoint ───────────────────────────────────────────────
class CheckpointManager:
    """Gerencia checkpoints para permitir a retomada de downloads interrompidos."""

    def __init__(self, course_name: str):
        self.checkpoint_file = DL_ROOT / clean(course_name) / ".checkpoint.json"
        self.checkpoint_file.parent.mkdir(parents=True, exist_ok=True)

    def save(self, module_idx: int, lesson_idx: int):
        """Salva o progresso atual no arquivo de checkpoint."""
        try:
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "module": module_idx,
                    "lesson": lesson_idx,
                    "timestamp": datetime.now().isoformat()
                }, f, indent=2)
        except IOError as e:
            logger.warning(f"Não foi possível salvar o checkpoint: {e}")

    def load(self) -> Dict[str, int]:
        """Carrega o último checkpoint salvo."""
        if self.checkpoint_file.exists():
            try:
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"📍 Checkpoint encontrado: Módulo {data['module']}, Lição {data['lesson']}")
                    return {"module": data['module'], "lesson": data['lesson']}
            except (IOError, json.JSONDecodeError) as e:
                logger.warning(f"Erro ao carregar o checkpoint: {e}")
        return {"module": 0, "lesson": 0}

    def clear(self):
        """Remove o arquivo de checkpoint após a conclusão bem-sucedida."""
        if self.checkpoint_file.exists():
            try:
                self.checkpoint_file.unlink()
            except IOError as e:
                logger.warning(f"Não foi possível remover o checkpoint: {e}")

# ─── Interface de Usuário ────────────────────────────────────────────────
def ask_int(prompt: str, lo: int, hi: int) -> int:
    """Solicita um input numérico do usuário dentro de um intervalo."""
    while True:
        try:
            n = int(input(prompt).strip())
            if lo <= n <= hi:
                return n
            print(f"❌ Valor deve estar entre {lo} e {hi}.")
        except ValueError:
            print("❌ Entrada inválida. Por favor, digite um número.")
        except KeyboardInterrupt:
            print("\n\n👋 Operação cancelada pelo usuário.")
            sys.exit(0)
