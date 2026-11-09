"""Conversa com o agente pelo terminal.

    agente-brecho                 # usa dados/loja.json
    agente-brecho --loja minha-loja.json --mostrar-ferramentas
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agente import Agente
from .loja import Loja

LOJA_PADRAO = Path(__file__).resolve().parents[2] / "dados" / "loja.json"


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agente-brecho", description="Atendente virtual de brechó com Claude.")
    p.add_argument("--loja", type=Path, default=LOJA_PADRAO)
    p.add_argument("--esforco", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--mostrar-ferramentas", action="store_true", help="mostra cada ferramenta usada")
    a = p.parse_args(argv)

    try:
        import anthropic

        cliente = anthropic.Anthropic()
    except Exception as erro:  # sem credencial ou sem o pacote
        sys.exit(f"Não foi possível iniciar o cliente da Anthropic: {erro}")

    loja = Loja.de_arquivo(a.loja)
    agente = Agente(loja, cliente, esforco=a.esforco)
    print(f"Atendimento da {loja.info['nome']}. Digite 'sair' para encerrar.\n")

    while True:
        try:
            mensagem = input("Cliente: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if mensagem.lower() in {"sair", "exit"}:
            break
        if not mensagem:
            continue
        turno = agente.responder(mensagem)
        if a.mostrar_ferramentas and turno.ferramentas_usadas:
            print(f"  [ferramentas: {', '.join(turno.ferramentas_usadas)}]")
        print(f"Loja: {turno.resposta}\n")

    if loja.pedidos:
        print("\nPedidos registrados nesta conversa:")
        print(json.dumps(loja.exportar_pedidos(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
