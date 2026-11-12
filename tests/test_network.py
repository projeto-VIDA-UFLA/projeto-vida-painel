import json

from painel import network


def estado(**campos):
    base = {"quando": 1000, "instalado": True, "ativo": False, "interface": "", "cabo": True,
            "ocupado": "", "resultado": None}
    base.update(campos)
    return json.dumps(base)


def test_sem_arquivo_ou_invalido_nao_instalado():
    for texto in (None, "", "{quebrado"):
        state, result = network.parse_state(texto, 1000)
        assert state["installed"] is False and result is None


def test_estado_velho_e_servico_parado():
    state, _ = network.parse_state(estado(), 1000 + network.STALE_S + 1)
    assert state["installed"] is False


def test_estado_ativo():
    state, _ = network.parse_state(estado(ativo=True, interface="enp4s0"), 1001)
    assert state == {"installed": True, "active": True, "device": "enp4s0", "cable": True, "busy": None}


def test_ocupado_e_resultado():
    resultado = {"id": "abc", "acao": "conectar", "ok": True, "ping": False}
    state, result = network.parse_state(estado(ocupado="conectar", resultado=resultado), 1000)
    assert state["busy"] == "connect" and result == resultado


def test_sem_pasta_nao_instalado(tmp_path):
    rede = network.Network(str(tmp_path / "nao-existe"))
    rede.refresh()
    assert rede.status()["installed"] is False
