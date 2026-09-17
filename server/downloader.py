"""Download engine: the fallback chain (yt-dlp → direct file → yt-dlp
metadata without formats → HTML scrape) plus a small hand-rolled priority
work queue that plays the role QThreadPool played in the desktop version —
a fixed concurrency limit, the ability to jump a queued item to the front
("prioritize"), and pause/resume by leaving a task's state around and
re-submitting it.

No Qt here: progress goes out through events.bus.publish(), which is
thread-safe and gets picked up by whichever browser tabs are connected.
"""
from __future__ import annotations

import itertools
import logging
import mimetypes
import queue
import re
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import gallery_engine
import yt_dlp
from errors import is_permanently_unavailable, is_transient
from events import bus
from ffmpeg_provider import get_ffmpeg_path
from quiet_logger import SILENT

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DownloaderDeMidias/2.0"

# Tentativas automáticas para erros que parecem passageiros (rede caiu no
# meio, DNS falhou, 429 temporário) — cada posição é quantos segundos
# esperar antes daquela tentativa. Depois de esgotar, vira erro normal.
RETRY_BACKOFF_SECONDS = (5, 20, 60)

# Separados em dois grupos de propósito: um link de vídeo real é sempre um
# resultado válido para o scraper de HTML, mas um link de *imagem* só deve
# ser aceito como "o download" quando o site nem tem extractor no yt-dlp —
# caso contrário isso mascara uma falha real (ex.: YouTube pedindo login)
# como se tivesse baixado o vídeo, entregando só a thumbnail. Ver
# `_looks_like_recognized_site` e o parâmetro `allow_image_only` abaixo.
_HTML_VIDEO_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:video(?::url)?["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<video[^>]+src=["\']([^"\']+)', re.I),
    re.compile(r'<source[^>]+src=["\']([^"\']+)', re.I),
]
_HTML_IMAGE_PATTERNS = [
    re.compile(r'<meta[^>]+property=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)', re.I),
]


class _Cancelled(Exception):
    pass


class DownloadTask:
    """A single download job. `run()` is called by the manager's dispatcher
    thread; state (dest path, cancel flag) survives between pause/resume
    since it's the same Python object being re-submitted, not re-created."""

    def __init__(self, task_id: str, url: str, output_dir: str, extra_opts: dict[str, Any]):
        self.task_id = task_id
        self.url = url
        self._cancelled = False
        self._cancel_reason: str | None = None
        self._output_dir = output_dir
        self._dest_path: Path | None = None
        self._auto_retries = 0
        self._opts: dict[str, Any] = {
            "outtmpl": str(Path(output_dir) / "%(title)s.%(ext)s"),
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "logger": SILENT,
            "progress_hooks": [self._on_progress],
            **extra_opts,
        }
        ffmpeg_path = get_ffmpeg_path()
        if ffmpeg_path:
            self._opts.setdefault("ffmpeg_location", ffmpeg_path)
        self._sync_gallery_dl_auth()

    def _sync_gallery_dl_auth(self) -> None:
        """Deriva cookies_file/cookies_from_browser (formato do
        gallery_engine) a partir das MESMAS opções que settings.
        ytdlp_auth_opts() já colocou em self._opts para o yt-dlp — assim
        os dois motores sempre usam a mesma sessão logada, sem precisar de
        um segundo parâmetro/canal de configuração separado. Chamado do
        __init__ e de novo no fim de update_opts(), pelo mesmo motivo que
        o comentário lá explica: um "Tentar de novo" manual depois de
        configurar cookies deve valer pros dois motores, não só pro
        yt-dlp."""
        self._gallery_dl_cookies_file: str = self._opts.get("cookiefile") or ""
        browser = self._opts.get("cookiesfrombrowser")
        # Mesma exclusividade de settings.ytdlp_auth_opts(): cookiefile
        # manda quando os dois estiverem presentes.
        self._gallery_dl_cookies_from_browser: str = (
            "" if self._gallery_dl_cookies_file else (browser[0] if browser else "")
        )

    @property
    def dest_path(self) -> Path | None:
        return self._dest_path

    def pause(self) -> None:
        self._cancel_reason = "pause"
        self._cancelled = True

    def cancel(self) -> None:
        self._cancel_reason = "remove"
        self._cancelled = True

    def reset(self) -> None:
        self._cancelled = False
        self._cancel_reason = None

    def update_opts(self, opts: dict[str, Any]) -> None:
        """Atualiza opções do yt-dlp (ex.: cookies) antes de uma nova
        tentativa — usado por DownloadManager.resume quando quem chamou
        passa `extra_opts` frescos (ver api.py: resume/retry_failed
        reaplicam settings.ytdlp_auth_opts() atual, não o que existia
        quando a task foi criada). Só deve ser chamado com a task parada
        (pausada/erro): `_opts` não tem lock próprio porque nada mais lê
        ou escreve nele fora do `run()` de um download ativo."""
        self._opts.update(opts)
        self._sync_gallery_dl_auth()

    # -------------------------------------------------------------- run
    def run(self) -> float | None:
        """Executa o download. Retorna None quando terminou de vez (sucesso
        ou falha definitiva), ou um timestamp (`time.time()`-like) de quando
        o DownloadManager deve tentar de novo sozinho."""
        logger.info("Iniciando download (%s): %s", self.task_id, self.url)
        bus.publish("status", task_id=self.task_id, status="downloading")

        if gallery_engine.is_supported(self.url) and gallery_engine.available():
            try:
                if self._try_gallery_dl():
                    return None
            except Exception as exc:
                # Motor novo — nunca deixa um bug aqui derrubar o download;
                # sempre dá pra cair pro caminho de sempre (yt-dlp + fallbacks).
                logger.warning("gallery-dl deu erro inesperado para %s: %s", self.task_id, exc)

        try:
            with yt_dlp.YoutubeDL(self._opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
            final_path = self._resolve_final_path(ydl, info)
            if final_path:
                self._dest_path = Path(final_path)
                bus.publish("file_ready", task_id=self.task_id, path=final_path)
        except _Cancelled:
            self._emit_stopped()
            return None
        except Exception as exc:
            if self._cancelled:
                self._emit_stopped()
                return None
            logger.warning("yt-dlp falhou para %s (%s): %s", self.task_id, self.url, exc)
            message = str(exc)

            if is_permanently_unavailable(message):
                # Vídeo removido/banido/etc — nenhum fallback vai achar
                # nada aqui, então nem tenta: economiza dois round-trips de
                # rede (probe de metadados + scrape de HTML) que só iam
                # falhar de qualquer jeito.
                logger.info("%s: conteúdo indisponível permanentemente, pulando fallback.", self.task_id)
            else:
                recognized = self._looks_like_recognized_site(message)
                if self._try_fallbacks(allow_image_only=not recognized):
                    logger.info("Fallback funcionou para %s", self.task_id)
                    bus.publish("finished", task_id=self.task_id, success=True, message="")
                    return None

            retry_at = self._maybe_schedule_retry(message)
            if retry_at is not None:
                return retry_at

            logger.error("Todas as estratégias falharam para %s: %s", self.task_id, exc)
            bus.publish("finished", task_id=self.task_id, success=False, message=message)
            return None
        logger.info("Download %s concluído", self.task_id)
        bus.publish("finished", task_id=self.task_id, success=True, message="")
        return None

    def _try_gallery_dl(self) -> bool:
        """Tenta o motor gallery-dl (Instagram/Pinterest — ver
        gallery_engine.SUPPORTED_HOST_HINTS) antes do yt-dlp para esses
        sites, onde historicamente se sai melhor: pega a mídia na
        resolução original em vez de cair no scraper de HTML genérico, e
        um post com várias mídias (carrossel do Instagram, ou um board do
        Pinterest que não passou pelo expand de playlist.py) baixa tudo
        numa chamada só — ver o loop de file_ready abaixo.

        Retorna True quando já publicou o resultado final (sucesso ou
        cancelamento) e `run()` deve parar por aqui. Retorna False quando
        não achou nada (site pediu login, erro, ou simplesmente não tinha
        nada pra esse motor) — `run()` segue pro yt-dlp exatamente como
        fazia antes desse motor existir, sem publicar nada nesse meio
        tempo (o eventual erro final continua vindo só do yt-dlp, pra não
        duplicar/confundir mensagem de erro)."""
        files_seen = 0

        def _on_file(path: str) -> None:
            nonlocal files_seen
            files_seen += 1
            self._dest_path = Path(path)
            bus.publish("file_ready", task_id=self.task_id, path=path)

        def _on_progress(count: int) -> None:
            # Sem total conhecido de antemão (não há uma listagem prévia —
            # baixa direto), então o percentual é só decorativo, pra dar
            # sensação de progresso; nunca chega a 100 sozinho, só quando
            # o download de fato termina com sucesso logo abaixo.
            bus.publish(
                "progress", task_id=self.task_id, percent=min(90, 15 * count),
                text=f"{count} arquivo(s) baixado(s)...",
            )

        outcome = gallery_engine.download(
            self.url, self._output_dir,
            cookies_file=self._gallery_dl_cookies_file,
            cookies_from_browser=self._gallery_dl_cookies_from_browser,
            on_file=_on_file, on_progress=_on_progress,
            should_cancel=lambda: self._cancelled,
        )

        if outcome.cancelled:
            self._emit_stopped()
            return True

        if not outcome.handled:
            if outcome.login_required:
                logger.info(
                    "gallery-dl pediu login para %s (%s) — tentando yt-dlp em seguida.",
                    self.task_id, self.url,
                )
            return False

        logger.info("gallery-dl concluiu %s: %d arquivo(s)", self.task_id, files_seen)
        bus.publish("finished", task_id=self.task_id, success=True, message="")
        return True

    def _maybe_schedule_retry(self, message: str) -> float | None:
        """Decide se vale tentar de novo sozinho porque o erro parece
        passageiro (rede caiu, DNS falhou, 429 temporário) — até
        len(RETRY_BACKOFF_SECONDS) vezes, com espera crescente. Retorna o
        horário (epoch) da próxima tentativa, ou None se não for o caso."""
        if self._cancelled or not is_transient(message):
            return None
        if self._auto_retries >= len(RETRY_BACKOFF_SECONDS):
            return None
        delay = RETRY_BACKOFF_SECONDS[self._auto_retries]
        self._auto_retries += 1
        logger.info(
            "%s: erro parece passageiro — tentativa automática %d/%d em %ds (%s)",
            self.task_id, self._auto_retries, len(RETRY_BACKOFF_SECONDS), delay, message,
        )
        return time.time() + delay

    def _emit_stopped(self) -> None:
        message = "pausado" if self._cancel_reason == "pause" else "cancelado"
        logger.info("Download %s parado (%s)", self.task_id, message)
        bus.publish("finished", task_id=self.task_id, success=False, message=message)

    @staticmethod
    def _looks_like_recognized_site(message: str) -> bool:
        """True quando o yt-dlp reconheceu um extractor específico para essa
        URL mas mesmo assim falhou (precisa de login, vídeo removido, region
        lock etc.) — diferente de simplesmente não conhecer o site.

        Isso decide se é seguro aceitar uma *imagem* (thumbnail) encontrada
        pelo scraper de HTML como "o download". Para um site desconhecido do
        yt-dlp, uma imagem pode legitimamente ser o conteúdo (ex.: página
        de blog, galeria). Para um site que o yt-dlp reconhece como fonte de
        vídeo/áudio (YouTube, Vimeo etc.), aceitar uma imagem no lugar seria
        enganoso: a pessoa pediu um vídeo e receberia uma miniatura sem
        aviso nenhum, com o card mostrando "Concluído" normalmente.
        """
        return "unsupported url" not in message.lower()

    @staticmethod
    def _resolve_final_path(ydl: "yt_dlp.YoutubeDL", info: dict | None) -> str | None:
        if not info:
            return None
        try:
            requested = info.get("requested_downloads") or []
            if requested:
                path = requested[0].get("filepath") or requested[0].get("_filename")
                if path:
                    return path
            return ydl.prepare_filename(info)
        except Exception:
            return None

    # --------------------------------------------------------- fallbacks
    def _try_fallbacks(self, allow_image_only: bool) -> bool:
        bus.publish("status", task_id=self.task_id, status="processando")
        # Preserva o que já existia em _dest_path *antes* desta chamada (um
        # arquivo parcial legítimo de uma pausa/retomada anterior, se for o
        # caso) e restaura esse valor entre cada estratégia. Sem isso, se a
        # 1ª estratégia baixar alguns bytes de uma URL e falhar no meio, o
        # _dest_path fica "sujo" apontando pro arquivo/extensão errado, e a
        # 2ª estratégia (URL completamente diferente) tentaria retomar por
        # cima dele — misturando conteúdo de duas fontes distintas no mesmo
        # arquivo final.
        original_dest_path = self._dest_path
        strategies = (
            self._download_direct_link,
            lambda: self._download_via_metadata(allow_image_only),
            lambda: self._download_via_html_scrape(allow_image_only),
        )
        for strategy in strategies:
            if self._cancelled:
                return False
            self._dest_path = original_dest_path
            try:
                if strategy():
                    return True
            except Exception:
                continue
        self._dest_path = original_dest_path
        return False

    def _download_direct_link(self) -> bool:
        return self._download_direct_file(self.url)

    def _download_via_metadata(self, allow_image_only: bool) -> bool:
        probe_opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": SILENT,
            "skip_download": True,
            "ignore_no_formats_error": True,
        }
        try:
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
        except Exception:
            return False
        if not info:
            return False
        media_url = info.get("url")
        if not media_url and allow_image_only:
            media_url = info.get("thumbnail")
        if not media_url:
            return False
        return self._download_direct_file(media_url)

    def _download_via_html_scrape(self, allow_image_only: bool) -> bool:
        try:
            request = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                content_type = response.headers.get_content_type()
                if "html" not in content_type:
                    return False
                html = response.read(2_000_000).decode("utf-8", errors="ignore")
        except Exception:
            return False

        patterns = _HTML_VIDEO_PATTERNS + (_HTML_IMAGE_PATTERNS if allow_image_only else [])
        for pattern in patterns:
            match = pattern.search(html)
            if not match:
                continue
            media_url = urljoin(self.url, match.group(1))
            if self._download_direct_file(media_url):
                return True
        if not allow_image_only and any(p.search(html) for p in _HTML_IMAGE_PATTERNS):
            logger.info(
                "%s: só achei uma imagem (thumbnail) na página, não o vídeo/áudio — "
                "site é reconhecido pelo yt-dlp, então isso não conta como sucesso "
                "(provável bloqueio de login; configure cookies para resolver).",
                self.task_id,
            )
        return False

    # ---------------------------------------------------------- transfer
    def _download_direct_file(self, url: str) -> bool:
        resume_from = 0
        if self._dest_path is not None and self._dest_path.exists():
            resume_from = self._dest_path.stat().st_size

        # Referer = página de origem: muitos CDNs (não só YouTube) recusam
        # com 403 um pedido de mídia sem um Referer que bata com o site
        # que "deveria" ter enviado o pedido (proteção contra hotlink).
        # Simples de simular já que sempre sabemos a página original
        # (self.url), mesmo quando a mídia em si está em outro domínio.
        headers = {"User-Agent": USER_AGENT, "Referer": self.url}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        try:
            request = urllib.request.Request(url, headers=headers)
            response = urllib.request.urlopen(request, timeout=30)
        except Exception:
            return False

        content_type = response.headers.get_content_type()
        if not content_type or content_type in ("text/html", "application/xhtml+xml"):
            response.close()
            return False

        status = getattr(response, "status", None) or response.getcode()
        if resume_from and status != 206:
            resume_from = 0

        return self._stream_to_file(response, url, content_type, resume_from)

    def _stream_to_file(self, response, source_url: str, content_type: str, resume_from: int) -> bool:
        if self._dest_path is None:
            ext = (
                mimetypes.guess_extension(content_type)
                or Path(urlparse(source_url).path).suffix
                or ""
            ).lstrip(".") or "bin"
            name_hint = Path(urlparse(source_url).path).stem or self.task_id
            safe_name = re.sub(r'[\\/:*?"<>|]', "_", name_hint)[:150] or self.task_id
            self._dest_path = _unique_path(Path(self._output_dir) / f"{safe_name}.{ext}")

        dest = self._dest_path
        mode = "ab" if resume_from else "wb"
        remaining = int(response.headers.get("Content-Length") or 0)
        total = (resume_from + remaining) if remaining else 0
        downloaded = resume_from

        try:
            with open(dest, mode) as file:
                while True:
                    if self._cancelled:
                        raise _Cancelled()
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    file.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        bus.publish(
                            "progress", task_id=self.task_id,
                            percent=downloaded / total * 100, text="",
                        )
        except _Cancelled:
            return False
        except Exception:
            if downloaded == resume_from:
                dest.unlink(missing_ok=True)
            return False
        finally:
            response.close()

        bus.publish("file_ready", task_id=self.task_id, path=str(dest))
        return True

    def _on_progress(self, data: dict[str, Any]) -> None:
        if self._cancelled:
            raise _Cancelled()
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes") or 0
            percent = (done / total * 100) if total else 0.0
            text = _human_rate(data.get("speed"))
            eta = data.get("eta")
            if eta:
                text = f"{text} • ETA {_human_eta(eta)}" if text else f"ETA {_human_eta(eta)}"
            bus.publish("progress", task_id=self.task_id, percent=percent, text=text)
        elif status == "finished":
            bus.publish("status", task_id=self.task_id, status="processando")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _human_eta(seconds) -> str:
    if not seconds or seconds < 0:
        return ""
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _human_rate(bytes_per_second) -> str:
    if not bytes_per_second:
        return ""
    value = bytes_per_second
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB/s"


class DownloadManager:
    """A fixed-size worker pool with real priority scheduling, built on a
    plain `queue.PriorityQueue` — Python's ThreadPoolExecutor doesn't
    support reordering or dropping already-queued work, which "prioritize"
    and "pause" both need."""

    def __init__(self, max_concurrent: int = 3):
        self._tasks: dict[str, DownloadTask] = {}
        self._queue: "queue.PriorityQueue[tuple[int, int, str]]" = queue.PriorityQueue()
        self._pending: dict[str, tuple[int, int]] = {}  # task_id -> (priority, seq)
        self._scheduled: dict[str, float] = {}  # task_id -> epoch quando deve começar
        self._counter = itertools.count()
        self._priority_counter = 0
        self._max_concurrent = max(1, max_concurrent)
        self._active = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._shutdown = False
        self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher.start()

    def set_max_concurrent(self, value: int) -> None:
        with self._cv:
            self._max_concurrent = max(1, value)
            self._cv.notify_all()

    def create_task(self, url: str, output_dir: str, extra_opts: dict[str, Any]) -> tuple[str, DownloadTask]:
        task_id = uuid.uuid4().hex[:8]
        task = DownloadTask(task_id, url, output_dir, extra_opts)
        with self._lock:
            self._tasks[task_id] = task
        return task_id, task

    def start(self, task: DownloadTask) -> None:
        self._enqueue(task.task_id, priority=0)

    def schedule(self, task_id: str, run_at: float, status_label: str = "agendado") -> None:
        """Coloca a tarefa pra começar sozinha quando `run_at` (epoch,
        `time.time()`) chegar, sem enfileirar agora. Usado tanto pelo
        agendamento manual (status "agendado") quanto pelo retry automático
        de erros passageiros (status "reagendando") — só muda o rótulo."""
        with self._cv:
            self._scheduled[task_id] = run_at
            self._cv.notify_all()
        bus.publish("status", task_id=task_id, status=status_label, scheduled_for=run_at)

    def start_now(self, task_id: str) -> bool:
        """Pula a espera de um item agendado/em retry e começa na hora."""
        with self._cv:
            if task_id not in self._scheduled:
                return False
            del self._scheduled[task_id]
            self._enqueue_locked(task_id, priority=0)
            self._cv.notify_all()
        return True

    def prioritize(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._pending:
                return False
            self._priority_counter += 1
            priority = self._priority_counter
        self._enqueue(task_id, priority=priority)
        return True

    def pause(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if not task:
            return
        with self._lock:
            was_pending = self._pending.pop(task_id, None) is not None
            was_scheduled = self._scheduled.pop(task_id, None) is not None
        if was_pending or was_scheduled:
            bus.publish("finished", task_id=task_id, success=False, message="pausado")
        else:
            task.pause()  # already running; it'll stop itself and emit "pausado"

    def resume(self, task_id: str, extra_opts: dict[str, Any] | None = None) -> None:
        """`extra_opts`, quando passado, é aplicado por cima das opções
        que a task já tinha (ver DownloadTask.update_opts) antes de
        reenfileirar — é o que faz um "Tentar de novo" manual pegar
        cookies configurados *depois* que o item original falhou, em vez
        de repetir o mesmo erro de login com as opções antigas
        congeladas na criação da task."""
        task = self._tasks.get(task_id)
        if not task:
            return
        if extra_opts:
            task.update_opts(extra_opts)
        task.reset()
        self._enqueue(task_id, priority=0)

    def remove(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if not task:
            return
        with self._lock:
            self._pending.pop(task_id, None)
            self._scheduled.pop(task_id, None)
        task.cancel()
        if task.dest_path and task.dest_path.exists():
            try:
                task.dest_path.unlink()
            except OSError:
                pass

    def forget(self, task_id: str) -> None:
        """Drops a finished task's reference without touching its file —
        for clearing rows that already completed successfully or failed."""
        with self._lock:
            self._tasks.pop(task_id, None)
            self._pending.pop(task_id, None)
            self._scheduled.pop(task_id, None)

    # ------------------------------------------------------------- internals
    def _enqueue_locked(self, task_id: str, priority: int) -> None:
        """Mesma coisa que _enqueue, mas assume que quem chamou já está
        segurando self._cv — existe pra ser usada de dentro de
        _promote_due_scheduled_locked/start_now sem dar deadlock num lock
        não-reentrante."""
        seq = next(self._counter)
        self._pending[task_id] = (priority, seq)
        self._queue.put((-priority, seq, task_id))

    def _enqueue(self, task_id: str, priority: int) -> None:
        with self._cv:
            self._enqueue_locked(task_id, priority)
            self._cv.notify_all()

    def _promote_due_scheduled_locked(self) -> None:
        """Assume que self._cv já está travado. Move pra fila normal
        qualquer item agendado/em retry cujo horário já chegou."""
        if not self._scheduled:
            return
        now = time.time()
        due = [task_id for task_id, run_at in self._scheduled.items() if run_at <= now]
        for task_id in due:
            del self._scheduled[task_id]
            if task_id in self._tasks:
                self._enqueue_locked(task_id, priority=0)

    def _dispatch_loop(self) -> None:
        while not self._shutdown:
            with self._cv:
                self._promote_due_scheduled_locked()
                while not self._shutdown and (self._active >= self._max_concurrent or self._queue.empty()):
                    self._cv.wait(timeout=0.5)
                    self._promote_due_scheduled_locked()
                if self._shutdown:
                    return
                try:
                    _, seq, task_id = self._queue.get_nowait()
                except queue.Empty:
                    continue
                current = self._pending.get(task_id)
                if not current or current[1] != seq:
                    continue  # stale entry: task was re-queued, paused, or removed since
                self._pending.pop(task_id, None)
                task = self._tasks.get(task_id)
                if task is None:
                    continue
                self._active += 1
            threading.Thread(target=self._run_task, args=(task,), daemon=True).start()

    def _run_task(self, task: DownloadTask) -> None:
        retry_at = None
        try:
            retry_at = task.run()
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify_all()
        if retry_at is not None:
            self.schedule(task.task_id, retry_at, status_label="reagendando")
