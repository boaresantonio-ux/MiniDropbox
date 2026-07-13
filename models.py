"""
Modelos da base de dados.

Em relação à versão anterior, esta limpa duas inconsistências que causavam
bugs: existiam dois modelos de arquivo competindo (`File` com id UUID e
`FileRecord` com id inteiro), e as rotas de download/preview/delete usavam
`FileRecord` e `app.config['UPLOAD_FOLDER']`, que nunca chegava a ser
definido. Agora há um único modelo de arquivo (`File`) e tudo referencia
os nós (`Node`) e réplicas (`FileReplica`) de forma coerente.
"""

import uuid
from datetime import datetime

from flask_login import UserMixin

from extensions import db


def novo_uuid():
    return str(uuid.uuid4())


class User(db.Model, UserMixin):
    __tablename__ = "user"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    profile_image = db.Column(db.String(255), default="default.webp")

    files = db.relationship("File", backref="owner", lazy=True)
    folders = db.relationship("Folder", backref="owner", lazy=True)


class Node(db.Model):
    """
    Um "servidor" do sistema distribuído.

    Fisicamente é uma pasta no disco (`path`) — como estamos a simular
    tudo num único computador, não há um processo de rede real por nó.
    A localização geográfica (`latitude`/`longitude`) é obrigatória: é
    ela que alimenta o índice espacial (R-tree) usado para escolher o nó
    mais próximo em caso de remoção de outro nó.

    `is_backup` marca o nó de backup fixo: não pode ser desativado nem
    removido, e recebe réplica de todo arquivo enviado.
    """

    __tablename__ = "nodes"

    id = db.Column(db.String(36), primary_key=True, default=novo_uuid)
    name = db.Column(db.String(100), nullable=False)

    host = db.Column(db.String(255), nullable=False, default="127.0.0.1")
    port = db.Column(db.Integer, nullable=False, default=5000)
    path = db.Column(db.String(500), nullable=False)

    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)

    enabled = db.Column(db.Boolean, default=True)
    is_backup = db.Column(db.Boolean, default=False)

    last_seen = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    replicas = db.relationship("FileReplica", backref="node", lazy=True)


class Folder(db.Model):
    __tablename__ = "folders"

    id = db.Column(db.String(36), primary_key=True, default=novo_uuid)
    name = db.Column(db.String(255), nullable=False)
    parent_id = db.Column(db.String(36), db.ForeignKey("folders.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    subfolders = db.relationship(
        "Folder", backref=db.backref("parent", remote_side=[id]), lazy=True
    )


class File(db.Model):
    __tablename__ = "files"

    id = db.Column(db.String(36), primary_key=True, default=novo_uuid)
    filename = db.Column(db.String(255), nullable=False)       # nome físico no disco
    display_name = db.Column(db.String(255), nullable=False)   # nome mostrado ao usuário
    size = db.Column(db.Integer, nullable=False)
    checksum = db.Column(db.String(64), nullable=False)
    folder_id = db.Column(db.String(36), db.ForeignKey("folders.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Soft delete: quando o usuário "remove" um arquivo, ele só desaparece
    # das pastas normais. A réplica no nó de backup fica intacta até o
    # usuário restaurar ou apagar em definitivo pela lixeira.
    deleted_at = db.Column(db.DateTime, nullable=True)

    replicas = db.relationship(
        "FileReplica", backref="file", lazy=True, cascade="all, delete-orphan"
    )

    # Aliases: o dashboard.html usa file_size/upload_date; o resto do app
    # (rotas, outros templates) usa size/created_at. Em vez de manter dois
    # nomes de coluna, expomos os mesmos dados sob os dois nomes.
    @property
    def file_size(self):
        return self.size

    @property
    def upload_date(self):
        return self.created_at

    @property
    def file_size(self):
        """Alias de `size` — nome esperado pelo template do dashboard."""
        return self.size

    @property
    def upload_date(self):
        """Alias de `created_at` — nome esperado pelo template do dashboard."""
        return self.created_at


class FileReplica(db.Model):
    __tablename__ = "file_replicas"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.String(36), db.ForeignKey("files.id"), nullable=False)
    node_id = db.Column(db.String(36), db.ForeignKey("nodes.id"), nullable=False)
    status = db.Column(db.String(20), default="STORED")  # STORED | PENDING | CORROMPIDO
    replicated_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Operation(db.Model):
    """Log simples de auditoria: cada upload, remoção ou redistribuição fica registada."""

    __tablename__ = "operations"

    id = db.Column(db.String(36), primary_key=True, default=novo_uuid)
    operation = db.Column(db.String(50), nullable=False)
    file_id = db.Column(db.String(36), nullable=True)
    node_id = db.Column(db.String(36), nullable=True)
    detail = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
