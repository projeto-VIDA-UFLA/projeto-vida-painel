"""Conexão do notebook com a Odroid pelo cabo: o perfil "VIDA Odroid" do NetworkManager.

O painel roda em container e não mexe na rede do Ubuntu. Quem mexe é o serviço
vida-rede do host, instalado uma vez por computador com
scripts/rede/preparar_computador.sh (alto-nível). Os dois conversam por arquivos na
pasta /run/vida-rede, montada no container:
  - estado.json: o serviço reescreve a cada 2 s (perfil instalado, ativo, interface,
    cabo ligado, operação em andamento e o resultado do último pedido);
  - pedido: o painel escreve "<id> conectar|desconectar"; o serviço atende em até 0,5 s.

Sem o serviço (por exemplo, o notebook principal, que tem configuração própria) não há
estado.json e o botão não aparece. Ao reiniciar o computador o perfil não volta sozinho:
a porta de rede fica no modo automático (DHCP).
"""
import asyncio
import json
import os
import time
import uuid

BRIDGE_DIR = os.environ.get("VIDA_REDE_DIR", "/run/vida-rede")
STALE_S = 6.0           # estado mais velho que isto: serviço parado
CONNECT_TIMEOUT_S = 60.0
DISCONNECT_TIMEOUT_S = 25.0


def parse_state(text, now):
    """estado.json -> dicionário do painel. Sem arquivo, inválido ou velho: não instalado."""
    empty = {"installed": False, "active": False, "device": None, "cable": False, "busy": None}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return empty, None
    if not isinstance(data, dict) or now - float(data.get("quando", 0)) > STALE_S:
        return empty, None
    state = {"installed": bool(data.get("instalado")), "active": bool(data.get("ativo")),
             "device": data.get("interface") or None, "cable": bool(data.get("cabo")),
             "busy": {"conectar": "connect", "desconectar": "disconnect"}.get(data.get("ocupado"))}
    return state, data.get("resultado")


class Network:
    def __init__(self, directory=BRIDGE_DIR):
        self.directory = directory
        self.state, self.result = parse_state(None, 0)
        self.pending = None     # "connect" / "disconnect" enquanto o painel espera o serviço

    def refresh(self):
        try:
            with open(os.path.join(self.directory, "estado.json"), encoding="utf-8") as stream:
                text = stream.read()
        except OSError:
            text = None
        self.state, self.result = parse_state(text, time.time())

    @property
    def busy(self):
        return self.pending or self.state["busy"]

    def status(self):
        return {**self.state, "busy": self.busy}

    async def _request(self, action, timeout):
        """Escreve o pedido e espera o resultado com o mesmo id."""
        request_id = uuid.uuid4().hex[:12]
        temporary = os.path.join(self.directory, ".pedido")
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(f"{request_id} {action}\n")
        os.replace(temporary, os.path.join(self.directory, "pedido"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            self.refresh()
            if isinstance(self.result, dict) and self.result.get("id") == request_id:
                return self.result
        raise RuntimeError("O serviço vida-rede não respondeu. Confira: systemctl status vida-rede")

    async def connect(self):
        """(interface, a Odroid responde ao ping). Levanta RuntimeError com a explicação."""
        self.refresh()
        if not self.state["installed"]:
            raise RuntimeError("Serviço vida-rede não instalado: rode scripts/rede/preparar_computador.sh do alto-nível.")
        self.pending = "connect"
        try:
            result = await self._request("conectar", CONNECT_TIMEOUT_S)
        finally:
            self.pending = None
        if not result.get("ok"):
            raise RuntimeError(result.get("mensagem") or "Falha ao conectar.")
        return result.get("interface") or None, bool(result.get("ping"))

    async def disconnect(self):
        self.refresh()
        if not self.state["installed"] or not self.state["active"]:
            return
        self.pending = "disconnect"
        try:
            result = await self._request("desconectar", DISCONNECT_TIMEOUT_S)
        finally:
            self.pending = None
        if not result.get("ok"):
            raise RuntimeError(result.get("mensagem") or "Falha ao desconectar.")
