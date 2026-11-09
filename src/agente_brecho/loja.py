"""Regras de negócio da loja: estoque, reservas e pedidos.

Esta camada não sabe nada de IA. O agente só consegue mexer na loja por
estas funções, então preço, disponibilidade e reserva sempre vêm daqui,
nunca da "imaginação" do modelo.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PRAZO_RESERVA = timedelta(hours=24)


class ErroDaLoja(Exception):
    """Erro de regra de negócio, com mensagem que pode ser mostrada ao cliente."""


def _normalizar(texto: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return sem_acento.lower()


@dataclass
class Produto:
    id: str
    nome: str
    categoria: str
    tamanho: str
    preco: float
    cor: str
    marca: str
    estado: str
    observacoes: str
    disponivel: bool

    def resumo(self) -> dict:
        return {"id": self.id, "nome": self.nome, "tamanho": self.tamanho, "preco": self.preco, "cor": self.cor}


@dataclass
class Reserva:
    produto_id: str
    cliente: str
    criada_em: datetime
    expira_em: datetime


@dataclass
class Pedido:
    numero: str
    cliente: str
    contato: str
    itens: list[str]
    entrega: str
    total: float
    criado_em: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Loja:
    def __init__(self, dados: dict, relogio=lambda: datetime.now(timezone.utc)):
        self.info = dados["loja"]
        self.politicas: dict[str, str] = dados["politicas"]
        self.produtos = {p["id"]: Produto(**p) for p in dados["produtos"]}
        self.reservas: dict[str, Reserva] = {}
        self.pedidos: list[Pedido] = []
        self._relogio = relogio

    @classmethod
    def de_arquivo(cls, caminho: str | Path) -> "Loja":
        return cls(json.loads(Path(caminho).read_text(encoding="utf-8")))

    # ---------- consultas ----------

    def _reserva_ativa(self, produto_id: str) -> Reserva | None:
        reserva = self.reservas.get(produto_id)
        if reserva and reserva.expira_em <= self._relogio():
            del self.reservas[produto_id]  # reserva vencida: a peça volta para a vitrine
            return None
        return reserva

    def esta_livre(self, produto: Produto) -> bool:
        return produto.disponivel and self._reserva_ativa(produto.id) is None

    def buscar(
        self,
        texto: str = "",
        categoria: str | None = None,
        tamanho: str | None = None,
        preco_maximo: float | None = None,
        limite: int = 5,
    ) -> list[Produto]:
        palavras = [p for p in re.findall(r"\w+", _normalizar(texto)) if len(p) > 2]
        achados = []
        for p in self.produtos.values():
            if not self.esta_livre(p):
                continue
            if categoria and _normalizar(categoria) not in _normalizar(p.categoria):
                continue
            if tamanho and _normalizar(tamanho) != _normalizar(p.tamanho):
                continue
            if preco_maximo is not None and p.preco > preco_maximo:
                continue
            alvo = _normalizar(f"{p.nome} {p.categoria} {p.cor} {p.marca}")
            acertos = sum(1 for palavra in palavras if palavra.rstrip("s") in alvo)
            if palavras and not acertos:
                continue
            achados.append((acertos, -p.preco, p))
        achados.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [p for _, _, p in achados[:limite]]

    def produto(self, produto_id: str) -> Produto:
        produto = self.produtos.get(produto_id.strip().upper())
        if not produto:
            raise ErroDaLoja(f"Não existe peça com o código {produto_id}.")
        return produto

    def politica(self, tema: str) -> str:
        if tema not in self.politicas:
            raise ErroDaLoja(f"Tema desconhecido. Temas disponíveis: {', '.join(self.politicas)}.")
        return self.politicas[tema]

    # ---------- ações ----------

    def reservar(self, produto_id: str, cliente: str) -> Reserva:
        produto = self.produto(produto_id)
        if not produto.disponivel:
            raise ErroDaLoja(f"A peça {produto.id} já foi vendida.")
        reserva = self._reserva_ativa(produto.id)
        if reserva and reserva.cliente != cliente:
            raise ErroDaLoja(f"A peça {produto.id} está reservada para outra pessoa.")
        agora = self._relogio()
        self.reservas[produto.id] = Reserva(produto.id, cliente, agora, agora + PRAZO_RESERVA)
        return self.reservas[produto.id]

    def registrar_pedido(self, cliente: str, contato: str, itens: list[str], entrega: str) -> Pedido:
        if not itens:
            raise ErroDaLoja("O pedido precisa de pelo menos uma peça.")
        if entrega not in {"retirada", "envio"}:
            raise ErroDaLoja("A entrega deve ser 'retirada' ou 'envio'.")
        produtos = [self.produto(i) for i in dict.fromkeys(itens)]
        for p in produtos:
            reserva = self._reserva_ativa(p.id)
            if not p.disponivel or (reserva and reserva.cliente != cliente):
                raise ErroDaLoja(f"A peça {p.id} não está disponível para este cliente.")
        for p in produtos:
            p.disponivel = False
            self.reservas.pop(p.id, None)
        numero = f"PED-{len(self.pedidos) + 1:04d}"
        pedido = Pedido(numero, cliente, contato, [p.id for p in produtos], entrega, round(sum(p.preco for p in produtos), 2))
        self.pedidos.append(pedido)
        return pedido

    def exportar_pedidos(self) -> list[dict]:
        return [asdict(p) for p in self.pedidos]
