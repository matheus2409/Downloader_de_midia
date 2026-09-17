"""Segundo motor de download, ao lado do yt-dlp: usa o gallery-dl (projeto
irmão do yt-dlp — mesmo espírito, mas focado em posts/galerias de imagem em
vez de vídeo) para os sites onde ele historicamente se sai melhor. Hoje:
Instagram e Pinterest — ver SUPPORTED_HOST_HINTS. Some outros sites "estilo
galeria" (Twitter/X, Tumblr, DeviantArt, Flickr...) ali quando fizer sentido;
o resto deste módulo já é genérico por URL, não tem nada específico de
Instagram/Pinterest espalhado pelo meio da lógica.

Por que subprocess em vez de chamar a biblioteca do gallery-dl direto (do
jeito que downloader.py chama yt_dlp.YoutubeDL)? O jeito "certo" de acompanhar
progresso pela API Python do gallery-dl é o sistema interno de hooks/
postprocessors — mas isso não é uma API pública documentada, e amarrar o app
nisso é o mesmo tipo de acoplamento a "detalhes internos que podem mudar
entre versões" que já quebrou o expansor de pastas do Pinterest antigo (ver
playlist.py e o comentário grande em cima de _expand_pinterest_board). Já a
flag `--Print evento:formato` é CLI documentada e estável — é o mesmo
mecanismo por baixo, só que por uma porta que o próprio gallery-dl promete
manter entre versões. Roda como `sys.executable -m gallery_dl` (não como
binário solto no PATH) pra sempre usar o mesmo interpretador/venv onde
`pip install -r requirements.txt` colocou o pacote.

Duas operações:
- download(): baixa uma URL — um post, um pin, OU uma coleção inteira
  (perfil, hashtag, board) que o gallery-dl souber expandir sozinho — para
  uma pasta, reportando cada arquivo conforme termina. Um carrossel/álbum
  vira vários arquivos numa chamada só; ver downloader.py sobre como isso
  vira múltiplos eventos file_ready para a mesma tarefa.
- list_items(): só enumera (sem baixar) os itens de uma URL de coleção,
  devolvendo uma URL por item. Usado por playlist.py para dar a um board do
  Pinterest o mesmo tratamento "um item, uma linha na fila" que os
  playlists do yt-dlp já têm — e só para Pinterest: as URLs de mídia do
  Instagram costumam levar um token assinado com validade curta, então uma
  coleção de lá é baixada inteira dentro de uma única chamada a download()
  em vez de virar N tarefas separadas que podem ficar pausadas/agendadas
  por tempo suficiente pra URL expirar antes de chegar a vez delas.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import queue as queue_module
import subprocess
import sys
import threading
from typing import Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

INSTAGRAM_HOST_HINTS = ("instagram.com",)
PINTEREST_HOST_HINTS = ("pinterest.", "pin.it")
SUPPORTED_HOST_HINTS = INSTAGRAM_HOST_HINTS + PINTEREST_HOST_HINTS

# Prefixos que diferenciam, numa mesma linha de stdout, um arquivo baixado
# com sucesso de um que falhou (ver download()). Improvável colidir com
# conteúdo real -- um caminho de arquivo nunca começa assim.
_OK_PREFIX = "GDL_OK\t"
_ERR_PREFIX = "GDL_ERR\t"

# Bits do código de saída do gallery-dl (ver gallery_dl.exception -- cada
# classe de erro tem um `code` que entra em OR no status final do job).
# Documentados aqui à mão porque não tem como importar só essas duas
# constantes sem carregar o pacote inteiro.
_STATUS_AUTH_REQUIRED = 16
_STATUS_CHALLENGE = 8


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """True quando o pacote gallery-dl está instalado neste interpretador.
    Cacheado: isso é consultado a cada URL nova adicionada à fila, e checar
    não deveria pagar o custo de um import pesado toda vez."""
    import importlib.util

    return importlib.util.find_spec("gallery_dl") is not None


def is_supported(url: str) -> bool:
    """True quando `url` bate com um domínio onde preferimos o gallery-dl
    ao yt-dlp. Checagem simples de host, no mesmo espírito de
    playlist._looks_like_pinterest -- de propósito NÃO usa o matching de
    extractors do próprio gallery-dl aqui, pra não pagar import + regex de
    ~100 extractors numa URL que talvez nem seja roteada pra cá."""
    host = urlparse(url).netloc.lower()
    return any(hint in host for hint in SUPPORTED_HOST_HINTS)


def installed_version() -> str | None:
    """Versão instalada do gallery-dl, para o mesmo tipo de checagem de
    "desatualizado" que health.py já faz para o yt-dlp. Import feito aqui
    dentro (não no topo do módulo) pelo mesmo motivo do `import yt_dlp` em
    health.py: este módulo continua importável mesmo sem o pacote
    instalado -- available() é que deve ser consultado antes de chamar
    download()/list_items()."""
    try:
        import gallery_dl

        return gallery_dl.__version__
    except Exception:
        return None


def _auth_args(cookies_file: str, cookies_from_browser: str) -> list[str]:
    """Mesma prioridade de login que settings.ytdlp_auth_opts() já usa para
    o yt-dlp -- cookies_file primeiro. O Instagram em particular fica bem
    mais confiável autenticado: sem login o gallery-dl ainda funciona para
    posts públicos, mas esbarra em limite de pedidos mais cedo."""
    if cookies_file:
        return ["-C", cookies_file]
    if cookies_from_browser:
        return ["--cookies-from-browser", cookies_from_browser]
    return []


class GalleryDlOutcome:
    """Resultado de download(). `handled` (ou seja, "encontrei pelo menos 1
    arquivo") decide se downloader.py encerra a tarefa por aqui ou deixa
    cair pro caminho antigo (yt-dlp + fallbacks) normalmente -- ver
    downloader.DownloadTask.run()."""

    def __init__(self) -> None:
        self.files: list[str] = []
        self.failed_count: int = 0
        self.login_required: bool = False
        self.cancelled: bool = False
        self.message: str = ""

    @property
    def handled(self) -> bool:
        return bool(self.files)


def _reader_thread(pipe, out_queue: "queue_module.Queue[str | None]") -> None:
    try:
        for line in iter(pipe.readline, ""):
            out_queue.put(line)
    finally:
        out_queue.put(None)  # sentinela: pipe fechou, processo terminou
        pipe.close()


def download(
    url: str,
    output_dir: str,
    *,
    cookies_file: str = "",
    cookies_from_browser: str = "",
    on_file: Callable[[str], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> GalleryDlOutcome:
    """Baixa `url` -- um post/pin único, ou uma coleção inteira que o
    gallery-dl souber expandir sozinho (perfil, hashtag, board) -- para
    `output_dir`. Bloqueante, no mesmo padrão de DownloadTask.run(): espera
    rodar dentro da thread própria daquele download.

    `on_file(caminho)` é chamado uma vez por arquivo concluído -- pode ser
    mais de um, um álbum/carrossel gera vários na mesma chamada.
    `on_progress(n)` é chamado junto, com a contagem corrente, pra quem
    quiser atualizar um texto tipo "N arquivo(s) baixado(s)...".
    `should_cancel()` é consultado periodicamente (a cada ~0.5s); quando
    virar True, o processo é encerrado e outcome.cancelled fica True --
    qualquer arquivo que já tiver terminado de verdade antes disso continua
    valendo em outcome.files.
    """
    outcome = GalleryDlOutcome()

    before: set[str] = set()
    try:
        before = set(os.listdir(output_dir))
    except OSError:
        pass

    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--no-colors",
        "-D", output_dir,
        "--Print", f"after:{_OK_PREFIX}{{_path}}",
        "--Print", f"error:{_ERR_PREFIX}{{filename}}",
        *_auth_args(cookies_file, cookies_from_browser),
        url,
    ]

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        outcome.message = f"Não consegui iniciar o gallery-dl: {exc}"
        return outcome

    stdout_queue: "queue_module.Queue[str | None]" = queue_module.Queue()
    threading.Thread(
        target=_reader_thread, args=(process.stdout, stdout_queue), daemon=True
    ).start()

    while True:
        if should_cancel is not None and should_cancel():
            outcome.cancelled = True
            process.terminate()
            break
        try:
            line = stdout_queue.get(timeout=0.5)
        except queue_module.Empty:
            continue
        if line is None:
            break
        line = line.rstrip("\n")
        if line.startswith(_OK_PREFIX):
            path = line[len(_OK_PREFIX):].strip()
            if path:
                outcome.files.append(path)
                if on_file:
                    on_file(path)
                if on_progress:
                    on_progress(len(outcome.files))
        elif line.startswith(_ERR_PREFIX):
            outcome.failed_count += 1

    if outcome.cancelled:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        try:
            process.stderr.close()
        except Exception:
            pass
        outcome.message = "cancelado"
        return outcome

    returncode = process.wait()
    try:
        stderr_tail = (process.stderr.read() or "").strip()[-800:]
    except Exception:
        stderr_tail = ""

    # Rede de segurança: se por algum motivo a linha "GDL_OK\t{_path}" não
    # veio (ex.: uma versão futura do gallery-dl mudar como o placeholder
    # `{_path}` se resolve), mas o processo terminou bem e apareceram
    # arquivos novos na pasta, conta esses -- nunca reporta "0 arquivos"
    # quando na verdade algo foi salvo em disco.
    if not outcome.files and (returncode == 0 or not returncode & (
        _STATUS_AUTH_REQUIRED | _STATUS_CHALLENGE
    )):
        try:
            after = set(os.listdir(output_dir))
        except OSError:
            after = set()
        novos = sorted(after - before)
        if novos:
            outcome.files = [os.path.join(output_dir, nome) for nome in novos]
            for caminho in outcome.files:
                if on_file:
                    on_file(caminho)
            if on_progress:
                on_progress(len(outcome.files))

    if outcome.files:
        if outcome.failed_count:
            logger.info(
                "gallery-dl: %s -> %d arquivo(s) ok, %d falharam",
                url, len(outcome.files), outcome.failed_count,
            )
        else:
            logger.info("gallery-dl: %s -> %d arquivo(s) ok", url, len(outcome.files))
        return outcome

    if returncode & _STATUS_AUTH_REQUIRED or returncode & _STATUS_CHALLENGE:
        outcome.login_required = True
        # Mesma frase que errors.is_login_required já reconhece (ver
        # errors.py) -- não é usada para decidir nada aqui dentro (essa
        # falha simplesmente cai pro caminho antigo do yt-dlp, ver
        # downloader.py), só ajuda a entender o app.log ao ler à mão.
        outcome.message = "gallery-dl: Sign in to confirm — este conteúdo exige login."
    else:
        outcome.message = stderr_tail or f"gallery-dl terminou com código {returncode}"
    logger.info("gallery-dl não encontrou nada para %s: %s", url, outcome.message)
    return outcome


def list_items(
    url: str,
    *,
    cookies_file: str = "",
    cookies_from_browser: str = "",
    timeout: float = 45.0,
) -> list[str] | None:
    """Enumera (sem baixar) os itens de uma URL de coleção -- usado hoje só
    para boards do Pinterest, ver playlist.py. Devolve None quando não deu
    pra extrair nada (URL não é uma coleção reconhecida, erro de rede,
    timeout etc.) -- quem chama trata isso como "não é o caso, tenta outra
    estratégia", no mesmo espírito de como o expand via yt-dlp já funciona.
    """
    cmd = [
        sys.executable, "-m", "gallery_dl",
        "--no-colors", "-j",
        *_auth_args(cookies_file, cookies_from_browser),
        url,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("gallery-dl -j falhou para %s: %s", url, exc)
        return None

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list):
        return None

    urls: list[str] = []
    seen: set[str] = set()
    for entry in data:
        # Cada item é uma tupla [tipo, url, kwdict] (ver
        # gallery_dl.extractor.message.Message / gallery_dl.job.DataJob).
        # Message.Url (3) = item de verdade, pronto para baixar.
        # Message.Queue (6) = delega para outro extractor (ex.: o board
        # aponta pra página do pin) -- também é uma URL válida de item pra
        # nós: quando essa URL virar uma tarefa própria mais tarde, uma
        # chamada nova a download() resolve e baixa ela na hora, o que é
        # inclusive mais seguro que resolver tudo de uma vez agora (sem
        # risco de um link assinado expirar enquanto a tarefa espera na
        # fila). Message.Directory (2) é só metadado da pasta, sem URL de
        # item -- ignorado.
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        msg_type, item_url = entry[0], entry[1]
        if msg_type not in (3, 6) or not isinstance(item_url, str):
            continue
        if item_url not in seen:
            seen.add(item_url)
            urls.append(item_url)

    return urls or None
