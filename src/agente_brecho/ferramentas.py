"""Ferramentas que o agente pode usar, e a execução delas.

Cada ferramenta tem um esquema JSON (o que o modelo vê) e uma função Python
(o que de fato roda). Com ``strict: true``, a API garante que os argumentos
enviados pelo modelo seguem o esquema; mesmo assim validamos de novo aqui,
porque nunca se confia cegamente numa entrada.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .loja import ErroDaLoja, Loja


def _objeto(propriedades: dict, obrigatorios: list[str]) -> dict:
    return {"type": "object", "properties": propriedades, "required": obrigatorios, "additionalProperties": False}


DEFINICOES: list[dict] = [
    {
        "name": "buscar_pecas",
        "description": (
            "Procura peças disponíveis no estoque. Use sempre que o cliente pedir uma peça, "
            "categoria, tamanho ou faixa de preço. Só peças livres (não vendidas nem reservadas) aparecem."
        ),
        "input_schema": _objeto(
            {
                "texto": {"type": "string", "description": "Palavras-chave: tipo de peça, cor, marca. Pode ser vazio."},
                "categoria": {"type": "string", "description": "Ex.: jaquetas, vestidos, calçados."},
                "tamanho": {"type": "string", "description": "Ex.: P, M, G, 37."},
                "preco_maximo": {"type": "number", "description": "Preço máximo em reais."},
            },
            ["texto"],
        ),
    },
    {
        "name": "ver_peca",
        "description": "Mostra todos os detalhes de uma peça pelo código (ex.: P001), incluindo estado e defeitos.",
        "input_schema": _objeto({"produto_id": {"type": "string"}}, ["produto_id"]),
    },
    {
        "name": "consultar_politica",
        "description": "Consulta uma regra da loja. Use antes de responder qualquer dúvida sobre essas regras.",
        "input_schema": _objeto(
            {
                "tema": {
                    "type": "string",
                    "enum": ["trocas", "frete", "pagamento", "reserva", "estado_das_pecas", "retirada"],
                }
            },
            ["tema"],
        ),
    },
    {
        "name": "reservar_peca",
        "description": (
            "Reserva uma peça por 24 horas para o cliente. Só use depois que o cliente "
            "confirmar claramente que quer reservar aquela peça."
        ),
        "input_schema": _objeto(
            {"produto_id": {"type": "string"}, "nome_cliente": {"type": "string"}},
            ["produto_id", "nome_cliente"],
        ),
    },
    {
        "name": "registrar_pedido",
        "description": (
            "Fecha o pedido. Só use depois de o cliente confirmar as peças, o nome, um contato "
            "(telefone ou e-mail) e a forma de entrega. O pagamento é combinado depois por um atendente."
        ),
        "input_schema": _objeto(
            {
                "nome_cliente": {"type": "string"},
                "contato": {"type": "string"},
                "produtos": {"type": "array", "items": {"type": "string"}},
                "entrega": {"type": "string", "enum": ["retirada", "envio"]},
            },
            ["nome_cliente", "contato", "produtos", "entrega"],
        ),
    },
    {
        "name": "chamar_atendente",
        "description": (
            "Passa a conversa para um atendente humano. Use quando o cliente pedir, quando estiver "
            "irritado, ou quando o assunto fugir do que as ferramentas resolvem (reclamação, negociação de preço, pedido já pago)."
        ),
        "input_schema": _objeto({"motivo": {"type": "string"}}, ["motivo"]),
    },
]

for _definicao in DEFINICOES:
    _definicao["strict"] = True

NOMES = {d["name"] for d in DEFINICOES}


@dataclass
class ResultadoFerramenta:
    conteudo: str
    erro: bool = False


@dataclass
class Registro:
    """Histórico do que o agente fez, para auditoria (o que um dono de loja ia querer ver)."""

    chamadas: list[dict] = field(default_factory=list)
    atendente_chamado: str | None = None


class Executor:
    def __init__(self, loja: Loja):
        self.loja = loja
        self.registro = Registro()
        self._funcoes: dict[str, Callable[[dict], Any]] = {
            "buscar_pecas": self._buscar,
            "ver_peca": lambda e: asdict(self.loja.produto(e["produto_id"])),
            "consultar_politica": lambda e: {"tema": e["tema"], "regra": self.loja.politica(e["tema"])},
            "reservar_peca": self._reservar,
            "registrar_pedido": self._pedido,
            "chamar_atendente": self._atendente,
        }

    def executar(self, nome: str, entrada: Any) -> ResultadoFerramenta:
        try:
            if nome not in self._funcoes:
                raise ErroDaLoja(f"Ferramenta desconhecida: {nome}")
            if not isinstance(entrada, dict):
                raise ErroDaLoja("Argumentos inválidos: era esperado um objeto.")
            faltando = [c for c in _obrigatorios(nome) if c not in entrada]
            if faltando:
                raise ErroDaLoja(f"Faltam argumentos: {', '.join(faltando)}")
            resultado = ResultadoFerramenta(json.dumps(self._funcoes[nome](entrada), ensure_ascii=False))
        except ErroDaLoja as e:
            resultado = ResultadoFerramenta(str(e), erro=True)
        except (KeyError, TypeError, ValueError) as e:
            resultado = ResultadoFerramenta(f"Argumentos inválidos: {e}", erro=True)
        self.registro.chamadas.append({"ferramenta": nome, "entrada": entrada, "erro": resultado.erro, "saida": resultado.conteudo})
        return resultado

    def _buscar(self, e: dict) -> dict:
        preco = e.get("preco_maximo")
        pecas = self.loja.buscar(
            texto=e.get("texto", ""),
            categoria=e.get("categoria") or None,
            tamanho=e.get("tamanho") or None,
            preco_maximo=float(preco) if preco is not None else None,
        )
        return {"encontradas": len(pecas), "pecas": [p.resumo() for p in pecas]}

    def _reservar(self, e: dict) -> dict:
        r = self.loja.reservar(e["produto_id"], e["nome_cliente"].strip())
        return {"reservado": r.produto_id, "para": r.cliente, "valida_ate": r.expira_em.isoformat(timespec="minutes")}

    def _pedido(self, e: dict) -> dict:
        p = self.loja.registrar_pedido(e["nome_cliente"].strip(), e["contato"].strip(), list(e["produtos"]), e["entrega"])
        return {"pedido": p.numero, "itens": p.itens, "total": p.total, "entrega": p.entrega}

    def _atendente(self, e: dict) -> dict:
        self.registro.atendente_chamado = e["motivo"]
        return {"transferido": True, "whatsapp": self.loja.info["whatsapp_humano"], "horario": self.loja.info["horario"]}


def _obrigatorios(nome: str) -> list[str]:
    return next(d["input_schema"]["required"] for d in DEFINICOES if d["name"] == nome)
