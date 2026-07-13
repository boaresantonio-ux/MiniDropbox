# Mini Dropbox Distribuído (Flask + SQLite + R-tree)

Sistema de armazenamento com réplicas distribuídas geograficamente,
consulta espacial via R-tree, e um nó de backup fixo. Reconstruído a
partir do teu `app.py` original — a seção "O que foi corrigido" no fim
explica exatamente o que mudou e por quê.

## Os 3 requisitos, e onde cada um vive no código

**1. Dropbox (upload/download/pastas)** → `app.py` (rotas `/upload`,
`/download`, `/create-folder`, etc.) + `templates/dashboard.html`.

**2. Sistema distribuído** → `models.py` (`Node`, `FileReplica`) +
`services/replication.py` (cópia física dos bytes para cada nó +
redistribuição ao remover um nó) + `app.py` (rotas `/nodes/*`).
  - **2.1 Criar servidores (pastas)**: rota `/nodes/add` — exige
    latitude/longitude no formulário, cria a pasta física do nó em
    `nodes/<slug>-<id>/`, e existe sempre um **nó de backup fixo**
    (`is_backup=True`) criado automaticamente, que não pode ser
    desativado nem removido.
  - **2.2 Remover servidores**: rota `/nodes/delete/<id>` — antes de
    remover, mostra uma página (`confirmar_remocao_no.html`) pedindo para
    onde os arquivos devem ir, com a opção mais próxima (via R-tree)
    já pré-selecionada.

**3. Base de dados espacial** → `services/geo.py`.
  - **3.1 Mapa**: `templates/nodes.html`, usando Leaflet + a API
    `/api/nos`.
  - **Consulta geográfica via R-tree**: classe `IndiceEspacialNos` em
    `services/geo.py`, usada tanto internamente (sugestão de nó mais
    próximo ao remover um nó) quanto exposta na rota
    `/api/nos/proximo` — clica em qualquer ponto do mapa em
    `/nodes` para testar a consulta ao vivo.
  - **UTM**: cada nó também mostra as coordenadas convertidas para UTM
    na tabela de `/nodes` (função `para_utm`), além do lat/lon.

## Estrutura

```
dropbox-flask/
├── app.py                        # rotas Flask
├── models.py                      # User, Node, Folder, File, FileReplica, Operation
├── extensions.py                   # instâncias db / login_manager
├── services/
│   ├── checksum.py                  # SHA-256 em blocos
│   ├── geo.py                        # haversine, UTM, índice espacial R-tree
│   └── replication.py                 # cópia física + redistribuição + lixeira/restauro
├── templates/
│   ├── base.html, login.html, register.html
│   ├── dashboard.html                   # escolhido pelo utilizador (Bootstrap)
│   ├── nodes.html                        # mapa + criação/remoção de nós (Bootstrap)
│   ├── confirmar_remocao_no.html          # escolher destino antes de remover
│   ├── lixeira.html                        # arquivos recuperáveis via backup
│   └── profile.html
├── static/css/style.css
├── requirements.txt
├── .gitignore
├── instance/                         # criada automaticamente — database.db mora aqui
├── nodes/                            # pastas físicas dos nós (criadas em runtime)
└── temp/                              # arquivo temporário durante o upload
```

`instance/`, `nodes/*`, `temp/*` e o `venv/` já estão no `.gitignore` —
são todos gerados em runtime, não fazem sentido versionados.

## 1. Instalar Python e dependências

```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

> **Nota sobre o `rtree`**: essa biblioteca precisa da `libspatialindex`
> por baixo. No Windows, o `pip install rtree` normalmente já baixa um
> wheel pré-compilado (não precisa de compilador instalado) para Python
> 3.9–3.12. Se der erro na instalação, confirma a versão do Python com
> `python --version` — versões muito novas ou muito antigas podem não
> ter wheel pronto ainda, nesse caso considera usar o Anaconda
> (`conda install -c conda-forge rtree`), que resolve isso sem dor.

Não precisas instalar nada de SQLite à parte — já vem embutido no Python.

## 2. Rodar

```cmd
python app.py
```

Abre **http://localhost:5000**. Na primeira execução, o Flask cria
automaticamente a pasta `instance/` (padrão do próprio framework para
arquivos gerados em runtime) com `instance/database.db` dentro, junto
com os 3 nós geográficos (Luanda, Cabinda, Benguela) e o nó de backup
fixo. Não precisas criar essa pasta manualmente — e ela já está no
`.gitignore`, não deve ir para o Git.

## 3. Fluxo de teste sugerido

1. Regista uma conta em `/register` e entra.
2. Vai a `/nodes` — o mapa mostra os 4 nós (3 geográficos + 1 backup,
   marcado em âmbar).
3. Clica em qualquer ponto do mapa para testar a consulta espacial
   (R-tree) — aparece o nó ativo mais próximo daquele ponto e a distância.
4. No dashboard, envia um arquivo — ele é fisicamente copiado para as
   pastas de todos os nós ativos (confere em `nodes/<pasta-do-nó>/`).
5. Volta a `/nodes` e clica em **"Remover"** num nó que não seja o
   backup — a página de confirmação mostra a distância a cada nó
   restante e sugere o mais próximo. Confirma.
6. No dashboard, o arquivo continua listado, agora com uma réplica a
   menos e uma nova no nó de destino escolhido.
7. Testa o botão **"Verificar"** num arquivo — ele recalcula o checksum
   de cada réplica física e avisa se alguma corrompeu.

## O que foi corrigido em relação ao `app.py` original

O arquivo que enviaste tinha alguns problemas que impediam certas rotas
de funcionar:

- **Dois modelos de arquivo competindo**: existia `File` (id UUID) e
  `FileRecord` (id inteiro) ao mesmo tempo. As rotas de
  download/preview/delete/rename/move usavam `FileRecord`, mas o
  dashboard e o upload usavam `File` — ou seja, um arquivo enviado nunca
  aparecia para essas outras rotas. Ficou só o `File`.
- **`app.config['UPLOAD_FOLDER']`** era referenciado em `download_file`
  e `preview_file`, mas nunca era definido em lugar nenhum — essas rotas
  dariam `KeyError` imediatamente. Agora o caminho vem da réplica de
  fato armazenada num nó (`caminho_replica`).
- **Réplicas nunca copiavam bytes de verdade**: `FileReplica` só marcava
  status `"STORED"`/`"PENDING"`, mas nenhum arquivo físico era criado nos
  outros nós. Agora `services/replication.py` faz a cópia real.
- **`create_folder`** usava um "marcador" (`FileRecord` com
  `filename="FOLDER_MARKER"`) para simular pastas, o que não integrava
  com o resto do sistema de arquivos. Agora `Folder` é um modelo próprio,
  com suporte a subpastas aninhadas.

## Lixeira e recuperação via backup

O nó de backup agora é uma rede de segurança de verdade: **apagar um
arquivo não apaga a réplica do backup**.

- `/delete/<file_id>` faz um **soft delete**: `File.deleted_at` é
  preenchido, a réplica e a cópia física em todos os nós normais são
  removidas, mas a réplica do backup fica intacta.
- `/lixeira` lista os arquivos nesse estado, com botões para **baixar**
  (puxa direto do backup), **restaurar** e **apagar em definitivo**.
- `/restaurar/<file_id>` copia a cópia física do backup de volta para
  todos os nós ativos e limpa `deleted_at` — o arquivo volta a aparecer
  normalmente no dashboard.
- `/apagar-definitivo/<file_id>` só existe a partir da lixeira: aí sim
  remove a réplica do backup e apaga o registro por completo — sem volta.
- Se, por algum motivo, um arquivo nunca chegou a ter réplica no backup
  (ex.: o nó de backup estava indisponível no momento do upload — o que
  não deveria acontecer, já que ele é sempre `enabled=True`), o
  `/delete` cai automaticamente para apagamento definitivo e avisa o
  usuário que aquele arquivo não era recuperável.

Isso está implementado em `services/replication.py`
(`mover_para_lixeira`, `restaurar_do_backup`) e nas rotas `/lixeira`,
`/restaurar/<id>`, `/apagar-definitivo/<id>` em `app.py`. O dashboard
ganhou um botão "Lixeira" (com contador) e um botão "Nós" no topo, já
que o `dashboard.html` original não tinha link nenhum para essas duas
páginas.

## Download/visualização pelo nó mais próximo (com indicador visual)

Antes, `/download` e `/preview` escolhiam "qualquer nó ativo, backup por
último" — sem olhar distância nenhuma. Agora escolhem de verdade pelo
nó mais próximo do cliente, e o dashboard mostra visualmente de onde
cada arquivo está vindo:

- **`_no_mais_proximo_com_replica(arquivo, lat, lon)`** (`app.py`): usa
  o mesmo índice R-tree (`indice_espacial.mais_proximos`) para ordenar
  os nós ativos por distância real ao cliente, e devolve o primeiro
  dessa lista que também tem uma réplica `STORED` do arquivo — ou seja,
  "o mais próximo que **realmente tem o arquivo**", não só o mais
  próximo do mapa todo. Se `Cabinda` for o mais perto mas só `Luanda`
  tiver a réplica, ele escolhe `Luanda`. Sem lat/lon, cai de volta no
  comportamento antigo.
- **`GET /api/arquivos/<id>/no-servidor?lat=&lon=`**: usada pelo
  dashboard para mostrar, **antes do clique**, de qual nó o arquivo
  viria — um selo colorido na tabela ("Servido por"): verde para nó
  normal, âmbar para backup, com a distância em km.
- **`/download` e `/preview`** aceitam `?lat=&lon=` (o dashboard já
  envia automaticamente) e devolvem os headers `X-Served-By-Node`,
  `X-Served-By-Backup` e `X-Served-By-Distance-Km` na resposta — dá
  para confirmar no painel de rede do navegador de onde o arquivo veio
  de verdade, útil para demonstrar o sistema.
- **No dashboard**, o JS tenta `navigator.geolocation` do navegador
  (com fallback para Luanda se o usuário negar ou não responder em
  3s), preenche o selo de cada arquivo via `fetch`, e ajusta os links
  de download/visualização para carregarem a mesma coordenada — assim
  o arquivo baixado é sempre o mesmo nó que o selo mostrou.

## Nota: compatibilização com o dashboard.html escolhido

O `templates/dashboard.html` agora é o que foi escolhido (Bootstrap 5 +
Font Awesome + Leaflet, tema claro), em vez do tema escuro/âmbar da
versão anterior. Para o backend bater certinho com ele:

- **`File.file_size` / `File.upload_date`**: o template usa esses nomes;
  o modelo (`models.py`) internamente guarda `size`/`created_at`, então
  adicionei duas `@property` que expõem os mesmos dados sob os dois
  nomes — não precisei renomear nada no resto do sistema.
- **`current_folder`**: o template compara direto com `fld.id` e exibe
  em `<code>`, então a rota `/dashboard` agora passa o **id da pasta**
  (string ou `""`), não mais o objeto `Folder`.
- **`folders`**: o template lista todas as pastas do usuário numa
  barra lateral plana (sem indentação por nível), então a rota passa
  `Folder.query.filter_by(user_id=...).all()` inteiro, não só as
  subpastas da pasta atual.
- **`nodes_json`**: o popup do mapa usa `node.host` e `node.port` além
  de nome/lat/lon/estado — adicionei esses dois campos que faltavam.
- **`static/uploads/profiles/default.webp`**: o template referencia essa
  imagem para o avatar; criei um placeholder simples para não quebrar.

O `templates/nodes.html` e `templates/confirmar_remocao_no.html` foram
redesenhados no mesmo estilo Bootstrap/Font Awesome do dashboard
escolhido (antes usavam o tema escuro), para a navegação entre páginas
não ficar inconsistente. A lógica (R-tree, redistribuição ao remover nó,
nó de backup protegido) continua exatamente a mesma — só a aparência
mudou.

`login.html`, `register.html` e `profile.html` continuam no tema
escuro/âmbar original — se quiseres, digo como levá-los para Bootstrap
também, mas como não foram mencionados no pedido, deixei como estavam.

## Limitações conhecidas (para não haver surpresas)


- Tudo roda num único computador — os "nós" são pastas locais, não
  servidores remotos de verdade. Para virar distribuído de verdade, cada
  nó precisaria de um processo Flask próprio escutando em `host:port`
  (os campos já existem no modelo `Node`, só não são usados ainda) e as
  cópias seriam feitas por HTTP em vez de `shutil.copy2`.
- O R-tree usa distância em graus (lat/lon) via bounding boxes; para
  achar o "k mais próximos" ele já ordena corretamente porque graus de
  latitude/longitude são monotônicos com a distância real na escala de
  Angola. Para áreas muito maiores (ex: comparando pontos perto do polo
  com pontos no equador) seria preciso projetar antes de indexar — não é
  o caso aqui.
