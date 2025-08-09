Downloader HLS/DASH (Flask)
Mini-app Flask para baixar conteúdo HLS/DASH sem DRM a partir de um JSON de preparo do player. Suporta seleção de áudio/legendas por idioma, força de AAC, escolha de contêiner (mp4/mkv) e auto-nome com padrão de séries.

Requisitos
Python 3.9+

ffmpeg instalado no sistema (recomendado)

macOS: brew install ffmpeg
Ubuntu/Debian: sudo apt-get update && sudo apt-get install -y ffmpeg
Alpine: apk add --no-cache ffmpeg
Windows: winget install Gyan.FFmpeg

Instalação
bash
Copiar
Editar
git clone <este-repo>
cd <este-repo>
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
Execução
bash
Copiar
Editar
# opcional, aponte para o ffmpeg do sistema
export FFMPEG_BIN=/usr/bin/ffmpeg

# opcional, logs detalhados do ffmpeg (gera ffmpeg-*.log)
export DEBUG_FFMPEG=1

# opcional, NÃO escolher automaticamente a melhor variante HLS
# (útil para debug; mantém o master como input)
# export DISABLE_VARIANT=1

python app.py
# abra http://localhost:5000
Como usar
No campo JSON da API, cole o JSON que contenha response.manifest_uri, response.cdns.cdn[].base_uri, drm_type e package_type.
Exemplo mínimo:

json
Copiar
Editar
{"result":"success","response":{"drm_type":"none","package_type":"hls","manifest_uri":"minno_XXX/index.m3u8","cdns":{"cdn":[{"priority":0,"base_uri":"https://example.cdn/hls"}]}}}
Preencha Série, Temporada, Episódio e Título (eles são usados no nome do arquivo).

(Opcional) Ajuste Contêiner (mp4/mkv), User-Agent, Referer/Origin, Cookie e headers extras.

Escolha Áudio (padrão/preferir/all) e Legendas (nenhuma/preferir/all).

Clique Baixar. Um spinner aparece; quando terminar, o navegador baixa o arquivo.

Os campos de identificação ficam salvos em localStorage para agilizar o próximo uso.

Recursos
Auto-nome: Serie-t<temporada>-e-<episodio>-<titulo>.<ext>
(ex.: Minha-Serie-t1-e-2-Piloto.mp4).
Se preencher manualmente “Nome do arquivo”, esse valor é usado.

Áudio

Padrão: pega a primeira faixa.

Preferir idioma: informe por,spa,eng (ordem de preferência).

Todos: inclui todas as faixas de áudio.

Legendas

Nenhuma, Preferir idioma (por,spa,eng) ou Todas.

Em MP4, as legendas são convertidas para mov_text (compatível).

Se o master HLS declarar #EXT-X-MEDIA:TYPE=SUBTITLES/AUDIO, a app usa o master como input (para garantir faixas externas).

Forçar AAC

Se marcado, re-encoda o áudio para AAC 160k (útil quando o MP4 não suporta o codec original).

Headers

Suporte a User-Agent, Referer, Origin, Cookie e headers extras (um por linha).

Útil para CDNs que validam origem/sessão.

Endpoints úteis
GET / — formulário.

GET /health — mostra caminho e versão do ffmpeg.

Dicas / Depuração
Se o download vier sem áudio, marque Forçar AAC ou selecione master automaticamente mantendo o padrão (a app já faz isso quando detecta grupos de áudio).

Se faltar legendas, use “Todas” ou informe idiomas em “Preferir idioma”.
Alguns streams têm closed captions (CEA-608/708), que não aparecem como 0:s. Se precisar, peça suporte específico para CC.

Em falha, ative DEBUG_FFMPEG=1 e verifique o tail do log retornado + ffmpeg-*.log.

Segurança e uso
Use apenas para conteúdos sem DRM e para os quais você tem permissão legal de baixar. Respeite termos de uso e direitos autorais.

Estrutura
markdown
Copiar
Editar
.
├── app.py
├── requirements.txt
└── templates/
    └── index.html
Licença
Livre para uso interno/estudos. Ajuste conforme sua necessidade.