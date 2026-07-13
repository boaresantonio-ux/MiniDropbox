import os
import re
import shutil
import uuid
from datetime import datetime

from flask import (
    Flask, render_template, redirect, url_for, request, flash,
    send_from_directory, jsonify, abort
)
from flask_login import (
    LoginManager, login_user, login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from extensions import db, login_manager
from models import User, Node, Folder, File, FileReplica, Operation
from services.checksum import calculate_checksum
from services.geo import IndiceEspacialNos, haversine_km, para_utm
from services.replication import (
    replicar_para_nos, remover_replicas_do_arquivo, redistribuir_arquivos_do_no,
    mover_para_lixeira, restaurar_do_backup, caminho_replica,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# instance_relative_config=True habilita app.instance_path — a pasta
# convencional do Flask para arquivos gerados em runtime (banco de dados,
# etc.) que não devem ir para o controle de versão junto do código-fonte.
app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = "chave_secreta_mini_dropbox_2026"
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB por arquivo

NODES_ROOT = os.path.join(BASE_DIR, "nodes")
TEMP_FOLDER = os.path.join(BASE_DIR, "temp")
PROFILE_FOLDER = os.path.join(app.static_folder, "uploads", "profiles")

os.makedirs(app.instance_path, exist_ok=True)  # cria instance/ se não existir
os.makedirs(NODES_ROOT, exist_ok=True)
os.makedirs(TEMP_FOLDER, exist_ok=True)
os.makedirs(PROFILE_FOLDER, exist_ok=True)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(app.instance_path, "database.db")

db.init_app(app)
login_manager.init_app(app)

# Índice espacial em memória, reconstruído sempre que a lista de nós muda.
indice_espacial = IndiceEspacialNos()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.template_filter("fmt_bytes")
def fmt_bytes(n):
    if n is None:
        return "—"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


# -----------------------------------------------------------------------------
# NÓS PADRÃO E ÍNDICE ESPACIAL
# -----------------------------------------------------------------------------

def slug(texto: str) -> str:
    texto = texto.strip().lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto or "no"


def atualizar_indice_espacial():
    """Reconstrói o R-tree a partir dos nós ativos. Chamar após qualquer mudança nos nós."""
    nodes_ativos = Node.query.filter_by(enabled=True).all()
    indice_espacial.construir(nodes_ativos)


def criar_pasta_do_no(nome: str) -> str:
    pasta = os.path.join(NODES_ROOT, f"{slug(nome)}-{uuid.uuid4().hex[:6]}")
    os.makedirs(pasta, exist_ok=True)
    return pasta


def create_default_nodes():
    """Cria os 3 nós geográficos + o nó de backup fixo, se ainda não existirem."""
    default_nodes = [
        {"name": "Luanda", "latitude": -8.8383, "longitude": 13.2343},
        {"name": "Cabinda", "latitude": -5.5500, "longitude": 12.2000},
        {"name": "Benguela", "latitude": -12.5763, "longitude": 13.4055},
    ]

    for data in default_nodes:
        if Node.query.filter_by(name=data["name"]).first():
            continue
        node = Node(
            name=data["name"],
            host="127.0.0.1",
            port=5000,
            path=criar_pasta_do_no(data["name"]),
            latitude=data["latitude"],
            longitude=data["longitude"],
            enabled=True,
            is_backup=False,
        )
        db.session.add(node)

    # Nó de backup fixo: sempre ativo, não pode ser removido nem desativado.
    if not Node.query.filter_by(is_backup=True).first():
        backup = Node(
            name="Backup Central",
            host="127.0.0.1",
            port=5099,
            path=criar_pasta_do_no("backup-central"),
            latitude=-11.2027,   # ponto central aproximado de Angola
            longitude=17.8739,
            enabled=True,
            is_backup=True,
        )
        db.session.add(backup)

    db.session.commit()
    atualizar_indice_espacial()


# -----------------------------------------------------------------------------
# AUTENTICAÇÃO
# -----------------------------------------------------------------------------

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not username or not email or not password:
            flash("Preenche todos os campos.", "warning")
            return redirect(url_for("register"))

        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Nome de utilizador ou email já registado.", "danger")
            return redirect(url_for("register"))

        novo_user = User(
            username=username,
            email=email,
            password=generate_password_hash(password, method="scrypt"),
        )
        db.session.add(novo_user)
        db.session.commit()
        flash("Conta criada com sucesso! Faz login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/", methods=["GET", "POST"])
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for("dashboard"))

        flash("Credenciais incorretas. Tenta novamente.", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sessão terminada.", "info")
    return redirect(url_for("login"))


# -----------------------------------------------------------------------------
# DASHBOARD / ARQUIVOS / PASTAS
# -----------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    search_query = request.args.get("search", "")
    sort_by = request.args.get("sort", "date_desc")

    # O template usa current_folder como STRING simples (comparado direto a
    # fld.id, exibido em <code> e enviado em campos hidden). Nunca deve ser
    # None aqui — em Jinja, None em value="{{ ... }}" vira o texto "None".
    current_folder = request.args.get("folder", "")
    folder_id = current_folder or None

    # A barra lateral deste dashboard lista todas as pastas do usuário,
    # sem aninhamento visual — mantemos parent_id no modelo (para o resto
    # do sistema), mas aqui exibimos a lista plana mesmo.
    folders = Folder.query.filter_by(user_id=current_user.id).all()

    # current_folder continua sendo o id (usado em comparações/campos
    # hidden no template); current_folder_nome é só para exibição.
    if folder_id:
        pasta_atual = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
        current_folder_nome = pasta_atual.name if pasta_atual else "Raiz"
    else:
        current_folder_nome = "Raiz"

    query = File.query.filter_by(user_id=current_user.id, folder_id=folder_id).filter(
        File.deleted_at.is_(None)
    )
    if search_query:
        query = query.filter(File.display_name.contains(search_query))

    ordenacoes = {
        "name_asc": File.display_name.asc(),
        "name_desc": File.display_name.desc(),
        "size_asc": File.size.asc(),
        "size_desc": File.size.desc(),
        "date_asc": File.created_at.asc(),
        "date_desc": File.created_at.desc(),
    }
    query = query.order_by(ordenacoes.get(sort_by, File.created_at.desc()))
    files = query.all()

    total_bytes = db.session.query(db.func.sum(File.size)).filter(
        File.user_id == current_user.id, File.deleted_at.is_(None)
    ).scalar() or 0
    total_mb = round(total_bytes / (1024 * 1024), 2)

    limite_bytes = app.config["MAX_CONTENT_LENGTH"]
    progress_percent = min(round((total_bytes / limite_bytes) * 100, 1), 100) if limite_bytes else 0

    total_lixeira = File.query.filter_by(user_id=current_user.id).filter(
        File.deleted_at.isnot(None)
    ).count()

    # Sidebar mostra TODOS os nós (para refletir status online/offline de
    # cada um, incluindo os desativados) — o card do mapa usa nodes_json.
    todos_os_nos = Node.query.order_by(Node.is_backup.desc(), Node.name).all()
    nodes_json = [
        {
            "name": n.name, "latitude": n.latitude, "longitude": n.longitude,
            "enabled": n.enabled, "host": n.host, "port": n.port, "is_backup": n.is_backup,
        }
        for n in todos_os_nos
    ]

    return render_template(
        "dashboard.html",
        files=files,
        folders=folders,
        current_folder=current_folder,
        current_folder_nome=current_folder_nome,
        nodes=todos_os_nos,
        nodes_json=nodes_json,
        total_mb=total_mb,
        progress_percent=progress_percent,
        total_lixeira=total_lixeira,
        search_query=search_query,
        sort_by=sort_by,
    )


@app.route("/upload", methods=["POST"])
@login_required
def upload_file():
    files = request.files.getlist("file")
    current_folder = request.form.get("folder") or None

    if not files or files[0].filename == "":
        flash("Nenhum ficheiro selecionado.", "warning")
        return redirect(url_for("dashboard", folder=current_folder))

    nodes_ativos = Node.query.filter_by(enabled=True).all()
    if not nodes_ativos:
        flash("Nenhum nó ativo — não é possível replicar o upload.", "danger")
        return redirect(url_for("dashboard", folder=current_folder))

    for file in files:
        if file.filename == "":
            continue

        file_id = str(uuid.uuid4())
        safe_name = secure_filename(file.filename)
        physical_name = f"{file_id}_{safe_name}"

        # Salva uma vez num diretório temporário só para calcular hash e
        # tamanho; a cópia "de verdade" é a que vai para cada nó.
        caminho_temp = os.path.join(TEMP_FOLDER, physical_name)
        file.save(caminho_temp)

        checksum = calculate_checksum(caminho_temp)
        size = os.path.getsize(caminho_temp)

        novo_arquivo = File(
            id=file_id,
            filename=physical_name,
            display_name=safe_name,
            size=size,
            checksum=checksum,
            folder_id=current_folder,
            user_id=current_user.id,
        )
        db.session.add(novo_arquivo)

        replicar_para_nos(file_id, current_user.id, physical_name, caminho_temp, nodes_ativos)

        db.session.add(Operation(
            operation="UPLOAD", file_id=file_id,
            detail=f"{len(nodes_ativos)} réplica(s) criada(s)",
        ))

        os.remove(caminho_temp)

    db.session.commit()
    flash(f"Ficheiro(s) enviado(s) e replicado(s) em {len(nodes_ativos)} nó(s).", "success")
    return redirect(url_for("dashboard", folder=current_folder))


@app.route("/create-folder", methods=["POST"])
@login_required
def create_folder():
    name = request.form.get("folder_name", "").strip()
    parent_id = request.form.get("parent_id") or None

    if name:
        db.session.add(Folder(name=name, parent_id=parent_id, user_id=current_user.id))
        db.session.commit()
        flash(f'Pasta "{name}" criada!', "success")

    return redirect(url_for("dashboard", folder=parent_id))


def _no_mais_proximo_com_replica(arquivo, lat=None, lon=None):
    """
    Escolhe qual nó deve servir este arquivo para download/visualização.

    Com lat/lon: usa o índice espacial (R-tree) para ordenar os nós
    ativos por distância real ao cliente, e devolve o primeiro dessa
    lista que realmente tenha uma réplica STORED do arquivo — ou seja,
    "o nó mais próximo que também tem o arquivo", não só o mais próximo
    do mapa todo.

    Sem lat/lon (ou se a consulta espacial não achar nenhum candidato
    válido): cai para "qualquer nó ativo disponível, backup por último",
    que era o comportamento antigo.

    Devolve (node, distancia_km) — distancia_km é None quando não há
    coordenada de referência.
    """
    ids_com_replica_stored = {
        r.node_id for r in arquivo.replicas
        if r.status == "STORED" and r.node and r.node.enabled
    }
    if not ids_com_replica_stored:
        return None, None

    if lat is not None and lon is not None:
        ordenados_por_distancia = indice_espacial.mais_proximos(lat, lon, quantidade=999)
        for node_id in ordenados_por_distancia:
            if node_id in ids_com_replica_stored:
                node = Node.query.get(node_id)
                distancia = round(haversine_km(lat, lon, node.latitude, node.longitude), 1)
                return node, distancia

    # Fallback: sem coordenadas, ou o índice espacial não devolveu nenhum
    # nó com réplica (não deveria acontecer, mas não custa ser defensivo).
    replicas_validas = [
        r for r in arquivo.replicas
        if r.node_id in ids_com_replica_stored
    ]
    replicas_validas.sort(key=lambda r: r.node.is_backup)  # backup por último
    escolhido = replicas_validas[0].node
    distancia = round(haversine_km(lat, lon, escolhido.latitude, escolhido.longitude), 1) \
        if lat is not None and lon is not None else None
    return escolhido, distancia


@app.route("/api/arquivos/<file_id>/no-servidor")
@login_required
def api_no_servidor(file_id):
    """Usado pelo dashboard para mostrar, antes do clique, de qual nó o arquivo viria."""
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    node, distancia = _no_mais_proximo_com_replica(arquivo, lat, lon)

    if not node:
        return jsonify({"erro": "sem réplica disponível"}), 404

    return jsonify({
        "node_id": node.id,
        "node": node.name,
        "is_backup": node.is_backup,
        "distancia_km": distancia,
    })


@app.route("/download/<file_id>")
@login_required
def download_file(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    node, distancia = _no_mais_proximo_com_replica(arquivo, lat, lon)

    if not node:
        flash("Nenhuma réplica disponível deste arquivo no momento.", "danger")
        return redirect(url_for("dashboard", folder=arquivo.folder_id))

    pasta = os.path.dirname(caminho_replica(node, current_user.id, arquivo.filename))
    resposta = send_from_directory(pasta, arquivo.filename, as_attachment=True, download_name=arquivo.display_name)
    resposta.headers["X-Served-By-Node"] = node.name
    resposta.headers["X-Served-By-Backup"] = "1" if node.is_backup else "0"
    if distancia is not None:
        resposta.headers["X-Served-By-Distance-Km"] = str(distancia)
    return resposta


@app.route("/preview/<file_id>")
@login_required
def preview_file(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)

    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    node, distancia = _no_mais_proximo_com_replica(arquivo, lat, lon)

    if not node:
        abort(404)

    pasta = os.path.dirname(caminho_replica(node, current_user.id, arquivo.filename))
    resposta = send_from_directory(pasta, arquivo.filename)
    resposta.headers["X-Served-By-Node"] = node.name
    resposta.headers["X-Served-By-Backup"] = "1" if node.is_backup else "0"
    if distancia is not None:
        resposta.headers["X-Served-By-Distance-Km"] = str(distancia)
    return resposta


@app.route("/delete/<file_id>", methods=["POST"])
@login_required
def delete_file(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)

    folder_id = arquivo.folder_id
    nodes_por_id = {n.id: n for n in Node.query.all()}
    tinha_backup = mover_para_lixeira(arquivo, nodes_por_id)

    if tinha_backup:
        arquivo.deleted_at = datetime.utcnow()
        db.session.add(Operation(operation="MOVER_LIXEIRA", file_id=file_id))
        db.session.commit()
        flash("Arquivo removido — continua guardado no nó de backup e pode ser restaurado na lixeira.", "success")
    else:
        # Não havia réplica de backup (ex.: nó de backup ficou indisponível
        # antes do upload deste arquivo) — sem rede de segurança, apaga de vez.
        db.session.delete(arquivo)
        db.session.add(Operation(operation="DELETE", file_id=file_id))
        db.session.commit()
        flash("Arquivo removido. Atenção: não havia réplica no backup, então não é recuperável.", "warning")

    return redirect(url_for("dashboard", folder=folder_id))


@app.route("/lixeira")
@login_required
def lixeira():
    arquivos = File.query.filter_by(user_id=current_user.id).filter(
        File.deleted_at.isnot(None)
    ).order_by(File.deleted_at.desc()).all()

    return render_template("lixeira.html", arquivos=arquivos)


@app.route("/restaurar/<file_id>", methods=["POST"])
@login_required
def restaurar_arquivo(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)
    if arquivo.deleted_at is None:
        flash("Este arquivo não está na lixeira.", "warning")
        return redirect(url_for("lixeira"))

    nodes_ativos = Node.query.filter_by(enabled=True).all()
    nodes_por_id = {n.id: n for n in Node.query.all()}

    if not restaurar_do_backup(arquivo, nodes_ativos, nodes_por_id):
        flash("Não foi possível restaurar: a cópia no nó de backup também se perdeu.", "danger")
        return redirect(url_for("lixeira"))

    arquivo.deleted_at = None
    db.session.add(Operation(operation="RESTAURAR", file_id=file_id))
    db.session.commit()

    flash(f'Arquivo "{arquivo.display_name}" restaurado a partir do backup.', "success")
    return redirect(url_for("dashboard", folder=arquivo.folder_id))


@app.route("/apagar-definitivo/<file_id>", methods=["POST"])
@login_required
def apagar_definitivo(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)
    if arquivo.deleted_at is None:
        flash("Move o arquivo para a lixeira antes de apagar em definitivo.", "warning")
        return redirect(url_for("dashboard", folder=arquivo.folder_id))

    nodes_por_id = {n.id: n for n in Node.query.all()}
    remover_replicas_do_arquivo(arquivo, nodes_por_id)  # agora inclui o backup

    db.session.delete(arquivo)
    db.session.add(Operation(operation="APAGAR_DEFINITIVO", file_id=file_id))
    db.session.commit()

    flash("Arquivo apagado em definitivo — já não pode ser recuperado.", "danger")
    return redirect(url_for("lixeira"))


@app.route("/rename/<file_id>", methods=["POST"])
@login_required
def rename_file(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id == current_user.id and request.form.get("new_name"):
        arquivo.display_name = request.form.get("new_name")
        db.session.commit()
        flash("Renomeado com sucesso!", "success")
    return redirect(url_for("dashboard", folder=arquivo.folder_id))


@app.route("/move/<file_id>", methods=["POST"])
@login_required
def move_file(file_id):
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id == current_user.id:
        arquivo.folder_id = request.form.get("target_folder") or None
        db.session.commit()
        flash("Arquivo movido!", "success")
    return redirect(url_for("dashboard", folder=arquivo.folder_id))


@app.route("/verificar/<file_id>", methods=["POST"])
@login_required
def verificar_integridade(file_id):
    """Recalcula o checksum de cada réplica e compara com o original."""
    arquivo = File.query.get_or_404(file_id)
    if arquivo.user_id != current_user.id:
        abort(403)

    ok, corrompidas = 0, 0
    for replica in arquivo.replicas:
        node = replica.node
        if not node:
            continue
        caminho = caminho_replica(node, current_user.id, arquivo.filename)
        if os.path.exists(caminho) and calculate_checksum(caminho) == arquivo.checksum:
            replica.status = "STORED"
            ok += 1
        else:
            replica.status = "CORROMPIDO"
            corrompidas += 1

    db.session.commit()
    if corrompidas:
        flash(f"{corrompidas} réplica(s) corrompida(s) detectada(s) — {ok} íntegra(s).", "danger")
    else:
        flash(f"Todas as {ok} réplicas estão íntegras.", "success")
    return redirect(url_for("dashboard", folder=arquivo.folder_id))


# -----------------------------------------------------------------------------
# NÓS (SISTEMA DISTRIBUÍDO + BASE ESPACIAL)
# -----------------------------------------------------------------------------

@app.route("/nodes")
@login_required
def nodes():
    todos_os_nos = Node.query.order_by(Node.is_backup.desc(), Node.name).all()

    nodes_json = [
        {
            "id": n.id, "name": n.name, "latitude": n.latitude, "longitude": n.longitude,
            "enabled": n.enabled, "is_backup": n.is_backup, "host": n.host, "port": n.port,
            "utm": para_utm(n.latitude, n.longitude),
            "replicas": FileReplica.query.filter_by(node_id=n.id).count(),
        }
        for n in todos_os_nos
    ]

    return render_template("nodes.html", nodes=todos_os_nos, nodes_json=nodes_json)


@app.route("/nodes/add", methods=["POST"])
@login_required
def add_node():
    name = request.form.get("name", "").strip()
    latitude = request.form.get("latitude", "").strip()
    longitude = request.form.get("longitude", "").strip()

    if not name or not latitude or not longitude:
        flash("Nome, latitude e longitude são obrigatórios para criar um nó.", "warning")
        return redirect(url_for("nodes"))

    try:
        latitude, longitude = float(latitude), float(longitude)
    except ValueError:
        flash("Latitude/longitude inválidas.", "danger")
        return redirect(url_for("nodes"))

    node = Node(
        name=name,
        host=request.form.get("host") or "127.0.0.1",
        port=int(request.form.get("port") or 5000),
        path=criar_pasta_do_no(name),
        latitude=latitude,
        longitude=longitude,
        enabled=True,
    )
    db.session.add(node)
    db.session.add(Operation(operation="CRIAR_NO", node_id=node.id, detail=name))
    db.session.commit()
    atualizar_indice_espacial()

    flash(f'Nó "{name}" criado em ({latitude}, {longitude}).', "success")
    return redirect(url_for("nodes"))


@app.route("/nodes/toggle/<node_id>", methods=["POST"])
@login_required
def toggle_node(node_id):
    node = Node.query.get_or_404(node_id)

    if node.is_backup:
        flash("O nó de backup fixo não pode ser desativado.", "warning")
        return redirect(url_for("nodes"))

    node.enabled = not node.enabled
    node.last_seen = datetime.utcnow()
    db.session.commit()
    atualizar_indice_espacial()

    flash(f'Nó "{node.name}" {"ativado" if node.enabled else "desativado"}.', "success")
    return redirect(url_for("nodes"))


@app.route("/nodes/delete/<node_id>")
@login_required
def confirmar_delete_node(node_id):
    """Antes de remover, mostra para onde os arquivos deste nó devem ir."""
    node = Node.query.get_or_404(node_id)

    if node.is_backup:
        flash("O nó de backup fixo não pode ser removido.", "warning")
        return redirect(url_for("nodes"))

    outros_nos = Node.query.filter(Node.id != node_id).all()
    if not outros_nos:
        flash("Não há outro nó disponível para receber os arquivos.", "danger")
        return redirect(url_for("nodes"))

    sugestao_ids = indice_espacial.mais_proximos(
        node.latitude, node.longitude, quantidade=1, excluir_ids={node.id}
    )
    sugestao_id = sugestao_ids[0] if sugestao_ids else outros_nos[0].id

    opcoes = sorted(
        [
            {"node": n, "distancia_km": round(haversine_km(node.latitude, node.longitude, n.latitude, n.longitude), 1)}
            for n in outros_nos
        ],
        key=lambda o: o["distancia_km"],
    )

    total_replicas = FileReplica.query.filter_by(node_id=node_id).count()

    return render_template(
        "confirmar_remocao_no.html",
        node=node,
        opcoes=opcoes,
        sugestao_id=sugestao_id,
        total_replicas=total_replicas,
    )


@app.route("/nodes/delete/<node_id>", methods=["POST"])
@login_required
def delete_node(node_id):
    node = Node.query.get_or_404(node_id)

    if node.is_backup:
        flash("O nó de backup fixo não pode ser removido.", "warning")
        return redirect(url_for("nodes"))

    target_node_id = request.form.get("target_node_id")
    node_destino = Node.query.get(target_node_id)
    if not node_destino or node_destino.id == node.id:
        flash("Escolhe um nó de destino válido para os arquivos.", "danger")
        return redirect(url_for("confirmar_delete_node", node_id=node_id))

    total_movido = redistribuir_arquivos_do_no(node, node_destino)

    db.session.add(Operation(
        operation="REMOVER_NO", node_id=node.id,
        detail=f"{total_movido} arquivo(s) redistribuído(s) para {node_destino.name}",
    ))

    if os.path.exists(node.path):
        shutil.rmtree(node.path, ignore_errors=True)

    nome_removido = node.name
    db.session.delete(node)
    db.session.commit()
    atualizar_indice_espacial()

    flash(
        f'Nó "{nome_removido}" removido. {total_movido} arquivo(s) redistribuído(s) para "{node_destino.name}".',
        "success",
    )
    return redirect(url_for("nodes"))


# -----------------------------------------------------------------------------
# API — usada pelo mapa (consulta espacial R-tree em tempo real)
# -----------------------------------------------------------------------------

@app.route("/api/nos")
@login_required
def api_nos():
    return jsonify([
        {
            "id": n.id, "name": n.name, "latitude": n.latitude, "longitude": n.longitude,
            "enabled": n.enabled, "is_backup": n.is_backup,
        }
        for n in Node.query.all()
    ])


@app.route("/api/nos/proximo")
@login_required
def api_no_mais_proximo():
    """Demonstra a consulta espacial: dado um ponto, devolve o nó ativo mais próximo via R-tree."""
    try:
        lat = float(request.args.get("lat"))
        lon = float(request.args.get("lon"))
    except (TypeError, ValueError):
        return jsonify({"erro": "parâmetros lat/lon inválidos"}), 400

    ids = indice_espacial.mais_proximos(lat, lon, quantidade=1)
    if not ids:
        return jsonify({"erro": "nenhum nó ativo"}), 404

    node = Node.query.get(ids[0])
    distancia = haversine_km(lat, lon, node.latitude, node.longitude)

    return jsonify({
        "id": node.id, "name": node.name,
        "latitude": node.latitude, "longitude": node.longitude,
        "distancia_km": round(distancia, 1),
    })


# -----------------------------------------------------------------------------
# PERFIL
# -----------------------------------------------------------------------------

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        current_user.username = request.form.get("username")
        pwd = request.form.get("password")
        if pwd:
            current_user.password = generate_password_hash(pwd, method="scrypt")

        image = request.files.get("profile_image")
        if image and image.filename:
            ext = os.path.splitext(image.filename)[1]
            filename = f"{uuid.uuid4()}{ext}"
            image.save(os.path.join(app.config["PROFILE_FOLDER"], filename))

            if current_user.profile_image != "default.webp":
                antigo = os.path.join(app.config["PROFILE_FOLDER"], current_user.profile_image)
                if os.path.exists(antigo):
                    os.remove(antigo)

            current_user.profile_image = filename

        db.session.commit()
        flash("Perfil atualizado!", "success")

    return render_template("profile.html")


app.config["PROFILE_FOLDER"] = PROFILE_FOLDER


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        create_default_nodes()
    app.run(debug=True)
