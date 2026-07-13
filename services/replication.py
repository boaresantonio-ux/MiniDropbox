"""
Replicação física: copiar bytes de verdade para a pasta de cada nó, e
redistribuir esses arquivos quando um nó é removido.

Isto é o que faltava na versão anterior: as réplicas eram só uma linha na
tabela `file_replicas` (status "PENDING"/"STORED"), sem nenhuma cópia real
do arquivo acontecer nas pastas dos outros nós.
"""

import os
import shutil

from extensions import db
from models import FileReplica


def caminho_replica(node, user_id, nome_fisico: str) -> str:
    """Caminho onde o arquivo de um usuário fica guardado dentro de um nó."""
    return os.path.join(node.path, str(user_id), nome_fisico)


def replicar_para_nos(file_id: str, user_id, nome_fisico: str, caminho_origem: str, nodes):
    """Copia o arquivo para a pasta de cada nó ativo e regista a réplica."""
    for node in nodes:
        destino = caminho_replica(node, user_id, nome_fisico)
        os.makedirs(os.path.dirname(destino), exist_ok=True)

        if os.path.abspath(caminho_origem) != os.path.abspath(destino):
            shutil.copy2(caminho_origem, destino)

        db.session.add(FileReplica(file_id=file_id, node_id=node.id, status="STORED"))


def remover_replicas_do_arquivo(arquivo, nodes_por_id: dict):
    """Apaga a cópia física de um arquivo em todos os nós onde ele estava."""
    for replica in list(arquivo.replicas):
        node = nodes_por_id.get(replica.node_id)
        if node:
            caminho = caminho_replica(node, arquivo.user_id, arquivo.filename)
            if os.path.exists(caminho):
                os.remove(caminho)


def mover_para_lixeira(arquivo, nodes_por_id: dict):
    """
    "Remove" um arquivo preservando a réplica do nó de backup.

    Apaga a cópia física e a linha de réplica em todo nó que NÃO seja o
    backup; a réplica do backup fica intocada — é ela que permite
    restaurar o arquivo mais tarde. Devolve False se não havia nenhuma
    réplica de backup (nesse caso o arquivo teria de ser apagado por
    completo, já que não haveria como recuperá-lo).
    """
    tinha_backup = False

    for replica in list(arquivo.replicas):
        node = nodes_por_id.get(replica.node_id)
        if node is None:
            db.session.delete(replica)
            continue

        if node.is_backup:
            tinha_backup = True
            continue  # preserva a réplica de backup

        caminho = caminho_replica(node, arquivo.user_id, arquivo.filename)
        if os.path.exists(caminho):
            os.remove(caminho)
        db.session.delete(replica)

    return tinha_backup


def restaurar_do_backup(arquivo, nodes_ativos, nodes_por_id: dict):
    """
    Restaura um arquivo que estava na lixeira: copia a cópia física do
    nó de backup de volta para todos os nós ativos que ainda não a têm.
    """
    node_backup = next((n for n in nodes_por_id.values() if n.is_backup), None)
    if node_backup is None:
        return False

    origem = caminho_replica(node_backup, arquivo.user_id, arquivo.filename)
    if not os.path.exists(origem):
        return False  # a cópia física do backup também se perdeu

    ids_com_replica = {r.node_id for r in arquivo.replicas}

    for node in nodes_ativos:
        if node.id in ids_com_replica:
            continue

        destino = caminho_replica(node, arquivo.user_id, arquivo.filename)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        if os.path.abspath(origem) != os.path.abspath(destino):
            shutil.copy2(origem, destino)

        db.session.add(FileReplica(file_id=arquivo.id, node_id=node.id, status="STORED"))

    return True


def redistribuir_arquivos_do_no(node_removido, node_destino):
    """
    Antes de remover um nó, copia para `node_destino` os arquivos que
    tinham réplica nele, e atualiza a base de dados de acordo.
    """
    replicas = FileReplica.query.filter_by(node_id=node_removido.id).all()
    total_movido = 0

    for replica in replicas:
        arquivo = replica.file
        if arquivo is None:
            db.session.delete(replica)
            continue

        origem = caminho_replica(node_removido, arquivo.user_id, arquivo.filename)
        destino = caminho_replica(node_destino, arquivo.user_id, arquivo.filename)
        os.makedirs(os.path.dirname(destino), exist_ok=True)

        if os.path.exists(origem) and not os.path.exists(destino):
            shutil.copy2(origem, destino)
            total_movido += 1

        ja_tinha_replica_no_destino = FileReplica.query.filter_by(
            file_id=arquivo.id, node_id=node_destino.id
        ).first()

        if not ja_tinha_replica_no_destino:
            db.session.add(FileReplica(file_id=arquivo.id, node_id=node_destino.id, status="STORED"))

        db.session.delete(replica)

    return total_movido
