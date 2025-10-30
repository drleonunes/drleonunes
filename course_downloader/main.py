# course_downloader/main.py
"""
Ponto de entrada principal para o Course Downloader.
Este script orquestra a interação do usuário, o provedor da plataforma
e o motor de download principal.
"""
from __future__ import annotations
import sys
import time
from pathlib import Path

# Importações dos nossos módulos principais
from course_downloader.core.downloader import (
    download_video_hls,
    save_attachment,
    save_complementary_readings,
    save_description,
)
from course_downloader.core.models import Course, Lesson
from course_downloader.core.utils import (
    DL_ROOT,
    CheckpointManager,
    ask_int,
    clean,
    logger,
    validate_ffmpeg,
)
from course_downloader.providers.base_provider import BaseProvider
from course_downloader.providers.hotmart import HotmartProvider

# Mapeamento de provedores disponíveis.
# Para adicionar uma nova plataforma, basta adicioná-la a este dicionário.
AVAILABLE_PROVIDERS = {
    "hotmart": HotmartProvider,
}

def download_lesson_content(provider: HotmartProvider, course: Course, lesson: Lesson, lesson_dir: Path):
    """Orquestra o download de todo o conteúdo de uma única lição."""

    # 1. Busca os detalhes da lição (vídeos, anexos, etc.)
    provider.get_lesson_details(lesson, course)

    # 2. Processa e baixa os vídeos
    if lesson.videos:
        logger.info(f"  🎬 Encontrados {len(lesson.videos)} vídeos.")
        for i, video in enumerate(lesson.videos, 1):
            video_order_str = str(i).zfill(2)
            video_name = f"{video_order_str}. {clean(video.name)}.mp4"
            video_path = lesson_dir / video_name

            # Obtém as URLs de stream do provedor
            video_url, audio_url, sub_url = provider.get_video_streams(video)

            if video_url:
                # Chama a função de download genérica
                download_video_hls(
                    provider.session,
                    video_url,
                    audio_url,
                    sub_url,
                    video_path,
                    provider.session.headers  # Usar headers da sessão do provedor
                )
            else:
                logger.warning(f"   - Não foi possível obter streams para o vídeo: {video.name}")

    # 3. Processa e baixa os anexos
    if lesson.attachments:
        logger.info(f"  📎 Encontrados {len(lesson.attachments)} anexos.")
        for attachment in lesson.attachments:
            # O provedor é responsável por encontrar a URL de download final
            download_url, headers = provider.get_attachment_download_url(attachment, course)
            if download_url:
                # Chama a função de download de anexo genérica
                save_attachment(provider.session, attachment, lesson_dir, download_url, headers)
            else:
                logger.warning(f"   - Não foi possível obter URL de download para o anexo: {attachment.file_name}")

    # 4. Salva conteúdo extra
    if lesson.description:
        save_description(lesson.description, lesson_dir)
    if lesson.complementary_readings:
        save_complementary_readings(lesson.complementary_readings, lesson_dir)


def main():
    """Função principal do orquestrador."""
    print("🎓 Universal Course Downloader")
    print("="*40)

    if not validate_ffmpeg():
        sys.exit(1)

    # --- Seleção do Provedor ---
    provider_key = "hotmart" # No futuro, pode ser uma escolha do usuário
    ProviderClass = AVAILABLE_PROVIDERS.get(provider_key)
    if not ProviderClass:
        logger.error(f"❌ Provedor '{provider_key}' não encontrado.")
        sys.exit(1)

    provider = ProviderClass()

    # --- Autenticação ---
    token = input(f"🔑 Cole seu token de autenticação para {provider_key.capitalize()}: ").strip()
    if not provider.authenticate(token):
        logger.error("❌ Falha na autenticação. Verifique seu token e tente novamente.")
        sys.exit(1)

    # --- Seleção do Curso ---
    courses = provider.get_courses()
    if not courses:
        logger.error("❌ Nenhum curso encontrado para esta conta.")
        sys.exit(1)

    print("\n=== Cursos Disponíveis ===")
    for i, c in enumerate(courses, 1):
        print(f"{i}. {c.name}")

    course_idx = ask_int("\nSelecione o curso para baixar: ", 1, len(courses))
    course = courses[course_idx - 1]

    # --- Seleção de Módulos ---
    modules = provider.get_modules(course)
    if not modules:
        logger.error(f"❌ Nenhum módulo encontrado para o curso '{course.name}'.")
        sys.exit(1)

    print("\n=== Módulos do Curso ===")
    print("0. BAIXAR TODOS OS MÓDULOS")
    for i, m in enumerate(modules, 1):
        print(f"{i}. {m.name}")

    module_choice = ask_int("\nSelecione o módulo (0 para todos): ", 0, len(modules))

    modules_to_download = modules if module_choice == 0 else [modules[module_choice - 1]]

    # --- Início do Download ---
    logger.info(f"🚀 Iniciando download para o curso: {course.name}")
    course_dir = DL_ROOT / clean(course.name)
    checkpoint = CheckpointManager(course.name)
    start_pos = checkpoint.load()

    start_time = time.time()
    total_lessons_processed = 0

    try:
        for mod_idx, module in enumerate(modules_to_download, 1):
            if mod_idx < start_pos["module"]:
                continue

            module_order_str = str(module.order).zfill(2)
            module_name = f"{module_order_str}. {clean(module.name)}"
            module_dir = course_dir / module_name

            print("\n" + "="*50)
            logger.info(f"📖 Processando Módulo {mod_idx}/{len(modules_to_download)}: {module.name}")
            print("="*50)

            lessons = provider.get_lessons(module)

            for lesson_idx, lesson in enumerate(lessons, 1):
                if mod_idx == start_pos["module"] and lesson_idx <= start_pos["lesson"]:
                    logger.info(f"   ⏭️ Pulando lição já processada: {lesson.name}")
                    continue

                lesson_order_str = str(lesson.order).zfill(2)
                lesson_name = f"{lesson_order_str}. {clean(lesson.name)}"
                lesson_dir = module_dir / lesson_name
                lesson_dir.mkdir(parents=True, exist_ok=True)

                logger.info(f"\n📚 Lição {lesson_idx}/{len(lessons)}: {lesson.name}")

                download_lesson_content(provider, course, lesson, lesson_dir)

                total_lessons_processed += 1
                checkpoint.save(mod_idx, lesson_idx)

    except KeyboardInterrupt:
        logger.warning("\n\n⏸️ DOWNLOAD INTERROMPIDO PELO USUÁRIO")
        logger.info("Execute o script novamente para retomar de onde parou.")
        sys.exit(0)

    # --- Finalização ---
    checkpoint.clear()
    total_time = time.time() - start_time
    logger.info("\n" + "*"*50)
    logger.info("🎉 DOWNLOAD COMPLETO! 🎉")
    logger.info(f"⏱️ Tempo total: {total_time/60:.1f} minutos")
    logger.info(f"📁 Seus arquivos estão em: {course_dir.resolve()}")
    logger.info("*"*50)

if __name__ == "__main__":
    main()
