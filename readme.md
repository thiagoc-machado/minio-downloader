# Downloader HLS/DASH (Flask)

Pequena aplicação Flask para baixar conteúdos **HLS/DASH sem DRM** a partir do **JSON** de uma API (ex.: `manifest_uri`, `cdns.base_uri`). Inclui seleção de áudio/legendas, nome automático de arquivo, e UI com spinner.

> ⚠️ **Aviso legal**: use apenas em conteúdos **sem DRM** e para os quais você **tem permissão**. Não me responsabilizo pelo uso indevido.

---

## Recursos

* **HLS**: usa o **master playlist** se houver grupos externos de **áudio** e/ou **legendas**; caso contrário escolhe a melhor variante por **maior bitrate**.
* **Áudio**: baixar pista padrão, **preferir idiomas** (ex.: `por,spa,eng`) ou **todas as pistas**.
* **Legendas**: incluir nenhuma, **preferir idiomas** ou **todas**. Em **MP4**, converte para `mov_text` para compatibilidade.
* **Headers**: suporta `Referer`, `Origin`, `Cookie` e headers extras.
* **Nome automático**: `Serie-t<temporada>-e-<episodio>-<titulo>.<ext>` (usa overrides do formulário + heurísticas do JSON).
* **UI**: spinner durante o download (via iframe oculto), campos persistidos no **localStorage**.
* **Debug**: endpoint `/health`; logs detalhados do FFmpeg com `DEBUG_FFMPEG=1`.

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

### Executando

```bash
# (opcional) aponte para o ffmpeg do sistema
export FFMPEG_BIN=/usr/bin/ffmpeg
# (opcional) logs do ffmpeg: cria ffmpeg-*.log
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
6. Clique **Baixar**. Um **spinner** aparece; quando o download terminar, ele some automaticamente (via iframe oculto).

> Dica: se o arquivo vier **sem áudio**, geralmente o master tem grupo externo de áudio — manter o **master** como entrada resolve (a app já faz isso automaticamente quando detecta `#EXT-X-MEDIA:TYPE=AUDIO`).

---

## Endpoints

* `GET /` — formulário.
* `POST /download` — processa o JSON, baixa e devolve o arquivo como **attachment**.
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
* `DEBUG_FFMPEG=1` — ativa `-loglevel debug -report` (gera `ffmpeg-*.log`).
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


