# course_downloader/providers/base_provider.py
from __future__ import annotations
import abc
from typing import List, Optional
import requests

from course_downloader.core.models import Course, Module, Lesson

"""
Este arquivo define o "contrato" que todo provedor de plataforma deve seguir.
A classe BaseProvider é uma classe base abstrata (ABC). Ela define os métodos
que uma classe de provedor concreta (como HotmartProvider) DEVE implementar.

Isso garante que o orquestrador principal (`main.py`) possa interagir com qualquer
provedor da mesma maneira, sem precisar conhecer os detalhes internos de cada um.
"""

class BaseProvider(abc.ABC):
    """
    Classe base abstrata para provedores de plataformas de curso.
    """

    def __init__(self):
        self.session: Optional[requests.Session] = None

    @abc.abstractmethod
    def authenticate(self, token: str) -> bool:
        """
        Autentica na plataforma usando o token/credencial fornecido.
        Deve criar e configurar uma `requests.Session` e armazená-la em `self.session`.
        Retorna True se a autenticação for bem-sucedida, False caso contrário.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_courses(self) -> List[Course]:
        """
        Busca e retorna a lista de cursos disponíveis para o usuário.
        Os objetos Course retornados não precisam ter os módulos preenchidos.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_modules(self, course: Course) -> List[Module]:
        """
        Busca e retorna a lista de módulos para um determinado curso.
        Os objetos Module retornados não precisam ter as lições preenchidas.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_lessons(self, module: Module) -> List[Lesson]:
        """
        Busca e retorna a lista de lições para um determinado módulo.
        Os objetos Lesson retornados não precisam ter os detalhes (vídeos, anexos) preenchidos.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def get_lesson_details(self, lesson: Lesson, course: Course) -> None:
        """
        Busca os detalhes de uma lição específica (vídeos, anexos, descrição).
        Este método deve MODIFICAR o objeto `lesson` passado como argumento,
        preenchendo os campos `videos`, `attachments`, `description`, etc.
        """
        raise NotImplementedError
