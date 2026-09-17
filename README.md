# Downloader de mídias

Baixe vídeos, imagens, áudios, documentos — e pastas/playlists inteiras de uma vez — a partir de um link. Roda como um **servidor local**: você abre no navegador, em `http://127.0.0.1:8765`, como se fosse mais uma aba, e ele continua baixando mesmo se você fechar essa aba (só para de verdade se você fechar o terminal onde ele está rodando).

Criado e mantido por [**matheus2409**](https://github.com/matheus2409).

> **Nota de segurança:** o servidor escuta só em `127.0.0.1` (sua própria máquina) — nunca fica acessível por outros dispositivos na rede. Isso é proposital: ele tem acesso de escrita ao seu disco, então não deve ficar exposto a mais ninguém.

## Por que virou um app web em vez de um programa de desktop

A versão anterior era um app PySide6 (janela própria, rodava "em segundo plano" no sentido de ser um processo separado do navegador). Essa versão troca isso por um servidor local + interface em HTML/CSS/JS, o que traz algumas vantagens diretas:

- Fecha a aba, os downloads continuam — o servidor é quem manda, o navegador só mostra o que está acontecendo.
- Reabre a aba (ou abre em outra), e o estado atual aparece na hora — nada se perde.
- Interface mais fácil de estender e de deixar com a sua cara, sem as limitações de layout do Qt.

## Requisitos

- [Python](https://www.python.org/downloads/) 3.10+
- **ffmpeg**: não precisa instalar nada — o `pip install` já traz um binário portátil embutido (via `imageio-ffmpeg`), baixado automaticamente no primeiro uso (precisa de internet nesse primeiro momento). Se você já tiver o ffmpeg instalado no sistema, o app usa o seu em vez do embutido.

## Instalação e uso

```bash
git clone https://github.com/matheus2409/Downloader-de-midias.git
cd Downloader-de-midias
pip install -r requirements.txt
python run.py
```

Isso abre `http://127.0.0.1:8765` automaticamente no seu navegador padrão. Deixe o terminal aberto — fechar ele derruba o servidor.

No Windows dá pra usar `run.bat` (duplo clique); no Linux/macOS, `./run.sh`.

## Funcionalidades

- **Download em massa**: cole vários links de uma vez, um por linha, arraste um `.txt`, ou importe pelo botão.
- **Pastas e playlists**: cole o link de uma playlist do YouTube/SoundCloud/Vimeo ou de uma **pasta do Pinterest** e o app expande sozinho pra cada item de dentro, baixando cada um na melhor qualidade — veja a seção "Instagram e Pinterest" abaixo. Um perfil ou hashtag do **Instagram**, ou um post com várias fotos (carrossel), também descem inteiros, mas como uma tarefa só.
- **Fila com limite configurável de simultâneos**, prioridade (furar a fila), pausar/retomar (retoma de onde parou quando o servidor de origem permite).
- **A fila sobrevive a um reinício do servidor** (`queue_state.json`): itens que ainda estavam esperando voltam a baixar sozinhos; itens que estavam baixando na hora do reinício voltam como "pausados"; itens agendados continuam agendados — um clique em "Retomar tudo" continua os pausados.
- **Ações em lote**: pausar tudo, retomar tudo, tentar de novo os que falharam, limpar concluídos.
- **Login/cookies** (botão 🔑 no topo) para sites que passaram a exigir conta pra liberar o download — ver seção própria abaixo.
- **Aviso proativo de saúde**: ao abrir a página, o app conta quantos downloads recentes falharam por exigir login e checa se o yt-dlp e/ou o gallery-dl instalados ficaram desatualizados (compara com o PyPI) — se algum desses for relevante, mostra um aviso no topo em vez de deixar a causa escondida em dezenas de erros individuais.
- **Legendas**: checkbox "🗨️ Legendas" baixa legenda em pt/en junto com o vídeo, quando o site tiver.
- **Agendamento**: "⏰ Agendar para depois" deixa escolher uma data/hora pro download começar sozinho — útil pra rodar de madrugada. Sobrevive a reinício do servidor.
- **Retentativa automática**: erro de rede passageiro (conexão caiu, timeout, HTTP 429) tenta de novo sozinho até 3 vezes com espera crescente (5s/20s/60s), sem precisar clicar em nada. Vídeo definitivamente indisponível (removido, banido) não entra nessa fila de retentativa — e nem gasta tempo tentando os fallbacks, já que eles nunca vão funcionar pra esse caso.
- **Busca/filtro** por título ou link, tanto na fila atual quanto no histórico.
- **Diagnóstico do histórico** (botão 🩺): acha downloads antigos que vieram como thumbnail em vez do conteúdo pedido (era o bug descrito na nota abaixo) e deixa re-baixar os selecionados de uma vez.
- **Integração via API** (botão 🔌): chave de API opcional pra proteger `POST /api/downloads`, e um webhook que avisa outra ferramenta (n8n, por exemplo) quando um download termina — ver seção própria.
- **Miniatura e tipo de mídia** por item (vídeo/áudio/imagem/documento).
- **Histórico** de tudo que já foi baixado, com atalho pra abrir a pasta.
- **Editor de presets** pela própria interface — cada preset é um JSON de opções do yt-dlp.
- **Seletor de pasta** com navegação pelas pastas do seu disco, direto pelo navegador.
- **Notificação do navegador** quando um lote termina (pede permissão na primeira vez).
- Detecção de link duplicado, aviso de pouco espaço em disco, mensagens de erro traduzidas, log em arquivo (`app.log`).

## O que ele consegue baixar

1. **gallery-dl** — só para **Instagram e Pinterest** (ver seção própria abaixo): pega a mídia na resolução original, e um post com várias mídias (carrossel do Instagram, board do Pinterest) baixa tudo de uma vez. Tentado antes do yt-dlp só para esses dois sites, porque historicamente é onde ele se sai melhor.
2. **yt-dlp** — cerca de 1800 sites de vídeo/áudio (e o que sobrar de Instagram/Pinterest se o gallery-dl não conseguir, ex.: pediu login).
3. **Link direto pro arquivo** — imagem, gif, vídeo, áudio, pdf, docx, zip etc.
4. **Metadados do yt-dlp sem exigir formato de vídeo** — pega a imagem/thumbnail mesmo sem "formato de vídeo", **só quando o site não tem extractor no yt-dlp** (ver nota abaixo).
5. **Raspagem leve de HTML** — como último recurso, procura `og:video`/`og:image`/`<video>` na página, com a mesma ressalva do item 4.

Isso não garante 100% dos links da internet — páginas com login, JavaScript pesado, ou proteção ativa contra scraping continuam sendo um problema sem solução mágica.

> **Nota (corrigido):** os itens 4 e 5 aceitavam qualquer imagem encontrada (ex.: `og:image`) como "sucesso", mesmo em sites que o yt-dlp reconhece de verdade (YouTube incluso). Na prática isso mascarava o bloqueio de login do YouTube: o download terminava marcado como concluído, mas o arquivo salvo era só a thumbnail, não o vídeo/áudio pedido — foi o que apareceu na análise do `app.log`. Agora uma imagem só é aceita como resultado final quando o site **não** é reconhecido pelo yt-dlp; num site reconhecido (YouTube, Vimeo etc.) que falhe, o erro real é mostrado em vez de uma miniatura disfarçada de sucesso.

## Login / cookies (sites que pedem conta)

Alguns sites — o YouTube é hoje o mais comum — passaram a bloquear downloads sem sessão logada, com um erro do tipo `Sign in to confirm you're not a bot`. O botão **🔑 Login/Cookies** no topo da página resolve isso de duas formas (escolha uma):

- **Arquivo cookies.txt** (mais confiável): exporte os cookies do site enquanto estiver logado no navegador, com uma extensão tipo "Get cookies.txt LOCALLY", e aponte o caminho do arquivo. Funciona com qualquer navegador.
- **Navegador instalado**: o yt-dlp lê o cookie storage direto do navegador (Chrome, Firefox, Edge, Brave, Opera, Vivaldi, Safari, Chromium ou Whale). Mais prático, mas exige fechar o navegador antes de baixar, e **não cobre variantes tipo Opera GX** — o yt-dlp trata como um navegador à parte e não lê o perfil dele. Pra Opera GX, use a opção de arquivo acima.

A configuração vale pra downloads adicionados depois de salva, e também é reaplicada quando você retoma um item pausado ou clica em **"Tentar de novo os que falharam"** — então dá pra configurar os cookies depois que uma leva de downloads já falhou por login, e simplesmente clicar em tentar de novo. O que continua não acontecendo é uma troca "ao vivo": um item que já está baixando neste exato momento não muda de cookie no meio do download, só na próxima tentativa.

Se mesmo com cookies configurados o erro persistir, o mais provável é o **yt-dlp estar desatualizado** — o YouTube muda a detecção de bot com frequência, e só versões novas do yt-dlp acompanham. Rode `pip install -U yt-dlp` de vez em quando. Ao abrir a página, o app já checa isso sozinho (comparando com a versão mais recente do PyPI) e também conta quantos downloads recentes falharam por login; se algum dos dois for relevante, aparece um aviso no topo com atalho direto pro botão de cookies. O mesmo tipo de checagem existe pro gallery-dl (ver seção abaixo) — o aviso mostra os dois, independentemente.

## Instagram e Pinterest (motor gallery-dl)

Esses dois sites usam um motor à parte, o **gallery-dl** (projeto irmão do yt-dlp, mesma ideia — mas focado em posts/galeria de imagem em vez de vídeo). Ele é tentado **antes** do yt-dlp só pra esses dois sites, porque historicamente é onde ele se sai melhor: pega a mídia na resolução original, e sabe lidar direito com posts de mídia múltipla — um carrossel do Instagram ou uma pasta do Pinterest colada direto (sem passar pelo expand em itens separados) baixam todas as fotos/vídeos de dentro numa única tarefa. Se o gallery-dl não conseguir nada (pediu login, erro, site fora do ar), a tarefa cai pro yt-dlp normalmente — sem diferença visível pra você, é só uma segunda tentativa nos bastidores.

Alguns detalhes que valem saber:

- **Um post = possivelmente vários arquivos.** Um carrossel do Instagram com 5 fotos gera 5 arquivos na pasta a partir de uma única tarefa na fila — o card mostra só o último arquivo que chegou, mas todos ficam salvos, e cada um vira uma linha própria no histórico.
- **Perfil/hashtag do Instagram baixa tudo de uma vez**, sem virar N itens separados na fila (diferente de uma pasta do Pinterest — ver por quê no parágrafo debaixo). Pra um perfil grande isso pode demorar; a tarefa mostra "N arquivo(s) baixado(s)..." conforme vai indo.
- **Login ajuda bastante no Instagram.** Sem cookies configurados (mesmo botão 🔑 Login/Cookies do topo, mesmo arquivo `cookies.txt` que já vale pro yt-dlp) ele ainda funciona pra posts públicos, mas esbarra em limite de pedidos mais rápido — principalmente pra perfil/hashtag inteiros. Pra Pinterest normalmente nem precisa, a maioria dos boards é pública.
- **Por que uma pasta do Pinterest vira N itens separados na fila, mas um perfil do Instagram não:** as URLs de imagem do Pinterest (`i.pinimg.com/...`) são permanentes; as do Instagram costumam levar um token com validade curta (horas). Espalhar um perfil do Instagram em N tarefas que podem ficar pausadas/agendadas por um tempo arriscaria o link expirar antes da vez de baixar — por isso ele desce inteiro de uma vez, dentro de uma chamada só.

Se o gallery-dl estiver desatualizado, o mesmo tipo de aviso que já existe pro yt-dlp aparece no topo da página (`pip install -U gallery-dl` resolve).

## Sobre pastas do Pinterest (leia antes de reportar bug)

O Pinterest não expõe uma lista simples de pins de uma pasta — a página carrega os pins aos poucos via uma API interna, sem documentação pública, conforme você rola a tela. Hoje quem resolve isso é o gallery-dl (seção acima), que é mantido ativamente por fora e acompanha as mudanças do Pinterest sem depender de mim. Só se o **gallery-dl também** não conseguir é que entra em ação um scraper de último recurso escrito à mão (`server/playlist.py`, função `_expand_pinterest_board`) que imita aquela chamada interna diretamente — **essa sim é a parte mais frágil do projeto**: se o Pinterest mudar o formato interno dos dados, essa função para de funcionar até o gallery-dl (ou, na pior hipótese, eu) se atualizar.

Se isso acontecer com você — pasta do Pinterest não expande mesmo depois de `pip install -U gallery-dl` — abra o link da pasta no navegador, aperte F12 → aba **Network**, recarregue a página e role um pouco pra baixo, procure por uma chamada pra `BoardFeedResource`. Me manda o formato da resposta (JSON) que eu ajusto.

## Diagnóstico do histórico

O bug descrito na nota acima (imagem no lugar do vídeo/áudio) já está corrigido, mas pode ter deixado itens assim no seu histórico de antes da correção. O botão **🩺 Diagnóstico** cruza `history.json` com `app.log` pra achar exatamente esses casos — quando o log já rotacionou e não dá pra confirmar por ali, cai pra uma checagem mais simples (é de um site de vídeo conhecido, mas o resultado salvo foi imagem). Marque os que quiser corrigir e clique em "Re-baixar selecionados": ele reenvia os mesmos links pra fila, usando a pasta/preset atuais — o ideal é já ter configurado os cookies antes.

## Integração via API (n8n e outras ferramentas)

O servidor já expõe uma API REST completa (`/api/downloads`, WebSocket em `/ws` pra acompanhar em tempo real) — dá pra outra ferramenta rodando na mesma máquina (ex.: um workflow do n8n) enfileirar downloads sem abrir essa página. O botão **🔌 Integração** configura dois pontos opcionais, ambos desligados por padrão:

- **Chave de API**: quando preenchida, `POST /api/downloads` passa a exigir o cabeçalho `X-API-Key` com esse valor (401 sem ele). Os outros endpoints (status, pausar, configurações) continuam livres — pensado só pra evitar que outro processo qualquer na máquina dispare downloads sem querer, não como um cadeado forte. O servidor continua ouvindo só em `127.0.0.1` de qualquer forma; isso não abre o app pra internet.
- **Webhook**: uma URL que recebe um `POST` em JSON (`{"task_id", "url", "status", "path", "media_type", "message"}`) toda vez que um download termina de verdade (sucesso ou erro) — pausar/cancelar não dispara. Como é o app que chama a URL (e não o contrário), funciona mesmo se o n8n estiver rodando em outro lugar (ex.: uma VPS) — só aponte pro webhook público do workflow.

## Estrutura do projeto

```
server/
├── main.py            → app FastAPI, WebSocket, sincronização de estado, disparo do webhook
├── api.py               → todos os endpoints REST, chave de API
├── downloader.py          → motor de download: fallbacks, agendamento/retentativa, fila de prioridade própria
├── gallery_engine.py        → motor gallery-dl (Instagram/Pinterest), tentado antes do yt-dlp pra esses dois sites
├── diagnostics.py            → acha no histórico downloads que vieram como thumbnail por engano
├── playlist.py                 → expande pastas/playlists em itens individuais
├── thumbnails.py                 → busca de miniaturas em segundo plano
├── state.py                        → snapshot do estado de cada download (persistido em queue_state.json)
├── events.py                         → pub/sub que leva eventos do backend pro WebSocket
├── media_types.py                      → classificação de tipo de mídia por extensão
├── ffmpeg_provider.py                    → resolve o binário do ffmpeg (embutido ou do sistema)
├── errors.py                              → tradução de erros técnicos, classificação transitório/permanente
├── history.py                               → persistência do histórico (history.json)
├── config.py                                  → configurações e presets (settings.json)
└── logging_config.py                            → log em arquivo (app.log)
web/
├── index.html            → interface
├── app.js                 → toda a lógica do frontend (WebSocket, fetch, renderização)
├── style.css                → tema visual
└── icon.png                   → ícone
tests/                  → testes automatizados (pytest)
.github/workflows/ci.yml → CI: testes e lint a cada push/PR
run.py / run.bat / run.sh → inicia o servidor e abre o navegador
requirements.txt / requirements-dev.txt
pyproject.toml         → configuração do ruff
```

## Qualidade e testes

```bash
pip install -r requirements-dev.txt
pytest tests -v
ruff check server tests
```

## O que eu testei vs. o que ainda depende de você testar

Validei a sintaxe de todo o código (`py_compile`/`ruff`), e a suíte automatizada passa inteira — 125 testes, 37 deles novos pra essa rodada (motor gallery-dl, a nova ordem de tentativas na expansão de pastas do Pinterest, o aviso de versão desatualizada). Pro motor novo especificamente, também instalei o gallery-dl de verdade aqui e li o código-fonte dele (não só a documentação) pra confirmar como as flags `--Print`/`-j` e os códigos de saída funcionam por dentro antes de escrever `gallery_engine.py` em cima disso — e os testes desse arquivo simulam o processo do gallery-dl com um script Python que imprime no mesmo formato, então o *parsing*, o cancelamento e a leitura em tempo real foram exercitados de verdade, não só o "caminho feliz" no papel.

O que eu **não** consigo fazer por aqui é rodar um download de verdade contra instagram.com ou pinterest.com — o ambiente onde eu trabalho não tem acesso à internet geral, só a alguns domínios de pacotes (PyPI, GitHub etc.), então isso nunca foi testado contra os sites reais. Pontos que valem sua atenção no primeiro teste de verdade:

- Um link de post/pin único de cada site, pra confirmar que baixa a mídia completa (não uma miniatura).
- Um carrossel do Instagram (várias fotos/vídeos num post só) — confirma se todos os arquivos aparecem na pasta e cada um vira uma linha no histórico.
- Uma pasta do Pinterest — confirma se ela expande em itens separados na fila (e não numa tarefa só).
- Se algo baixar "0 arquivos" mesmo aparentando ter funcionado (o gallery-dl não deu erro), é bem provável que seja o placeholder `{_path}` usado no `--Print` (ver comentário em `gallery_engine.download`) — tem uma rede de segurança pra esse caso específico (compara o conteúdo da pasta antes/depois), mas me avisa se acontecer mesmo assim, porque significa que a suposição sobre esse placeholder específico não bateu numa versão do gallery-dl que eu não testei.

## Ideias para continuar refinando

Coisas que ainda não implementei mas que fariam sentido numa próxima rodada:

- Somar outros sites "estilo galeria" ao motor novo — Twitter/X, Tumblr, DeviantArt, Flickr são os candidatos óbvios (o gallery-dl já suporta todos, só falta somar o domínio em `gallery_engine.SUPPORTED_HOST_HINTS`).
- Expandir perfil/hashtag do Instagram em itens separados na fila (hoje baixa tudo numa tarefa só, de propósito — ver seção "Instagram e Pinterest" sobre o porquê).
- Marcar no histórico qual motor baixou cada item (hoje não dá pra saber, olhando só a interface, se algo veio do gallery-dl ou do yt-dlp).
- Tema claro alternável.
- Limite de banda (não travar a internet da casa toda).
- Ícone de instalação como PWA (pra abrir "como app" mesmo sendo uma aba).

Me fala quais dessas fazem sentido pra você (ou qualquer outra ideia) que eu já vou implementando.

## Licença

[MIT](LICENSE)
