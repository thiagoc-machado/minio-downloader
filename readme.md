# Downloader HLS/DASH (Flask)

Pequena aplicação Flask para baixar conteúdos **HLS/DASH sem DRM** a partir do **JSON** de uma API (ex.: `manifest_uri`, `cdns.base_uri`). Inclui seleção de áudio/legendas, nome automático de arquivo, UI com spinner e salvamento direto na biblioteca do servidor.

> ⚠️ **Aviso legal**: use apenas em conteúdos **sem DRM** e para os quais você **tem permissão**. Não me responsabilizo pelo uso indevido.

---

## Recursos

* **HLS**: usa o **master playlist** se houver grupos externos de **áudio** e/ou **legendas**; caso contrário escolhe a melhor variante por **maior bitrate**.
* **Áudio**: baixar pista padrão, **preferir idiomas** (ex.: `por,spa,eng`) ou **todas as pistas**.
* **Legendas**: incluir nenhuma, **preferir idiomas** ou **todas**. Em **MP4**, converte para `mov_text` para compatibilidade.
* **Headers**: suporta `Referer`, `Origin`, `Cookie` e headers extras.
* **JSON de detalhes**: aceita um segundo JSON com `ProgramId`, `Vods`, `CatalogInfo`, `SeasonNumber`, `EpisodeNumber`, etc. Esse é o JSON usado para preencher os metadados.
* **Nome automático**: `Serie-t<temporada>-e-<episodio>-<titulo>.<ext>` (usa os campos manuais e, quando você clica em `Preencher automático`, os dados do JSON de detalhes).
* **Metadados embutidos**: grava tags úteis no arquivo final com base nos campos preenchidos e no JSON de detalhes.
* **UI**: spinner durante o processamento, campos persistidos no **localStorage**.
* **Saída**: o arquivo é salvo no servidor na biblioteca configurada, e a UI mostra o caminho final.
* **Debug**: endpoint `/health`; logs detalhados do FFmpeg com `DEBUG_FFMPEG=1`.
* **Captura via Tampermonkey**: recebe JSONs do navegador em `/api/capture` e a UI puxa o último payload automaticamente.

---

## Requisitos

* **Python** 3.9+
* **ffmpeg** (recomendado o do sistema):

  * Ubuntu/Debian: `sudo apt-get install -y ffmpeg`
  * macOS: `brew install ffmpeg`
  * Alpine: `apk add --no-cache ffmpeg`
* Alternativa: fallback via `imageio-ffmpeg` (pode ser mais instável em alguns ambientes).

### Instalação

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Docker

```bash
docker build -t minio-downloads .
docker run --rm -p 8090:5000 -e TZ=Europe/Madrid minio-downloads
```

Se quiser persistir os logs do FFmpeg:

```bash
docker run --rm -p 8090:5000 \
  -e TZ=Europe/Madrid \
  -v /mnt/storage/minio-downloads/logs:/app/logs \
  minio-downloads
```

Exemplo para o seu `docker-compose.yml`:

```yaml
  minio-downloads:
    build:
      context: ./minio-downloads
      dockerfile: Dockerfile
    container_name: minio-downloads
    environment:
      - TZ=Europe/Madrid
      - PORT=5000
      - WEB_CONCURRENCY=2
      - DEBUG_FFMPEG=0
      - DISABLE_VARIANT=0
      - MEDIA_ROOT_SERIES=/media/series
      - MEDIA_ROOT_MOVIES=/media/movies
      - MEDIA_ROOT_CRISTAO=/media/cristaos
      - MEDIA_ROOT_AUDIOLIVROS_INFANTIL=/media/AudioLivros_infantil
    volumes:
      - /mnt/storage/series:/media/series
      - /mnt/storage/movies:/media/movies
      - /mnt/storage/cristaos:/media/cristaos
      - /mnt/storage/AudioLivros_infantil:/media/AudioLivros_infantil
      - /mnt/storage/minio-downloads/logs:/app/logs
    ports:
      - '8090:5000'
    restart: unless-stopped
```

Se o diretório do projeto ficar mesmo em `./minio-downloads`, basta colocar este repositório ali e apontar o `build.context` para essa pasta.

Na UI, escolha a categoria:

* `Séries` -> `Nome/Season XX/arquivo.ext`
* `Filmes` -> `Nome/arquivo.ext`
* `Cristãos` -> `Nome/arquivo.ext`
* `AudioLivros infantil` -> `Nome/arquivo.ext`

Para séries, o download final fica em uma estrutura no estilo Sonarr:

`/media/series/Nome-da-Serie/Season 01/Nome-do-arquivo.ext`

Para `Filmes` e `Cristãos`, o arquivo fica direto na pasta do título:

`/media/movies/Nome-do-filme/Nome-do-arquivo.ext`
`/media/cristaos/Nome-da-obra/Nome-do-arquivo.ext`

Para `AudioLivros infantil`, o arquivo fica direto na pasta do título:

`/media/AudioLivros_infantil/Nome-da-obra/Nome-do-arquivo.ext`

### Executando

```bash
# (opcional) aponte para o ffmpeg do sistema
export FFMPEG_BIN=/usr/bin/ffmpeg
# (opcional) logs do ffmpeg: cria logs/ffmpeg-*.log
export DEBUG_FFMPEG=1

python app.py
# Abra http://localhost:5000
```

---

## Uso

1. **Cole o JSON** da API no campo “JSON da API”. Exemplo mínimo:

   ```json
   {
     "result": "success",
     "response": {
       "drm_type": "none",
       "package_type": "hls",
       "manifest_uri": "minno_Aip0NbKL-rIqGBf3R/index.m3u8",
       "cdns": {"cdn": [{"priority": 0, "base_uri": "https://.../hls-itc"}]}
     }
   }
   ```
2. Preencha **Série / Temporada / Episódio / Título**. O nome é pré-visualizado e salvo no `localStorage`.
3. Escolha **contêiner** (`mp4` ou `mkv`).
4. Se necessário, informe **Referer/Origin/Cookie** (copiados do DevTools do player).
5. Selecione **Áudio** (padrão / preferir idiomas / todos) e **Legendas** (nenhuma / preferir / todas). Idiomas são códigos como `por,spa,eng`.
6. Clique **Baixar**. Um **spinner** aparece; ele some quando o arquivo termina de ser salvo no servidor.

> O campo **JSON detalhes do vídeo** é opcional, mas ajuda muito quando a API principal traz só parte dos dados.

> Dica: o formulário principal não é usado para metadados. O preenchimento automático vem do campo **JSON detalhes do vídeo**.

### Integrando com Tampermonkey

1. Instale o userscript em `tampermonkey_capture.user.js`.
2. Ajuste `SERVER_URL` para apontar para seu servidor, por exemplo `http://192.168.1.230:8090/api/capture`.
3. Se o host do servidor não for `localhost`/`127.0.0.1`, adicione esse host no bloco `@connect` do userscript.
4. Abra o site `https://kids.gominno.com/` no Chrome.
5. Clique em `Capturar agora` no botão do Tampermonkey.
6. Recarregue a página do site para garantir que os hooks do script sejam instalados antes das requisições.
7. Navegue até a parte do site que faz a chamada da API.
8. O script tenta capturar os dois JSONs: o do vídeo (`roll`) e o dos detalhes (`play-options`).
9. Quando ambos forem enviados, o app recebe os dados e pode iniciar o download automaticamente.
10. Na UI do app, o campo de detalhes é preenchido sozinho e você ainda pode editar os campos manualmente.

Se preferir, clique em `Verificar captura agora` para puxar o último JSON salvo no servidor.
Você também pode baixar o userscript pronto direto na interface, pelo botão `Baixar script do Tampermonkey`.

> Dica: se o arquivo vier **sem áudio**, geralmente o master tem grupo externo de áudio — manter o **master** como entrada resolve (a app já faz isso automaticamente quando detecta `#EXT-X-MEDIA:TYPE=AUDIO`).

---

## Endpoints

* `GET /` — formulário.
* `POST /download` — processa o JSON, baixa e salva o arquivo no servidor, respondendo com JSON do caminho final.
* `GET /health` — JSON com `ffmpeg` em uso e sua versão.

---

## Como funciona (resumo técnico)

* Lê `response.manifest_uri`; se relativo, combina com `cdns.cdn[].base_uri` de menor `priority`.
* Se `package_type=hls`:

  * Faz fetch do **master**; se contiver `#EXT-X-MEDIA:TYPE=AUDIO` ou `#EXT-X-MEDIA:TYPE=SUBTITLES` e você pediu subs, usa **master** como **input**.
  * Caso contrário, escolhe a **melhor variante** (`#EXT-X-STREAM-INF` com maior `BANDWIDTH`).
* Monta o comando do **ffmpeg** com `-map` conforme suas escolhas:

  * vídeo: `-map 0:v:0`
  * áudio:

    * `default` → `-map 0:a:0?`
    * `prefer por,spa,eng` → `-map 0:a:m:language:por? -map 0:a:m:language:spa? ...` + fallback `-map 0:a:0?`
    * `all` → `-map 0:a?`
  * legendas: similar a áudio (`none|prefer|all`). Em MP4 usa `-c:s mov_text`.
* Remuxa com `-c copy` (ou re-encode de áudio para AAC caso marcado).

---

## Variáveis de ambiente

* `FFMPEG_BIN` — caminho do ffmpeg preferido (ex.: `/usr/bin/ffmpeg`).
* `DEBUG_FFMPEG=1` — ativa `-loglevel debug -report` e salva os logs em `logs/ffmpeg-*.log`.
* `DISABLE_VARIANT=1` — desativa escolha da melhor variante (força usar master sempre que possível).

---

## Solução de problemas

* **`ffmpeg not found`**: instale o ffmpeg e/ou ajuste `FFMPEG_BIN`.
* **Segfault (code -11)** com `imageio-ffmpeg`: use o ffmpeg do sistema (no Alpine, `apk add ffmpeg`).
* **Sem áudio**: use master (a app já detecta grupo de áudio). Verifique `audio_mode` e `audio_pref`.
* **Sem legendas**: defina `subs_mode` ≠ `none`; para MP4, convertemos para `mov_text`. CEA-608/708 (CC) não aparecem como `0:s`; suporte pode exigir extração específica.
* **403/401**: informe `Referer/Origin/Cookie` corretos. Alguns CDNs exigem `User-Agent` realista.
* **`max_muxing_queue_size`** erro: opção deve vir **antes** do arquivo de saída (já ajustado no código).

---

## Deploy

* Dev: `python app.py` (debug=True).
* Prod (exemplo):

  ```bash
  gunicorn -w 2 -b 0.0.0.0:5000 app:app
  ```
* Proxy reverso (Nginx) e ajuste de **timeout** podem ser necessários para downloads longos.

---

## Roadmap (idéias)

* Presets de idioma (pt-BR / es / en).
* Extração de **closed captions** (CEA-608/708) em HLS.
* Suporte completo a **DASH (.mpd)** com seleção de adaptação.
* Barra de progresso (SSE/WebSocket) ao invés de iframe.

---

## Licença
