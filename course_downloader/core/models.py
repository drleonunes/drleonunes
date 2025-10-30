# course_downloader/core/models.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional

"""
Este arquivo define as estruturas de dados agnósticas à plataforma.
O "motor" de download principal usará esses objetos, e cada "provedor"
será responsável por traduzir a resposta da API específica da sua plataforma
para estes modelos padronizados.
"""

@dataclass
class Attachment:
    """Representa um anexo para download."""
    file_name: str
    file_size: int
    # O provedor é responsável por preencher os dados necessários
    # para que o downloader possa baixar este anexo.
    # Usaremos um dicionário flexível para isso.
    provider_data: dict = field(default_factory=dict)

@dataclass
class Video:
    """Representa um vídeo para download."""
    name: str
    # A URL da página "embed" que contém o player.
    # O downloader usará isso para encontrar o manifesto de stream (.m3u8).
    embed_url: str

@dataclass
class Lesson:
    """Representa uma lição do curso."""
    hash: str  # Usaremos o 'hash' da Hotmart como ID genérico por enquanto
    name: str
    order: int
    # Estes campos são preenchidos sob demanda para economizar requisições à API.
    videos: List[Video] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
    description: Optional[str] = None
    complementary_readings: List[dict] = field(default_factory=list)

@dataclass
class Module:
    """Representa um módulo do curso."""
    name: str
    order: int
    lessons: List[Lesson] = field(default_factory=list)

@dataclass
class Course:
    """Representa um curso completo."""
    id: str  # ID específico da plataforma
    name: str
    slug: Optional[str] = None  # Slug usado em URLs, nem todas as plataformas têm
    modules: List[Module] = field(default_factory=list)
