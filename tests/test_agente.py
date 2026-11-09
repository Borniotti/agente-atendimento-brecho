import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agente_brecho import DEFINICOES, Agente, ErroDaLoja, Executor, Loja

DADOS = json.loads((Path(__file__).parent.parent / "dados" / "loja.json").read_text(encoding="utf-8"))


class Relogio:
    def __init__(self):
        self.agora = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.agora


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def loja(relogio):
    return Loja(json.loads(json.dumps(DADOS)), relogio=relogio)


# ---------- regras da loja ----------

def test_busca_por_texto_ignora_acento_e_plural(loja):
    nomes = [p.nome for p in loja.buscar("jaquetas")]
    assert nomes and all("Jaqueta" in n for n in nomes)


def test_busca_com_filtros(loja):
    pecas = loja.buscar("", categoria="jaquetas", tamanho="M", preco_maximo=300)
    assert [p.id for p in pecas] == ["P008"]


def test_busca_nao_mostra_vendido(loja):
    assert "P007" not in [p.id for p in loja.buscar("saia")]


def test_reserva_bloqueia_outra_pessoa(loja):
    loja.reservar("P001", "Ana")
    with pytest.raises(ErroDaLoja, match="reservada"):
        loja.reservar("P001", "Bruno")
    assert "P001" not in [p.id for p in loja.buscar("jaqueta")]


def test_reserva_vence_em_24h(loja, relogio):
    loja.reservar("P001", "Ana")
    relogio.agora += timedelta(hours=24, minutes=1)
    loja.reservar("P001", "Bruno")  # não levanta erro: a reserva da Ana venceu


def test_pedido_baixa_estoque_e_soma_total(loja):
    pedido = loja.registrar_pedido("Ana", "11 99999-0000", ["P002", "P009", "P002"], "envio")
    assert pedido.total == round(79.9 + 49.9, 2)
    assert pedido.itens == ["P002", "P009"]
    assert not loja.produtos["P002"].disponivel


def test_pedido_respeita_reserva_de_outro(loja):
    loja.reservar("P004", "Ana")
    with pytest.raises(ErroDaLoja):
        loja.registrar_pedido("Bruno", "x@y.com", ["P004"], "retirada")
    assert loja.registrar_pedido("Ana", "a@a.com", ["P004"], "retirada").numero == "PED-0001"


# ---------- ferramentas ----------

def test_esquemas_sao_estritos():
    for d in DEFINICOES:
        assert d["strict"] is True
        assert d["input_schema"]["additionalProperties"] is False


def test_executor_transforma_erro_em_resultado(loja):
    executor = Executor(loja)
    resultado = executor.executar("ver_peca", {"produto_id": "P999"})
    assert resultado.erro and "P999" in resultado.conteudo


def test_executor_recusa_ferramenta_inexistente_e_argumento_faltando(loja):
    executor = Executor(loja)
    assert executor.executar("apagar_tudo", {}).erro
    assert executor.executar("reservar_peca", {"produto_id": "P001"}).erro
    assert len(executor.registro.chamadas) == 2  # tudo fica no histórico de auditoria


# ---------- laço do agente, com um "modelo" roteirizado ----------

def bloco_texto(texto):
    return SimpleNamespace(type="text", text=texto)


def bloco_ferramenta(id_, nome, entrada):
    return SimpleNamespace(type="tool_use", id=id_, name=nome, input=entrada)


def resposta(stop_reason, *blocos):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=list(blocos),
        usage=SimpleNamespace(input_tokens=50, output_tokens=10, cache_read_input_tokens=40),
    )


class ModeloRoteirizado:
    """Devolve respostas prontas, na ordem, e guarda o que recebeu."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.pedidos = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Copia o histórico, porque o agente continua alterando a mesma lista depois.
        self.pedidos.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.respostas.pop(0)


def test_agente_busca_e_responde(loja):
    modelo = ModeloRoteirizado(
        resposta("tool_use", bloco_ferramenta("t1", "buscar_pecas", {"texto": "jaqueta", "tamanho": "M"})),
        resposta("end_turn", bloco_texto("Tenho a jaqueta de couro P008 por R$ 249,00.")),
    )
    turno = Agente(loja, modelo).responder("Tem jaqueta M?")
    assert turno.resposta.startswith("Tenho a jaqueta")
    assert turno.ferramentas_usadas == ["buscar_pecas"]
    assert turno.passos == 2 and turno.uso["cache_lido"] == 80

    # O resultado da ferramenta voltou para o modelo com o id certo e o preço real do estoque.
    resultado = modelo.pedidos[1]["messages"][-1]["content"][0]
    assert resultado["tool_use_id"] == "t1" and "249.0" in resultado["content"]


def test_varias_ferramentas_voltam_numa_mensagem_so(loja):
    modelo = ModeloRoteirizado(
        resposta(
            "tool_use",
            bloco_ferramenta("a", "consultar_politica", {"tema": "trocas"}),
            bloco_ferramenta("b", "consultar_politica", {"tema": "frete"}),
        ),
        resposta("end_turn", bloco_texto("ok")),
    )
    Agente(loja, modelo).responder("Como funciona troca e frete?")
    ultima = modelo.pedidos[1]["messages"][-1]
    assert ultima["role"] == "user" and [r["tool_use_id"] for r in ultima["content"]] == ["a", "b"]


def test_erro_de_ferramenta_vai_marcado_para_o_modelo(loja):
    modelo = ModeloRoteirizado(
        resposta("tool_use", bloco_ferramenta("t1", "reservar_peca", {"produto_id": "P007", "nome_cliente": "Ana"})),
        resposta("end_turn", bloco_texto("Essa já foi vendida.")),
    )
    Agente(loja, modelo).responder("Quero a saia P007")
    resultado = modelo.pedidos[1]["messages"][-1]["content"][0]
    assert resultado["is_error"] is True


def test_limite_de_passos_chama_atendente(loja):
    em_loop = [resposta("tool_use", bloco_ferramenta(f"t{i}", "buscar_pecas", {"texto": "x"})) for i in range(3)]
    agente = Agente(loja, ModeloRoteirizado(*em_loop), max_passos=3)
    turno = agente.responder("oi")
    assert "atendente" in turno.resposta
    assert agente.executor.registro.atendente_chamado


def test_recusa(loja):
    turno = Agente(loja, ModeloRoteirizado(resposta("refusal"))).responder("...")
    assert "atendente" in turno.resposta


def test_historico_continua_entre_mensagens(loja):
    modelo = ModeloRoteirizado(resposta("end_turn", bloco_texto("Oi!")), resposta("end_turn", bloco_texto("Claro.")))
    agente = Agente(loja, modelo)
    agente.responder("oi")
    agente.responder("me ajuda?")
    assert [m["role"] for m in modelo.pedidos[1]["messages"]] == ["user", "assistant", "user"]


def test_pedido_ao_claude(loja):
    modelo = ModeloRoteirizado(resposta("end_turn", bloco_texto("Oi!")))
    Agente(loja, modelo).responder("oi")
    pedido = modelo.pedidos[0]
    assert pedido["model"] == "claude-opus-5-5"
    assert pedido["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Brechó Exemplo" in pedido["system"][0]["text"]
    assert pedido["fallbacks"] == "default"
    assert "tool_choice" not in pedido
