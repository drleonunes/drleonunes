# course_downloader/providers/hotmart.py
from __future__ import annotations

import json
import logging
from typing import List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin, urlparse

import m3u8
from bs4 import BeautifulSoup

# Importações da nossa nova estrutura
from course_downloader.core.models import Course, Module, Lesson, Video, Attachment
from course_downloader.core.utils import create_session, fetch_with_retry, logger
from course_downloader.providers.base_provider import BaseProvider

# ─── Constantes Específicas da Hotmart ───────────────────────────────────
URLS = {
    "courses": "https://api-hub.cb.hotmart.com/club-drive-api/rest/v2/purchase/?archived=UNARCHIVED",
    "modules": "https://api-club-course-consumption-gateway.hotmart.com/v1/navigation",
    "lessons": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/lessons/{lesson_hash}",
    "attachment": "https://api-club-course-consumption-gateway.hotmart.com/v2/web/attachments/{attachment_id}",
    "complementary": "https://api-club-course-consumption-gateway-ga.cb.hotmart.com/v1/pages/{page_hash}/complementary-content",
}

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://hotmart.com",
    "Referer": "https://hotmart.com",
}

class HotmartProvider(BaseProvider):
    """Implementação do provedor para a plataforma Hotmart."""

    def authenticate(self, token: str) -> bool:
        """Autentica na Hotmart, validando o token e configurando a sessão."""
        if not token or not token.startswith('eyJ'):
            logger.error("❌ Token da Hotmart parece ser inválido (deve começar com 'eyJ').")
            return False

        self.session = create_session()
        self.session.headers.update(BASE_HEADERS | {"Authorization": f"Bearer {token}"})
        logger.info("✅ Autenticação com a Hotmart configurada.")
        return True

    def get_courses(self) -> List[Course]:
        """Busca a lista de cursos do usuário na Hotmart."""
        logger.info("📡 Buscando cursos na Hotmart...")
        resp = fetch_with_retry(self.session, URLS["courses"], self.session.headers)
        if not resp:
            logger.error("❌ Falha ao buscar a lista de cursos da Hotmart.")
            return []

        raw_courses = resp.json().get("data", [])
        courses = []
        for p in raw_courses:
            if p.get("purchase", {}).get("purchaseCancelled", False):
                continue

            product = p["product"]
            slug = product.get("hotmartClub", {}).get("slug")
            if slug:  # Apenas cursos com slug são acessíveis
                courses.append(Course(
                    id=str(product["id"]),
                    name=product["name"],
                    slug=slug
                ))

        logger.info(f"📊 Encontrados {len(courses)} cursos disponíveis.")
        return courses

    def get_modules(self, course: Course) -> List[Module]:
        """Busca os módulos de um curso específico da Hotmart."""
        logger.info(f"📡 Buscando módulos para o curso: {course.name}...")

        headers = self.session.headers.copy()
        headers.update({
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "slug": course.slug,
            "x-product-id": course.id,
        })

        resp = fetch_with_retry(self.session, URLS["modules"], headers)
        if not resp:
            logger.error(f"❌ Falha ao buscar módulos para o curso {course.name}.")
            return []

        raw_modules = resp.json().get("modules", [])
        modules = []
        for i, mod_data in enumerate(raw_modules, 1):
            # Pré-carrega as lições aqui, pois a API da Hotmart as envia junto com os módulos
            lessons = self._parse_lessons_from_module(mod_data)
            module = Module(
                name=mod_data['name'],
                order=i,
                lessons=lessons
            )
            modules.append(module)

        logger.info(f"📚 Encontrados {len(modules)} módulos.")
        return modules

    def _parse_lessons_from_module(self, module_data: dict) -> List[Lesson]:
        """Helper para extrair lições de um payload de módulo."""
        lessons = []
        pages = module_data.get("pages", [])
        for i, lesson_data in enumerate(pages, 1):
            lessons.append(Lesson(
                hash=lesson_data['hash'],
                name=lesson_data['name'],
                order=i
            ))
        return lessons

    def get_lessons(self, module: Module) -> List[Lesson]:
        """Retorna as lições que já foram pré-carregadas com o módulo."""
        # A API da Hotmart retorna as lições junto com os módulos,
        # então não precisamos de uma nova requisição aqui. Apenas retornamos o que já temos.
        return module.lessons

    def get_lesson_details(self, lesson: Lesson, course: Course) -> None:
        """Busca os detalhes completos de uma lição (vídeos, anexos)."""
        logger.info(f"   - Buscando detalhes da lição: {lesson.name}")

        lesson_url = URLS["lessons"].format(lesson_hash=lesson.hash)
        headers = self.session.headers.copy()
        headers.update({
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "x-product-id": course.id,
        })

        resp = fetch_with_retry(self.session, lesson_url, headers)
        if not resp:
            logger.error(f"   ❌ Falha ao buscar detalhes da lição: {lesson.name}")
            return

        lesson_full = resp.json()

        # Preenche o objeto Lesson existente
        lesson.description = lesson_full.get("description") or lesson_full.get("content")
        lesson.complementary_readings = lesson_full.get("complementaryReadings", [])

        # Extrai vídeos
        videos_data = [m for m in lesson_full.get("medias", []) if m.get("type") == "VIDEO"]
        for video_data in videos_data:
            lesson.videos.append(Video(
                name=video_data['name'],
                embed_url=video_data['url']
            ))

        # Extrai anexos
        attachments_data = lesson_full.get("attachments", [])
        if not attachments_data: # Tenta endpoint alternativo
             attachments_data = self._fetch_complementary_attachments(lesson.hash, course.id)

        for att_data in attachments_data:
            lesson.attachments.append(Attachment(
                file_name=att_data.get('fileName', 'anexo'),
                file_size=att_data.get('fileSize', 0),
                provider_data=att_data # Passa todos os dados para o downloader decidir como usar
            ))

    def _fetch_complementary_attachments(self, page_hash: str, course_id: str) -> list:
        """Busca anexos do endpoint 'complementary-content'."""
        comp_url = URLS["complementary"].format(page_hash=page_hash)
        headers = self.session.headers.copy()
        headers.update({
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "x-product-id": course_id,
        })
        resp = fetch_with_retry(self.session, comp_url, headers)
        return resp.json().get('attachments', []) if resp else []

    def get_attachment_download_url(self, attachment: Attachment, course: Course) -> Tuple[Optional[str], dict]:
        """Obtém a URL de download final para um anexo."""
        att_data = attachment.provider_data
        file_membership_id = att_data.get('fileMembershipId')
        if not file_membership_id:
            return None, {}

        # Lógica de múltiplos métodos para encontrar a URL, simplificada
        att_url_v2 = URLS["attachment"].format(attachment_id=file_membership_id)
        headers = self.session.headers.copy()
        headers.update({
            "x-app-name": "app-club-consumer_v1.210.1_production",
            "x-product-id": course.id,
        })

        resp = fetch_with_retry(self.session, att_url_v2, headers, max_retries=1)
        if resp:
            try:
                data = resp.json()
                url = data.get('downloadUrl') or data.get('directDownloadUrl')
                if url:
                    return url, headers
            except json.JSONDecodeError:
                pass

        # Plano B: URL de download construída (padrão comum)
        fallback_url = f"https://api-club-file.cb.hotmart.com/v1/files/{file_membership_id}/download"
        return fallback_url, headers

    def get_video_streams(self, video: Video) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Extrai as URLs de stream de vídeo, áudio e legendas da página de embed."""
        resp = fetch_with_retry(self.session, video.embed_url, self.session.headers)
        if not resp:
            return None, None, None

        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return None, None, None

        data = json.loads(tag.string)
        assets = (data.get("props", {}).get("pageProps", {})
                        .get("applicationData", {}).get("mediaAssets", []))

        # Encontra o primeiro stream não-DRM
        master_url = ""
        for a in assets:
            u = a.get("urlEncrypted") or a.get("url") or ""
            if "/drm/" not in u and ".m3u8" in u:
                master_url = u
                break

        if not master_url:
            logger.warning("   ⚠️ Vídeo com DRM ou formato não suportado encontrado. Pulando.")
            return None, None, None

        # Analisa o manifesto master para encontrar as melhores qualidades
        try:
            resp = fetch_with_retry(self.session, master_url, self.session.headers)
            if not resp:
                raise Exception("Falha ao carregar playlist master")
            pl = m3u8.loads(resp.text, uri=master_url)
            base = master_url.rsplit("/", 1)[0] + "/"

            best_video = max(pl.playlists, key=lambda p: (p.stream_info.resolution or (0, 0))[1])
            video_url = urljoin(base, best_video.uri)

            audio_url = None
            if best_video.stream_info.audio:
                gid = best_video.stream_info.audio
                for m in pl.media:
                    if m.type == "AUDIO" and m.group_id == gid and m.uri:
                        audio_url = urljoin(base, m.uri)
                        break

            sub_url = None
            for m in pl.media:
                if m.type == "SUBTITLES" and m.uri:
                    sub_url = urljoin(base, m.uri) # Pega a primeira legenda que encontrar
                    break

            return video_url, audio_url, sub_url

        except Exception as e:
            logger.error(f"   ❌ Erro ao analisar playlist M3U8: {e}")
            return None, None, None
