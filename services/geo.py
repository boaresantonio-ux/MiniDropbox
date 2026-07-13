"""
Camada espacial do sistema distribuído.

Três coisas vivem aqui:

1. `haversine_km`      — distância real entre duas coordenadas geográficas.
2. `para_utm`           — conversão lat/lon (WGS84) para coordenadas UTM,
                           só para exibição (a lógica de proximidade usa
                           sempre lat/lon + haversine, que é mais simples
                           e não depende de qual fuso UTM cada nó cai).
3. `IndiceEspacialNos`   — índice R-tree sobre a localização dos nós, para
                           consultas de vizinho-mais-próximo em O(log n)
                           em vez de comparar a distância a todos os nós
                           um a um.

Por que R-tree e não comparar tudo num loop: com 3-4 nós não faria
diferença nenhuma, mas é a mesma estrutura de índice que bancos espaciais
de verdade usam por baixo — o PostGIS usa GiST (uma generalização do
R-tree) e o SpatiaLite usa R-tree nativo do SQLite. Usar a biblioteca
`rtree` aqui (bindings Python para a libspatialindex) demonstra a mesma
ideia sem depender de compilar a extensão SpatiaLite, que é mais chata de
instalar no Windows.
"""

import math

import utm as utm_lib
from rtree import index


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância em linha reta entre dois pontos na superfície da Terra."""
    raio_terra_km = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * raio_terra_km * math.asin(math.sqrt(a))


def para_utm(lat: float, lon: float):
    """Converte lat/lon para (easting, northing, zona) UTM. Usado só para exibição."""
    try:
        easting, northing, zona, letra = utm_lib.from_latlon(lat, lon)
        return {"easting": round(easting, 1), "northing": round(northing, 1), "zona": f"{zona}{letra}"}
    except Exception:
        return None


class IndiceEspacialNos:
    """
    Índice R-tree sobre a localização dos nós ativos.

    Precisa ser reconstruído (`construir`) sempre que a lista de nós muda:
    criação, remoção, ou ativar/desativar um nó.
    """

    def __init__(self):
        self._idx = index.Index()
        self._id_por_posicao = {}

    def construir(self, nodes):
        self._idx = index.Index()
        self._id_por_posicao = {}
        for posicao, node in enumerate(nodes):
            if node.latitude is None or node.longitude is None:
                continue
            # rtree indexa bounding boxes (min_x, min_y, max_x, max_y);
            # um ponto é só uma bbox de área zero. Convenção: x=longitude, y=latitude.
            self._idx.insert(posicao, (node.longitude, node.latitude, node.longitude, node.latitude))
            self._id_por_posicao[posicao] = node.id

    def mais_proximos(self, lat: float, lon: float, quantidade: int = 1, excluir_ids=None):
        """Devolve até `quantidade` ids de nó, ordenados do mais próximo ao mais distante."""
        excluir_ids = excluir_ids or set()
        candidatos = self._idx.nearest((lon, lat, lon, lat), quantidade + len(excluir_ids))

        resultado = []
        for posicao in candidatos:
            node_id = self._id_por_posicao.get(posicao)
            if node_id and node_id not in excluir_ids and node_id not in resultado:
                resultado.append(node_id)
            if len(resultado) >= quantidade:
                break
        return resultado
